# ============================================================
# FILE: CORE/models_fsm.py
# ROLE: FSM для ActivePosition. Интерпретатор событий WebSocket.
# ============================================================
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field, fields
from typing import Dict, Any, TYPE_CHECKING, Literal
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.restorator import BotState

ExitStatusType = Literal["NORMAL", "GRID_TP", "STOP_LOSS", "TTL_CLOSE"]

logger = UnifiedLogger("ws")

@dataclass
class ActivePosition:
    symbol: str             
    side: str               
    
    in_pending: bool = False             # 1. Ордер отправлен (Слот занят)
    in_position: bool = False            # 2. Позиция налита

    exit_status: ExitStatusType = "NORMAL"
    last_exit_status: ExitStatusType = "NORMAL"
    
    is_closed_by_exchange: bool = False  
    exit_in_flight: bool = False         # <-- НОВЫЙ ФЛАГ: Сетевой запрос выхода в процессе
    
    tp_grid_initiated: bool = False
    tp_orders: dict = field(default_factory=dict)
    tp_progress: int = 0
    last_notified_tp_progress: int = 0  # <-- ДОБАВЛЕНО: Для предотвращения спама в ТГ о тейках
    
    entry_price: float = 0.0             
    pending_price: float = 0.0           
    avg_price: float = 0.0                
    exit_price_hint: float = 0.0  # Цена, по которой мы ПОСЛЕДНИЙ РАЗ отправили ордер на выход, или исполнился закрывающий ордер

    # --- ДОБАВЛЕНО ДЛЯ РАСЧЕТА ИСТИННОГО ВЫХОДА (VWAP) ---
    total_exit_usd: float = 0.0   
    total_exit_qty: float = 0.0   
    # -----------------------------------------------------
    
    pending_qty: float = 0.0             
    current_qty: float = 0.0    
    closed_qty: float = 0.0 
    max_realized_qty: float = 0.0        
    
    mid_price: float = 0.0
    
    current_target_rate: float = 1.0     
    close_order_id: str = ""             
    
    opened_at: float = field(default_factory=time.time)
    marked_for_death_ts: float = 0.0
    sl_trigger_time: float = 0.0
    emergency_ttl_ts: float = 0.0       # <-- НОВОЕ: Принудительный дедлайн при ошибке ТП

    def to_dict(self) -> dict:
        return self.__dict__

    @classmethod
    def from_dict(cls, data: dict) -> "ActivePosition":
        valid_keys = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


class WsInterpreter:
    def __init__(self, state: BotState, active_positions_locker: Dict[str, asyncio.Lock]):
        self.state = state
        self._locks = active_positions_locker

    def _get_lock(self, pos_key: str) -> asyncio.Lock:
        if pos_key not in self._locks:
            self._locks[pos_key] = asyncio.Lock()
        return self._locks[pos_key]

    @staticmethod
    def _safe_float(val: Any, default: float = 0.0) -> float:
        try: return float(val) if val is not None else default
        except (ValueError, TypeError): return default

    async def process_phemex_message(self, event_data: Dict[str, Any]):
        orders = event_data.get("orders_p") or event_data.get("orders") or []
        positions = event_data.get("positions_p") or event_data.get("positions") or []
        
        for order in orders:
            await self._handle_order_update(order)
        for pos in positions:
            await self._handle_position_update(pos)

    async def _handle_order_update(self, o: Dict[str, Any]):
        symbol = o.get("symbol")
        if not symbol: return

        raw_pos_side = str(o.get("posSide", "")).upper()
        order_side = str(o.get("side", "")).lower()

        if raw_pos_side in ("NONE", ""):
            pos_side = "LONG" if order_side == "sell" else "SHORT"
        else:
            pos_side = raw_pos_side

        pos_key = f"{symbol}_{pos_side}"
        ord_status = str(o.get("ordStatus", "")).upper()
        exec_status = str(o.get("execStatus", "")).upper()

        async with self._get_lock(pos_key):
            pos: ActivePosition = self.state.active_positions.get(pos_key)
            if not pos: return

            is_closing_order = (pos.side == "LONG" and order_side == "sell") or \
                               (pos.side == "SHORT" and order_side == "buy")

            if ord_status in ("FILLED", "PARTIALLYFILLED") or "FILL" in exec_status:
                fill_price = self._safe_float(o.get("execPriceRp", 0.0))
                if fill_price <= 0:
                    fill_price = self._safe_float(o.get("priceRp", o.get("price", 0.0)))
                
                # --- ИЗВЛЕКАЕМ ОБЪЕМ ТЕКУЩЕЙ ЗАЛИВКИ ---
                exec_qty = self._safe_float(o.get("execQtyRp", o.get("execQty", o.get("lastExecQty", 0.0))))

                if is_closing_order and fill_price > 0:
                    pos.exit_price_hint = fill_price
                    # --- НАКАПЛИВАЕМ VWAP ---
                    if exec_qty > 0:
                        pos.total_exit_usd += (fill_price * exec_qty)
                        pos.total_exit_qty += exec_qty
                    # ------------------------
                elif not is_closing_order and fill_price > 0:
                    if pos.entry_price == 0.0:
                        pos.opened_at = time.time()
                        pos.entry_price = fill_price

                order_id = o.get("orderID")
                if order_id and order_id in pos.tp_orders and (ord_status == "FILLED" or "FILL" in exec_status):
                    if pos.tp_orders[order_id].get("status") != "FILLED":
                        pos.tp_orders[order_id]["status"] = "FILLED"
                        pos.tp_progress += 1
                        idx = pos.tp_orders[order_id].get("idx", "?")
                        logger.info(f"[{pos_key}] 🎯 Исполнен тейк-профит #{idx} по цене {fill_price} (Объем: {exec_qty})")

    async def _handle_position_update(self, p: Dict[str, Any]):
        symbol = p.get("symbol")
        raw_pos_side = str(p.get("posSide", "")).upper()
        
        if not symbol or raw_pos_side in ("NONE", ""): return

        pos_key = f"{symbol}_{raw_pos_side}"

        async with self._get_lock(pos_key):
            pos: ActivePosition = self.state.active_positions.get(pos_key)
            if not pos: return
            
            # 4. Парсер фиксирует только текущее количество ордера. Ему похеру на реджекты и канселы.
            if "size" in p or "sizeRq" in p:
                raw_size = self._safe_float(p.get("sizeRq", p.get("size")))
                size = abs(raw_size) 
                avg_price = self._safe_float(p.get("avgEntryPriceRp", p.get("avgEntryPrice")))

                if size > 0:
                    pos.current_qty = size

                    # --- ФИКСАЦИЯ ИСТИННОГО ОБЪЕМА ---
                    if size > pos.max_realized_qty:
                        pos.max_realized_qty = size
                    # ---------------------------------

                    # 3. При исполнении ставим флаг in_position = True а in_pending сразу в false.
                    pos.in_position = True
                    pos.in_pending = False
                    if avg_price > 0: pos.avg_price = avg_price
                else:
                    if pos.in_position:
                        pos.is_closed_by_exchange = True
                        pos.in_position = False
                        pos.closed_qty = pos.current_qty # Сохраняем объем перед обнулением
                    pos.current_qty = 0.0