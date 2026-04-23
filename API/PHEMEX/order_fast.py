import time
import hmac
import hashlib
import asyncio
import ujson
from typing import Any, Dict, Optional
from curl_cffi.requests import AsyncSession
from c_log import UnifiedLogger
from utils import float_to_str

logger = UnifiedLogger("api_fast")

class PhemexFastClient:
    BASE_URL = "https://api.phemex.com"

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.secret_bytes = api_secret.encode("utf-8")
        
        # Используем HTTP/2 и имитацию браузера
        self.session = AsyncSession(
            base_url=self.BASE_URL,
            impersonate="chrome110",
            http_version=2,
            verify=True
        )

        self._lock = asyncio.Lock()
        self._last_send_time = 0
        self.MIN_SEND_INTERVAL = 0.002

    def _get_signature(self, path: str, query_no_q: str, expiry: int, body_str: str) -> str:
        message = (path + query_no_q + str(expiry) + body_str).encode("utf-8")
        return hmac.new(self.secret_bytes, message, hashlib.sha256).hexdigest()

    async def _request(self, method: str, path: str, query_no_q: str = "", body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        async with self._lock:
            now = time.monotonic()
            if (elapsed := now - self._last_send_time) < self.MIN_SEND_INTERVAL:
                await asyncio.sleep(self.MIN_SEND_INTERVAL - elapsed)
            self._last_send_time = time.monotonic()

        url = path + (f"?{query_no_q}" if query_no_q else "")
        body_str = ujson.dumps(body) if body else ""
        expiry = int(time.time() + 60)
        signature = self._get_signature(path, query_no_q, expiry, body_str)
        
        headers = {
            "Content-Type": "application/json",
            "x-phemex-access-token": self.api_key,
            "x-phemex-request-expiry": str(expiry),
            "x-phemex-request-signature": signature
        }

        try:
            resp = await self.session.request(
                method, url, data=body_str if body_str else None, headers=headers, timeout=5.0
            )
            if resp.status_code not in (200, 201, 202):
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
            
            data = ujson.loads(resp.content)
            if data.get("code", 0) != 0:
                raise RuntimeError(f"Phemex Error: {data}")
            return data
        except Exception as e:
            logger.error(f"Fast API Error: {e}")
            raise

    async def place_market_order(self, symbol: str, side: str, qty: float, pos_side: str, reduce_only: bool = False) -> Dict[str, Any]:
        body = {
            "symbol": symbol,
            "side": side,
            "orderQtyRq": float_to_str(qty),
            "ordType": "Market",
            "posSide": pos_side,
        }
        if reduce_only:
            body["reduceOnly"] = True
        return await self._request("POST", "/g-orders", body=body)

    async def close(self):
        await self.session.close()