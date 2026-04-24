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
        phemex_sym_api: Any = None,
        received_ms: int = 0
    ) -> bool:
        """
        Основной входной путь для сигнала от Upbit.
        Возвращает True если сигнал прошел проверки и запущен в обработку, иначе False.
        """
        if raw_symbol in self._processing: return False
        self._processing.add(raw_symbol)
        
        try:
            raw_symbol_upper = raw_symbol.upper().strip()
            phemex_symbol = raw_symbol_upper if raw_symbol_upper.endswith("USDT") else f"{raw_symbol_upper}USDT"

            # 1. Проверка черного списка (в памяти)
            bl_result = black_list.is_blacklisted_sync(phemex_symbol)
            # logger.debug(f"[BL-CHECK] symbol={phemex_symbol} | blacklisted={bl_result} | bl.symbols={getattr(black_list, 'symbols', '???')}")
            if bl_result:
                logger.warning(f"[{phemex_symbol}] Монета в BlackList. Отказ от входа.")
                return False

            # 2. Подписка WS (в фоне, не ждем)
            if st_stream:
                asyncio.create_task(st_stream.add_symbols([phemex_symbol]))
            
            # 3. Проверка наличия спецификаций в памяти
            if phemex_symbol not in symbol_specs:
                logger.warning(f"[{phemex_symbol}] Спецификации отсутствуют. Отказ от входа.")
                return False

            # 4. МГНОВЕННОЕ получение цены из кэша (без циклов и ожиданий)
            signal = self.create_signal_instant(phemex_symbol, side, st_stream, price_manager)
            
            if signal:
                # Стартовая точка: момент получения сигнала от Upbit-монитора.
                # Если received_ms не передан (напр. из тестов), fallback на текущий момент.
                signal.timestamp = received_ms / 1000.0 if received_ms > 0 else time.time()
                # ПРЯМОЙ ПРОСТРЕЛ
                asyncio.create_task(self.on_signal_callback(signal))
                return True
            else:
                logger.warning(f"[{phemex_symbol}] Не удалось получить цены из кэша. Отказ от входа.")
                return False
            
        except Exception as e:
            logger.error(f"❌ SignalEngine Critical Error for {raw_symbol}: {e}")
            return False
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