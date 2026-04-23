# ============================================================
# FILE: ENTRY/upbit_signal.py
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
        logger.warning(f"⚠️ Ошибка загрузки cfg.json: {e}. Используем дефолты.")
        return {}

CFG = load_cfg()

# Параметры из конфига
WAIT_FOR_GLOBAL_RETRY_SEC     = CFG.get("upbit_wait_for_global_retry_sec", 20)
BANG_SLEEP_SEC                = CFG.get("upbit_bang_sleep_sec", 30)
N_STABLE_BEFORE_SPEEDUP       = CFG.get("upbit_n_stable_before_speedup", 10)
COOLDOWN_STEP_SEC             = CFG.get("upbit_cooldown_step_sec", 1.0)
MIN_COOLDOWN_SEC              = CFG.get("upbit_min_cooldown_sec", 1.0)
ADAPTIVE_SPEEDUP_ENABLED      = CFG.get("upbit_adaptive_speedup_enabled", True)

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
        self.initial_cooldown  = initial_cooldown
        self.current_cooldown  = initial_cooldown
        self._streak           = 0

    def on_success(self) -> float | None:
        if not ADAPTIVE_SPEEDUP_ENABLED:
            return None
        self._streak += 1
        if self._streak >= N_STABLE_BEFORE_SPEEDUP:
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
    def __init__(self, proxies: list, cooldown_sec: float = 5.0):
        self.proxies = list(proxies) if proxies else [None]
        self.sessions: dict[str | None, cffi_requests.AsyncSession] = {}
        self.last_used: dict[str | None, float] = {p: 0.0 for p in self.proxies}
        self.cooldown = cooldown_sec

    def _create_session(self, proxy: str | None):
        proxy_dict = {"http": proxy, "https": proxy} if proxy else None
        return cffi_requests.AsyncSession(
            impersonate="chrome120", 
            proxies=proxy_dict,
            headers=DEFAULT_HEADERS,
            timeout=5.0
        )

    def get_next_ready_proxy(self) -> tuple[str | None, cffi_requests.AsyncSession] | None:
        now = time.time()
        for proxy in self.proxies:
            if now - self.last_used.get(proxy, 0.0) >= self.cooldown:
                if proxy not in self.sessions:
                    self.sessions[proxy] = self._create_session(proxy)
                self.last_used[proxy] = now
                return proxy, self.sessions[proxy]
        return None

    def close_all(self):
        for s in self.sessions.values():
            try: s.close()
            except: pass
        self.sessions.clear()

