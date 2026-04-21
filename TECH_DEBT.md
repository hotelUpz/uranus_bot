# Uranus Bot 1.0

## IMPROVE

1. Чекаем и исправляем коллаборацию сигнальной логики. парс, обработка сигнала и сам вход должны происходить максимально быстро. (прокси я добавлю, планирую парс анонсов апбит раз в 1 секунду)

2. Строим новую модель тейк профита. Я на распутье. То ли юзать старую систему связанную с ловлей объемов в стакане (только ее нужно будет существенно пределать), то ли тупо кидать лимитные ордера. если второй вариант, то можно использовать следующий подход:

import asyncio
import time
from typing import *
from b_context import BotContext
from API.MX.mx import MexcClient
from c_log import ErrorHandler, log_time
from c_utils import Utils, to_human_digit, calc_next_sl, sleep_generator
from .valide import OrderValidator


class TPControl:
    def __init__(
        self,
        context: BotContext,
        info_handler: ErrorHandler,
        mx_client: MexcClient,
        preform_message: Callable,
        utils: Utils,
        direction: str,
        tp_control_frequency: float,
        chat_id: str
    ):
        info_handler.wrap_foreign_methods(self)
        self.info_handler = info_handler

        self.context = context
        self.mx_client = mx_client
        self.preform_message = preform_message
        self.contracts_template = utils.contracts_template
        self.tp_control_frequency = tp_control_frequency
        self.direction = direction   
        self.chat_id = chat_id
        # //        

    async def tp_factory(
            self,
            symbol: str,
            symbol_data: dict,
            sign: int,
            debug_label: str = "",
            debug: bool = True
        ):
        """
        Фабрика лимиток на закрытие позиции.s
        """
        pos_data = symbol_data[self.direction]

        # // round values
        spec = symbol_data.get("spec", {})
        price_precision = spec.get("price_precision")
        vol_unit = spec.get("vol_unit")
        contract_precision = spec.get("contract_precision")

        leverage = pos_data.get("leverage")
        total_contracts = pos_data.get("contracts")

        # cur_entry = await self.mx_client.get_fair_price(symbol)

        while not pos_data.get("entry_price"):
            await asyncio.sleep(0.15)

        cur_entry = pos_data.get("entry_price")

        if not cur_entry:
            print(f"[ERROR][{debug_label}]: не удалось получить цену для выставления тейк-профитов. {log_time()}")
            return
        
        remaining_contracts = total_contracts
        for idx, (indent, volume) in enumerate(self.context.users_configs[self.chat_id]["config"]["fin_settings"]["tp_levels_gen"], start=1):
            try:
                success = False
                reason = "N/A"
                cur_time = int(time.time() * 1000)
                # // set contracts:   
                if idx < len(self.context.users_configs[self.chat_id]["config"]["fin_settings"]["tp_levels_gen"]):
                    cur_contract = remaining_contracts * volume / 100
                else:
                    cur_contract = remaining_contracts

                # округляем вниз
                cur_contract = round(cur_contract / vol_unit) * vol_unit
                cur_contract = round(cur_contract, contract_precision)

                if cur_contract <= 0:
                    reason = f"Недостаточно контрактов для выставления лимитного ордера. {log_time()}"
                    print(reason)
                    continue

                remaining_contracts -= cur_contract

                # // set target price:
                target_price = cur_entry * (1 + (sign * indent / 100))
                target_price = to_human_digit(round(target_price, price_precision))

                # // set tp limit order:
                order_data = await self.mx_client.make_order(                  
                    symbol=symbol,
                    contract=cur_contract,
                    side="SELL",           # -- всегда для закрытия
                    position_side=self.direction,
                    leverage=leverage,
                    open_type=self.context.users_configs[self.chat_id]["config"]["fin_settings"].get("margin_mode", 2),
                    debug_price=target_price,
                    price=target_price,
                    stopLossPrice=None,
                    takeProfitPrice=None,
                    market_type="LIMIT",        
                    debug=False   # флаг дебага
                )
                
                # // validation and report:
                valid_resp = OrderValidator.validate_and_log(order_data, debug_label)
                order_id = None
                if isinstance(valid_resp, dict):
                    success = valid_resp.get("success", False)
                    cur_time = valid_resp.get("ts", int(time.time() * 1000))
                    order_id = valid_resp.get("order_id")
                    reason = valid_resp.get("reason", "N/A")

                # async with self.context.bloc_async:
                if success: 
                    pos_data["set_ids"].add(order_id)
                    store_data = pos_data["order_stream_data"]   
                    store_data.setdefault(order_id, {})     
                    store_data[order_id].update({
                        "idx": idx,
                        "price": target_price,
                    })
                    pos_data["tp_prices"].append(target_price)
                    # if debug: 
                    #     print(f"[{symbol}] TP лимитка #{idx} установлена. orderId={order_id}, price={target_price}")

            finally:
                if not success:
                    self.preform_message(
                        chat_id=self.chat_id,
                        marker=f"tp_order_failed",
                        body={"symbol": symbol, "reason": reason, "cur_time": cur_time},
                        is_print=True
                    )
                # === Pause ===
                sleep_pause = sleep_generator(tp_len=len(self.context.users_configs[self.chat_id]["config"]["fin_settings"]["tp_levels_gen"]))[idx-1]
                # print(f"#{idx}: sleep {sleep_pause:.2f} сек")
                await asyncio.sleep(sleep_pause)

    async def execute_sl_template(
            self,
            order_params: Dict,
            symbol: str,
            pos_data: Dict,
            sl_price: float,
            debug_label: str,
            debug: bool = True
        ):
        if not order_params:
            return

        order_resp = await self.mx_client.create_stop_loss_take_profit(**order_params)
        if isinstance(order_resp, Exception):
            self.info_handler.debug_error_notes(
                f"[ERROR] create_stop_loss_take_profit failed for {symbol}: {order_resp}", is_print=True
            )

        valid_resp = OrderValidator.validate_and_log(order_resp, debug_label)
        order_id = None
        if isinstance(valid_resp, dict):
            success = valid_resp.get("success", False)
            cur_time = valid_resp.get("ts", int(time.time() * 1000))
            order_id = valid_resp.get("order_id")
            reason = valid_resp.get("reason", "N/A")
            pos_data["sl_id"] = (order_id, sl_price)
            if debug: print(f"new sl_id: {order_id}")
            
        else:
            success = False
            cur_time = int(time.time() * 1000)
            reason = "N/A"

        if not success:
            self.preform_message(
                chat_id=self.chat_id,
                marker=f"sl_order_failed",
                body={"symbol": symbol, "reason": reason, "cur_time": cur_time},
                is_print=True
            )
        progress = pos_data["progress"]
        if progress is not None and progress > 0:
            self.preform_message(
                chat_id=self.chat_id,
                marker="progress",
                body={
                    "symbol": symbol,
                    "progress": pos_data["progress"],
                    "cur_sl": pos_data["sl_id"][1] if pos_data["sl_id"] else "N/A",
                    "cur_time": cur_time,
                },
                is_print=True
            )
        # if debug: print(f"[DEBUG][sl_control]: {self.context.position_vars}")

    def find_current_progress(self, pos_data: dict) -> tuple[str, int, float] | None:
        """
        Находит первый ордер для указанного символа, который уже частично или полностью исполнен.
        Игнорирует отменённые и недействительные ордера.
        Для LONG возвращает ордер с максимальной ценой, для SHORT — с минимальной.
        Возвращает (order_id, idx, price) или None, если подходящих ордеров нет.
        """
        orders = pos_data.get("order_stream_data", {})
        if not orders:
            # print("find_current_progress: no order_stream_data")
            return None

        valid_orders = {
            oid: data
            for oid, data in orders.items()
            if data.get("state") is not None and int(data["state"]) not in {2, 4} 
            and "price" in data and data["price"] is not None
        }

        if not valid_orders:
            return None

        # безопасное приведение цены к float
        if self.direction == "LONG":
            current_progress = max(
                valid_orders.items(),
                key=lambda x: float(x[1]["price"])
            )
        else:
            current_progress = min(
                valid_orders.items(),
                key=lambda x: float(x[1]["price"])
            )

        order_id, data = current_progress
        return order_id, data.get("idx"), data.get("price")

    async def sl_control(
        self,
        symbol: str,
        symbol_data: dict,
        sign: int,
        debug_label: str,
        debug: bool = True
    ) -> Optional[bool]:
        """
        Подготавливает данные для ордеров (tp/sl) и вызывает их исполнение.
        Возвращает True, если ордера были выставлены, иначе False.
        """
        pos_data = symbol_data[self.direction]   
        old_progress = pos_data["progress"]

        # --- Проверка: первый SL уже прошёл успешно? ---
        first_sl = not pos_data.get("sl_initiated", False)

        entry_price = pos_data.get("entry_price", 0.0)
        if not entry_price:
            if debug:
                print(f"[{debug_label}] entry_price отсутствует → мониторинг пропущен")
            return
        
        is_move_sl = False        
        if not first_sl:
            progress_data = self.find_current_progress(pos_data)
            if not progress_data:
                return None
            
            order_id, current_progress, price = progress_data
            is_move_sl = old_progress != current_progress
            # async with self.context.bloc_async:
            if is_move_sl: pos_data["progress"] = current_progress
            # print(f"current_progress: {current_progress}")

            if pos_data["progress"] >= len(self.context.users_configs[self.chat_id]["config"]["fin_settings"]["tp_levels_gen"]):
                pos_data["force_reset_flag"] = True
                print(f"[{debug_label}] все тейк-профит лимитки исполнены. {log_time()}")
                return True

        if not (is_move_sl or first_sl):
            return None

        if pos_data["progress"] > 0:
            # async with self.context.bloc_async:
            await self.mx_client.cancel_order_template(
                symbol=symbol,
                pos_data=pos_data,
                key_list=["sl"]
            )

        price_precision = symbol_data.get("spec", {}).get("price_precision")
        sl_price = calc_next_sl(
            entry_price = pos_data.get("entry_price"),
            progress=pos_data.get("progress"),
            base_sl=self.context.users_configs[self.chat_id]["config"]["fin_settings"].get("sl"),
            sl_type=self.context.users_configs[self.chat_id]["config"]["fin_settings"].get("sl_type"),
            tp_prices=pos_data.get("tp_prices"),
            sign=sign,
            price_precision=price_precision
        )
    
        if sl_price is not None:
            move_sl_task = {
                "symbol": symbol,
                "position_side": self.direction,
                "leverage": pos_data.get("leverage"),
                "open_type": self.context.users_configs[self.chat_id]["config"]["fin_settings"].get("margin_mode", 2),
                "close_order_type": "sl",
                "order_type": 1,
                "contract": pos_data.get("contracts"),
                "price": sl_price,
            }
            await self.execute_sl_template(
                order_params=move_sl_task,
                symbol=symbol,
                pos_data=pos_data,
                sl_price=sl_price,
                debug_label=debug_label,
                debug=False
            )

            # ✅ отметка, что первый TP реально поставлен
            if first_sl: pos_data["sl_initiated"] = True   
        
        return False
                    
    async def tp_orchestrator(
        self,
        symbol: str,
        symbol_data: dict,
        sign: int, 
        debug_label: str
    ):
        pos_data = symbol_data.get(self.direction, {})
        if not pos_data.get("in_position"):
            return False
        
        if pos_data["preexisting"]:
            pos_data["tp_initiated"] = True
            pos_data["sl_initiated"] = True

        if pos_data["tp_initiated"] and pos_data["sl_initiated"] and not pos_data.get("in_position"):
            return True

        if not pos_data.get("tp_initiated", False):
            await self.tp_factory(
                symbol=symbol,
                symbol_data=symbol_data,
                sign=sign,
                debug_label=debug_label,
                debug=True
            )            
            pos_data["tp_initiated"] = True

        if self.context.users_configs[self.chat_id]["config"]["fin_settings"].get("sl") is not None:
            return await self.sl_control(
                symbol=symbol,                
                symbol_data=symbol_data,
                sign=sign,
                debug_label=debug_label,
                debug=True
            )

        return False

    async def tp_control_flow(self, symbol: str, symbol_data: Dict,
                              sign: int, debug_label: str) -> None:

        while not self.context.stop_bot and not self.context.stop_bot_iteration:
            await asyncio.sleep(self.tp_control_frequency)
            if await self.tp_orchestrator(
                symbol=symbol,
                symbol_data=symbol_data,
                sign=sign,
                debug_label=debug_label
            ):  
                print(f"[DEBUG]:задача tp_control_flow завершена. {log_time()}")
                return

