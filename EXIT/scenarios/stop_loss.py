# ============================================================
# FILE: EXIT/scenarios/stop_loss.py
# ROLE: Stop-loss trigger: fixed % / breakeven / trailing stop
# ============================================================

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition


class StopLossScenario:
    """
    Проверяет, нужно ли закрыть позицию по stop-loss или trailing-stop.
    """

    def __init__(self, cfg: dict) -> None:
        self._enabled: bool = bool(cfg.get("enable", False))
        self._trailing: bool = bool(cfg.get("trailing", True)) 
        self._pct: float = float(cfg.get("percent", 5.0)) / 100.0
        self._ttl_sec: float = float(cfg.get("ttl_sec", 0.0))
        self._stabilization_sec: float = float(cfg.get("stabilization_sec", 0.0))

    def is_triggered(self, pos: "ActivePosition", current_price: float, now: float) -> bool:
        if not self._enabled or current_price <= 0:
            return False

        if now - pos.opened_at < self._stabilization_sec:
            return False

        condition_met = False

        # ПУЛЕНЕПРОБИВАЕМЫЙ ПОИСК ИНДЕКСА:
        # Ищем фактический максимальный индекс среди всех реально исполненных целей
        filled_indices = [
            info.get("idx", 0) 
            for info in pos.tp_orders.values() 
            if info.get("status") == "FILLED" and isinstance(info.get("idx"), int)
        ]
        max_filled_idx = max(filled_indices) if filled_indices else 0

        # Если трейлинг включен И есть хотя бы одна исполненная цель
        if self._trailing and max_filled_idx > 0:
            # === ТРЕЙЛИНГ СТОП (отключает базовый стоп-лосс) ===
            if max_filled_idx == 1:
                # Исполнилась только 1-я цель -> стоп в безубыток
                sl_price = pos.entry_price
            else:
                # Исполнилась 2-я (или выше) цель -> стоп на место (max_filled_idx - 1)
                target_idx = max_filled_idx - 1
                
                target_price = pos.entry_price # Fallback на безубыток (страховка)
                for info in pos.tp_orders.values():
                    if info.get("idx") == target_idx:
                        target_price = info.get("price", pos.entry_price)
                        break
                
                sl_price = target_price

            # Триггер виртуального трейлинг-стопа
            if pos.side == "LONG":
                condition_met = current_price <= sl_price
            else:
                condition_met = current_price >= sl_price

        else:
            # === БАЗОВЫЙ СТОП-ЛОСС (из конфига) ===
            # Работает пока нет ни одного "FILLED" ордера
            if pos.side == "LONG":
                condition_met = current_price <= pos.entry_price * (1.0 - self._pct)
            else:
                condition_met = current_price >= pos.entry_price * (1.0 + self._pct)

        # Обработка таймера TTL (защита от сквизов)
        if condition_met:
            if self._ttl_sec <= 0:
                return True
            
            if getattr(pos, 'sl_trigger_time', 0.0) == 0.0:
                pos.sl_trigger_time = now
                
            if now - pos.sl_trigger_time >= self._ttl_sec:
                return True
        else:
            pos.sl_trigger_time = 0.0
            
        return False