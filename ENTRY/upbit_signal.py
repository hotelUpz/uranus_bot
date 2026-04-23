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

# Основные параметры
POLL_INTERVAL_SEC = UPBIT_CFG.get("poll_interval_sec", 0.1)
MIN_COOLDOWN_SEC  = UPBIT_CFG.get("min_cooldown_sec", 0.1)
PROXIES           = UPBIT_CFG.get("proxies", [None])
BANG_SLEEP_SEC    = UPBIT_CFG.get("bang_sleep_sec", 30)

# Блок adaptive
ADAPTIVE_CFG      = UPBIT_CFG.get("adaptive", {})
ADAPTIVE_ENABLED  = ADAPTIVE_CFG.get("enabled", False)
ADAPTIVE_STEP     = ADAPTIVE_CFG.get("step", 0.1)
ADAPTIVE_N_STABLE = ADAPTIVE_CFG.get("n_stable", 10)

# Блок prober
PROBER_CFG        = UPBIT_CFG.get("prober", {})
PROBER_START      = PROBER_CFG.get("start_delay", 0.5)
PROBER_STEP       = PROBER_CFG.get("step", 0.1)
PROBER_HITS       = PROBER_CFG.get("hits_per_step", 100)

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
# 2. АДАПТИВНЫЙ РЕГУЛЯТОР
# ==========================================
class AdaptiveCooldownTuner:
    def __init__(self, initial_cooldown: float):
        self.initial_cooldown = initial_cooldown
        self.current_cooldown = initial_cooldown
        self._streak = 0

    def on_success(self) -> float | None:
        if not ADAPTIVE_ENABLED: return None
        self._streak += 1
        if self._streak >= ADAPTIVE_N_STABLE:
            self._streak = 0
            new_cd = max(MIN_COOLDOWN_SEC, round(self.current_cooldown - ADAPTIVE_STEP, 2))
            if new_cd != self.current_cooldown:
                self.current_cooldown = new_cd
                return new_cd
        return None

    def on_rate_limit(self) -> float:
        self._streak = 0
        if ADAPTIVE_ENABLED:
            self.current_cooldown = min(10.0, self.current_cooldown * 2.0)
        else:
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
        for s in list(self.sessions.values()):
            try: await s.close()
            except: pass
        self.sessions.clear()

