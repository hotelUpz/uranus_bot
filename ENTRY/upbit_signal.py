# # ============================================================
# # FILE: ENTRY/upbit_signal.py
# # ROLE: UPBIT SIGNAL
# # ============================================================

# from __future__ import annotations

# import asyncio
# import re
# import json
# import os
# import time
# import argparse
# from datetime import datetime, timezone, timedelta

# from curl_cffi import requests as cffi_requests
# # from curl_cffi.requests.errors import RequestsError

# from c_log import UnifiedLogger
# from ENTRY._utils import get_upbit_status_and_sleep_time

# logger = UnifiedLogger("upbit_signal")

# # ==========================================
# # 1. ЗАГРУЗКА КОНФИГА
# # ==========================================
# def load_cfg() -> dict:
#     try:
#         with open("cfg.json", "r", encoding="utf-8") as f:
#             return json.load(f)
#     except Exception as e:
#         logger.warning(f"[CFG] Error loading cfg.json: {e}")
#         return {}

# CFG = load_cfg()
# UPBIT_CFG = CFG.get("upbit", {})

# # Основные параметры
# POLL_INTERVAL_SEC = UPBIT_CFG.get("poll_interval_sec", 0.1)
# MIN_COOLDOWN_SEC  = UPBIT_CFG.get("min_cooldown_sec", 0.1)
# PROXIES           = UPBIT_CFG.get("proxies", [None])
# BANG_SLEEP_SEC    = UPBIT_CFG.get("bang_sleep_sec", 30)

# # Блок adaptive
# ADAPTIVE_CFG      = UPBIT_CFG.get("adaptive", {})
# ADAPTIVE_ENABLED  = ADAPTIVE_CFG.get("enabled", False)
# ADAPTIVE_STEP     = ADAPTIVE_CFG.get("step", 0.1)
# ADAPTIVE_N_STABLE = ADAPTIVE_CFG.get("n_stable", 10)

# # Блок prober
# PROBER_CFG        = UPBIT_CFG.get("prober", {})
# PROBER_START      = PROBER_CFG.get("start_delay", 0.5)
# PROBER_STEP       = PROBER_CFG.get("step", 0.1)
# PROBER_HITS       = PROBER_CFG.get("hits_per_step", 100)

# # Тестирование backoff без реальных 429
# MOCK_429          = False

# _RATE_LIMIT = object()

# DEFAULT_HEADERS = {
#     "accept": "application/json, text/plain, */*",
#     "accept-language": "en-US,en;q=0.9,ru;q=0.8",
#     "origin": "https://upbit.com",
#     "referer": "https://upbit.com/",
#     "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
#     "sec-ch-ua-mobile": "?0",
#     "sec-ch-ua-platform": '"Windows"',
#     "sec-fetch-dest": "empty",
#     "sec-fetch-mode": "cors",
#     "sec-fetch-site": "same-site",
# }

# # ==========================================
# # 2. АДАПТИВНЫЙ РЕГУЛЯТОР
# # ==========================================
# class AdaptiveCooldownTuner:
#     def __init__(self, initial_cooldown: float):
#         self.initial_cooldown = initial_cooldown
#         self.current_cooldown = initial_cooldown
#         self._streak = 0
#         self._backoff_active = False

#     def on_success(self) -> float | None:
#         self._streak += 1
        
#         # Если мы в режиме восстановления после 429
#         if self._backoff_active:
#             if self._streak >= 5: # Для восстановления нужно 5 успешных шагов
#                 self._streak = 0
#                 old_cd = self.current_cooldown
#                 # Снижаем кулдаун на 30% в сторону начального
#                 self.current_cooldown = max(self.initial_cooldown, round(self.current_cooldown * 0.7, 2))
#                 if self.current_cooldown <= self.initial_cooldown:
#                     self._backoff_active = False
#                 if self.current_cooldown != old_cd:
#                     return self.current_cooldown
#             return None

