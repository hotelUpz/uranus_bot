# ============================================================
# FILE: ENTRY/signal_engine.py
# ROLE: Полный обработчик и форматер сигналов (Signal Sniper Engine)
# ============================================================
from __future__ import annotations

import asyncio
import time
from typing import Optional, Literal, Any, Dict, Tuple
from dataclasses import dataclass
from c_log import UnifiedLogger

logger = UnifiedLogger("signal")

# Константы для пауз и таймингов
STAKAN_POLLING_STEP_SEC    = 0.001 # Шаг поллинга стакана (1мс)
STAKAN_POLLING_MAX_ATTEMPTS = 30    # Макс. кол-во попыток (итого 30мс)
FETCH_SPECS_RETRY_PAUSE_SEC = 0.5   # Пауза при ошибке фетча спеков

@dataclass
class EntrySignal:
    symbol: str
    side: Literal["LONG", "SHORT"]
    price: float
    init_ask1: float
    init_bid1: float
    mid_price: float
    b_price: Optional[float] = None
    p_price: Optional[float] = None
    spread: Optional[float] = None
    timestamp: float = 0.0

class SignalEngine:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.signal_queue = asyncio.Queue()
        self._processing = set() # Чтобы не обрабатывать один и тот же символ дважды в моменте

    async def handle_upbit_signal(
        self, 
        raw_symbol: str, 
        side: Literal["LONG", "SHORT"],
        st_stream: Any,
        price_manager: Any,
        symbol_specs: Dict[str, Any],
        black_list: Any,
        phemex_sym_api: Any,
    ):
        """
        Основной входной путь для сигнала от Upbit.
        Превращает сырой тикер в полноценный EntrySignal и кладет в очередь.
        """
        if raw_symbol in self._processing: return
        self._processing.add(raw_symbol)
        
        try:
            # 1. Конвертация в Phemex символ
            phemex_symbol = f"{raw_symbol}USDT"
            
            # 2. Быстрая проверка черного списка
            if await black_list.is_blacklisted(phemex_symbol):
                return

            # 3. Запуск фоновых задач (Подписка WS + Спецификации)
            # Мы НЕ ЖДЕМ их завершения прямо сейчас, чтобы как можно быстрее начать поллинг стакана.
            sub_task = None
            if st_stream:
                sub_task = asyncio.create_task(st_stream.add_symbols([phemex_symbol]))
            
            spec_task = None
            if phemex_symbol not in symbol_specs:
                spec_task = asyncio.create_task(self._fetch_and_update_specs(phemex_symbol, phemex_sym_api, symbol_specs))

            # 4. ФОРМИРОВАНИЕ СИГНАЛА (Ожидание стакана)
            # Это самая важная часть. Поллинг стакана идет параллельно с сетевым запросом спецификаций.
            signal = await self.create_signal(phemex_symbol, side, st_stream, price_manager)
            
            if signal:
                signal.timestamp = time.time()
                
                # Дожидаемся спецификаций, если они еще не прилетели. 
                # Без них Orchestrator не сможет посчитать объем лота.
                if spec_task:
                    await spec_task
                
                await self.signal_queue.put(signal)
            
        except Exception as e:
            logger.error(f"❌ SignalEngine Critical Error for {raw_symbol}: {e}")
        finally:
            self._processing.discard(raw_symbol)

    async def _fetch_and_update_specs(self, symbol: str, api: Any, symbol_specs: Dict[str, Any]):
        """Быстро дотягивает спецификации для новой монеты."""
        try:
            # Используем curl_cffi версию api.get_all
            all_specs = await api.get_all()
            for s in all_specs:
                symbol_specs[s.symbol] = s
        except Exception as e:
            logger.error(f"⚠️ Error fetching specs for {symbol}: {e}")
            await asyncio.sleep(FETCH_SPECS_RETRY_PAUSE_SEC)

    async def create_signal(self, symbol: str, side: str, st_stream: Any, price_manager: Any) -> Optional[EntrySignal]:
        """
        Пытается получить лучшие цены из WS стакана. 
        Если стакан пуст, фолбечится на цены из REST-кэша.
        """
        bids, asks = [], []
        
        # 1. Пытаемся поймать WS стакан (максимально быстрый поллинг)
        if st_stream:
            for _ in range(STAKAN_POLLING_MAX_ATTEMPTS):
                bids, asks = st_stream.get_depth(symbol)
                if bids and asks:
                    break
                await asyncio.sleep(STAKAN_POLLING_STEP_SEC)

        # 2. Оценка результатов и ФОЛБЕК
        ask1, bid1 = None, None

        if not bids or not asks:
            # WS не успел или отвалился, берем цену из PriceCacheManager
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