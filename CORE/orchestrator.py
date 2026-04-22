# ============================================================
# FILE: CORE/orchestrator.py
# ROLE: Главный оркестратор торговых процессов (Game Loop Pattern)
# ============================================================

from __future__ import annotations
import asyncio
import time
import traceback
from typing import Dict, Any, Set, TYPE_CHECKING, List, Tuple, Optional
import os
import aiohttp
from pathlib import Path

from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.order import PhemexPrivateClient
from API.PHEMEX.ws_private import PhemexPrivateWS
from API.BINANCE.ticker import BinanceTickerAPI
from API.PHEMEX.ticker import PhemexTickerAPI
from API.PHEMEX.funding import PhemexFunding
from API.BINANCE.funding import BinanceFunding

from ENTRY.signal_engine import SignalEngine
from API.PHEMEX.stakan import PhemexStakanStream, DepthTop
from CORE.restorator import BotState
from CORE.executor import OrderExecutor
from CORE.models_fsm import WsInterpreter, ActivePosition
from CORE._utils import BlackListManager, PriceCacheManager, ConfigManager, Reporters

from EXIT.scenarios.grid_tp import GridTPFactory
from EXIT.scenarios.stop_loss import StopLossScenario
from EXIT.scenarios.ttl_close import TtlCloseScenario
from ENTRY.funding_manager import FundingManager
from ANALYTICS.tracker import PerformanceTracker, format_duration
from ENTRY.pattern_math import EntrySignal

from c_log import UnifiedLogger
from utils import get_config_summary

logger = UnifiedLogger("bot")
BASE_DIR = Path(__file__).resolve().parent.parent
CFG_PATH = BASE_DIR / "cfg.json"


