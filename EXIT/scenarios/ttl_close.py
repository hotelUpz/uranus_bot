# ============================================================
# FILE: EXIT/scenarios/ttl_close.py
# ROLE: TTL (time-to-live) market close trigger
# ============================================================

from __future__ import annotations
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition


class TtlCloseScenario:
    """
    Проверяет, превышено ли время жизни позиции.

    Если (now - opened_at) >= ttl_sec → позиция закрывается по маркету.
    """

    def __init__(self, cfg: dict) -> None:
        self._enabled: bool = bool(cfg.get("enable", False))
        self._ttl_sec: float = float(cfg.get("ttl_sec", 3600))

    def is_triggered(self, pos: "ActivePosition", now: float) -> bool:
        if not self._enabled:
            return False
        return (now - pos.opened_at) >= self._ttl_sec