#         # Обычная адаптивная логика (если включена)
#         if not ADAPTIVE_ENABLED: return None
#         if self._streak >= ADAPTIVE_N_STABLE:
#             self._streak = 0
#             new_cd = max(MIN_COOLDOWN_SEC, round(self.current_cooldown - ADAPTIVE_STEP, 2))
#             if new_cd != self.current_cooldown:
#                 self.current_cooldown = new_cd
#                 return new_cd
#         return None

#     def on_rate_limit(self) -> float:
#         self._streak = 0
#         self._backoff_active = True
#         # Exponential Backoff: увеличиваем в 2 раза, лимит 60с
#         self.current_cooldown = min(60.0, round(self.current_cooldown * 2.0, 2))
#         return self.current_cooldown

# # ==========================================
# # 3. МЕНЕДЖЕР СЕССИЙ
# # ==========================================
# class SmartSessionManager:
#     def __init__(self, proxies: list):
#         self.proxies = list(proxies) if proxies else [None]
#         self.sessions: dict[str | None, cffi_requests.AsyncSession] = {}

#     def get_session(self, proxy: str | None) -> cffi_requests.AsyncSession:
#         if proxy not in self.sessions:
#             proxy_dict = {"http": proxy, "https": proxy} if proxy else None
#             self.sessions[proxy] = cffi_requests.AsyncSession(
#                 impersonate="chrome120", 
#                 proxies=proxy_dict,
#                 headers=DEFAULT_HEADERS,
#                 timeout=5.0
#             )
#         return self.sessions[proxy]

#     async def close_all(self):
#         for s in list(self.sessions.values()):
#             try: await s.close()
#             except: pass
#         self.sessions.clear()

# # ==========================================
# # 4. МОНИТОРИНГ
# # ==========================================
# class UpbitLiveMonitor:
#     def __init__(self, poll_interval_sec: float, proxies: list, 
#                  on_signal=None, 
#                  is_paused_func=None,
#                  cache_file: str = "live_signals.json"):
#         self.poll_interval = poll_interval_sec
#         self.session_manager = SmartSessionManager(proxies)
#         self.on_signal = on_signal
#         self._is_paused_func = is_paused_func
#         self.cache_file = cache_file
        
#         self.keywords = ["Listing", "Adds", "Market Support", "New Market", "KRW"]
#         self.seen_ids = set()
#         self.seen_symbols = set()
#         self.signals_log = []
#         self._is_startup = True
#         self.api_url = "https://api-manager.upbit.com/api/v1/announcements"
#         self._mock_req_count = 0
#         self._load_cache()

#     async def aclose(self):
#         """Полная остановка и очистка ресурсов"""
#         logger.info("[UPBIT] Закрытие сессий мониторинга...")
#         await self.session_manager.close_all()

#     def _load_cache(self):
#         if os.path.exists(self.cache_file):
#             try:
#                 with open(self.cache_file, "r", encoding="utf-8") as f:
#                     self.signals_log = json.load(f)
#                 for s in self.signals_log:
#                     if "symbol" in s: self.seen_symbols.add(s["symbol"])
#                 logger.info(f"[CACHE] Loaded: {len(self.seen_symbols)} symbols.")
#             except Exception as e:
#                 logger.error(f"[CACHE] Error: {e}")

#     def _save_cache(self):
#         """Запись кеша в фоне, чтобы не тормозить цикл мониторинга."""
#         def sync_save():
#             try:
#                 with open(self.cache_file, "w", encoding="utf-8") as f:
#                     json.dump(self.signals_log, f, ensure_ascii=False, indent=4)
#             except Exception as e:
#                 logger.error(f"[CACHE] Write error: {e}")
        
#         # Запускаем в отдельном потоке, чтобы не блокировать Event Loop
#         asyncio.get_event_loop().run_in_executor(None, sync_save)