class TradingBot:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        
        self.max_active_positions = self.cfg["app"]["max_active_positions"]
        self.quota_asset = self.cfg["quota_asset"]
        
        self.signal_timeout_sec = self.cfg["entry"]["signal_timeout_sec"]
        self.hedge_mode = self.cfg["risk"]["hedge_mode"]
        upd_sec = self.cfg["entry"]["pattern"]["binance"]["update_prices_sec"]
        
        api_key = os.getenv("API_KEY") or self.cfg["credentials"]["api_key"]
        api_secret = os.getenv("API_SECRET") or self.cfg["credentials"]["api_secret"]

        self.bl_manager = BlackListManager(CFG_PATH, self.quota_asset)        
        self.black_list = self.bl_manager.load_from_config(self.cfg.get("black_list", []))
        
        self.state = BotState(black_list=self.black_list)    
        self.tracker = PerformanceTracker(self.state)   
        self._is_running = False

        self.phemex_sym_api = PhemexSymbols()
        self.binance_ticker_api = BinanceTickerAPI()
        self.phemex_ticker_api = PhemexTickerAPI()        
        self.phemex_funding_api = PhemexFunding()
        self.binance_funding_api = BinanceFunding()
        self.session = aiohttp.ClientSession()

        self.price_manager = PriceCacheManager(self.binance_ticker_api, self.phemex_ticker_api, upd_sec)
        self.private_client = PhemexPrivateClient(api_key, api_secret, self.session)
        self.private_ws = PhemexPrivateWS(api_key, api_secret)

        tg_cfg = self.cfg.get("tg", {})
        if tg_cfg.get("enable"):
            from TG.tg_sender import TelegramSender
            token = os.getenv("TELEGRAM_TOKEN") or tg_cfg["token"]
            chat_id = os.getenv("TELEGRAM_CHAT_ID") or tg_cfg["chat_id"]
            self.tg = TelegramSender(token, chat_id)
        else:
            self.tg = None

        self.funding_manager = FundingManager(
            self.cfg["entry"]["pattern"],
            self.phemex_funding_api,
            self.binance_funding_api
        )

        upbit_cfg = self.cfg.get("upbit", {})
        upbit_enabled = self.cfg.get("entry", {}).get("pattern", {}).get("upbit_signal", False)
        if upbit_enabled:
            from ENTRY.upbit_signal import UpbitLiveMonitor, MockUpbitLiveMonitor
            if upbit_cfg.get("test_mode", False):
                logger.warning("🧨 [Upbit] Запущен MOCK-режим (тестовые сигналы, реальных сделок нет).")
                self._upbit_monitor = MockUpbitLiveMonitor(
                    poll_interval_sec=upbit_cfg.get("poll_interval_sec", 8.0),
                    on_signal=self._on_upbit_signal,
                    symbols_to_mock=upbit_cfg.get("symbols_to_mock", ["BTCUSDT", "ETHUSDT", "SOLUSDT"]),
                )
            else:
                self._upbit_monitor = UpbitLiveMonitor(
                    poll_interval_sec=upbit_cfg.get("poll_interval_sec", 8.0),
                    proxies=upbit_cfg.get("proxies", []),
                    cooldown_sec=upbit_cfg.get("cooldown_sec", 8.0),
                    on_signal=self._on_upbit_signal,
                )
        else:
            self._upbit_monitor = None
            
        self.stakan_stream: Optional[PhemexStakanStream] = None
        self._stakan_task: Optional[asyncio.Task] = None

        self.signal_engine = SignalEngine(self.cfg["entry"], self.funding_manager)

        self._processing: Set[str] = set()
        self._signal_timeouts: Dict[str, float] = {}
        self.cfg_manager = ConfigManager(CFG_PATH, self)
        
        self.active_positions_locker: Dict[str, asyncio.Lock] = {} 
        self.global_entry_lock = asyncio.Lock()

        self.ws_handler = WsInterpreter(state=self.state, active_positions_locker=self.active_positions_locker) 
        self.executor = OrderExecutor(self)
        
        exit_cfg = self.cfg["exit"]
        scen_cfg = exit_cfg["scenarios"]
        
        self.grid_tp_factory  = GridTPFactory(scen_cfg.get("grid_tp", {}))
        self.stop_loss_scen   = StopLossScenario(scen_cfg.get("stop_loss", {}))
        self.ttl_close_scen   = TtlCloseScenario(scen_cfg.get("ttl_market_close", {}))

        self.min_quarantine_threshold_usdt = -abs(self.cfg["risk"]["quarantine"]["min_quarantine_threshold_usdt"])
        self.force_quarantine_threshold_usdt = -abs(self.cfg["risk"]["quarantine"]["force_quarantine_threshold_usdt"])

    async def _await_task(self, task: asyncio.Task | None):
        if not task: return
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        except Exception as e: logger.debug(f"Task shutdown note: {e}")

    def set_blacklist(self, symbols: list) -> tuple[bool, str]:
        success, msg = self.bl_manager.update_and_save(symbols)
        if success:
            self.black_list = self.bl_manager.symbols
            if hasattr(self.state, 'black_list'):
                self.state.black_list = self.black_list
        return success, msg    

    async def quarantine_util(self, symbol) -> bool:        
        if symbol in self.state.quarantine_until:
            q_val = self.state.quarantine_until[symbol]
            try: limit_time = float(q_val)
            except (ValueError, TypeError): limit_time = 0.0

            if time.time() > limit_time:
                del self.state.quarantine_until[symbol]
                self.state.consecutive_fails[symbol] = 0
                asyncio.create_task(self.state.save()) 
                return True            
            elif not any(k.startswith(f"{symbol}_") for k in self.state.active_positions): 
                return False
        return True
        
    def apply_entry_quarantine(self, symbol: str):
        q_hours = self.cfg.get("entry", {}).get("quarantine", {}).get("quarantine_hours", 1)
        if str(q_hours).lower() == "inf":
            self.state.quarantine_until[symbol] = "inf"
            logger.warning(f"[{symbol}] 🚫 Помещен в бессрочный карантин (вход не удался).")
        elif float(q_hours) > 0:
            self.state.quarantine_until[symbol] = time.time() + (float(q_hours) * 3600)
            logger.warning(f"[{symbol}] 🚫 Помещен в карантин на {q_hours}ч (вход не удался).")
        asyncio.create_task(self.state.save())

    def apply_loss_quarantine(self, symbol: str, trade_pnl: float):
        q_cfg = self.cfg.get("risk", {}).get("quarantine", {})
        max_fails = q_cfg.get("max_consecutive_fails", 1)
        q_hours = q_cfg.get("quarantine_hours", "inf")

        self.state.consecutive_fails[symbol] = self.state.consecutive_fails.get(symbol, 0) + 1
        quarantine_condition = (
            (self.state.consecutive_fails[symbol] >= max_fails and
            trade_pnl <= self.min_quarantine_threshold_usdt) or (trade_pnl <= self.force_quarantine_threshold_usdt)
        )
        if quarantine_condition:
            if str(q_hours).lower() == "inf": self.state.quarantine_until[symbol] = "inf" 
            else: self.state.quarantine_until[symbol] = time.time() + (float(q_hours) * 3600)
            logger.warning(f"[{symbol}] 💀 Карантин ПОТЕРЬ: {self.state.consecutive_fails[symbol]} фейлов. Блокировка на {q_hours}ч.")
        asyncio.create_task(self.state.save())

    async def _recover_state(self):
        try:
            self.state.load()
            resp = await self.private_client.get_active_positions()
            data_block = resp.get("data", {})
            data = data_block.get("positions", []) if isinstance(data_block, dict) else data_block
            if not isinstance(data, list): data = []

            exchange_positions = {}
            for item in data:
                size = float(item.get("sizeRq", item.get("size", 0)))
                if size != 0:
                    symbol = item.get("symbol")
                    pos_side_raw = item.get("posSide", item.get("side", "")).lower()
                    pos_side = "LONG" if pos_side_raw in ("long", "buy") else "SHORT"
                    pos_key = f"{symbol}_{pos_side}"
                    exchange_positions[pos_key] = {"size": abs(size), "side": pos_side, "symbol": symbol}

            keys_to_remove = [k for k in list(self.state.active_positions.keys()) if k not in exchange_positions]
            for k in keys_to_remove:
                pos = self.state.active_positions.get(k)
                
                # 👇 БАГФИКС: Мягкое удаление для спасения аналитики
                if pos and pos.in_position:
                    pos.is_closed_by_exchange = True # Отдаем на растерзание Game Loop
                else:
                    self.state.active_positions.pop(k, None) # Удаляем только пустые/фантомные

            for pos_key, ex_data in exchange_positions.items():
                if pos_key in self.state.active_positions:
                    self.state.active_positions[pos_key].current_qty = ex_data["size"]
                    self.state.active_positions[pos_key].in_position = True
                    self.state.active_positions[pos_key].in_pending = False

            await self.state.save()
            logger.info(f"✅ Стейт синхронизирован. В памяти {len(self.state.active_positions)} активных позиций.")
        except Exception as e:
            logger.error(f"❌ Ошибка Recovery: {e}")

    def _get_lock(self, pos_key: str) -> asyncio.Lock:
        if pos_key not in self.active_positions_locker:
            self.active_positions_locker[pos_key] = asyncio.Lock()
        return self.active_positions_locker[pos_key]

    def _check_risk_limits(self, symbol: str) -> bool:
        working_symbols = set()
        has_long, has_short = False, False
        
        for pos_key, pos in self.state.active_positions.items():
            if pos.in_position or pos.in_pending:
                working_symbols.add(pos.symbol)
                if pos.symbol == symbol:
                    if pos.side == "LONG": has_long = True
                    if pos.side == "SHORT": has_short = True

        if not self.hedge_mode and (has_long or has_short):
            return False

        if symbol in working_symbols:
            return True

        if len(working_symbols) >= self.max_active_positions:
            return False
            
        return True
    
    # Новый метод в TradingBot:
    async def _on_upbit_signal(self, symbol: str) -> None:
        """Сигнал с Upbit: новый листинг. Всегда входим LONG."""
        side = self.cfg.get("upbit", {}).get("default_side", "long").upper()  # хардкод LONG, но из конфига
        phemex_symbol = f"u{symbol}USDT"   # формат Phemex для perpetual
        
        if phemex_symbol in self.black_list:
            logger.info(f"[Upbit] {phemex_symbol} в блэклисте, пропускаем.")
            return
        if phemex_symbol not in self.symbol_specs:
            # Новая монета может появиться на Phemex уже после старта бота.
            # Делаем быстрый ре-фетч спецификаций и перезапускаем стакан.
            logger.info(f"[Upbit] {phemex_symbol} не в кэше symbol_specs, пробуем ре-фетч...")
            try:
                symbols_info = await self.phemex_sym_api.get_all(quote="USDT", only_active=True)
                self.symbol_specs = {s.symbol: s for s in symbols_info if s and s.symbol not in self.black_list}
                # Перезапускаем стакан на обновлённом наборе символов
                if getattr(self, 'stakan_stream', None):
                    self.stakan_stream.stop()
                    await self._await_task(getattr(self, '_stakan_task', None))
                    symbols_to_stream = list(self.symbol_specs.keys())
                    from API.PHEMEX.stakan import PhemexStakanStream
                    self.stakan_stream = PhemexStakanStream(symbols_to_stream)
                    mid_cache = self.price_manager.phemex_prices
                    def on_stakan_depth(sym, depth):
                        if depth.bid1 > 0 and depth.ask1 > 0:
                            mid_cache[sym] = (depth.bid1 + depth.ask1) / 2.0
                    self._stakan_task = asyncio.create_task(self.stakan_stream.run(on_stakan_depth))
            except Exception as e:
                logger.error(f"[Upbit] Ошибка ре-фетча symbol_specs: {e}")
            if phemex_symbol not in self.symbol_specs:
                logger.warning(f"[Upbit] {phemex_symbol} всё равно не на Phemex, пропускаем.")
                return
        if not await self.quarantine_util(phemex_symbol):
            return

        pos_key = f"{phemex_symbol}_{side}"
        logger.warning(f"🚀 [Upbit Signal] {phemex_symbol} → {side}")

        async with self.global_entry_lock:
            if not self._can_open_position(phemex_symbol, side):
                logger.info(f"[Upbit] Отклонен вход для {phemex_symbol} (лимит позиций или хедж).")
                return

            async with self._get_lock(pos_key):
                if pos_key in self.state.active_positions:
                    return
                self.state.active_positions[pos_key] = ActivePosition(
                    symbol=phemex_symbol, side=side, pending_qty=0.0,
                    in_pending=True, in_position=False,
                    mid_price=0.0,
                )

        # EntrySignal нужен для executor — берем текущую цену с Phemex
        _, p_price = self.price_manager.get_prices(phemex_symbol)
        if p_price <= 0:
            logger.warning(f"[Upbit] Нет цены для {phemex_symbol}, вход невозможен.")
            async with self._get_lock(pos_key):
                self.state.active_positions.pop(pos_key, None)
            return

        signal = EntrySignal(
            symbol=phemex_symbol,
            side=side,
            price=p_price,
            init_ask1=p_price,
            init_bid1=p_price,
            mid_price=p_price,
        )

        success = await self.executor.execute_entry(phemex_symbol, pos_key, signal)
        if success:
            await self.state.save()
        else:
            self.apply_entry_quarantine(phemex_symbol)
            async with self._get_lock(pos_key):
                p = self.state.active_positions.get(pos_key)
                if p and not p.in_position:
                    p.marked_for_death_ts = time.time()
        
    # --- СЕТЕВЫЕ ОПЕРАЦИИ (ВНЕ ЛОКА) ---
    async def _payloader(self, action_payload: List[Tuple], symbol: str) -> None:
        
        async def process_action(action: Tuple):
            cmd = action[0]
            price, timeout, pos_key = action[1], action[2], action[3]
            
            try:
                # Уходим в сеть (вне лока)
                if cmd == "MARKET_EXIT":
                    success = await self.executor.execute_market_exit(symbol, pos_key)
                else:
                    success = await self.executor.execute_exit(symbol, pos_key, price, timeout)
            finally:
                # ГАРАНТИРОВАННО снимаем флаг полета после возврата управления
                async with self._get_lock(pos_key):
                    p = self.state.active_positions.get(pos_key)
                    if p:
                        p.exit_in_flight = False
                        p.last_exit_status = p.exit_status

        # Параллельный запуск всех экшенов. gather дождется всех, но флаги снимутся асинхронно!
        tasks = [process_action(act) for act in action_payload]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    logger.error(f"Критическая ошибка внутри _payloader: {res}")

    # --- ЛОГИКА АНАЛИЗА И ВЫХОДА ---
    async def _evaluate_exit_scenarios(self, symbol: str, long_key: str, short_key: str) -> None:
        now = time.time()
        actions_to_execute: List[Tuple] = []

        _, p_price = self.price_manager.get_prices(symbol)
        current_price = p_price
        if current_price <= 0: return
        
        exit_cfg = self.cfg.get("exit", {}).get("scenarios", {})

        for pos_key in (long_key, short_key):
            async with self._get_lock(pos_key):
                pos = self.state.active_positions.get(pos_key)
                if not pos or not pos.in_position or pos.current_qty <= 0:
                    continue

                if not pos.tp_grid_initiated:
                    volume_24h = self.price_manager.get_volume(pos.symbol)
                    spec = self.symbol_specs.get(pos.symbol)
                    if spec:
                        orders = self.grid_tp_factory.calculate_grid(
                            symbol=pos.symbol,
                            side=pos.side,
                            entry_price=pos.entry_price,
                            total_qty=pos.current_qty,
                            tick_size=spec.tick_size,
                            lot_size=spec.lot_size,
                            volume_24h_usd=volume_24h
                        )
                        if orders:
                            asyncio.create_task(self.executor.execute_tp_grid(pos.symbol, pos_key, orders))
                        else:
                            logger.error(f"[{pos_key}] Ошибка расчета Grid TP.")
                    continue

                if pos.exit_in_flight:
                    continue

                is_stop_loss = self.stop_loss_scen.is_triggered(pos, current_price)
                is_ttl       = self.ttl_close_scen.is_triggered(pos, now)

                if is_stop_loss or is_ttl:
                    pos.exit_status = "STOP_LOSS" if is_stop_loss else "TTL_CLOSE"
                    pos.exit_in_flight = True
                    logger.debug(f"[{pos_key}] Сработал {'Stop-Loss' if is_stop_loss else 'TTL Market Close'}. Цена: {current_price}")
                    actions_to_execute.append(("MARKET_EXIT", current_price, 5.0, pos_key))

        # --- СЕТЕВЫЕ ОПЕРАЦИИ (ВНЕ ЛОКА) ---
        if actions_to_execute:
            await self._payloader(actions_to_execute, symbol)

    async def _process_symbol_exits(self, symbol: str, long_key: str, short_key: str):
        if symbol in self._processing: return
        self._processing.add(symbol)
        try:
            await self._evaluate_exit_scenarios(symbol, long_key, short_key)
        except Exception as e:
            err_tb = traceback.format_exc()
            logger.error(f"Exit pipeline error for {symbol}: {e}\n{err_tb}")
        finally:
            self._processing.discard(symbol)

    async def _main_trading_loop(self):
        logger.info("🎮 Главная торговая живолупа (Game Loop) запущена.")
        while self._is_running:
            keys_to_check = list(self.state.active_positions.keys())
            for pos_key in keys_to_check:
                async with self._get_lock(pos_key):
                    try:
                        pos: "ActivePosition" = self.state.active_positions.get(pos_key)
                        if not pos: continue

                        # --- СБОРЩИК ФАНТОМНЫХ ВХОДОВ ---
                        if getattr(pos, 'marked_for_death_ts', 0) > 0:
                            if pos.in_position:
                                pos.marked_for_death_ts = 0.0 
                            elif time.time() - pos.marked_for_death_ts > 5.0:
                                logger.debug(f"[{pos_key}] 🗑 Удаление фантомной позиции (no WS fill).")
                                self.state.active_positions.pop(pos_key, None)
                                self.active_positions_locker.pop(pos_key, None)
                                asyncio.create_task(self.state.save())
                                continue

                        if getattr(pos, 'last_notified_tp_progress', 0) < getattr(pos, 'tp_progress', 0):
                            new_count = pos.tp_progress
                            if self.tg:
                                msg = f"🎯 <b>[{pos.symbol}]</b> Тейк-профит (уровень {new_count}) исполнен!\n💰 Зафиксирована часть прибыли."
                                asyncio.create_task(self.tg.send_message(msg))
                            pos.last_notified_tp_progress = new_count
                            asyncio.create_task(self.state.save())

                        if getattr(pos, 'is_closed_by_exchange', False):
                            if pos.entry_price > 0.0:
                                
                                exit_pr = pos.exit_price_hint or pos.avg_price
                                duration_sec = time.time() - pos.opened_at
                                
                                net_pnl, is_win = self.tracker.register_trade(
                                    symbol=pos.symbol,
                                    side=pos.side,
                                    entry_price=pos.avg_price if pos.avg_price > 0 else pos.entry_price,
                                    exit_price=exit_pr,
                                    qty=pos.max_realized_qty,
                                    duration_sec=duration_sec
                                )

                                emoji = "💵" if is_win else "🩸"

                                current_status = pos.exit_status if pos.exit_status in ("EXTREME", "BREAKEVEN") else pos.last_exit_status
                                
                                if current_status == "EXTREME": 
                                    semantic = "⚠️ Аварийный выход (EXTREME Mode)"
                                elif current_status == "BREAKEVEN": 
                                    semantic = "🛡 Выход по безубытку (TTL)"                                
                                else: 
                                    semantic = "🎯 Тейк-профит" if is_win else "📉 Убыток (Ручное/Неизвестно)"

                                if is_win:
                                    self.state.consecutive_fails[pos.symbol] = 0
                                    self.state.quarantine_until.pop(pos.symbol, None)
                                else:
                                    self.apply_loss_quarantine(pos.symbol, net_pnl)

                                if self.tg:
                                    msg = Reporters.exit_success(pos_key, semantic, exit_pr)
                                    msg += f"\n⏳ Время в сделке: {format_duration(duration_sec)}"
                                    asyncio.create_task(self.tg.send_message(msg))
                                    
                                logger.info(f"[{pos_key}] 🛑 Позиция закрыта. {emoji} PnL: {net_pnl:.4f}$ | Время: {format_duration(duration_sec)}")

                            # Снимаем все возможные ордера-призраки (лимитки тейк-профитов) перед удалением позиции
                            asyncio.create_task(self.executor.cancel_all_orders(pos.symbol))

                            self.state.active_positions.pop(pos_key, None)
                            self.active_positions_locker.pop(pos_key, None) 
                            asyncio.create_task(self.state.save())

                    except Exception as e:
                        logger.error(f"[{pos_key}] 💥 Критическая ошибка при обработке позиции в Game Loop: {e}\n{traceback.format_exc()}")

            active_symbols = set()
            for pos in self.state.active_positions.values():
                if pos.in_position or pos.in_pending:
                    active_symbols.add(pos.symbol)
                    
            tasks = []
            for symbol in active_symbols:
                if symbol not in self._processing:
                    pos_long_key, pos_short_key = f"{symbol}_LONG", f"{symbol}_SHORT"
                    tasks.append(self._process_symbol_exits(symbol, pos_long_key, pos_short_key))
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=False)
            
            await asyncio.sleep(0.01)

    async def _on_ws_subscribe(self):
        """Срабатывает при первом подключении и каждом успешном реконнекте WS"""
        logger.info("🔄 WS Подписан на приватный канал. Запуск синхронизации стейта (Recover)...")
        await self._recover_state()

    async def start(self):
        if getattr(self, '_is_running', False): return
        self._is_running = True
        logger.info("▶️ Инициализация систем...")
        summary = get_config_summary(self.cfg)
        logger.info(f"⚙️ БОТ ЗАПУЩЕН С НАСТРОЙКАМИ\n{summary}")
        if self.tg: asyncio.create_task(self.tg.send_message("🟢 <b>ТОРГОВЛЯ НАЧАТА</b>"))

        symbols_info = await self.phemex_sym_api.get_all(quote=self.bl_manager.quota_asset, only_active=True)
        self.symbol_specs = {s.symbol: s for s in symbols_info if s and s.symbol not in self.black_list}

        await self._recover_state()
        
        # --- БЫСТРЫЙ ПАРСИНГ ЦЕН ---
        async def on_stakan_depth(d: DepthTop):
            if d.bids and d.asks:
                mid = (d.bids[0][0] + d.asks[0][0]) / 2
                self.price_manager.phemex_prices[d.symbol] = mid
                
        symbols_to_stream = list(self.symbol_specs.keys())
        if symbols_to_stream:
            self.stakan_stream = PhemexStakanStream(symbols_to_stream, depth=1, chunk_size=40, throttle_ms=0)
            self._stakan_task = asyncio.create_task(self.stakan_stream.run(on_stakan_depth))

        # ==========================================
        # ИНИЦИАЛИЗАЦИЯ ФИНАНСОВОГО АУДИТА
        # ==========================================
        try:
            if hasattr(self, 'tracker'):
                saved_start_balance = self.tracker.data.get("start_balance", 0.0)
                
                if saved_start_balance > 0:
                    logger.info(f"💰 Стартовый баланс успешно загружен из стейта: {saved_start_balance:.2f} {self.quota_asset}")
                else:
                    logger.info("📡 Запрашиваем Equity с биржи для инициализации аудита...")
                    usd_balance = await self.private_client.get_equity(self.quota_asset)
                    
                    self.tracker.set_initial_balance(usd_balance)
                    logger.info(f"💰 Стартовый баланс зафиксирован: {usd_balance:.2f} {self.quota_asset}")
                    await self.state.save()
                    
        except Exception as e:
            logger.error(f"❌ Не удалось получить/установить стартовый баланс: {e}")
        # ==========================================

        logger.info("🔄 Прогрев кэша цен и фандинга...")
        await self.price_manager.warmup()
        self._price_updater_task = asyncio.create_task(self.price_manager.loop())
        await self._await_task(getattr(self, '_funding_task', None))
        self._funding_task = asyncio.create_task(self.funding_manager.run())
        if self._upbit_monitor:
            self._upbit_task = asyncio.create_task(self._upbit_monitor.run())
        await asyncio.sleep(1)

        self._private_ws_task = asyncio.create_task(
            self.private_ws.run(
                self.ws_handler.process_phemex_message,
                on_subscribe=self._on_ws_subscribe
            )
        )

        self._game_loop_task = asyncio.create_task(self._main_trading_loop())

    async def aclose(self):
        await self.stop()
        logger.info("💾 Финальное сохранение стейта на диск...")
        await self.state.save()
        await self.phemex_sym_api.aclose()
        await self.binance_ticker_api.aclose()
        await self.phemex_ticker_api.aclose()
        await self.phemex_funding_api.aclose()
        if self.tg: await self.tg.aclose()
        if self.session and not self.session.closed: await self.session.close()

    async def stop(self):
        if not getattr(self, '_is_running', False): return
        self._is_running = False
        if getattr(self, 'stakan_stream', None):
            self.stakan_stream.stop()
        logger.info("⏹ Остановка процессов...")
        if self.tg: await self.tg.send_message("⏹ Остановка процессов...")
        self.price_manager.stop()
        self.funding_manager.stop()
        await self.private_ws.aclose()
        await self._await_task(getattr(self, '_price_updater_task', None))
        await self._await_task(getattr(self, '_funding_task', None))
        await self._await_task(getattr(self, '_private_ws_task', None))
        await self._await_task(getattr(self, '_game_loop_task', None))
        await self._await_task(getattr(self, '_upbit_task', None))
        await self._await_task(getattr(self, '_stakan_task', None))
        self._processing.clear()
        self._signal_timeouts.clear()
        await self.state.save()