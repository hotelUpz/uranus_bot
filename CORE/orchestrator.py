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
from pathlib import Path

from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.order import PhemexPrivateClient
from API.PHEMEX.ws_private import PhemexPrivateWS
from API.PHEMEX.ticker import PhemexTickerAPI

from ENTRY.signal_engine import SignalEngine, EntrySignal
from API.PHEMEX.stakan import PhemexStakanStream, DepthTop
from CORE.restorator import BotState
from CORE._executor import OrderExecutor
from CORE.models_fsm import WsInterpreter, ActivePosition
from CORE._utils import BlackListManager, PriceCacheManager, ConfigManager, Reporters

from EXIT.scenarios.grid_tp import GridTPFactory
from EXIT.scenarios.stop_loss import StopLossScenario
from EXIT.scenarios.ttl_close import TtlCloseScenario
from ANALYTICS.tracker import PerformanceTracker, format_duration

from c_log import UnifiedLogger
from utils import get_config_summary

logger = UnifiedLogger("bot")
BASE_DIR = Path(__file__).resolve().parent.parent
CFG_PATH = BASE_DIR / "cfg.json"

# Константы для пауз и таймингов
READY_CHECK_INITIAL_SLEEP = 1.0
READY_CHECK_TIMEOUT = 30.0
READY_CHECK_INTERVAL = 1.0

UPBIT_SIGNAL_SAFETY_RELEASE_SEC = 10.0  # Резервная разблокировка если сигнал не прошел
UPBIT_SIGNAL_RELEASE_PAUSE_SEC  = 5.0   # Пауза перед возвратом монитора после входа
MAIN_LOOP_SLEEP_SEC             = 0.002 # Шаг игрового цикла (2мс)
START_UP_WARMUP_SEC             = 1.0   # Прогрев после запуска 
SPECS_UPDATE_INTERVAL_SEC       = 60.0  # <--- НОВАЯ КОНСТАНТА: как часто проверяем новые листинги
REPORT_INTERVAL_HOURS           = 6.0  # <--- ДОБАВИТЬ ЭТУ СТРОКУ


