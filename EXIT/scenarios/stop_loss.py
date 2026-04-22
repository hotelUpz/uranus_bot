# ============================================================
# FILE: EXIT/scenarios/stop_loss.py
# ROLE: Stop-loss trigger: fixed % или безубыток после первого TP
# ============================================================

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition


class StopLossScenario:
    """
    Проверяет, нужно ли закрыть позицию по stop-loss.

    Логика:
      - tp_progress == 0: SL на уровне entry_price * (1 ± percent/100)
      - tp_progress  > 0: SL переставляется в безубыток → entry_price
    """

    def __init__(self, cfg: dict) -> None:
        self._enabled: bool = bool(cfg.get("enable", False))
        self._pct: float = float(cfg.get("percent", 5.0)) / 100.0
        self._ttl_sec: float = float(cfg.get("ttl_sec", 0.0))

    def is_triggered(self, pos: "ActivePosition", current_price: float, now: float) -> bool:
        if not self._enabled or current_price <= 0:
            return False

        if pos.tp_progress > 0:
            # Безубыток
            sl_price = pos.entry_price
            if pos.side == "LONG":
                condition_met = current_price <= sl_price
            else:
                condition_met = current_price >= sl_price
        else:
            # Фиксированный SL
            if pos.side == "LONG":
                condition_met = current_price <= pos.entry_price * (1.0 - self._pct)
            else:
                condition_met = current_price >= pos.entry_price * (1.0 + self._pct)

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