#     def _parse_iso_to_ms(self, dt_str: str) -> int:
#         if not dt_str: return 0
#         KST = timezone(timedelta(hours=9))
#         try:
#             dt = datetime.fromisoformat(dt_str)
#             if dt.tzinfo is None: dt = dt.replace(tzinfo=KST)
#             return int(dt.timestamp() * 1000)
#         except: return 0

#     def _extract_symbol(self, title: str) -> str | None:
#         match = re.search(r"\(([^)]+)\)", title)
#         if match:
#             symbol = match.group(1).strip().upper()
#             return re.sub(r"\(.*\)", "", symbol).strip() or None
#         if "for" in title.lower():
#             idx = title.lower().find("for")
#             tail = title[idx + 3:].strip()
#             if tail: return tail.split()[0].upper()
#         return None

#     async def fetch_announcements(self, session: cffi_requests.AsyncSession, label: str):
#         if MOCK_429:
#             self._mock_req_count += 1
#             if self._mock_req_count % 5 == 0: # Имитируем 429 на каждый 5-й запрос
#                 return _RATE_LIMIT

#         params = {"category": "trade", "page": 1, "per_page": 10, "os": "web"}
#         try:
#             resp = await session.get(self.api_url, params=params)
#             if resp.status_code == 200:
#                 return resp.json().get("data", {}).get("notices", [])
#             if resp.status_code == 429:
#                 return _RATE_LIMIT
#             return []
#         except:
#             return []

#     async def process_new_listing(self, notice: dict, symbol: str, is_startup: bool):
#         announce_ms = self._parse_iso_to_ms(notice.get("first_listed_at") or notice.get("listed_at"))
#         announce_str = datetime.fromtimestamp(announce_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

#         # Метка времени именно в момент получения сигнала ботом
#         received_ms = int(time.time() * 1000)
#         received_str = datetime.fromtimestamp(received_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f UTC")[:-3]

#         if not is_startup and (received_ms - announce_ms > 300000): return

#         if not is_startup and self._on_signal:
#             await self._on_signal(symbol, received_ms)
#             latency_sec = (received_ms - announce_ms) / 1000.0
#             logger.info(
#                 f"[SIGNAL] {symbol} | "
#                 f"Announce: {announce_str} | "
#                 f"Received: {received_str} | "
#                 f"Latency: {latency_sec:+.3f}s"
#             )

#         self.seen_symbols.add(symbol)
#         self.signals_log.append({
#             "symbol": symbol,
#             "announce_ts_ms": announce_ms,
#             "announce_ts_str": announce_str,
#             "received_ts_ms": received_ms,
#             "received_ts_str": received_str,
#             "latency_sec": round((received_ms - announce_ms) / 1000.0, 3),
#             "status": "NEW" if not is_startup else "INIT"
#         })
#         self._save_cache()

#     async def _worker_loop(self, proxy: str | None, offset: float, base_cd: float):
#         await asyncio.sleep(offset)
#         session = self.session_manager.get_session(proxy)
#         tuner = AdaptiveCooldownTuner(base_cd)
#         label = proxy or "Localhost"
        
#         while proxy in self.session_manager.proxies:
#             # 1. Спрашиваем таможню
#             is_active, sleep_time = get_upbit_status_and_sleep_time()
            
#             if not is_active:
#                 # Переводим в часы для красивого лога
#                 sleep_hours = sleep_time / 3600
#                 logger.info(f"[Upbit] Биржа спит. Уходим в анабиоз на {sleep_hours:.2f} часов до старта рабочего дня (KST)...")
                
#                 # Засыпаем до наступления 08:30 KST ближайшего понедельника или след. дня
#                 await asyncio.sleep(sleep_time)
#                 continue # После пробуждения цикл начнется заново и еще раз проверит окно
            
#             loop_start = time.time()
#             if self._is_paused_func and self._is_paused_func():
#                 await asyncio.sleep(tuner.current_cooldown)
#                 continue

#             notices = await self.fetch_announcements(session, label)
            
