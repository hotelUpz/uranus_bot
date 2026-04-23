# ============================================================
# FILE: ENTRY/signal_engine.py
# ROLE: Быстрое формирование EntrySignal из стакана
# ============================================================
from __future__ import annotations

import asyncio
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
        
    async def create_signal(self, symbol: str, side: str, st_stream: Any, price_manager: Any) -> Optional[EntrySignal]:
        """
        Пытается получить лучшие цены из WS стакана. 
        Если стакан пуст, фолбечится на цены из REST-кэша.
        """
        bids, asks = [], []
        
        # 1. Пытаемся поймать WS стакан (максимально быстрый поллинг)
        if st_stream:
            for _ in range(30):
                bids, asks = st_stream.get_depth(symbol)
                if bids and asks:
                    break
                await asyncio.sleep(0.001) # Ультракороткая пауза (1мс)

        # 2. Оценка результатов и ФОЛБЕК
        if not bids or not asks:
            # WS не успел или отвалился, берем цену из PriceCacheManager
            phemex_price, _ = price_manager.get_prices(symbol)
            
            if phemex_price <= 0:
                return None  # Цен нет нигде, вход невозможен
                
            ask1 = bid1 = phemex_price
            mid_price = phemex_price
        else:
            ask1, bid1 = asks[0][0], bids[0][0]
            mid_price = (ask1 + bid1) / 2.0
        
        # 3. Формирование цены входа
        entry_price = ask1 if side == "LONG" else bid1
        
        return EntrySignal(
            side=side,
            price=entry_price,
            init_ask1=ask1,
            init_bid1=bid1,
            mid_price=mid_price
        )