# ==========================================
# 4. ОСНОВНОЙ КЛАСС МОНИТОРИНГА
# ==========================================
class UpbitLiveMonitor:
    def __init__(self, poll_interval_sec: float, proxies: list,
                 on_signal=None,
                 cooldown_sec: float = 5.0,
                 cache_file: str = "live_signals.json",
                 is_paused_func=None):  # <--- ВОССТАНОВЛЕНО
        self._on_signal = on_signal
        self._is_paused_func = is_paused_func  # <--- СОХРАНЯЕМ ФУНКЦИЮ
        self.poll_interval = poll_interval_sec
        self.api_url = "https://api-manager.upbit.com/api/v1/announcements"
        self.cache_file = cache_file
        
        self.session_manager = SmartSessionManager(proxies, cooldown_sec)
        self.tuner = AdaptiveCooldownTuner(cooldown_sec)
        
        self.keywords = ["Market Support for", "신규 거래지원", "디지털 자산 추가"]
        self.seen_symbols: set[str] = set()
        self.seen_ids: set[int] = set()
        self.signals_log: list[dict] = []
        
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
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.signals_log, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Ошибка JSON: {e}")

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
        return None

    async def fetch_announcements(self, session: cffi_requests.AsyncSession, proxy_label: str):
        params = {"category": "trade", "page": 1, "per_page": 10, "os": "web"}
        try:
            response = await session.get(self.api_url, params=params)
            if response.status_code == 200:
                return response.json().get("data", {}).get("notices", [])
            elif response.status_code == 429:
                return _RATE_LIMIT
            return []
        except RequestsError as e:
            logger.warning(f"[{proxy_label}] Сетевая ошибка: {e}")
            return []

    async def run(self):
        logger.info(f"🚀 ЗАПУСК (Системный интервал: {self.poll_interval}с)")
        is_startup = True
        
        try:
            while True:
                loop_start = time.time()
                
                # --- ЛОГИКА ПАУЗЫ (ВОССТАНОВЛЕНА) ---
                if self._is_paused_func and self._is_paused_func():
                    # Если бот на паузе, просто ждем системный интервал и проверяем снова
                    await asyncio.sleep(self.poll_interval)
                    continue
                # ------------------------------------
                
                res = self.session_manager.get_next_ready_proxy()
                if not res:
                    await asyncio.sleep(0.05)
                    continue
                
                proxy, session = res
                proxy_label = proxy or "Localhost"
                
                notices = await self.fetch_announcements(session, proxy_label)
                
                if notices is _RATE_LIMIT:
                    new_cd = self.tuner.on_rate_limit()
                    self.session_manager.cooldown = new_cd
                    logger.warning(f"🛑 429 на {proxy_label}. CD сброшен до {new_cd}с. Пауза {BANG_SLEEP_SEC}с")
                    await asyncio.sleep(BANG_SLEEP_SEC)
                    continue

                if isinstance(notices, list):
                    for notice in notices:
                        n_id = notice.get("id")
                        if not n_id or n_id in self.seen_ids: continue
                        
                        self.seen_ids.add(n_id)
                        title = notice.get("title", "")
                        
                        if any(kw in title for kw in self.keywords):
                            symbol = self._extract_symbol(title)
                            if symbol and symbol not in self.seen_symbols:
                                await self.process_new_listing(notice, symbol, is_startup)

                    speedup = self.tuner.on_success()
                    if speedup:
                        self.session_manager.cooldown = speedup
                        logger.info(f"⚡ Адаптивное ускорение: CD прокси -> {speedup}с")

                if is_startup:
                    logger.info("✅ Синхронизация завершена.")
                    is_startup = False

                elapsed = time.time() - loop_start
                sleep_time = max(0.0, self.poll_interval - elapsed)
                await asyncio.sleep(sleep_time)

        finally:
            self.session_manager.close_all()

    async def process_new_listing(self, notice: dict, symbol: str, is_startup: bool):
        announce_ms = self._parse_iso_to_ms(notice.get("first_listed_at") or notice.get("listed_at"))
        announce_str = datetime.fromtimestamp(announce_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        
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

# ==========================================
# 5. ТЕСТИРОВЩИК ЛИМИТОВ (PROBER)
# ==========================================
class WafLimitTester:
    def __init__(self, proxy: str | None = None, start_delay: float = 6.0, step: float = 0.5):
        self.proxy = proxy
        self.current_delay = start_delay
        self.step = step
        self.api_url = "https://api-manager.upbit.com/api/v1/announcements"
        
    async def run(self):
        logger.info(f"🧪 ЗАПУСК WAF PROBER (IP: {self.proxy or 'Local'})")
        proxy_dict = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        session = cffi_requests.AsyncSession(impersonate="chrome120", proxies=proxy_dict, headers=DEFAULT_HEADERS)
        
        try:
            while self.current_delay > 0:
                logger.info(f"⏳ Проверка: задержка {self.current_delay:.1f}с...")
                resp = await session.get(self.api_url, params={"category": "trade", "page": 1, "per_page": 1})
                if resp.status_code == 200:
                    logger.info(f"✅ OK (200)")
                    await asyncio.sleep(self.current_delay)
                    self.current_delay -= self.step
                elif resp.status_code == 429:
                    logger.warning(f"🛑 БАН (429) на {self.current_delay:.1f}с!")
                    break
                else:
                    logger.error(f"❌ Статус {resp.status_code}")
                    break
        finally:
            session.close()

# ==========================================
# 6. MOCK ГЕНЕРАТОР
# ==========================================
class MockUpbitLiveMonitor(UpbitLiveMonitor):
    def __init__(self, poll_interval_sec: float, on_signal=None, symbols_to_mock=None, is_paused_func=None):
        self._on_signal = on_signal
        self._is_paused_func = is_paused_func # <--- И ТУТ ВОССТАНОВЛЕНО
        self.poll_interval = poll_interval_sec
        self.symbols_to_mock = symbols_to_mock or ["BTC", "ETH", "SOL"]
        
    async def run(self):
        import random
        logger.info("🟢 MOCK ГЕНЕРАТОР ЗАПУЩЕН")
        while True:
            await asyncio.sleep(self.poll_interval)
            
            # --- ЛОГИКА ПАУЗЫ ДЛЯ МОКА ---
            if self._is_paused_func and self._is_paused_func():
                continue
            # -----------------------------
            
            s = random.choice(self.symbols_to_mock)
            if self._on_signal: await self._on_signal(s)

# ==========================================
# 7. CLI ENTRY POINT
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-waf", action="store_true", help="Найти минимальную задержку")
    parser.add_argument("--mock", action="store_true", help="Запустить генератор фейковых сигналов")
    args = parser.parse_args()

    async def dummy_on_signal(symbol: str):
        logger.info(f"🔔 СИГНАЛ ПЕРЕДАН: {symbol}")

    # Имитация функции паузы для теста
    def dummy_is_paused():
        return False

    if args.test_waf:
        tester = WafLimitTester(
            proxy=None, 
            start_delay=CFG.get("upbit_prober_start_delay", 6.0),
            step=CFG.get("upbit_prober_step", 0.5)
        )
        asyncio.run(tester.run())
            
    elif args.mock:
        monitor = MockUpbitLiveMonitor(
            poll_interval_sec=2.0, 
            on_signal=dummy_on_signal,
            is_paused_func=dummy_is_paused
        )
        asyncio.run(monitor.run())
            
    else:
        # Боевой запуск из конфига
        monitor = UpbitLiveMonitor(
            poll_interval_sec=CFG.get("upbit_signal_poll_interval", 0.5),
            proxies=CFG.get("upbit_proxies", [None]),
            cooldown_sec=CFG.get("upbit_cooldown_sec", 5.0),
            is_paused_func=dummy_is_paused
        ) 
        asyncio.run(monitor.run())