#             if notices is _RATE_LIMIT:
#                 new_cd = tuner.on_rate_limit()
#                 logger.warning(f"[429] Rate Limit on [{label}]. CD -> {new_cd:.2f}s. Penalty sleep {BANG_SLEEP_SEC}s...")
#                 await asyncio.sleep(BANG_SLEEP_SEC)
#                 continue
                
#             if isinstance(notices, list):
#                 for n in notices:
#                     n_id = n.get("id")
#                     if not n_id or n_id in self.seen_ids: continue
#                     self.seen_ids.add(n_id)
#                     title = n.get("title", "")
#                     if any(kw in title for kw in self.keywords):
#                         symbol = self._extract_symbol(title)
#                         if symbol and symbol not in self.seen_symbols:
#                             await self.process_new_listing(n, symbol, self._is_startup)

#                 speedup = tuner.on_success()
#                 if speedup:
#                     logger.info(f"[SPEEDUP] [{label}] New CD -> {speedup:.2f}s")

#             elapsed = time.time() - loop_start
#             await asyncio.sleep(max(0.0, tuner.current_cooldown - elapsed))

#     async def run(self):
#         proxy_count = len(self.session_manager.proxies)
#         base_cd = max(MIN_COOLDOWN_SEC, self.poll_interval * proxy_count)
        
#         mode = "ADAPTIVE" if ADAPTIVE_ENABLED else "STATIC"
#         logger.info(f"[RUN] STARTING WORKERS ({mode})")
#         logger.info(f"  Target interval: {self.poll_interval}s")
#         logger.info(f"  Cooldown per slot: {base_cd:.2f}s")
        
#         workers = []
#         offset_step = base_cd / proxy_count if proxy_count > 0 else 0
#         for i, p in enumerate(self.session_manager.proxies):
#             workers.append(asyncio.create_task(self._worker_loop(p, i * offset_step, base_cd)))
            
#         try:
#             await asyncio.sleep(base_cd + 1.0)
#             self._is_startup = False
#             logger.info("[OK] Sync complete. Waiting for listings...")
#             await asyncio.gather(*workers)
#         except asyncio.CancelledError:
#             logger.info("[UPBIT] Получен сигнал отмены. Останавливаем воркеры...")
#             for w in workers:
#                 w.cancel()
#             await asyncio.gather(*workers, return_exceptions=True)
#             raise
#         finally:
#             await self.aclose()

# # ==========================================
# # 5. ТЕСТИРОВЩИК (STRESS PROBER)
# # ==========================================
# class WafLimitTester:
#     def __init__(self, proxy: str | None, start_delay: float, step: float, hits: int):
#         self.proxy = proxy
#         self.current_delay = start_delay
#         self.step = step
#         self.hits_per_step = hits
#         self.api_url = "https://api-manager.upbit.com/api/v1/announcements"
#         self._mock_req_count = 0
        
#     async def run(self):
#         label = self.proxy or "Localhost"
#         logger.info(f"--- START SPEED TEST on [{label}] ---")
        
#         proxy_dict = {"http": self.proxy, "https": self.proxy} if self.proxy else None
#         session = cffi_requests.AsyncSession(impersonate="chrome120", proxies=proxy_dict, headers=DEFAULT_HEADERS)
#         params = {"category": "trade", "page": 1, "per_page": 10, "os": "web"}
        
#         try:
#             while self.current_delay > 0:
#                 logger.info(f">>> [{label}] Testing level: {self.current_delay:.2f}s")
#                 for i in range(1, self.hits_per_step + 1):
#                     if MOCK_429:
#                         self._mock_req_count += 1
#                         if self._mock_req_count % 5 == 0:
#                             logger.warning(f"[MOCK] BAN (429) at {self.current_delay:.2f}s (Hit {i})!")
#                             # В тестере мы просто имитируем паузу и продолжаем, чтобы увидеть "как если бы"
#                             logger.info(f"--- [MOCK TEST] Simulating penalty sleep {BANG_SLEEP_SEC}s ---")
#                             await asyncio.sleep(1.0) # В тесте не будем ждать полные 30с
#                             continue

