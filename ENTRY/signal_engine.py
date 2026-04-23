# ============================================================
# FILE: ENTRY/signal_engine.py
# ROLE: Полный обработчик и форматер сигналов (Signal Sniper Engine)
# ============================================================
from __future__ import annotations

import asyncio
import time
from typing import Optional, Literal, Any, Dict, Callable, Awaitable
from dataclasses import dataclass
from c_log import UnifiedLogger

logger = UnifiedLogger("signal")

@dataclass
class EntrySignal:
    symbol: str
    side: Literal["LONG", "SHORT"]
    price: float
    init_ask1: float
    init_bid1: float
    mid_price: float
    timestamp: float = 0.0

class SignalEngine:
    def __init__(self, cfg: Dict[str, Any], on_signal_callback: Callable[[EntrySignal], Awaitable[None]]):
        self.cfg = cfg
        self.on_signal_callback = on_signal_callback
        self._processing = set()

    async def handle_upbit_signal(
        self, 
        raw_symbol: str, 
        side: Literal["LONG", "SHORT"],
        st_stream: Any,
        price_manager: Any,
        symbol_specs: Dict[str, Any],
        black_list: Any,
    ):
        """
        Основной входной путь для сигнала от Upbit.
        """
        if raw_symbol in self._processing: return
        self._processing.add(raw_symbol)
        
        try:
            phemex_symbol = f"{raw_symbol}USDT"
            
            # 1. Проверка черного списка (в памяти)
            if black_list.is_blacklisted_sync(phemex_symbol):
                return

            # 2. Подписка WS (в фоне, не ждем)
            if st_stream:
                asyncio.create_task(st_stream.add_symbols([phemex_symbol]))
            
            # 3. Проверка наличия спецификаций в памяти
            if phemex_symbol not in symbol_specs:
                logger.warning(f"[{phemex_symbol}] Спецификации отсутствуют. Пропуск.")
                return

            # 4. МГНОВЕННОЕ получение цены из кэша (без циклов и ожиданий)
            signal = self.create_signal_instant(phemex_symbol, side, st_stream, price_manager)
            
            if signal:
                signal.timestamp = time.time()
                # ПРЯМОЙ ПРОСТРЕЛ
                asyncio.create_task(self.on_signal_callback(signal))
            
        except Exception as e:
            logger.error(f"❌ SignalEngine Critical Error for {raw_symbol}: {e}")
        finally:
            self._processing.discard(raw_symbol)

    def create_signal_instant(self, symbol: str, side: str, st_stream: Any, price_manager: Any) -> Optional[EntrySignal]:
        """
        Мгновенно вытаскивает цены из кэша. Если данных нет - возвращает None.
        """
        # 1. Пробуем стакан (если стрим готов)
        bids, asks = [], []
        if st_stream:
            bids, asks = st_stream.get_depth(symbol)

        # 2. Фолбэк на ценовой кэш
        if not bids or not asks:
            phemex_price, _ = price_manager.get_prices(symbol)
            if phemex_price <= 0:
                return None
            ask1 = bid1 = phemex_price
            mid_price = phemex_price
        else:
            ask1, bid1 = asks[0][0], bids[0][0]
            mid_price = (ask1 + bid1) / 2.0
        
        # 3. Формирование цены входа
        entry_price = ask1 if side == "LONG" else bid1
        
        return EntrySignal(
            symbol=symbol,
            side=side,
            price=entry_price,
            init_ask1=ask1,
            init_bid1=bid1,
            mid_price=mid_price
        )