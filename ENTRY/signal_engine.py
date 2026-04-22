# ============================================================
# FILE: ENTRY/signal_engine.py
# ROLE: Формирование EntrySignal на основе цен Phemex
# ============================================================
from __future__ import annotations

import time
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ENTRY.funding_manager import FundingManager
    from ENTRY.pattern_math import EntrySignal


class SignalEngine:
    """
    Сигнальный движок. 
    Раньше здесь была сложная математика паттернов. 
    Теперь — простая проверка фандинга и упаковка EntrySignal.
    """
    def __init__(self, cfg: Dict[str, Any], funding_manager: 'FundingManager'):
        self.cfg = cfg
        self.funding_manager = funding_manager  
        
    def create_signal(self, symbol: str, side: str, bids: list, asks: list) -> Optional['EntrySignal']:
        """
        Создает EntrySignal на основе текущих цен в стакане.
        """
        from ENTRY.pattern_math import EntrySignal
        
        # 1. Проверка фандинга
        if not self.funding_manager.is_trade_allowed(symbol):
            return None
            
        if not bids or not asks:
            return None

        # 2. Берем лучшие цены
        ask1, bid1 = asks[0][0], bids[0][0]
        mid_price = (ask1 + bid1) / 2.0
        
        # Для LONG входим по ask1, для SHORT по bid1
        entry_price = ask1 if side == "LONG" else bid1
        
        return EntrySignal(
            side=side, # type: ignore
            price=entry_price,
            init_ask1=ask1,
            init_bid1=bid1,
            mid_price=mid_price
        )