#                     resp = await session.get(self.api_url, params=params)
#                     if resp.status_code == 200:
#                         if i % 10 == 0 or i == self.hits_per_step:
#                             logger.info(f"  [{i}/{self.hits_per_step}] CD {self.current_delay:.2f}s OK")
#                         await asyncio.sleep(self.current_delay)
#                     elif resp.status_code == 429:
#                         logger.warning(f"[429] REAL BAN at {self.current_delay:.2f}s (Hit {i})!")
#                         return
#                     else:
#                         logger.error(f"Error {resp.status_code} on {label}")
#                         return
#                 self.current_delay = round(self.current_delay - self.step, 2)
#         finally:
#             await session.close()

# # ==========================================
# # 6. MOCK ГЕНЕРАТОР
# # ==========================================
# class MockUpbitLiveMonitor:
#     def __init__(self, poll_interval_sec: float, on_signal=None, is_paused_func=None):
#         self._on_signal = on_signal
#         self.poll_interval = poll_interval_sec
#         self._is_paused_func = is_paused_func
        
#     async def run(self):
#         import random
#         logger.info("[MOCK] GENERATOR STARTED")
#         while True:
#             await asyncio.sleep(self.poll_interval)
#             if self._is_paused_func and self._is_paused_func(): continue
            
#             sym = random.choice(["BTC", "ETH"])
#             # logger.info(f"🟢 [MOCK] Сгенерирован тестовый сигнал: {sym}")
#             if self._on_signal: await self._on_signal(sym, int(time.time() * 1000))

# async def run_all_probers():
#     """Запускает пробер для каждого прокси из конфига по очереди"""
#     for p in PROXIES:
#         tester = WafLimitTester(p, PROBER_START, PROBER_STEP, PROBER_HITS)
#         await tester.run()
#         print("\n" + "="*50 + "\n")

# if __name__ == "__main__":
#     parser = argparse.ArgumentParser()
#     parser.add_argument("--test-waf", action="store_true")
#     parser.add_argument("--mock", action="store_true")
#     args = parser.parse_args()

#     async def dummy_on_signal(s, ts): logger.info(f"SIGNAL: {s} at {ts}")
#     def dummy_is_paused(): return False

#     if args.test_waf:
#         asyncio.run(run_all_probers())
#     elif args.mock:
#         asyncio.run(MockUpbitLiveMonitor(POLL_INTERVAL_SEC, dummy_on_signal, dummy_is_paused).run())
#     else:
#         asyncio.run(UpbitLiveMonitor(POLL_INTERVAL_SEC, PROXIES, dummy_on_signal, dummy_is_paused).run())

# # python -m ENTRY.upbit_signal --test-waf

# ============================================================
# FILE: ENTRY/upbit_signal.py
# ROLE: UPBIT SIGNAL
# ============================================================

from __future__ import annotations

import asyncio
import re
import json
import os
import time
import argparse
from datetime import datetime, timezone, timedelta

from curl_cffi import requests as cffi_requests
from c_log import UnifiedLogger
from ENTRY._utils import get_upbit_status_and_sleep_time

logger = UnifiedLogger("upbit_signal")

# ==========================================
# 1. ЗАГРУЗКА КОНФИГА
# ==========================================
def load_cfg() -> dict:
    try:
        with open("cfg.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[CFG] Error loading cfg.json: {e}")
        return {}

CFG = load_cfg()
UPBIT_CFG = CFG.get("upbit", {})

POLL_INTERVAL_SEC = UPBIT_CFG.get("poll_interval_sec", 0.1)
MIN_COOLDOWN_SEC  = UPBIT_CFG.get("min_cooldown_sec", 0.1)
PROXIES           = UPBIT_CFG.get("proxies", [None])
BANG_SLEEP_SEC    = UPBIT_CFG.get("bang_sleep_sec", 30)

