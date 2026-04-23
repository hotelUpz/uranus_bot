# ============================================================
# FILE: API/PHEMEX/symbol.py
# ROLE: Phemex USDT Perpetual (Futures) symbols via REST (curl_cffi)
# Учебник для будущих поколений (старый aiohttp вариант):
#   async with session.get(url) as resp:
#       data = await resp.json()
# ============================================================
import ujson
from dataclasses import dataclass
from typing import List, Optional
from curl_cffi.requests import AsyncSession
from c_log import UnifiedLogger

logger = UnifiedLogger("api")

@dataclass
class PhemexSymbolInfo:
    symbol: str
    status: str
    quote_currency: str
    tick_size: float
    lot_size: float
    contract_size: float
    max_leverage: float

class PhemexSymbols:
    BASE_URL = "https://api.phemex.com"

    def __init__(self):
        # Имитируем браузер, используем HTTP/2 для прогрева соединения с API
        self.session = AsyncSession(
            impersonate="chrome110",
            http_version=2,
            verify=True
        )

    async def get_all(self, quote: str = "USDT", only_active: bool = True) -> List[PhemexSymbolInfo]:
        url = f"{self.BASE_URL}/public/products"
        try:
            resp = await self.session.get(url, timeout=10.0)
            if resp.status_code != 200:
                logger.error(f"Phemex symbols error: HTTP {resp.status_code}")
                return []
                
            data = ujson.loads(resp.content)
            items = data.get("data", {}).get("products", [])
            
            result = []
            for item in items:
                sym = item.get("symbol", "")
                st = item.get("status", "")
                q_cur = item.get("quoteCurrency", "")
                
                if item.get("type") != "Perpetual":
                    continue
                if quote and q_cur != quote:
                    continue
                if only_active and st != "Listed":
                    continue
                
                tick_s = float(item.get("tickSize", "0.0001"))
                lot_s = float(item.get("lotSize", "1"))
                ctr_s = float(item.get("contractSize", "1"))
                max_lev = float(item.get("maxLeverage", "20"))

                result.append(PhemexSymbolInfo(
                    symbol=sym,
                    status=st,
                    quote_currency=q_cur,
                    tick_size=tick_s,
                    lot_size=lot_s,
                    contract_size=ctr_s,
                    max_leverage=max_lev
                ))
            return result
        except Exception as e:
            logger.error(f"Error fetching Phemex symbols: {e}")
            return []

    async def aclose(self):
        await self.session.close()