# ============================================================
# python -m API.PHEMEX.symbol
# ROLE: Phemex USDT Perpetual (Futures) symbols via REST (curl_cffi)
# ============================================================
import ujson
import asyncio
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
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
    min_qty: float

class PhemexSymbols:
    BASE_URL = "https://api.phemex.com"

    def __init__(self, session: Optional[AsyncSession] = None):
        self.session = session or AsyncSession(
            impersonate="chrome120",
            http_version=2,
            verify=True
        )

    def _is_active_status(self, status: str) -> bool:
        s = str(status or "").strip().lower()
        if not s: return True
        banned = ("delist", "suspend", "pause", "settle", "close", "expired")
        return not any(word in s for word in banned)

    def _to_float(self, v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except (ValueError, TypeError):
            return default

    def _parse_perp(self, obj: Dict[str, Any], quote: str = "USDT") -> Optional[PhemexSymbolInfo]:
        sym = str(obj.get("symbol", "")).upper()
        if not sym: return None
        
        # 1. Фильтруем спот (начинается с 'S')
        if sym.startswith("S"): return None

        # 2. Фильтруем "мусорные" множители (из примера)
        if sym.startswith("U1000") or sym.startswith("U100") or sym.startswith("U10000"):
            return None

        # 3. Проверка Quota Asset (должен заканчиваться на USDT или другой заданный ассет)
        quote_upper = quote.upper()
        if not sym.endswith(quote_upper):
            return None

        # 4. Проверка валют в полях quoteCurrency/settleCurrency
        q_cur = obj.get("quoteCurrency")
        s_cur = obj.get("settleCurrency")
        q = str(q_cur or s_cur or "").upper()
        
        if q != quote_upper: 
            return None

        status = str(obj.get("status") or obj.get("state") or "Listed")
        
        # Извлекаем параметры из правильных полей согласно черновику
        tick_size = self._to_float(obj.get("tickSize"))
        lot_size = self._to_float(obj.get("qtyStepSize"))
        # Пытаемся вытянуть min_qty из разных полей (V1/V2 API)
        min_qty = self._to_float(obj.get("minOrderQty") or obj.get("minQty") or obj.get("minOrderQtyRq") or lot_size)
        contract_size = self._to_float(obj.get("contractSize"), 1.0)
        max_lvg = self._to_float(obj.get("limitOrderMaxLeverage") or obj.get("maxLeverage"), 20.0)

        return PhemexSymbolInfo(
            symbol=sym,
            status=status,
            quote_currency=q,
            tick_size=tick_size,
            lot_size=lot_size,
            min_qty=min_qty,
            contract_size=contract_size,
            max_leverage=max_lvg
        )

    async def get_all(self, quote: str = "USDT", only_active: bool = True) -> List[PhemexSymbolInfo]:
        url = f"{self.BASE_URL}/public/products"
        try:
            resp = await self.session.get(url, timeout=15.0)
            if resp.status_code != 200:
                logger.error(f"Phemex symbols error: HTTP {resp.status_code} | Body: {resp.text[:200]}")
                return []
                
            # logger.debug(f"Phemex symbols: HTTP 200, Content-Length: {len(resp.content)}")
            data = ujson.loads(resp.content)
            root = data.get("data", {})
            if not isinstance(root, dict): 
                logger.error(f"Phemex symbols: 'data' field is not a dict! Keys: {data.keys()}")
                return []

            # ВАЖНО: Ищем в perpProductsV2 или perpProducts (из черновика)
            items = root.get("perpProductsV2") or root.get("perpProducts") or []
            
            result = []
            seen = set()
            for item in items:
                si = self._parse_perp(item, quote=quote)
                if si:
                    if only_active and not self._is_active_status(si.status):
                        continue
                    if si.symbol not in seen:
                        seen.add(si.symbol)
                        result.append(si)
            
            # logger.info(f"Phemex symbols: parsed {len(result)} perpetuals for {quote}")
            return result
        except Exception as e:
            logger.error(f"Error fetching Phemex symbols: {e}")
            return []

    async def aclose(self):
        await self.session.close()

if __name__ == "__main__":
    async def test():
        api = PhemexSymbols()
        try:
            res = await api.get_all(quote="USDT")
            print(f"Fetched {len(res)} USDT symbols")
            if res:
                print(f"Example: {res[0]}")
                # Выведем BTCUSDT для проверки
                for r in res:
                    if r.symbol == "BTCUSDT":
                        print(f"Found BTCUSDT: {r}")
        finally:
            await api.aclose()
    asyncio.run(test())

# ============================================================
# ШПАРГАЛКА ПО ЗАПУСКУ:
# python -m API.PHEMEX.symbol
# ============================================================