ADAPTIVE_CFG      = UPBIT_CFG.get("adaptive", {})
ADAPTIVE_ENABLED  = ADAPTIVE_CFG.get("enabled", False)
ADAPTIVE_STEP     = ADAPTIVE_CFG.get("step", 0.1)
ADAPTIVE_N_STABLE = ADAPTIVE_CFG.get("n_stable", 10)

PROBER_CFG        = UPBIT_CFG.get("prober", {})
PROBER_START      = PROBER_CFG.get("start_delay", 0.5)
PROBER_STEP       = PROBER_CFG.get("step", 0.1)
PROBER_HITS       = PROBER_CFG.get("hits_per_step", 100)

MOCK_429          = False
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
        self._backoff_active = False

    def on_success(self) -> float | None:
        self._streak += 1
        if self._backoff_active:
            if self._streak >= 5: 
                self._streak = 0
                old_cd = self.current_cooldown
                self.current_cooldown = max(self.initial_cooldown, round(self.current_cooldown * 0.7, 2))
                if self.current_cooldown <= self.initial_cooldown:
                    self._backoff_active = False
                if self.current_cooldown != old_cd:
                    return self.current_cooldown
            return None

        if not ADAPTIVE_ENABLED: return None
        if self._streak >= ADAPTIVE_N_STABLE:
            self._streak = 0
            new_cd = max(MIN_COOLDOWN_SEC, round(self.current_cooldown - ADAPTIVE_STEP, 2))
            if new_cd != self.current_cooldown:
                self.current_cooldown = new_cd
                return new_cd
        return None

    def on_rate_limit(self) -> float:
        self._streak = 0
        self._backoff_active = True
        self.current_cooldown = min(60.0, round(self.current_cooldown * 2.0, 2))
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
        self.poll_interval = poll_interval_sec
        self.session_manager = SmartSessionManager(proxies)
        self.on_signal = on_signal
        self._is_paused_func = is_paused_func
        self.cache_file = cache_file
        
        self.keywords = ["Listing", "Adds", "Market Support", "New Market", "KRW", "신규 거래지원", "디지털 자산 추가"]
        
        self.seen_ids = set()
        self.seen_symbols = set()
        self.signals_log = []
        self._is_startup = True
        self.api_url = "https://api-manager.upbit.com/api/v1/announcements"
        self._mock_req_count = 0
        self._load_cache()

    async def aclose(self):
        logger.info("[UPBIT] Закрытие сессий мониторинга...")
        await self.session_manager.close_all()

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self.signals_log = json.load(f)
                for s in self.signals_log:
                    if "symbol" in s: self.seen_symbols.add(s["symbol"])
                logger.info(f"[CACHE] Loaded: {len(self.seen_symbols)} symbols.")
            except Exception as e:
                logger.error(f"[CACHE] Error: {e}")

    def _save_cache(self):
        def sync_save():
            try:
                with open(self.cache_file, "w", encoding="utf-8") as f:
                    json.dump(self.signals_log, f, ensure_ascii=False, indent=4)
            except Exception as e:
                logger.error(f"[CACHE] Write error: {e}")
        asyncio.get_event_loop().run_in_executor(None, sync_save)

    def _parse_iso_to_ms(self, dt_str: str) -> int:
        if not dt_str: return 0
        KST = timezone(timedelta(hours=9))
        try:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=KST)
            return int(dt.timestamp() * 1000)
        except Exception:
            try:
                clean = dt_str[:19].replace("T", " ")
                dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=KST)
                return int(dt.timestamp() * 1000)
            except Exception: 
                return 0

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
        if MOCK_429:
            self._mock_req_count += 1
            if self._mock_req_count % 5 == 0:
                return _RATE_LIMIT

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
        # Строго как в старом коде: берем только listed_at. Нет ключа - скипаем.
        listed_at = notice.get("listed_at")
        if not listed_at:
            return
            
        announce_ms = self._parse_iso_to_ms(listed_at)
        received_ms = int(time.time() * 1000)
        
        if announce_ms == 0:
            announce_ms = received_ms
            
        announce_str = datetime.fromtimestamp(announce_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        received_str = datetime.fromtimestamp(received_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f UTC")[:-3]

        if not is_startup and self._on_signal:
            await self._on_signal(symbol, received_ms)
            latency_sec = (received_ms - announce_ms) / 1000.0
            logger.info(
                f"[SIGNAL] {symbol} | "
                f"Announce: {announce_str} | "
                f"Received: {received_str} | "
                f"Latency: {latency_sec:+.3f}s"
            )

        self.seen_symbols.add(symbol)
        self.signals_log.append({
            "symbol": symbol,
            "announce_ts_ms": announce_ms,
            "announce_ts_str": announce_str,
            "received_ts_ms": received_ms,
            "received_ts_str": received_str,
            "latency_sec": round((received_ms - announce_ms) / 1000.0, 3),
            "status": "NEW" if not is_startup else "INIT"
        })
        self._save_cache()

    async def sync_initial_state(self):
        """Полная замена старой логики if is_startup: ... для многопоточной работы"""
        logger.info("[SYNC] Собираем базу текущих новостей перед запуском...")
        proxy = self.session_manager.proxies[0] if self.session_manager.proxies else None
        session = self.session_manager.get_session(proxy)
        
        while True:
            notices = await self.fetch_announcements(session, "StartupSync")
            if isinstance(notices, list):
                for n in notices:
                    n_id = n.get("id")
                    if n_id: 
                        self.seen_ids.add(n_id)
                    title = n.get("title", "")
                    if any(kw in title for kw in self.keywords):
                        symbol = self._extract_symbol(title)
                        if symbol and symbol not in self.seen_symbols:
                            await self.process_new_listing(n, symbol, is_startup=True)
                break 
            elif notices is _RATE_LIMIT:
                logger.warning("[SYNC] Rate limit. Пауза 5 сек...")
                await asyncio.sleep(5)
            else:
                logger.warning("[SYNC] Ошибка. Повтор через 5 сек...")
                await asyncio.sleep(5)
                
        self._is_startup = False
        logger.info(f"[OK] Синхронизация завершена (собрано {len(self.seen_ids)} ID). Ждем новые анонсы...")

    async def _worker_loop(self, proxy: str | None, offset: float, base_cd: float):
        await asyncio.sleep(offset)
        session = self.session_manager.get_session(proxy)
        tuner = AdaptiveCooldownTuner(base_cd)
        label = proxy or "Localhost"
        
        while proxy in self.session_manager.proxies:
            is_active, sleep_time = get_upbit_status_and_sleep_time()
            
            if not is_active:
                now_utc = datetime.now(timezone.utc)
                now_kst = now_utc.astimezone(timezone(timedelta(hours=9)))
                
                wake_utc = now_utc + timedelta(seconds=sleep_time)
                wake_kst = now_kst + timedelta(seconds=sleep_time)
                
                sleep_hours = sleep_time / 3600
                
                logger.info(
                    f"[{label}] Биржа спит. Анабиоз на {sleep_hours:.2f} ч.\n"
                    f"   Засыпаем:    {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC | {now_kst.strftime('%Y-%m-%d %H:%M:%S')} KST\n"
                    f"   Пробуждение: {wake_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC | {wake_kst.strftime('%Y-%m-%d %H:%M:%S')} KST"
                )
                
                await asyncio.sleep(sleep_time)
                
                actual_utc = datetime.now(timezone.utc)
                actual_kst = actual_utc.astimezone(timezone(timedelta(hours=9)))
                logger.info(f"[{label}] ПРОБУЖДЕНИЕ. Факт: {actual_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC | {actual_kst.strftime('%Y-%m-%d %H:%M:%S')} KST. Возобновляем парсинг...")
                continue
            
            loop_start = time.time()
            if self._is_paused_func and self._is_paused_func():
                await asyncio.sleep(tuner.current_cooldown)
                continue

            notices = await self.fetch_announcements(session, label)
            
            if notices is _RATE_LIMIT:
                new_cd = tuner.on_rate_limit()
                logger.warning(f"[429] Rate Limit on [{label}]. CD -> {new_cd:.2f}s. Penalty sleep {BANG_SLEEP_SEC}s...")
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
                    logger.info(f"[SPEEDUP] [{label}] New CD -> {speedup:.2f}s")

            elapsed = time.time() - loop_start
            await asyncio.sleep(max(0.0, tuner.current_cooldown - elapsed))

    async def run(self):
        proxy_count = len(self.session_manager.proxies)
        base_cd = max(MIN_COOLDOWN_SEC, self.poll_interval * proxy_count)
        
        await self.sync_initial_state()
        
        mode = "ADAPTIVE" if ADAPTIVE_ENABLED else "STATIC"
        logger.info(f"[RUN] STARTING WORKERS ({mode})")
        logger.info(f"  Target interval: {self.poll_interval}s")
        logger.info(f"  Cooldown per slot: {base_cd:.2f}s")
        
        workers = []
        offset_step = base_cd / proxy_count if proxy_count > 0 else 0
        for i, p in enumerate(self.session_manager.proxies):
            workers.append(asyncio.create_task(self._worker_loop(p, i * offset_step, base_cd)))
            
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            logger.info("[UPBIT] Получен сигнал отмены. Останавливаем воркеры...")
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        finally:
            await self.aclose()

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
        self._mock_req_count = 0
        
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
                    if MOCK_429:
                        self._mock_req_count += 1
                        if self._mock_req_count % 5 == 0:
                            logger.warning(f"[MOCK] BAN (429) at {self.current_delay:.2f}s (Hit {i})!")
                            logger.info(f"--- [MOCK TEST] Simulating penalty sleep {BANG_SLEEP_SEC}s ---")
                            await asyncio.sleep(1.0) 
                            continue

                    resp = await session.get(self.api_url, params=params)
                    if resp.status_code == 200:
                        if i % 10 == 0 or i == self.hits_per_step:
                            logger.info(f"  [{i}/{self.hits_per_step}] CD {self.current_delay:.2f}s OK")
                        await asyncio.sleep(self.current_delay)
                    elif resp.status_code == 429:
                        logger.warning(f"[429] REAL BAN at {self.current_delay:.2f}s (Hit {i})!")
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
        self._is_running = True
        
    async def aclose(self):
        """Метод для корректного выключения"""
        self._is_running = False
        logger.info("[MOCK] MOCK-генератор остановлен.")
        
    async def run(self):
        import random
        logger.info("[MOCK] GENERATOR STARTED")
        while self._is_running:
            await asyncio.sleep(self.poll_interval)
            if self._is_paused_func and self._is_paused_func(): continue
            
            sym = random.choice(["ETH"])
            if self._on_signal: await self._on_signal(sym, int(time.time() * 1000))

async def run_all_probers():
    for p in PROXIES:
        tester = WafLimitTester(p, PROBER_START, PROBER_STEP, PROBER_HITS)
        await tester.run()
        print("\n" + "="*50 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-waf", action="store_true")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    async def dummy_on_signal(s, ts): logger.info(f"SIGNAL: {s} at {ts}")
    def dummy_is_paused(): return False

    if args.test_waf:
        asyncio.run(run_all_probers())
    elif args.mock:
        asyncio.run(MockUpbitLiveMonitor(POLL_INTERVAL_SEC, dummy_on_signal, dummy_is_paused).run())
    else:
        asyncio.run(UpbitLiveMonitor(POLL_INTERVAL_SEC, PROXIES, dummy_on_signal, dummy_is_paused).run())


# python -m ENTRY.upbit_signal