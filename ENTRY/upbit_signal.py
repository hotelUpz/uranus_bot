from __future__ import annotations

import asyncio
import re
import json
import os
import time
import argparse
from datetime import datetime, timezone, timedelta

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.errors import RequestsError

from c_log import UnifiedLogger

logger = UnifiedLogger("upbit_signal")

# ==========================================
# 1. ЗАГРУЗКА КОНФИГА
# ==========================================
def load_cfg() -> dict:
    try:
        with open("cfg.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"⚠️ Ошибка загрузки cfg.json: {e}")
        return {}

CFG = load_cfg()
UPBIT_CFG = CFG.get("upbit", {})

# Боевые константы
POLL_INTERVAL_SEC         = UPBIT_CFG.get("poll_interval_sec", 0.1)
MIN_COOLDOWN_SEC          = UPBIT_CFG.get("min_cooldown_sec", 0.1)
BANG_SLEEP_SEC            = UPBIT_CFG.get("bang_sleep_sec", 30)

ADAPTIVE_CFG              = UPBIT_CFG.get("adaptive", {})
ADAPTIVE_ENABLED          = ADAPTIVE_CFG.get("enabled", False)
COOLDOWN_STEP_SEC         = ADAPTIVE_CFG.get("step", 0.1)
N_STABLE_STREAK           = ADAPTIVE_CFG.get("n_stable", 10)

PROBER_CFG                = UPBIT_CFG.get("prober", {})

_RATE_LIMIT = object()

DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9,ru;q=0.8",
    "origin": "https://upbit.com",
    "referer": "https://upbit.com/",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}

# ==========================================
# 2. АДАПТИВНЫЙ ТЮНЕР
# ==========================================
class AdaptiveCooldownTuner:
    def __init__(self, initial_cooldown: float):
        self.initial_cooldown = initial_cooldown
        self.current_cooldown = initial_cooldown
        self._streak = 0

    def on_success(self) -> float | None:
        if not ADAPTIVE_ENABLED: return None
        self._streak += 1
        if self._streak >= N_STABLE_STREAK:
            self._streak = 0
            new_cd = max(MIN_COOLDOWN_SEC, self.current_cooldown - COOLDOWN_STEP_SEC)
            if new_cd != self.current_cooldown:
                self.current_cooldown = new_cd
                return new_cd
        return None

    def on_rate_limit(self) -> float:
        self._streak = 0
        self.current_cooldown = self.initial_cooldown
        return self.current_cooldown

# ==========================================
# 3. МЕНЕДЖЕР СЕССИЙ
# ==========================================
class SmartSessionManager:
    def __init__(self, proxies: list):
        self.proxies = list(proxies) if proxies else [None]
        self.sessions: dict[str | None, cffi_requests.AsyncSession] = {}

    def get_session(self, proxy: str | None) -> cffi_requests.AsyncSession:
        if proxy not in self.sessions:
            proxy_dict = {"http": proxy, "https": proxy} if proxy else None
            self.sessions[proxy] = cffi_requests.AsyncSession(
                impersonate="chrome120", 
                proxies=proxy_dict,
                headers=DEFAULT_HEADERS,
                timeout=5.0
            )
        return self.sessions[proxy]

    async def close_all(self):
        for s in self.sessions.values():
            try: await s.close()
            except: pass
        self.sessions.clear()

# ==========================================
# 4. МОНИТОРИНГ
# ==========================================
class UpbitLiveMonitor:
    def __init__(self, poll_interval_sec: float, proxies: list, on_signal=None, is_paused_func=None):
        self._on_signal = on_signal
        self._is_paused_func = is_paused_func
        self.poll_interval = poll_interval_sec
        self.api_url = "https://api-manager.upbit.com/api/v1/announcements"
        self.session_manager = SmartSessionManager(proxies)
        
        self.keywords = ["Market Support for", "신규 거래지원", "디지털 자산 추가"]
        self.seen_ids = set()
        self.seen_symbols = set()
        self._is_startup = True

    def _parse_iso_to_ms(self, dt_str: str) -> int:
        if not dt_str: return 0
        KST = timezone(timedelta(hours=9))
        try:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=KST)
            return int(dt.timestamp() * 1000)
        except: return 0

    def _extract_symbol(self, title: str) -> str | None:
        match = re.search(r"\(([^)]+)\)", title)
        if match:
            s = match.group(1).strip().upper()
            return re.sub(r"\(.*\)", "", s).strip() or None
        return None

    async def fetch_announcements(self, session: cffi_requests.AsyncSession, label: str):
        params = {"category": "trade", "page": 1, "per_page": 10, "os": "web"}
        try:
            resp = await session.get(self.api_url, params=params)
            if resp.status_code == 200:
                return resp.json().get("data", {}).get("notices", [])
            if resp.status_code == 429:
                return _RATE_LIMIT
            return []
        except:
            return []

    async def process_new_listing(self, notice: dict, symbol: str, is_startup: bool):
        announce_ms = self._parse_iso_to_ms(notice.get("first_listed_at") or notice.get("listed_at"))
        if not is_startup and (time.time() * 1000 - announce_ms > 300000): return
        
        if not is_startup and self._on_signal:
            await self._on_signal(symbol)
            logger.info(f"🚀 SIGNAL: {symbol}")
            
        self.seen_symbols.add(symbol)

    async def _worker_loop(self, proxy: str | None, offset: float, base_cd: float):
        await asyncio.sleep(offset)
        session = self.session_manager.get_session(proxy)
        tuner = AdaptiveCooldownTuner(base_cd)
        label = proxy or "Local"
        
        while proxy in self.session_manager.proxies:
            loop_start = time.time()
            if self._is_paused_func and self._is_paused_func():
                await asyncio.sleep(tuner.current_cooldown)
                continue

            notices = await self.fetch_announcements(session, label)
            
            if notices is _RATE_LIMIT:
                new_cd = tuner.on_rate_limit()
                logger.warning(f"🛑 429 on {label}. CD reset to {new_cd}s. Bang sleep {BANG_SLEEP_SEC}s")
                await asyncio.sleep(BANG_SLEEP_SEC)
                continue
                
            if isinstance(notices, list):
                for n in notices:
                    n_id = n.get("id")
                    if not n_id or n_id in self.seen_ids: continue
                    self.seen_ids.add(n_id)
                    title = n.get("title", "")
                    if any(kw in title for kw in self.keywords):
                        symbol = self._extract_symbol(title)
                        if symbol and symbol not in self.seen_symbols:
                            await self.process_new_listing(n, symbol, self._is_startup)

                tuner.on_success()

            elapsed = time.time() - loop_start
            await asyncio.sleep(max(0.0, tuner.current_cooldown - elapsed))

    async def run(self):
        proxy_count = len(self.session_manager.proxies)
        base_cd = max(MIN_COOLDOWN_SEC, self.poll_interval * proxy_count)
        
        logger.info(f"🚀 START MONITORING | System Interval: {self.poll_interval}s | Base CD: {base_cd}s")
        
        workers = []
        offset_step = base_cd / proxy_count
        for i, p in enumerate(self.session_manager.proxies):
            workers.append(asyncio.create_task(self._worker_loop(p, i * offset_step, base_cd)))
            
        await asyncio.sleep(base_cd + 1.0)
        self._is_startup = False
        await asyncio.gather(*workers)