class TradingBot:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        
        self.max_active_positions = self.cfg["app"]["max_active_positions"]
        self.quota_asset = self.cfg["quota_asset"]
        
        self.signal_timeout_sec = self.cfg["entry"]["signal_timeout_sec"]
        upd_sec = 1.0
        
        api_key = os.getenv("API_KEY") or self.cfg["credentials"]["api_key"]
        api_secret = os.getenv("API_SECRET") or self.cfg["credentials"]["api_secret"]

        self.bl_manager = BlackListManager(CFG_PATH, self.quota_asset)        
        self.black_list = self.bl_manager.load_from_config(self.cfg.get("black_list", []))
        
        self.state = BotState(black_list=self.black_list)    
        self.tracker = PerformanceTracker(self.state)   
        self._is_running = False
        self.stop_another_request = False

        from curl_cffi.requests import AsyncSession
        self.phemex_session = AsyncSession(impersonate="chrome120", http_version=2, verify=True)
        
        self.phemex_sym_api = PhemexSymbols(session=self.phemex_session)
        self.phemex_ticker_api = PhemexTickerAPI(session=self.phemex_session)        

        self.price_manager = PriceCacheManager(self.phemex_ticker_api, upd_sec, self)
        self.signal_engine = SignalEngine(self.cfg["entry"], on_signal_callback=self._process_entry_from_signal)
        self.private_client = PhemexPrivateClient(api_key, api_secret, session=self.phemex_session)
        self.private_ws = PhemexPrivateWS(api_key, api_secret)

        tg_cfg = self.cfg.get("tg", {})
        if tg_cfg.get("enable"):
            from TG.tg_sender import TelegramSender
            token = os.getenv("TELEGRAM_TOKEN") or tg_cfg["token"]
            chat_id = os.getenv("TELEGRAM_CHAT_ID") or tg_cfg["chat_id"]
            self.tg = TelegramSender(token, chat_id)
        else:
            self.tg = None

        # --- ДОБАВИТЬ ЭТОТ БЛОК НИЖЕ ---
        report_id = os.getenv("REPORT_CHAT_ID")
        self.report_tg = TelegramSender(
            os.getenv("TELEGRAM_TOKEN") or tg_cfg.get("token", ""),
            report_id
        ) if report_id else None

        upbit_cfg = self.cfg.get("upbit", {})
        upbit_enabled = self.cfg.get("entry", {}).get("pattern", {}).get("upbit_signal", False)
        if upbit_enabled:
            from ENTRY.upbit_signal import UpbitLiveMonitor, MockUpbitLiveMonitor
            if upbit_cfg.get("use_mock", False):
                logger.warning("[MOCK] [Upbit] Запущен MOCK-режим (тестовые сигналы для отладки ордеров).")
                self._upbit_monitor = MockUpbitLiveMonitor(
                    poll_interval_sec=upbit_cfg.get("poll_interval_sec", 1.0),
                    on_signal=self._on_upbit_signal,
                    is_paused_func=lambda: self.stop_another_request
                )
            else:
                self._upbit_monitor = UpbitLiveMonitor(
                    upbit_cfg=upbit_cfg,
                    on_signal=self._on_upbit_signal,
                    is_paused_func=lambda: self.stop_another_request
                )
        else:
            self._upbit_monitor = None          
        
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

        self._is_validate_notional_limit = self.cfg["risk"]["_is_validate_notional_limit"]

    async def _await_task(self, task: asyncio.Task | None):
        if not task: return
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        except Exception as e: logger.debug(f"Task shutdown note: {e}")

    async def set_blacklist(self, symbols: list) -> tuple[bool, str]:
        success, msg = await self.bl_manager.update_and_save(symbols)
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
                symbol = ex_data["symbol"]
                # Blacklist-guard: позиции по заблокированным монетам не восстанавливаем
                if symbol in self.black_list:
                    logger.warning(f"[RECOVER] {pos_key} — монета в BlackList, пропускаем восстановление.")
                    continue
                if pos_key in self.state.active_positions:
                    self.state.active_positions[pos_key].current_qty = ex_data["size"]
                    self.state.active_positions[pos_key].in_position = True
                    self.state.active_positions[pos_key].in_pending = False
                else:
                    # Новая позиция, которой нет в локальном стейте (реконнект/перезапуск)
                    new_pos = ActivePosition(symbol=symbol, side=ex_data["side"])
                    new_pos.current_qty = ex_data["size"]
                    new_pos.in_position = True
                    self.state.active_positions[pos_key] = new_pos

            await self.state.save()
            logger.info(f"[OK] Стейт синхронизирован. В памяти {len(self.state.active_positions)} активных позиций.")
        except Exception as e:
            logger.error(f"[ERR] Ошибка Recovery: {e}")

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
            logger.error("[ERR] Критическая ошибка конфига: Некорректные проценты объемов в сетке TP!")
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
            
        logger.info(f"[OK] Финансовая валидация пройдена. Мин. кусок сетки TP: {min_pct}%")
        return True

    def _get_lock(self, pos_key: str) -> asyncio.Lock:
        if pos_key not in self.active_positions_locker:
            self.active_positions_locker[pos_key] = asyncio.Lock()
        return self.active_positions_locker[pos_key]

    def _can_open_position(self, symbol: str, side: str = "LONG") -> bool:
        if symbol in self.black_list:
            logger.warning(f"[SKIP] [{symbol}] Оркестратор: Отказ, монета в BlackList.")
            return False

        working_symbols = set()
        for pos_key, pos in self.state.active_positions.items():
            if pos.in_position or pos.in_pending:
                working_symbols.add(pos.symbol)

        if symbol in working_symbols:
            logger.warning(f"[SKIP] [{symbol}] Оркестратор: Отказ, позиция по монете УЖЕ существует.")
            return False

        if len(working_symbols) >= self.max_active_positions:
            logger.warning(f"[SKIP] [{symbol}] Оркестратор: Отказ, достигнут лимит позиций ({self.max_active_positions}).")
            return False

        return True

    async def _process_entry_from_signal(self, signal: "EntrySignal"):
        """Компактный интерфейс исполнения входа."""
        phemex_symbol = signal.symbol
        side = signal.side
        pos_key = f"{phemex_symbol}_{side}"

        # Детальный лог в консоль перед попыткой войти
        logger.info(f"➡️ [ОРКЕСТРАТОР] Получен сигнал {phemex_symbol} ({side}). Проверка лимитов...")

        if not self._can_open_position(phemex_symbol, side):
            logger.warning(f"[SKIP] [{phemex_symbol}] _can_open_position запретил вход.")
            return

        spec = self.symbol_specs.get(phemex_symbol)
        if not spec:
            logger.warning(f"[SKIP] [{phemex_symbol}] Оркестратор: Отказ, не загружены спецификации (lot/tick) с биржи.")
            return

        try:
            # Исполняем вход (напрямую, без тасок)
            success = await self.executor.execute_entry(
                symbol=phemex_symbol,
                pos_key=pos_key,
                signal=signal
            )
            
            if success:
                # Вход завершен успешно. Добавляем в блэклист в фоне
                asyncio.create_task(self.bl_manager.update_and_save([phemex_symbol]))
            else:
                logger.warning(f"[{pos_key}] Оркестратор: Вход не удался. Монета {phemex_symbol} НЕ добавлена в BlackList.")

        except Exception as e:
            logger.error(f"[SKIP] [{phemex_symbol}] Критическая ошибка входа (execute_entry_from_signal): {e}")
    
    # Новый метод в TradingBot:
    async def _on_upbit_signal(self, symbol: str, received_ms: int = 0) -> None:
        """Входящий сигнал от Upbit: Прямой выстрел в движок БЕЗ создания тасок."""
        try:
            self.stop_another_request = True 
            
            # Прямой await пробивает Event Loop до самой отправки ордера без переключений контекста
            success = await self.signal_engine.handle_upbit_signal(
                raw_symbol=symbol,
                side="LONG",
                st_stream=self.st_stream,
                price_manager=self.price_manager,
                symbol_specs=self.symbol_specs,
                black_list=self.bl_manager,
                received_ms=received_ms
            )
            
            # Если сигнал отбракован - мгновенно снимаем лок монитора
            if not success:
                self.stop_another_request = False
            else:
                # Запускаем разблокировку монитора в фоне ТОЛЬКО если вошли (пауза 5 секунд)
                async def release_block():
                    await asyncio.sleep(UPBIT_SIGNAL_RELEASE_PAUSE_SEC)
                    self.stop_another_request = False
                asyncio.create_task(release_block())
        except Exception as e:
            logger.error(f"[CRITICAL] Ошибка в _on_upbit_signal для {symbol}: {e}\n{traceback.format_exc()}")
            self.stop_another_request = False
        
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
                        logger.error(f"[{pos_key}] [ERR] Ошибка Grid TP: Нет спецификаций (tick_size/lot_size) для {pos.symbol}!")
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
                        logger.error(f"[{pos_key}] [ERR] Ошибка расчета Grid TP (возвращен пустой список ордеров).")
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
        logger.info("[LOOP] Главная торговая живолупа (Game Loop) запущена.")
        while self._is_running:
            # 1. ОБРАБОТКА ТЕКУЩИХ ПОЗИЦИЙ
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
                                logger.debug(f"[{pos_key}] [DEL] Удаление фантомной позиции (no WS fill).")
                                self.state.active_positions.pop(pos_key, None)
                                self.active_positions_locker.pop(pos_key, None)
                                asyncio.create_task(self.state.save())
                                continue

                        if getattr(pos, 'last_notified_tp_progress', 0) < getattr(pos, 'tp_progress', 0):
                            new_count = pos.tp_progress
                            if self.tg:
                                msg = f"[HIT] <b>[{pos.symbol}]</b> Тейк-профит (уровень {new_count}) исполнен!\nЗафиксирована часть прибыли."
                                asyncio.create_task(self.tg.send_message(msg))
                            pos.last_notified_tp_progress = new_count
                            asyncio.create_task(self.state.save())

                        if getattr(pos, 'is_closed_by_exchange', False):
                            if pos.entry_price > 0.0:
                                
                                # --- РАСЧЕТ ИСТИННОЙ ЦЕНЫ ВЫХОДА (VWAP) ---
                                if getattr(pos, 'total_exit_qty', 0.0) > 0:
                                    exit_pr = pos.total_exit_usd / pos.total_exit_qty
                                else:
                                    # Фолбэк, если биржа не прислала объем
                                    exit_pr = pos.exit_price_hint or pos.avg_price
                                # ------------------------------------------
                                
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

                                current_status = pos.exit_status if pos.exit_status in ("STOP_LOSS", "TTL_CLOSE") else pos.last_exit_status
                                
                                if current_status == "STOP_LOSS": 
                                    semantic = "[WARN] Стоп-лосс (Аварийный выход)"
                                elif current_status == "TTL_CLOSE": 
                                    semantic = "[PROT] Выход по таймауту (TTL Market)"                                
                                else: 
                                    semantic = "[HIT] Тейк-профит" if is_win else "[DOWN] Убыток (Ручное/Неизвестно)"

                                if is_win:
                                    self.state.consecutive_fails[pos.symbol] = 0

                                if self.tg:
                                    msg = Reporters.exit_success(pos_key, semantic, exit_pr, net_pnl, emoji)
                                    msg += f"\n⏳ Время в сделке: {format_duration(duration_sec)}"
                                    asyncio.create_task(self.tg.send_message(msg))
                                    
                                logger.info(f"[{pos_key}] [HALT] Позиция закрыта. {emoji} PnL: {net_pnl:.4f}$ | Средняя цена выхода: {exit_pr:.4f} | Время: {format_duration(duration_sec)}")

                            # Снимаем все возможные ордера-призраки (лимитки тейк-профитов) перед удалением позиции
                            asyncio.create_task(self.executor.cancel_all_orders(pos.symbol))

                            self.state.active_positions.pop(pos_key, None)
                            self.active_positions_locker.pop(pos_key, None) 
                            self.stop_another_request = False # Мгновенное высвобождение монитора
                            asyncio.create_task(self.state.save())

                    except Exception as e:
                        logger.error(f"[{pos_key}] [ERR] Критическая ошибка при обработке позиции в Game Loop: {e}\n{traceback.format_exc()}")

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
            
            await asyncio.sleep(MAIN_LOOP_SLEEP_SEC)

    async def _on_ws_subscribe(self):
        """Срабатывает при первом подключении и каждом успешном реконнекте WS"""
        logger.info("[WS] Подписан на приватный канал. Запуск синхронизации стейта (Recover)...")
        await self._recover_state()
        self.ws_ready = True

    async def _refresh_stakan_stream(self):
        """Умный перезапуск стрима стаканов на актуальном наборе символов."""
        # 1. Глушим старый стрим
        if getattr(self, 'st_stream', None):
            self.st_stream.stop()
            
        await self._await_task(self._stakan_task)
        
        symbols_to_stream = list(self.symbol_specs.keys())
        if not symbols_to_stream: return
        
        logger.info(f"[WS] Инициализация стрима стаканов для {len(symbols_to_stream)} пар...")
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

    async def _specs_updater_loop(self):
        """Фоновый воркер для обновления спецификаций монет (тиков, лотов) и стакана."""
        while self._is_running:
            # Сначала спим, так как при старте бота мы уже скачали актуальный список
            await asyncio.sleep(SPECS_UPDATE_INTERVAL_SEC)
            
            try:
                # Получаем только активные монеты с биржи
                specs = await self.phemex_sym_api.get_all(quote=self.quota_asset, only_active=True)
                if not specs:
                    continue

                # Формируем новый словарь строго в UPPERCASE и без блэклиста
                new_specs = {
                    s.symbol.upper().strip(): s 
                    for s in specs 
                    if s and s.symbol.upper().strip() not in self.black_list
                }
                
                # Сравниваем старые и новые ключи (сеты)
                old_symbols = set(self.symbol_specs.keys())
                current_symbols = set(new_specs.keys())
                
                added = current_symbols - old_symbols
                removed = old_symbols - current_symbols
                
                # Обновляем глобальный кэш спецификаций
                self.symbol_specs.update(new_specs)

                # --- ИСПРАВЛЕНИЕ: Удаляем пропавшие монеты из памяти ---
                for sym in removed:
                    self.symbol_specs.pop(sym, None)
                # --------------------------------------------------------
                
                # Если добавились новые монеты — настраиваем их и перезапускаем стакан
                if added:
                    logger.info(f"🔄 [UPDATE] На бирже появились новые монеты ({len(added)} шт): {', '.join(added)}")
                    
                    # Используем обновленный GlobalLeverageSetter для хирургической настройки
                    try:
                        from CORE.lvg_setter import GlobalLeverageSetter
                        
                        lev_cfg = self.cfg.get("risk", {}).get("leverage", {})
                        
                        setter = GlobalLeverageSetter(
                            api_key=os.getenv("API_KEY") or self.cfg.get("credentials", {}).get("api_key", ""),
                            api_secret=os.getenv("API_SECRET") or self.cfg.get("credentials", {}).get("api_secret", ""),
                            leverage_val=lev_cfg.get("val", 10),
                            margin_mode=lev_cfg.get("margin_mode", 1),
                            black_list=list(self.black_list),
                            use_cache=True, # ФОРСИРУЕМ КЭШ: чтобы настроить только новинки (added)
                            cache_path=Path(__file__).resolve().parent.parent / "leverage_cache.json",
                            delay_sec=lev_cfg.get("delay_sec", 0.25)
                        )
                        # Запускаем настройку. Благодаря кэшу он настроит только те, что еще не были настроены (added)
                        await setter.apply()
                        
                    except Exception as lvg_err:
                        logger.error(f"[UPDATE] Ошибка при настройке плеч через GlobalSetter: {lvg_err}")

                    # 2. Перезапускаем WS стакана
                    logger.info("Перезапуск WS стакана для захвата новых листингов...")
                    await self._refresh_stakan_stream()
                    
                    # Обновляем якоря в мониторе Upbit, чтобы он знал о новой монете
                    if self._upbit_monitor:
                        anchors = set(s.replace("USDT", "") for s in self.symbol_specs.keys())
                        if hasattr(self._upbit_monitor, 'update_anchors'):
                            self._upbit_monitor.update_anchors(anchors)
                    
                elif removed:
                    # Если монета ушла на тех. обслуживание, стакан переоткрывать не нужно, 
                    # WS Phemex сам перестанет слать по ней эвенты.
                    logger.debug(f"ℹ️ [UPDATE] С биржи временно пропало {len(removed)} монет. WS продолжает работу.")
                    
            except Exception as e:
                logger.error(f"[WARN] Ошибка обновления спецификаций в фоне: {e}")

    async def _wait_for_systems_ready(self) -> bool:
        """Ожидает, пока все фоновые системы (цены, фандинг, стаканы) не получат первые данные."""
        logger.info(f"[WAIT] Ожидание готовности систем (Timeout: {READY_CHECK_TIMEOUT}s)...")
        start_time = time.time()
        
        # ФОРСИРОВАННЫЙ ПРОГРЕВ: Делаем один прямой запрос цен через REST, чтобы не ждать WS
        try:
            ticker_api = PhemexTickerAPI()
            tickers = await ticker_api.get_all_tickers()
            for sym, t_data in tickers.items():
                self.price_manager.phemex_prices[sym] = t_data.price
            logger.info(f"[FAST] Пре-фетч цен завершен. Получено {len(tickers)} котировок.")
            await ticker_api.aclose()
        except Exception as e:
            logger.warning(f"[WARN] Не удалось выполнить пре-фетч цен: {e}")

        await asyncio.sleep(READY_CHECK_INITIAL_SLEEP)
        
        while time.time() - start_time < READY_CHECK_TIMEOUT:
            # 1. Проверяем кэш цен (наполняется из стакана)
            stakan_ready = False
            if self.st_stream and self.st_stream.last_msg_ts > 0:
                stakan_ready = True
            
            # 2. Проверяем кэш цен из REST
            prices_ready = len(self.price_manager.phemex_prices) > 0
                
            # 3. Проверяем приватный вебсокет
            ws_ready = getattr(self, 'ws_ready', False)
                
            if stakan_ready and prices_ready and ws_ready:
                logger.info(f"[OK] Системы готовы за {time.time() - start_time:.1f}с. Данные получены.")
                return True
            
            logger.info(f"   - Ожидание данных: Стаканы [{'OK' if stakan_ready else 'WAIT'}], Цены [{'OK' if prices_ready else 'WAIT'}]")
            await asyncio.sleep(READY_CHECK_INTERVAL)
            
        return False
    
    async def _send_developer_report(self, is_test: bool = False):
        """Формирует и отправляет аудит-отчет."""
        target_tg = self.report_tg or self.tg # Если нет отдельного чата, бьем в основной
        if not target_tg: return
        try:
            summary = self.tracker.get_summary_text()
            prefix = "🧪 <b>ТЕСТОВЫЙ ОТЧЕТ</b>\n" if is_test else "📅 <b>ПЕРИОДИЧЕСКИЙ ОТЧЕТ</b>\n"
            await target_tg.send_message(prefix + summary)
            
            if os.path.exists(self.state.filepath):
                await target_tg.send_document(self.state.filepath, caption="📄 Текущий стейт бота (bot_state.json)")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки отчета разработчику: {e}")

    async def _periodic_report_loop(self):
        """Фоновый луп для отправки отчетов каждые N часов."""
        logger.info(f"📊 Цикл отчетов запущен (каждые {REPORT_INTERVAL_HOURS}ч)")
        while getattr(self, '_is_running', False):
            try:
                await asyncio.sleep(REPORT_INTERVAL_HOURS * 3600)
                if getattr(self, '_is_running', False):
                    await self._send_developer_report()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in report loop: {e}")
                await asyncio.sleep(60)

    async def start(self):
        if getattr(self, '_is_running', False): return
        self._is_running = True
        logger.info("[RUN] Инициализация систем...")
        summary = get_config_summary(self.cfg)
        logger.info(f"[CFG] БОТ ЗАПУЩЕН С НАСТРОЙКАМИ\n{summary}")
        if self.tg: asyncio.create_task(self.tg.send_message("[START] <b>ТОРГОВЛЯ НАЧАТА</b>"))

        # СТАЛО (Жесткий контроль регистра):
        symbols_info = await self.phemex_sym_api.get_all(quote=self.bl_manager.quota_asset, only_active=True)
        self.symbol_specs = {
            s.symbol.upper().strip(): s 
            for s in symbols_info 
            if s and s.symbol.upper().strip() not in self.black_list
        }

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
                    logger.info(f"[USD] Стартовый баланс успешно загружен из стейта: {saved_start_balance:.2f} {self.quota_asset}")
                else:
                    logger.info("[REST] Запрашиваем Equity с биржи для инициализации аудита...")
                    usd_balance = await self.private_client.get_equity(self.quota_asset)
                    
                    self.tracker.set_initial_balance(usd_balance)
                    logger.info(f"[USD] Стартовый баланс зафиксирован: {usd_balance:.2f} {self.quota_asset}")
                    await self.state.save()
                    
        except Exception as e:
            logger.error(f"[ERR] Не удалось получить/установить стартовый баланс: {e}")
        # ==========================================

        logger.info("[WS] Прогрев кэша цен и фандинга...")
        await self.price_manager.warmup()

        # ==========================================
        # ПРЕ-ФЛАЙТ ВАЛИДАЦИЯ ЛИМИТОВ
        # ==========================================
        if self._is_validate_notional_limit:
            if not await self._validate_notional_limit():
                logger.error("[HALT] Бот остановлен из-за ошибки в расчете лимитов риска.")
                # Мягко гасим всё, что успели запустить выше, но не убиваем ТГ-бота
                await self.stop() 
                return
            # ==========================================

        self._price_updater_task = asyncio.create_task(self.price_manager.loop())
        
        await asyncio.sleep(START_UP_WARMUP_SEC)

        self._private_ws_task = asyncio.create_task(
            self.private_ws.run(
                self.ws_handler.process_phemex_message,
                on_subscribe=self._on_ws_subscribe
            )
        )

        self._game_loop_task = asyncio.create_task(self._main_trading_loop())
        self._specs_updater_task = asyncio.create_task(self._specs_updater_loop())

        # --- ЖЕСТКАЯ ПРОВЕРКА ГОТОВНОСТИ ---
        if not await self._wait_for_systems_ready():
            msg = f"[ERR] КРИТИЧЕСКАЯ ОШИБКА: Системы не инициализированы (нет данных) за {READY_CHECK_TIMEOUT} сек!"
            logger.error(msg)
            if self.tg: await self.tg.send_message(f"[ALARM] {msg}\nБот остановлен.")
            
            # Мягко гасим всё, что успели запустить, но не убиваем процесс
            await self.stop()
            return

        # Сигналы Upbit запускаем В ПОСЛЕДНЮЮ ОЧЕРЕДЬ, когда всё остальное уже шуршит
        if self._upbit_monitor:
            # Пробрасываем текущие якоря (символы Phemex без USDT) для улучшения парсинга
            anchors = set(s.replace("USDT", "") for s in self.symbol_specs.keys())
            if hasattr(self._upbit_monitor, 'update_anchors'):
                self._upbit_monitor.update_anchors(anchors)
            
            self._upbit_task = asyncio.create_task(self._upbit_monitor.run())

        # --- ДОБАВИТЬ ЭТОТ БЛОК В КОНЕЦ МЕТОДА start ---
        if self.report_tg or self.tg:
            self._report_task = asyncio.create_task(self._periodic_report_loop())
            asyncio.create_task(self._send_developer_report(is_test=True))

    async def aclose(self):
        await self.stop()
        
        logger.info("[SAVE] Финальное сохранение стейта на диск...")
        try:
            await self.state.save()
        except Exception as e:
            logger.error(f"[SHUT] Ошибка сохранения стейта: {e}")
            
        try:
            if hasattr(self, 'phemex_session') and self.phemex_session:
                await self.phemex_session.close()
        except Exception as e:
            logger.debug(f"[SHUT] Ошибка закрытия сессии Phemex: {e}")
            
        if self.tg: 
            try:
                await self.tg.aclose()
            except Exception as e:
                logger.debug(f"[SHUT] Ошибка закрытия сессии TG: {e}")
        
        if getattr(self, 'report_tg', None):
            try:
                await self.report_tg.aclose()
            except Exception as e:
                logger.debug(f"[SHUT] Ошибка закрытия сессии Report TG: {e}")

    async def stop(self):
        if not getattr(self, '_is_running', False): return
        self._is_running = False
        
        if getattr(self, 'st_stream', None):
            self.st_stream.stop()
            
        logger.info("[SHUT] Остановка процессов...")
        if self.tg: 
            try:
                await self.tg.send_message("[SHUT] Остановка процессов...")
            except: pass
            
        if hasattr(self, 'price_manager'):
            self.price_manager.stop()
            
        try:
            if hasattr(self, 'private_ws') and self.private_ws:
                await self.private_ws.aclose()
        except Exception as e:
            logger.debug(f"[SHUT] Ошибка остановки Private WS: {e}")

        for task_attr in ['_price_updater_task', '_private_ws_task', '_game_loop_task', '_upbit_task', '_stakan_task', '_specs_updater_task', '_report_task']:
            await self._await_task(getattr(self, task_attr, None))

        if getattr(self, '_upbit_monitor', None) and hasattr(self._upbit_monitor, 'aclose'):
            try:
                await self._upbit_monitor.aclose()
            except Exception as e:
                logger.debug(f"[SHUT] Ошибка остановки Upbit Monitor: {e}")

        self._processing.clear()
        self._signal_timeouts.clear()