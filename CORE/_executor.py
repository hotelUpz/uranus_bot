# ============================================================
# FILE: CORE/executor.py
# ROLE: Шаблоны отправки ордеров на биржу, защита от гонок потоков...
# ============================================================
from __future__ import annotations

import asyncio
import time
import pytz
from typing import Dict, Any, TYPE_CHECKING, Optional
from c_log import UnifiedLogger
from decimal import Decimal, ROUND_DOWN
from consts import TIME_ZONE
from CORE._utils import Reporters

if TYPE_CHECKING:
    from CORE.orchestrator import TradingBot
    from ENTRY.signal_engine import EntrySignal

from CORE.models_fsm import ActivePosition
from dotenv import load_dotenv    
load_dotenv()

logger = UnifiedLogger(name="bot")
TZ = pytz.timezone(TIME_ZONE)

WAIT_ENTRY_TIMEOUT_SEC = 2.0
WAIT_ENTRY_MIN_WAIT_SEC = 0.05
SMART_WAIT_POLL_STEP_SEC = 0.01   # Шаг проверки налива (10мс)
GRID_TP_BASE_SLEEP_SEC = 0.1      # Базовый слип между ордерами сетки
GRID_TP_RETRY_SLEEP_SEC = 1.0     # Слип при ошибке 11011
MARKET_EXIT_RETRY_SLEEP_SEC = 0.5 # Слип между попытками выхода по маркету

