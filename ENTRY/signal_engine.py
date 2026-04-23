# ============================================================
# FILE: ENTRY/signal_engine.py
# ROLE: Быстрое формирование EntrySignal из стакана
# ============================================================
from __future__ import annotations

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

class SignalEngine:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        
    def create_signal(self, symbol: str, side: str, bids: list, asks: list) -> Optional[Any]:
        
        if not bids or not asks:
            return None

        ask1, bid1 = asks[0][0], bids[0][0]
        mid_price = (ask1 + bid1) / 2.0
        entry_price = ask1 if side == "LONG" else bid1
        
        return EntrySignal(
            side=side,
            price=entry_price,
            init_ask1=ask1,
            init_bid1=bid1,
            mid_price=mid_price
        )