# ==========================================
# 5. WAF PROBER (STRESS TESTER)
# ==========================================
class WafLimitTester:
    def __init__(self, start_delay: float, step: float, hits_per_step: int = 10):
        self.current_delay = start_delay
        self.step = step
        self.hits_per_step = hits_per_step
        self.api_url = "https://api-manager.upbit.com/api/v1/announcements"
        
    async def run(self):
        logger.info(f"--- START SPEED TEST (STRESS MODE) ---")
        logger.info(f"Start: {self.current_delay}s | Step: -{self.step}s | Hits per Step: {self.hits_per_step}")
        
        session = cffi_requests.AsyncSession(impersonate="chrome120", headers=DEFAULT_HEADERS)
        params = {"category": "trade", "page": 1, "per_page": 10, "os": "web"}
        
        try:
            while self.current_delay >= MIN_COOLDOWN_SEC:
                logger.info(f">>> Testing level: {self.current_delay:.2f}s")
                
                for i in range(1, self.hits_per_step + 1):
                    resp = await session.get(self.api_url, params=params)
                    
                    if resp.status_code == 200:
                        if i % 5 == 0 or i == self.hits_per_step:
                            logger.info(f"  [{i}/{self.hits_per_step}] Cooldown {self.current_delay:.2f}s OK")
                        await asyncio.sleep(self.current_delay)
                    elif resp.status_code == 429:
                        logger.warning(f"🛑 BANNED (429) at {self.current_delay:.2f}s on hit {i}!")
                        return
                    else:
                        logger.error(f"Error {resp.status_code}")
                        return
                
                next_delay = round(self.current_delay - self.step, 2)
                if next_delay < MIN_COOLDOWN_SEC:
                    logger.info(f"🎯 Target reached: {MIN_COOLDOWN_SEC}s. IP is a beast!")
                    break
                self.current_delay = next_delay
                
        finally:
            await session.close()

# ==========================================
# 6. MOCK GENERATOR
# ==========================================
class MockUpbitLiveMonitor:
    def __init__(self, poll_interval_sec: float, on_signal=None, is_paused_func=None):
        self._on_signal = on_signal
        self.poll_interval = poll_interval_sec
        
    async def run(self):
        import random
        logger.info(f"🛠 MOCK MODE | Interval: {self.poll_interval}s")
        while True:
            await asyncio.sleep(self.poll_interval)
            if self._on_signal:
                await self._on_signal(random.choice(["BTC", "ETH", "SOL"]))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-waf", action="store_true")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    def dummy_on_signal(s): logger.info(f"🔔 SIGNAL RECEIVED: {s}")
    def dummy_is_paused(): return False

    if args.test_waf:
        asyncio.run(WafLimitTester(
            start_delay=PROBER_CFG.get("start_delay", 1.0),
            step=PROBER_CFG.get("step", 0.1),
            hits_per_step=PROBER_CFG.get("hits_per_step", 10)
        ).run())
    elif args.mock:
        asyncio.run(MockUpbitLiveMonitor(poll_interval_sec=POLL_INTERVAL_SEC, on_signal=dummy_on_signal).run())
    else:
        asyncio.run(UpbitLiveMonitor(POLL_INTERVAL_SEC, UPBIT_CFG.get("proxies", [None]), dummy_on_signal, dummy_is_paused).run())

# python -m ENTRY.upbit_signal --test-waf