def round_step(value: float, step: float) -> float:
    if not step or step <= 0:
        return value
    val_d = Decimal(str(value))
    step_d = Decimal(str(step))
    rounded = (val_d / step_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_d
    return float(rounded)

class OrderExecutor:
    """
    Прокладка между оркестратором и сетевым адаптером. Содержит шаблоны рутинных операций перед финальной отправкой запросов.
    """
    def __init__(self, tb: "TradingBot"):
        self.tb = tb
        self.client = tb.private_client
        self.cfg = tb.cfg
        
        entry_cfg = self.cfg["entry"]
        self.max_entry_retries = entry_cfg["max_place_order_retries"]
        self.entry_mode = entry_cfg["mode"].lower()
        self.limit_dist_pct = float(entry_cfg["limit_distance_pct"])
        
        # Параметры выхода
        self.max_exit_retries = self.cfg["exit"]["max_place_order_retries"]
        
        # Блок риска: если Notional или Margin не заданы, падает сразу
        risk = self.cfg["risk"]
        self.notional_limit = float(risk["notional_limit"])

    async def _smart_wait(self, pos_key: str, initial_qty: float, timeout_sec: float, min_wait_sec: float) -> None:
        """
        Безопасная утилита умного поллинга. 
        Ждет минимальное время, а затем каждые 10мс проверяет изменение объема позиции.
        Если объем изменился (или позиция закрылась) — досрочно вырывается из петли.
        """
        start_ts = time.time()
        
        # 1. Обязательный минимальный таймаут (чтобы ордер успел встать в стакан)
        if min_wait_sec > 0:
            await asyncio.sleep(min_wait_sec)
            
        # 2. Петля поллинга
        poll_step = SMART_WAIT_POLL_STEP_SEC
        while time.time() - start_ts < timeout_sec:
            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                
                # Если позиции уже нет (успела закрыться) — выходим
                if not pos:
                    break
                
                # Если текущий объем перестал быть равен стартовому (налили частично или полностью) — выходим
                if getattr(pos, 'current_qty', 0.0) != initial_qty:
                    break
                    
            await asyncio.sleep(poll_step)

    async def cancel_all_orders(self, symbol: str) -> bool:
        try:
            resp = await self.client.cancel_all_orders(symbol)
            return resp.get("code") == 0
        except: return False

    async def execute_cancel(self, symbol: str, pos_side: str, order_id: str) -> bool:
        """
        Роль: некая промежуточная прокладка между сетевым адаптером и инициирующей стороной.
        Ошибку неудачи отмены логируем.
        """
        if not order_id: return False
        try:
            resp = await self.client.cancel_order(symbol, order_id, pos_side)
            if resp.get("code") == 0: return True
            
            err_msg = str(resp.get("msg", "")).lower()
            if "filled" not in err_msg and "not found" not in err_msg:
                logger.debug(f"[{symbol}] Отказ отмены {order_id}: {resp}")
            return False
        except Exception as e:
            logger.debug(f"[{symbol}] Исключение отмены {order_id}: {e}")
            return False

    async def execute_entry(self, symbol: str, pos_key: str, signal: EntrySignal) -> bool:
        """
        ВХОД (Market или Aggressive Limit) с ожиданием WS и отменой остатка.
        """
        try:
            spec = self.tb.symbol_specs.get(symbol)
            if not spec:
                err_msg = f"🚨 <b>[{pos_key}]</b> Ошибка входа: Нет спецификаций для монеты!"
                logger.error(err_msg)
                if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(err_msg))
                return False

            ref_price = signal.price
            if not ref_price or ref_price <= 0:
                return False

            qty = round_step(self.notional_limit / ref_price, spec.lot_size)
            if qty < spec.lot_size:
                logger.warning(f"[{pos_key}] Qty {qty} меньше минимального лота {spec.lot_size}. Пропуск.")
                return False

            phemex_pos_side = "Long" if signal.side == "LONG" else "Short"
            side = "Buy" if signal.side == "LONG" else "Sell"

            initial_qty = 0.0
            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                if not pos:
                    pos = ActivePosition(symbol=symbol, side=signal.side)
                    self.tb.state.active_positions[pos_key] = pos
                
                initial_qty = getattr(pos, 'current_qty', 0.0)
                pos.pending_qty = qty
                pos.in_pending = True

            # 2. Цикл отправки с ретраями
            for attempt in range(max(1, self.max_entry_retries)):
                try:
                    is_limit_used = False
                    
                    if self.entry_mode == "limit":
                        # АГРЕССИВНАЯ ЛИМИТКА
                        limit_pr = ref_price * (1 + self.limit_dist_pct / 100.0) if side == "Buy" else ref_price * (1 - self.limit_dist_pct / 100.0)
                        limit_pr = round_step(limit_pr, spec.tick_size)
                        
                        resp = await self.client.place_limit_order(
                            symbol, side, qty, limit_pr, phemex_pos_side
                        )
                        is_limit_used = True
                    else:
                        # ЧИСТЫЙ МАРКЕТ
                        resp = await self.client.place_market_order(symbol, side, qty, phemex_pos_side)

                    # 3. Обработка успешного ответа API
                    if resp.get("code") == 0:
                        order_id = resp.get("data", {}).get("orderID")
                        
                        # Умное ожидание физического налива по вебсокету
                        await self._smart_wait(pos_key, initial_qty, WAIT_ENTRY_TIMEOUT_SEC, WAIT_ENTRY_MIN_WAIT_SEC)
                        filled_ts = time.time()

                        # 4. Отмена недолитых остатков (только для лимиток)
                        if order_id and is_limit_used:
                            curr_qty = 0.0
                            async with self.tb._get_lock(pos_key):
                                pos = self.tb.state.active_positions.get(pos_key)
                                if pos: curr_qty = getattr(pos, 'current_qty', 0.0)
                                
                            if curr_qty < qty:
                                logger.info(f"[{pos_key}] Исполнено {curr_qty} из {qty}. Отменяю остаток (лимит)...")
                                asyncio.create_task(self.execute_cancel(symbol, phemex_pos_side, order_id))

                        # 5. Итоговая проверка и отчет
                        async with self.tb._get_lock(pos_key):
                            pos = self.tb.state.active_positions.get(pos_key)
                            if pos and (pos.current_qty > 0 or getattr(pos, "in_position", False)):
                                pos.in_pending = False
                                entry_usd_vol = pos.current_qty * ref_price

                                # Замер полной латенции: сигнал → налив WS
                                signal_ts = getattr(signal, 'timestamp', 0.0)
                                if signal_ts > 0:
                                    total_latency = filled_ts - signal_ts
                                    logger.info(f"[{pos_key}] ⏱ Латенция сигнал→налив: {total_latency:.3f}s")

                                logger.info(f"[{pos_key}] ✅ Вход выполнен. Объем: {pos.current_qty} (≈ {entry_usd_vol:.2f} $)")
                                
                                if self.tb.tg:
                                    latency_str = f"{total_latency:.3f}s" if signal_ts > 0 else "N/A"
                                    msg = Reporters.entry_signal(symbol, signal, signal.init_bid1, signal.init_ask1)
                                    msg += f"\nНалив за: <b>{latency_str}</b>"
                                    asyncio.create_task(self.tb.tg.send_message(msg))
                                return True
                            
                            if pos:
                                pos.in_pending = False
                                
                        err_msg = f"🚨 <b>[{pos_key}]</b> Ордер принят, но налива по WS не последовало за {WAIT_ENTRY_TIMEOUT_SEC}с!"
                        logger.warning(err_msg)
                        if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(err_msg))
                        return False
                        
                    else:
                        err_msg = f"🚨 <b>[{pos_key}]</b> Отказ API при входе (попытка {attempt+1}): {resp}"
                        logger.warning(err_msg)
                        if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(err_msg))
                        
                except Exception as e:
                    err_msg = f"🚨 <b>[{pos_key}]</b> Исключение при выполнении входа: {e}"
                    logger.error(err_msg)
                    if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(err_msg))
                
                if self.max_entry_retries > 1: await asyncio.sleep(0.3) 

            # Если все попытки исчерпаны
            err_msg = f"🚨 <b>[{pos_key}]</b> FATAL: Не удалось войти в позицию после {self.max_entry_retries} попыток."
            logger.error(err_msg)
            if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(err_msg))
            return False

        except Exception as e:
            logger.error(f"[{pos_key}] Глобальная ошибка execute_entry: {e}")
            return False
        
    async def execute_tp_grid(self, symbol: str, pos_key: str, grid_orders: list) -> bool:
        """
        Расставляет сетку тейк-профитов последовательно с инкрементальной паузой
        между ордерами во избежание рейт-лимитов API.
        Слип между ордерами: 0.1s + 0.1s * idx (база 0.1, потом +0.1 с каждым ордером).
        """
        async with self.tb._get_lock(pos_key):
            pos = self.tb.state.active_positions.get(pos_key)
            if not pos or not getattr(pos, 'in_position', False):
                return False
            phemex_pos_side = "Long" if pos.side == "LONG" else "Short"
            side = "Sell" if pos.side == "LONG" else "Buy"

        # Защита от дублей при краше бота во время инициализации сетки
        await self.cancel_all_orders(symbol)
        
        results = []
        for idx, order in enumerate(grid_orders):
            # Слип между ордерами
            await asyncio.sleep(GRID_TP_BASE_SLEEP_SEC + GRID_TP_BASE_SLEEP_SEC * idx)
            
            # Попытки для каждого ордера сетки (важно при TE_REDUCE_ONLY_ABORT)
            for tp_attempt in range(3):
                try:
                    resp = await self.client.place_limit_order(
                        symbol, side, order["qty"], order["price"], phemex_pos_side, reduce_only=True
                    )
                    results.append((idx, resp, None))
                    break # Успех
                except Exception as e:
                    err_msg = str(e).lower()
                    if "11011" in err_msg or "reduce_only" in err_msg:
                        if tp_attempt < 2:
                            logger.debug(f"[{pos_key}] TP #{idx+1} [11011] retry {tp_attempt+1}...")
                            await asyncio.sleep(GRID_TP_RETRY_SLEEP_SEC)
                            continue
                    results.append((idx, None, e))
                    break

        success_count = 0
        async with self.tb._get_lock(pos_key):
            pos = self.tb.state.active_positions.get(pos_key)
            if not pos:
                return False

            for idx, resp, err in results:
                order_info = grid_orders[idx]
                if err:
                    # 👇 АЛЕРТ: Исключение (чаще всего TE_REDUCE_ONLY_ABORT или таймаут)
                    err_msg = f"🚨 <b>[{pos_key}]</b> Ошибка выставления TP #{order_info['idx']}: {err}"
                    logger.error(err_msg)
                    if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(err_msg))
                    continue
                    
                if resp and resp.get("code") == 0:
                    order_id = resp.get("data", {}).get("orderID")
                    if order_id:
                        pos.tp_orders[order_id] = {
                            "idx": order_info["idx"],
                            "price": order_info["price"],
                            "qty": order_info["qty"],
                            "status": "NEW"
                        }
                        success_count += 1
                else:
                    # 👇 АЛЕРТ: Биржа вернула код ошибки
                    err_msg = f"🚨 <b>[{pos_key}]</b> Отказ API при выставлении TP #{order_info['idx']}: {resp}"
                    logger.warning(err_msg)
                    if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(err_msg))

            if success_count > 0:
                pos.tp_grid_initiated = True
                logger.info(f"[{pos_key}] 🕸 Сетка TP успешно выставлена: {success_count}/{len(grid_orders)} ордеров.")
                return True
            else:
                pos.tp_grid_initiated = False
                # 👇 АЛЕРТ: Полный провал выставления сетки
                err_msg = f"🚨 <b>[{pos_key}]</b> КРИТИЧЕСКАЯ ОШИБКА: Не удалось выставить ни одного ордера сетки TP!"
                logger.error(err_msg)
                if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(err_msg))
                return False

    async def execute_exit(self, symbol: str, pos_key: str, order_price: float = 0.0, timeout_sec: float = 0.0) -> bool:
        """
        УНИВЕРСАЛЬНЫЙ ВЫХОД ПО МАРКЕТУ (Stop-Loss, TTL и т.д.).
        1. Снимает все активные ордера (в т.ч. сетку TP), чтобы освободить маржу.
        2. Бьет по стакану маркет-ордером на весь объем.
        """
        try:
            spec = self.tb.symbol_specs.get(symbol)
            if not spec: return False

            # 1. Отменяем ВСЕ лимитки (защита от гонок с TP)
            await self.cancel_all_orders(symbol)

            async with self.tb._get_lock(pos_key):
                pos = self.tb.state.active_positions.get(pos_key)
                if not pos or not getattr(pos, 'in_position', False) or pos.current_qty <= 0: 
                    return False
                qty = pos.current_qty
                pos_side_raw = pos.side
                
                # Очищаем стейт лимиток и старых ордеров
                pos.tp_orders.clear()
                pos.close_order_id = ""
                
                # Сохраняем намерение цены выхода для статистики, если передано
                if order_price > 0:
                    pos.exit_price_hint = order_price

            target_qty = round_step(qty, spec.lot_size)
            if target_qty < spec.lot_size: return False

            # Строгий Hedge Mode (никакого Merged)
            phemex_pos_side = "Long" if pos_side_raw == "LONG" else "Short"
            side = "Sell" if pos_side_raw == "LONG" else "Buy"

            # 2. Швыряем МАРКЕТ
            for attempt in range(max(1, self.max_exit_retries)):
                try:
                    resp = await self.client.place_market_order(symbol, side, target_qty, phemex_pos_side, reduce_only=True)
                    if resp.get("code") == 0:
                        logger.info(f"[{pos_key}] 🏁 Выход выполнен ПО МАРКЕТУ. Объем: {target_qty}")
                        return True
                    else:
                        # 👇 АЛЕРТ: Биржа не дает закрыть позицию!
                        err_msg = f"🚨 <b>[{pos_key}]</b> Ошибка выхода API (попытка {attempt+1}): {resp}"
                        logger.warning(err_msg)
                        if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(err_msg))
                        
                except Exception as e:
                    # 👇 АЛЕРТ: Отвал сети в момент закрытия
                    err_msg = f"🚨 <b>[{pos_key}]</b> Исключение при выходе по маркету: {e}"
                    logger.error(err_msg)
                    if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(err_msg))
                
                await asyncio.sleep(MARKET_EXIT_RETRY_SLEEP_SEC)
                
            # 👇 АЛЕРТ: Все попытки исчерпаны, позиция зависла
            err_msg = f"🚨 <b>[{pos_key}]</b> FATAL ERROR: Не удалось закрыть позицию по маркету после {self.max_exit_retries} попыток! СРОЧНО ПРОВЕРЬТЕ ТЕРМИНАЛ!"
            logger.error(err_msg)
            if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(err_msg))
            return False
            
        except Exception as e:
            err_msg = f"🚨 <b>[{pos_key}]</b> Глобальная ошибка execute_exit: {e}"
            logger.error(err_msg)
            if self.tb.tg: asyncio.create_task(self.tb.tg.send_message(err_msg))
            return False

    async def execute_market_exit(self, symbol: str, pos_key: str) -> bool:
        """Алиас для обратной совместимости с оркестратором."""
        return await self.execute_exit(symbol, pos_key)
