# ============================================================
# FILE: API/PHEMEX/ticker.py
# ROLE: Phemex 24h ticker snapshot — price + USD turnover volume
# ============================================================
import aiohttp
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

@dataclass
class TickerData:
    price: float
    volume_24h_usd: float  # turnoverRp — оборот в USD за 24ч


class PhemexTickerAPI:
    BASE_URL = "https://api.phemex.com"

    def __init__(self, timeout_sec: float = 10.0):
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(timeout=self._timeout, connector=connector)
        return self._session

    async def aclose(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_all_tickers(self) -> Dict[str, TickerData]:
        """Цены + объёмы (USD) по всем символам Phemex."""
        session = await self._get_session()
        async with session.get(f"{self.BASE_URL}/md/v3/ticker/24hr/all") as resp:
            resp.raise_for_status()
            data = await resp.json()

        items = data.get("result", [])
        if not isinstance(items, list):
            return {}

        result: Dict[str, TickerData] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            sym = item.get("symbol")
            raw_price = item.get("lastRp") or item.get("lastPriceRp") or item.get("lastPrice")
            raw_volume = item.get("turnoverRv") or item.get("turnoverRp") or item.get("turnover24hRp") or "0"
            if not sym or raw_price is None:
                continue
            try:
                price = float(raw_price)
                volume = float(raw_volume)
                if price > 0:
                    result[sym] = TickerData(price=price, volume_24h_usd=volume)
            except (ValueError, TypeError):
                continue

        return result

    async def get_all_prices(self) -> Dict[str, float]:
        """Обратная совместимость — только цены."""
        tickers = await self.get_all_tickers()
        return {sym: t.price for sym, t in tickers.items()}


if __name__ == "__main__":
    import asyncio

    CHECK = ["SOONUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    TOP_N = 20

    async def _test():
        api = PhemexTickerAPI(timeout_sec=15.0)
        try:
            print("\n📡 Phemex ticker test...\n")
            tickers = await api.get_all_tickers()
            print(f"Total symbols received: {len(tickers)}\n")

            print(f"{'Symbol':<18} {'Price':>14} {'Volume 24h USD':>20}")
            print("-" * 55)
            for sym in CHECK:
                t = tickers.get(sym)
                if t:
                    print(f"{sym:<18} {t.price:>14,.6f} {t.volume_24h_usd:>20,.0f}")
                else:
                    print(f"{sym:<18} {'N/A':>14} {'N/A':>20}")

            print(f"\n--- Top-{TOP_N} by 24h volume ---")
            print(f"{'#':<4} {'Symbol':<20} {'Volume USD':>20}")
            print("-" * 46)
            ranked = sorted(tickers.items(), key=lambda x: x[1].volume_24h_usd, reverse=True)
            for i, (sym, t) in enumerate(ranked[:TOP_N], 1):
                print(f"{i:<4} {sym:<20} {t.volume_24h_usd:>20,.0f}")
        finally:
            await api.aclose()

    asyncio.run(_test())