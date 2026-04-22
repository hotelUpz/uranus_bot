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

# Константы для проверки готовности систем
READY_CHECK_INITIAL_SLEEP = 1.0
READY_CHECK_TIMEOUT = 10.0
READY_CHECK_INTERVAL = 0.5


class TradingBot:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        
        self.max_active_positions = self.cfg["app"]["max_active_positions"]
        self.quota_asset = self.cfg["quota_asset"]
        
        self.signal_timeout_sec = self.cfg["entry"]["signal_timeout_sec"]
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
            
        self.signal_engine = SignalEngine(self.cfg["entry"], self.funding_manager)
        
        self.st_stream: Optional[PhemexStakanStream] = None
        self._stakan_task: Optional[asyncio.Task] = None

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

    async def _validate_notional_limit(self) -> bool:
        """
        Математическая проверка: сможет ли notional_limit покрыть минимальный шаг
        сетки тейк-профитов с учетом шага лота на бирже для всех монет.
        """
        # Если сетка выключена, валидация не требуется
        if not self.cfg.get("exit", {}).get("scenarios", {}).get("grid_tp", {}).get("enable"):
            return True
            
        grid_map = self.cfg.get("exit", {}).get("scenarios", {}).get("grid_tp", {}).get("map", [])
        if not grid_map:
            return True
            
        # 1. Ищем самый мелкий процент объема среди всех уровней всех бакетов
        min_pct = 100.0
        for bucket in grid_map:
            vols = bucket.get("volumes", [])
            if vols:
                min_pct = min(min_pct, min(vols))
                
        if min_pct <= 0 or min_pct > 100:
            logger.error("❌ Критическая ошибка конфига: Некорректные проценты объемов в сетке TP!")
            return False
            
        buffer_mult = 1.10  # 10% буфер запаса на проскальзывания цены
        notional = float(self.cfg["risk"]["notional_limit"])
        failed_symbols = []
        
        # 2. Проверяем каждую монету
        for symbol, spec in self.symbol_specs.items():
            _, current_price = self.price_manager.get_prices(symbol)
            if current_price <= 0:
                continue
                
            # Минимальная цена лота в долларах
            min_usd_lot = spec.lot_size * current_price
            
            # Какой долларовый объем приходится на самый мелкий ордер сетки?
            smallest_chunk_usd = notional * (min_pct / 100.0)
            
            # Если наш кусок сетки меньше биржевого лимита + буфер
            if smallest_chunk_usd < min_usd_lot * buffer_mult:
                failed_symbols.append((symbol, min_usd_lot))
                
        # 3. Реакция на провал валидации
        if failed_symbols:
            # Сортируем по "тяжести" нарушения
            failed_symbols.sort(key=lambda x: x[1], reverse=True)
            worst_sym, worst_lot_usd = failed_symbols[0]
            
            # Считаем, сколько реально нужно денег на notional_limit
            required_notional = (worst_lot_usd * buffer_mult) / (min_pct / 100.0)
            
            msg = (f"⛔️ ОШИБКА РИСК-МЕНЕДЖМЕНТА: Значение notional_limit ({notional}$) слишком мало!\n"
                   f"Из-за шага лота для монеты {worst_sym} минимальная сделка стоит ~{worst_lot_usd:.2f}$.\n"
                   f"Самый мелкий ордер сетки TP — это {min_pct}% от объема ({smallest_chunk_usd:.2f}$).\n"
                   f"Биржа отвергнет выставление тейк-профитов!\n"
                   f"💡 Решение: Увеличьте notional_limit минимум до {required_notional:.2f}$ "
                   f"или уберите монету {worst_sym} в black_list.")
            
            logger.error(msg)
            if self.tg:
                await self.tg.send_message(msg)
            return False
            
        logger.info(f"✅ Финансовая валидация пройдена. Мин. кусок сетки TP: {min_pct}%")
        return True

    def _get_lock(self, pos_key: str) -> asyncio.Lock:
        if pos_key not in self.active_positions_locker:
            self.active_positions_locker[pos_key] = asyncio.Lock()
        return self.active_positions_locker[pos_key]

    def _can_open_position(self, symbol: str, side: str = "LONG") -> bool:
        working_symbols = set()
        has_long, has_short = False, False
        
        for pos_key, pos in self.state.active_positions.items():
            if pos.in_position or pos.in_pending:
                working_symbols.add(pos.symbol)

        if symbol in working_symbols:
            return True

        if len(working_symbols) >= self.max_active_positions:
            return False
            
        return True
    
    # Новый метод в TradingBot:
    async def _on_upbit_signal(self, symbol: str) -> None:
        """Сигнал с Upbit: новый листинг. Всегда входим LONG."""
        side = self.cfg.get("upbit", {}).get("default_side", "long").upper()  # хардкод LONG, но из конфига
        phemex_symbol = f"{symbol}USDT"   # формат Phemex для perpetual
        
        if phemex_symbol in self.black_list:
            logger.info(f"[Upbit] {phemex_symbol} в блэклисте, пропускаем.")
            return
        if phemex_symbol not in self.symbol_specs:
            logger.info(f"[Upbit] {phemex_symbol} не в кэше, пробуем ре-фетч спецификаций...")
            try:
                symbols_info = await self.phemex_sym_api.get_all(quote="USDT", only_active=True)
                self.symbol_specs = {s.symbol: s for s in symbols_info if s and s.symbol not in self.black_list}
                # Если монета появилась — переподписываемся на WS
                if phemex_symbol in self.symbol_specs:
                    await self._refresh_stakan_stream()
            except Exception as e:
                logger.error(f"[Upbit] Ошибка ре-фетча specs: {e}")
            
            if phemex_symbol not in self.symbol_specs:
                logger.warning(f"[Upbit] {phemex_symbol} всё ещё нет на Phemex, отмена.")
                return

        pos_key = f"{phemex_symbol}_{side}"
        logger.warning(f"🚀 [Upbit Signal] {phemex_symbol} → {side}")
        # Сразу в бан, чтобы не было повторных входов по этому же сигналу (перма-бан в cfg.json)
        self.set_blacklist(list(set(self.black_list + [phemex_symbol])))

        # Запрашиваем цены из кэша (снапшот)
        bids, asks = self.st_stream.get_depth(phemex_symbol) if self.st_stream else ([], [])
        signal = self.signal_engine.create_signal(phemex_symbol, side, bids, asks)
        
        if not signal:
            logger.warning(f"[Upbit] Не удалось сформировать сигнал для {phemex_symbol} (нет цен или фандинг).")
            return

        async with self.global_entry_lock:
            if not self._can_open_position(phemex_symbol, side):
                logger.info(f"[Upbit] Отклонен вход для {phemex_symbol} (лимит позиций).")
                return

            async with self._get_lock(pos_key):
                if pos_key in self.state.active_positions:
                    return
                self.state.active_positions[pos_key] = ActivePosition(
                    symbol=phemex_symbol, side=side, pending_qty=0.0,
                    in_pending=True, in_position=False,
                    mid_price=signal.mid_price, # Используем актуальную цену из сигнала
                )

        # 3. ВХОД (ВСЕГДА РЕАЛЬНЫЙ)
        success = await self.executor.execute_entry(phemex_symbol, pos_key, signal)

        if success:
            await self.state.save()
        else:
            logger.warning(f"[{phemex_symbol}] Ошибка входа! Монета заносится в черный список.")
            self.set_blacklist(list(set(self.black_list + [phemex_symbol])))
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
                    if not spec:
                        logger.error(f"[{pos_key}] ❌ Ошибка Grid TP: Нет спецификаций (tick_size/lot_size) для {pos.symbol}!")
                        continue
                        
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
                        pos.tp_grid_initiated = True # Предотвращаем спам тасок
                        asyncio.create_task(self.executor.execute_tp_grid(pos.symbol, pos_key, orders))
                    else:
                        logger.error(f"[{pos_key}] ❌ Ошибка расчета Grid TP (возвращен пустой список ордеров).")
                    continue

                if pos.exit_in_flight:
                    continue

                is_stop_loss = self.stop_loss_scen.is_triggered(pos, current_price, now)
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

    async def _refresh_stakan_stream(self):
        """Перезапускает стрим стаканов на актуальном наборе символов."""
        await self._await_task(self._stakan_task)
        
        symbols_to_stream = list(self.symbol_specs.keys())
        if not symbols_to_stream: return
        
        logger.info(f"🔄 Рестарт стрима стаканов на {len(symbols_to_stream)} символов...")
        self.st_stream = PhemexStakanStream(symbols_to_stream)
        
        # Замыкаем кэш цен напрямую на price_manager
        phemex_cache = self.price_manager.phemex_prices
        
        async def on_stakan_depth(depth: DepthTop):
            if depth.bids and depth.asks:
                bid1 = depth.bids[0][0]
                ask1 = depth.asks[0][0]
                if bid1 > 0 and ask1 > 0:
                    phemex_cache[depth.symbol] = (bid1 + ask1) / 2.0
                
        self._stakan_task = asyncio.create_task(self.st_stream.run(on_stakan_depth))

    async def _wait_for_systems_ready(self) -> bool:
        """Ожидает, пока все фоновые системы (цены, фандинг, стаканы) не получат первые данные."""
        logger.info(f"⏳ Ожидание готовности систем (Timeout: {READY_CHECK_TIMEOUT}s)...")
        start_time = time.time()
        
        await asyncio.sleep(READY_CHECK_INITIAL_SLEEP)
        
        while time.time() - start_time < READY_CHECK_TIMEOUT:
            # 1. Проверяем кэш цен (наполняется из стакана)
            # Мы проверяем именно last_msg_ts у st_stream, чтобы убедиться, что WS ожил
            stakan_ready = False
            if self.st_stream and self.st_stream.last_msg_ts > 0:
                stakan_ready = True
            
            # 2. Проверяем кэш фандинга
            funding_ready = True
            if self.funding_manager.enable_phemex:
                funding_ready = len(self.funding_manager.phemex_cache) > 0
                
            # 3. Проверяем кэш цен из REST (warmup уже прошел, но на всякий случай)
            prices_ready = len(self.price_manager.phemex_prices) > 0
                
            if stakan_ready and funding_ready and prices_ready:
                logger.info(f"✅ Системы готовы за {time.time() - start_time:.1f}с. Данные получены.")
                return True
            
            await asyncio.sleep(READY_CHECK_INTERVAL)
            
        return False

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
        await self._refresh_stakan_stream()

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

        # ==========================================
        # ПРЕ-ФЛАЙТ ВАЛИДАЦИЯ ЛИМИТОВ
        # ==========================================
        if not await self._validate_notional_limit():
            logger.error("🛑 Бот остановлен из-за ошибки в расчете лимитов риска.")
            # Мягко гасим всё, что успели запустить выше, но не убиваем ТГ-бота
            await self.stop() 
            return
        # ==========================================

        self._price_updater_task = asyncio.create_task(self.price_manager.loop())
        await self._await_task(getattr(self, '_funding_task', None))
        self._funding_task = asyncio.create_task(self.funding_manager.run())
        
        await asyncio.sleep(1)

        self._private_ws_task = asyncio.create_task(
            self.private_ws.run(
                self.ws_handler.process_phemex_message,
                on_subscribe=self._on_ws_subscribe
            )
        )

        self._game_loop_task = asyncio.create_task(self._main_trading_loop())
        logger.info("🎮 Главная торговая живолупа (Game Loop) запущена.")

        # --- ЖЕСТКАЯ ПРОВЕРКА ГОТОВНОСТИ ---
        if not await self._wait_for_systems_ready():
            msg = f"❌ КРИТИЧЕСКАЯ ОШИБКА: Системы не инициализированы (нет данных) за {READY_CHECK_TIMEOUT} сек!"
            logger.error(msg)
            if self.tg: await self.tg.send_message(f"🚨 {msg}\nБот остановлен.")
            
            # Мягко гасим всё, что успели запустить, но не убиваем процесс
            await self.stop()
            return

        # Сигналы Upbit запускаем В ПОСЛЕДНЮЮ ОЧЕРЕДЬ, когда всё остальное уже шуршит
        if self._upbit_monitor:
            self._upbit_task = asyncio.create_task(self._upbit_monitor.run())

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
        
        # 👇 ИСПРАВЛЕНО: обращаемся к правильному атрибуту
        if getattr(self, 'st_stream', None):
            self.st_stream.stop()
            
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