# ==========================================
# 4. МОНИТОРИНГ
# ==========================================
class UpbitLiveMonitor:
    def __init__(self, poll_interval_sec: float, proxies: list, 
                 on_signal=None, 
                 is_paused_func=None,
                 cache_file: str = "live_signals.json"):
        self._on_signal = on_signal
        self._is_paused_func = is_paused_func
        self.poll_interval = poll_interval_sec
        self.api_url = "https://api-manager.upbit.com/api/v1/announcements"
        self.cache_file = cache_file
        self.session_manager = SmartSessionManager(proxies)
        
        self.keywords = ["Market Support for", "신규 거래지원", "디지털 자산 추가"]
        self.seen_ids = set()
        self.seen_symbols = set()
        self.signals_log = []
        self._is_startup = True
        
        self._load_cache()

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self.signals_log = json.load(f)
                for s in self.signals_log:
                    if "symbol" in s: self.seen_symbols.add(s["symbol"])
                logger.info(f"💾 Кеш загружен: {len(self.seen_symbols)} монет.")
            except Exception as e:
                logger.error(f"Ошибка кеша: {e}")

    def _save_cache(self):
        """Запись кеша в фоне, чтобы не тормозить цикл мониторинга."""
        def sync_save():
            try:
                with open(self.cache_file, "w", encoding="utf-8") as f:
                    json.dump(self.signals_log, f, ensure_ascii=False, indent=4)
            except Exception as e:
                logger.error(f"Ошибка записи JSON: {e}")
        
        # Запускаем в отдельном потоке, чтобы не блокировать Event Loop
        asyncio.get_event_loop().run_in_executor(None, sync_save)

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
            symbol = match.group(1).strip().upper()
            return re.sub(r"\(.*\)", "", symbol).strip() or None
        if "for" in title.lower():
            idx = title.lower().find("for")
            tail = title[idx + 3:].strip()
            if tail: return tail.split()[0].upper()
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
        announce_str = datetime.fromtimestamp(announce_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        
        if not is_startup and (time.time() * 1000 - announce_ms > 300000): return
        
        if not is_startup and self._on_signal:
            await self._on_signal(symbol)
            logger.info(f"🚀 СИГНАЛ: {symbol} | Анонс: {announce_str}")
            
        self.seen_symbols.add(symbol)
        self.signals_log.append({
            "symbol": symbol,
            "announce_ts_ms": announce_ms,
            "announce_ts_str": announce_str,
            "status": "NEW" if not is_startup else "INIT"
        })
        self._save_cache()

    async def _worker_loop(self, proxy: str | None, offset: float, base_cd: float):
        await asyncio.sleep(offset)
        session = self.session_manager.get_session(proxy)
        tuner = AdaptiveCooldownTuner(base_cd)
        label = proxy or "Localhost"
        
        while proxy in self.session_manager.proxies:
            loop_start = time.time()
            if self._is_paused_func and self._is_paused_func():
                await asyncio.sleep(tuner.current_cooldown)
                continue

            notices = await self.fetch_announcements(session, label)
            
            if notices is _RATE_LIMIT:
                new_cd = tuner.on_rate_limit()
                logger.warning(f"🛑 429 на [{label}]. CD -> {new_cd:.2f}с. Bang sleep {BANG_SLEEP_SEC}с...")
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

                speedup = tuner.on_success()
                if speedup:
                    logger.info(f"⚡ [{label}] Ускорение! Новый CD -> {speedup:.2f}с")

            elapsed = time.time() - loop_start
            await asyncio.sleep(max(0.0, tuner.current_cooldown - elapsed))

    async def run(self):
        proxy_count = len(self.session_manager.proxies)
        base_cd = max(MIN_COOLDOWN_SEC, self.poll_interval * proxy_count)
        
        mode = "АДАПТИВНЫЙ" if ADAPTIVE_ENABLED else "СТАТИЧНЫЙ"
        logger.info(f"🚀 ЗАПУСК ВОРКЕРОВ ({mode})")
        logger.info(f" 🎯 Целевой интервал: {self.poll_interval}с")
        logger.info(f" 🛡️ Кулдаун на слот: {base_cd:.2f}с")
        
        workers = []
        offset_step = base_cd / proxy_count if proxy_count > 0 else 0
        for i, p in enumerate(self.session_manager.proxies):
            workers.append(asyncio.create_task(self._worker_loop(p, i * offset_step, base_cd)))
            
        await asyncio.sleep(base_cd + 1.0)
        self._is_startup = False
        logger.info("✅ Синхронизация завершена. Ждем листинги...")
        await asyncio.gather(*workers)

# ==========================================
# 5. ТЕСТИРОВЩИК (STRESS PROBER)
# ==========================================
class WafLimitTester:
    def __init__(self, proxy: str | None, start_delay: float, step: float, hits: int):
        self.proxy = proxy
        self.current_delay = start_delay
        self.step = step
        self.hits_per_step = hits
        self.api_url = "https://api-manager.upbit.com/api/v1/announcements"
        
    async def run(self):
        label = self.proxy or "Localhost"
        logger.info(f"--- START SPEED TEST on [{label}] ---")
        
        proxy_dict = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        session = cffi_requests.AsyncSession(impersonate="chrome120", proxies=proxy_dict, headers=DEFAULT_HEADERS)
        params = {"category": "trade", "page": 1, "per_page": 10, "os": "web"}
        
        try:
            while self.current_delay > 0:
                logger.info(f">>> [{label}] Testing level: {self.current_delay:.2f}s")
                for i in range(1, self.hits_per_step + 1):
                    resp = await session.get(self.api_url, params=params)
                    if resp.status_code == 200:
                        if i % 10 == 0 or i == self.hits_per_step:
                            logger.info(f"  [{i}/{self.hits_per_step}] CD {self.current_delay:.2f}s OK")
                        await asyncio.sleep(self.current_delay)
                    elif resp.status_code == 429:
                        logger.warning(f"🛑 БАН (429) на {self.current_delay:.2f}с (Hit {i})!")
                        return
                    else:
                        logger.error(f"Error {resp.status_code} on {label}")
                        return
                self.current_delay = round(self.current_delay - self.step, 2)
        finally:
            await session.close()

# ==========================================
# 6. MOCK ГЕНЕРАТОР
# ==========================================
class MockUpbitLiveMonitor:
    def __init__(self, poll_interval_sec: float, on_signal=None, is_paused_func=None):
        self._on_signal = on_signal
        self.poll_interval = poll_interval_sec
        self._is_paused_func = is_paused_func
        
    async def run(self):
        import random
        logger.info("🟢 MOCK ГЕНЕРАТОР ЗАПУЩЕН")
        while True:
            await asyncio.sleep(self.poll_interval)
            if self._is_paused_func and self._is_paused_func(): continue
            if self._on_signal: await self._on_signal(random.choice(["BTC", "ETH", "SOL"]))

async def run_all_probers():
    """Запускает пробер для каждого прокси из конфига по очереди"""
    for p in PROXIES:
        tester = WafLimitTester(p, PROBER_START, PROBER_STEP, PROBER_HITS)
        await tester.run()
        print("\n" + "="*50 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-waf", action="store_true")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    def dummy_on_signal(s): logger.info(f"🔔 SIGNAL: {s}")
    def dummy_is_paused(): return False

    if args.test_waf:
        asyncio.run(run_all_probers())
    elif args.mock:
        asyncio.run(MockUpbitLiveMonitor(POLL_INTERVAL_SEC, dummy_on_signal, dummy_is_paused).run())
    else:
        asyncio.run(UpbitLiveMonitor(POLL_INTERVAL_SEC, PROXIES, dummy_on_signal, dummy_is_paused).run())

# python -m ENTRY.upbit_signal --test-waf