# ============================================================
# FILE: ENTRY/signal_engine.py
# ROLE: Полный обработчик и форматер сигналов (Signal Sniper Engine)
# ============================================================
from __future__ import annotations

import time
from typing import Optional, Literal, Any, Dict, Callable, Awaitable, TYPE_CHECKING
from dataclasses import dataclass
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from API.PHEMEX.stakan import PhemexStakanStream
    from CORE._utils import PriceCacheManager, BlackListManager
    from API.PHEMEX.symbol import PhemexSymbols

logger = UnifiedLogger("signal")

@dataclass
class EntrySignal:
    symbol: str
    side: Literal["LONG", "SHORT"]
    price: float  # Сюда теперь всегда ложится mid_price
    init_ask1: float
    init_bid1: float
    mid_price: float
    timestamp: float = 0.0      # Время анонса (Publication Time)
    detected_at: float = 0.0    # Время обнаружения ботом (Detection Time)

class SignalEngine:
    def __init__(self, cfg: Dict[str, Any], on_signal_callback: Callable[[EntrySignal], Awaitable[None]]):
        self.cfg = cfg
        self.on_signal_callback = on_signal_callback
        self._processing = set()

    async def handle_upbit_signal(
        self, 
        raw_symbol: str, 
        side: Literal["LONG", "SHORT"],
        st_stream: Optional[PhemexStakanStream],
        price_manager: PriceCacheManager,
        symbol_specs: Dict[str, Any],
        black_list: BlackListManager,
        received_ms: int = 0
    ) -> bool:
        
        if raw_symbol in self._processing: 
            # Не спамим сильно, но логируем блокировку дубля
            logger.debug(f"[SKIP] [{raw_symbol}] Отказ: Сигнал уже находится в обработке (_processing).")
            return False
            
        self._processing.add(raw_symbol)
        
        try:
            raw_symbol_upper = raw_symbol.upper().strip()
            phemex_symbol = raw_symbol_upper if raw_symbol_upper.endswith("USDT") else f"{raw_symbol_upper}USDT"

            if black_list.is_blacklisted_sync(phemex_symbol):
                logger.warning(f"[SKIP] [{phemex_symbol}] Отказ: Монета находится в BlackList.")
                return False

            if phemex_symbol not in symbol_specs:
                logger.warning(f"[SKIP] [{phemex_symbol}] Отказ: Спецификации отсутствуют (монеты еще нет на Phemex).")
                return False

            signal = self.create_signal_instant(phemex_symbol, side, st_stream, price_manager)
            
            if signal:
                signal.timestamp = received_ms / 1000.0 if received_ms > 0 else time.time()
                signal.detected_at = time.time()
                await self.on_signal_callback(signal)
                return True
            else:
                return False
            
        except Exception as e:
            logger.error(f"❌ SignalEngine Critical Error for {raw_symbol}: {e}")
            return False
        finally:
            self._processing.discard(raw_symbol)

    def create_signal_instant(
        self, 
        symbol: str, 
        side: str, 
        st_stream: Optional[PhemexStakanStream], 
        price_manager: PriceCacheManager
    ) -> Optional[EntrySignal]:
        bids, asks = [], []
        if st_stream:
            bids, asks = st_stream.get_depth(symbol)

        if not bids or not asks:
            phemex_price, _ = price_manager.get_prices(symbol)
            if phemex_price <= 0:
                logger.error(
                    f"🚨 [СНАЙПЕР-СТОП] [SKIP] [{symbol}] Цены нет в стакане и фоновом REST-кэше! "
                    f"Свежий листинг без сформированной ликвидности. Отказ от входа."
                )
                return None  
            ask1 = bid1 = mid_price = phemex_price
        else:
            ask1, bid1 = asks[0][0], bids[0][0]
            mid_price = (ask1 + bid1) / 2.0
        
        # ФИКС: Используем mid_price для расчета объемов, а не голый спред
        return EntrySignal(
            symbol=symbol,
            side=side,
            price=mid_price, # <--- Точный mid_price ложится в основу расчета объема
            init_ask1=ask1,
            init_bid1=bid1,
            mid_price=mid_price
        )