# ============================================================
# FILE: API/PHEMEX/drafts/symbol_aio.py (DRAFT - aiohttp version)
# ROLE: Old aiohttp implementation for future generations.
# ============================================================
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import aiohttp

@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    status: str
    quote: str
    tick_size: Optional[float]
    lot_size: Optional[float]
    max_leverage: Optional[float]

class PhemexSymbolsAIO:
    BASE_URL = "https://api.phemex.com"

    def __init__(self, timeout_sec: float = 20.0, retries: int = 3):
        self._timeout = aiohttp.ClientTimeout(total=float(timeout_sec))
        self._retries = int(retries)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                return self._session
            connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300, enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(timeout=self._timeout, connector=connector)
            return self._session

    async def aclose(self) -> None:
        if self._session is not None:
            try: await self._session.close()
            except Exception: pass
        self._session = None

    async def _get_json(self, path: str) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        last_err: Optional[Exception] = None
        for attempt in range(1, self._retries + 1):
            try:
                session = await self._get_session()
                async with session.get(url) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}: {text}")
                    data = await resp.json()
                    return data
            except Exception as e:
                last_err = e
                if attempt < self._retries:
                    await asyncio.sleep(0.4 * attempt)
        raise RuntimeError(f"Phemex symbols failed: {path} err={last_err}")