(это из некоего старого кода для другой биржи, но идеи можешь почерпнуть). И конечно в этой чистой воды утилиты не должно быть сетевых вызовов. В текущем виде это антипаттерн.


Смотри суть:

у нас будет карта тейк-профитов:

PHEMEX_TP_MAP = {
    (0, 500): {                             
        "levels":  [5, 12, 25, 50, 80],   
        "volumes": [20, 20, 20, 20, 20],  
    },
    (500, 1000): {                          
        "levels":  [4, 8, 15, 25, 40],
        "volumes": [20, 20, 20, 20, 20],
    },
    (1000, float("+inf")): {                
        "levels":  [3, 10, 20, 35, 55],
        "volumes": [20, 20, 20, 20, 20],
    },
}

И нужно эту карту последовательно отработать.

Текущее состояние файла тейк-профита (это легаси из другой торговой системы):
# ============================================================
# FILE: EXIT/scenarios/base.py
# ============================================================
from __future__ import annotations
from typing import TYPE_CHECKING
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition
    from API.PHEMEX.stakan import DepthTop

logger = UnifiedLogger("base")

class BaseScenario:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg["enable"] 
        self.stab_ttl = cfg["stabilization_ttl"]
        self.target_rate = cfg["target_rate"]
        self.shift_demotion = cfg["shift_demotion"]
        self.min_target_rate = cfg["min_target_rate"]
        self.shift_ttl = cfg["shift_ttl"]

    def _calc_virtual_tp(self, pos: ActivePosition) -> float:
        """Вычисляет виртуальный TP без сайд-эффектов."""
        if pos.side == "LONG":
            return pos.entry_price + (pos.base_target_price_100 - pos.entry_price) * pos.current_target_rate
        return pos.entry_price - (pos.entry_price - pos.base_target_price_100) * pos.current_target_rate

    def scen_base_analyze(self, depth: DepthTop, pos: ActivePosition, now: float) -> float | None:
        if not self.enable:
            return None   

        if pos.current_qty <= 0:
            return None

        if (now - pos.opened_at) < self.stab_ttl:
            return None

        if not pos.in_base_mode:
            pos.in_base_mode = True
            pos.last_shift_ts = now
            pos.current_target_rate = self.target_rate

        # Логика сдвига: просто упираемся в min_target_rate и стоим на нем
        time_since_shift = now - pos.last_shift_ts
        if time_since_shift >= self.shift_ttl and pos.current_target_rate > self.min_target_rate:
            old_rate = pos.current_target_rate
            pos.current_target_rate = max(self.min_target_rate, pos.current_target_rate - self.shift_demotion)
            pos.last_shift_ts = now
            logger.info(f"[{pos.symbol}] 📉 Base: СДВИГ ТП. Rate: {old_rate:.3f} → {pos.current_target_rate:.3f}")

        # 1. ВЫЧИСЛЕНИЕ ВИРТУАЛЬНОЙ ЦЕЛИ
        virtual_tp = self._calc_virtual_tp(pos)

        # 2. АКТИВНЫЙ ХАНТИНГ (Поиск уровня с максимальным объемом у/выше virtual_tp)
        ideal_target_price = None
        max_vol = -1.0

        if pos.side == "LONG":
            for price, vol in depth.bids:
                if price >= virtual_tp:
                    if vol > max_vol:
                        max_vol = vol
                        ideal_target_price = price
                # else:
                #     break
        else:
            for price, vol in depth.asks:
                if price <= virtual_tp:
                    if vol > max_vol:
                        max_vol = vol
                        ideal_target_price = price
                # else:
                #     break

        return ideal_target_price