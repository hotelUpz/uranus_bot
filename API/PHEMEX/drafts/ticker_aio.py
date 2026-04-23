# ============================================================
# FILE: API/PHEMEX/drafts/ticker_aio.py (DRAFT - aiohttp version)
# ROLE: Old aiohttp implementation for future generations.
# ============================================================
import aiohttp
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

@dataclass
class TickerData:
    price: float
    volume_24h_usd: float

class PhemexTickerAPIAIO:
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
        session = await self._get_session()
        async with session.get(f"{self.BASE_URL}/md/v3/ticker/24hr/all") as resp:
            resp.raise_for_status()
            data = await resp.json()

        items = data.get("result", [])
        result: Dict[str, TickerData] = {}
        for item in items:
            sym = item.get("symbol")
            raw_price = item.get("lastRp") or item.get("lastPriceRp") or item.get("lastPrice")
            raw_volume = item.get("turnoverRv") or item.get("turnoverRp") or item.get("turnover24hRp") or "0"
            if not sym or raw_price is None: continue
            try:
                result[sym] = TickerData(price=float(raw_price), volume_24h_usd=float(raw_volume))
            except Exception: continue
        return result
