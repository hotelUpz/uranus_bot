# ============================================================
# FILE: ENTRY/pattern_math.py
# ============================================================

from __future__ import annotations
import time
from typing import Optional, Literal, Any, Dict
from dataclasses import dataclass


@dataclass
class EntrySignal:
    side: Literal["LONG", "SHORT"]
    price: float
    init_ask1: float
    init_bid1: float
    mid_price: float
    b_price: Optional[float] = None
    p_price: Optional[float] = None
    spread: Optional[float] = None


class StakanDetector:
    """
    Фоновый детектор стакана по bid/ask спреду.
    Читает секцию cfg["entry"]["pattern"]["phemex"].
    """
    def __init__(self, phemex_cfg: dict[str, Any]):
        self.enabled: bool    = phemex_cfg.get("enable", True)
        self.depth: int       = phemex_cfg.get("depth", 5)
        self.max_spread: float = phemex_cfg.get("ask1_bid1_max_spread", 1.0)
        self.ttl: float       = phemex_cfg.get("ttl", 10.0)

        self._cache: Dict[str, float] = {}   # symbol → first_seen_ts
        self._valid: set[str] = set()

    def update(self, symbol: str,
               bids: list[tuple[float, float]],
               asks: list[tuple[float, float]]) -> None:
        if not self.enabled:
            return

        if not bids or not asks:
            self._evict(symbol)
            return

        ask1, bid1 = asks[0][0], bids[0][0]
        if ask1 <= 0:
            return

        spread_pct = abs(ask1 - bid1) / ask1 * 100
        now = time.time()

        if spread_pct <= self.max_spread:
            self._cache.setdefault(symbol, now)
            if now - self._cache[symbol] >= self.ttl:
                self._valid.add(symbol)
        else:
            self._evict(symbol)

    def is_valid(self, symbol: str) -> bool:
        if not self.enabled:
            return True
        if symbol in self._valid:
            return True
        # Динамическая проверка если WS задержал flush
        if symbol in self._cache:
            if time.time() - self._cache[symbol] >= self.ttl:
                self._valid.add(symbol)
                return True
        return False

    def _evict(self, symbol: str) -> None:
        self._cache.pop(symbol, None)
        self._valid.discard(symbol)