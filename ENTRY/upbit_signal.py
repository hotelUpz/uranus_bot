# ============================================================
# FILE: ENTRY/upbit_signal.py
# ============================================================

from __future__ import annotations

import asyncio
import re
import json
import os
import time
from datetime import datetime, timezone, timedelta

# Импортируем curl_cffi вместо aiohttp
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.errors import RequestsError

from c_log import UnifiedLogger

# ==========================================
# 1. НАСТРОЙКИ ЛОГИРОВАНИЯ
# ==========================================
logger = UnifiedLogger("upbit_signal")

# ==========================================
# 2. УМНЫЙ МЕНЕДЖЕР ПРОКСИ И СЕССИЙ (CURL_CFFI)
# ==========================================
WAIT_FOR_GLOBAL_RETRY_SEC = 20
BANG_SLEEP_SEC           = 30    # Штрафной слип при 429
N_STABLE_BEFORE_SPEEDUP  = 10    # Сколько успешных запросов нужно, чтобы ускориться
COOLDOWN_STEP_SEC        = 1.0   # Шаг снижения кулдауна
MIN_COOLDOWN_SEC         = 1.0   # Нижняя граница кулдауна (рискованно идти ниже)

_COOLDOWN   = object()   # Sentinel: все слоты на перезарядке
_EMPTY_POOL = object()   # Sentinel: нет прокси в пуле
_RATE_LIMIT = object()   # Sentinel: пришел 429, нужен штрафной слип

# Идеальные заголовки для имитации браузера. 
# Библиотека curl_cffi сама сделает правильный TLS-отпечаток, 
# но WAF также смотрит на HTTP-заголовки.
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
# АДАПТИВНЫЙ РЕГУЛЯТОР КУЛДАУНА
# ==========================================
class AdaptiveCooldownTuner:
    """
    Следит за успехами/ошибками запросов и плавно
    снижает кулдаун (ускоряет опрос) при стабильной работе,
    а при 429 — немедленно откатывает его на исходное значение.
    """
    def __init__(self, initial_cooldown: float):
        self.initial_cooldown  = initial_cooldown
        self.current_cooldown  = initial_cooldown
        self._streak           = 0  # Счетчик успешных тиков подряд

    def on_success(self) -> float | None:
        """Вызывать при HTTP 200. Возвращает новый кулдаун если он изменился, иначе None."""
        self._streak += 1
        if self._streak >= N_STABLE_BEFORE_SPEEDUP:
            self._streak = 0
            new_cd = max(MIN_COOLDOWN_SEC, self.current_cooldown - COOLDOWN_STEP_SEC)
            if new_cd != self.current_cooldown:
                self.current_cooldown = new_cd
                return new_cd
        return None

    def on_rate_limit(self) -> float:
        """Вызывать при HTTP 429. Сбрасывает стрик и откатывает кулдаун."""
        self._streak = 0
        self.current_cooldown = self.initial_cooldown
        return self.current_cooldown

class SmartSessionManager:
    def __init__(self, proxies: list, cooldown_sec: float = 8.0, poll_interval_sec: float = 1.0):
        raw_proxies = [p for p in proxies]
        if None not in raw_proxies and not raw_proxies:
            raw_proxies = [None]
            
        self.initial_proxies = list(raw_proxies)
        self.proxies = list(raw_proxies)
        
        self.sessions: dict[str | None, cffi_requests.AsyncSession] = {}
        self.last_used: dict[str | None, float] = {p: 0.0 for p in self.proxies}
        
        self.cooldown = cooldown_sec
        self.poll_interval = poll_interval_sec
        self.effective_interval = self._calc_interval()

    def _calc_interval(self) -> float:
        count = len(self.proxies)
        if count == 0: 
            return self.cooldown
        return max(self.poll_interval, self.cooldown / count)

    def _format_proxy_dict(self, proxy_url: str | None) -> dict | None:
        if not proxy_url:
            return None
        return {"http": proxy_url, "https": proxy_url}

    async def get_ready_session(self) -> tuple[str | None, cffi_requests.AsyncSession | None] | object:
        if not self.proxies:
            return _EMPTY_POOL

        now = time.time()
        for proxy in self.proxies:
            if now - self.last_used.get(proxy, 0.0) >= self.cooldown:
                self.last_used[proxy] = now
                
                # Создаем сессию, если её нет.
                # impersonate="chrome120" — это магия, которая копирует JA3 отпечаток и HTTP/2 настройки Хрома
                if proxy not in self.sessions:
                    proxy_dict = self._format_proxy_dict(proxy)
                    self.sessions[proxy] = cffi_requests.AsyncSession(
                        impersonate="chrome120", 
                        proxies=proxy_dict,
                        headers=DEFAULT_HEADERS,
                        timeout=5.0
                    )
                    
                return proxy, self.sessions[proxy]
                
        return _COOLDOWN

    async def remove_proxy(self, proxy: str | None):
        if proxy in self.proxies:
            self.proxies.remove(proxy)
            
        if proxy in self.sessions:
            try:
                self.sessions[proxy].close() # В curl_cffi закрытие синхронное
            except Exception:
                pass
            del self.sessions[proxy]
            
        if proxy in self.last_used:
            del self.last_used[proxy]
            
        old_interval = self.effective_interval
        self.effective_interval = self._calc_interval()
        logger.warning(f"🗑 Прокси '{proxy or 'Localhost'}' удален. Осталось слотов: {len(self.proxies)}. Интервал: {old_interval:.2f}с -> {self.effective_interval:.2f}с")

    async def restore_initial_pool(self):
        await self.close_all()
        self.proxies = list(self.initial_proxies)
        self.last_used = {p: 0.0 for p in self.proxies}
        self.effective_interval = self._calc_interval()
        logger.info(f"🔄 Пул прокси восстановлен до {len(self.proxies)} слотов.")

    async def close_all(self):
        for s in self.sessions.values():
            try:
                s.close()
            except Exception:
                pass
        self.sessions.clear()

# ==========================================
# 3. ОСНОВНОЙ КЛАСС МОНИТОРИНГА
# ==========================================
class UpbitLiveMonitor:
    def __init__(self, poll_interval_sec: float, proxies: list,
                 on_signal=None,
                 cooldown_sec: float = 8.0,
                 cache_file: str = "live_signals.json"):
        self._on_signal = on_signal
        self.poll_interval = poll_interval_sec
        self.api_url = "https://api-manager.upbit.com/api/v1/announcements"
        self.cache_file = cache_file
        self.cooldown_sec = cooldown_sec
        
        self.session_manager = SmartSessionManager(
            proxies=proxies,
            cooldown_sec=cooldown_sec,
            poll_interval_sec=poll_interval_sec
        )
        
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
                    
                for signal in self.signals_log:
                    if "symbol" in signal:
                        self.seen_symbols.add(signal["symbol"])
                        
                logger.info(f"💾 Восстановлено {len(self.seen_symbols)} монет из кеша.")
            except Exception as e:
                logger.error(f"Ошибка чтения кеша: {e}")

    def _save_cache(self):
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.signals_log, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Ошибка сохранения JSON: {e}")

    def _parse_iso_to_ms(self, dt_str: str) -> int:
        if not dt_str: return 0
        KST = timezone(timedelta(hours=9)) 
        try:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
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

    async def fetch_latest_announcements(self, time_since_last: float) -> list[dict]:
        res = await self.session_manager.get_ready_session()
        
        if res is _EMPTY_POOL:
            return _EMPTY_POOL
            
        if res is _COOLDOWN:
            return []

        proxy, session = res
        params = {"category": "trade", "page": 1, "per_page": 10, "os": "web"}
        
        for attempt in range(2):
            try:
                # В curl_cffi параметры и вызовы выглядят как в библиотеке requests
                response = await session.get(self.api_url, params=params)
                status = response.status_code
                
                if status == 200:
                    logger.info(f"[{proxy or 'Localhost'}] Успех: HTTP 200 | Прошло с пред. запроса: {time_since_last:.3f}с")
                    data = response.json()
                    return data.get("data", {}).get("notices", [])
                    
                elif status == 429:
                    logger.warning(f"🛑 HTTP 429 (Бан IP): Прокси {proxy or 'Локальный IP'}. Штрафной слип {BANG_SLEEP_SEC}с")
                    return _RATE_LIMIT
                    
                else:
                    if attempt == 0:
                        logger.warning(f"[{proxy or 'Localhost'}] HTTP Error {status}. Ретрай...")
                        await asyncio.sleep(0.5)
                        continue
                    else:
                        logger.error(f"[{proxy or 'Localhost'}] HTTP Error {status} (попытка 2). Удаляем прокси.")
                        await self.session_manager.remove_proxy(proxy)
                        return []

            except RequestsError as e:
                # Ошибки уровня curl_cffi (таймауты, разрывы)
                if attempt == 0:
                    logger.warning(f"[{proxy or 'Localhost'}] Сетевая ошибка: {e}. Ретрай...")
                    await asyncio.sleep(0.5)
                else:
                    logger.error(f"[{proxy or 'Localhost'}] Сетевой отказ (попытка 2): {e}. Удаляем прокси.")
                    await self.session_manager.remove_proxy(proxy)
                    return []
                    
        return []

    async def process_new_listing(self, notice: dict, symbol: str, is_startup: bool = False):
        title = notice.get("title", "")
        listed_at = notice.get("first_listed_at") or notice.get("listed_at")
        
        if not listed_at:
            return

        announce_ms = self._parse_iso_to_ms(listed_at)
        announce_str = datetime.fromtimestamp(announce_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        
        signal_status = "INITIAL_SIGNAL" if is_startup else "NEW_SIGNAL"
        
        signal_data = {
            "symbol": symbol,
            "announce_ts_ms": announce_ms,
            "announce_ts_str": announce_str,
            "title": title,
            "status": signal_status
        }
        
        self.seen_symbols.add(symbol)
        self.signals_log.append(signal_data)
        self._save_cache()

        if not is_startup and self._on_signal:
            await self._on_signal(symbol)
            logger.debug(
                f"\n{'='*60}\n"
                f"🚀 СИГНАЛ ПАМПА: ВЫШЕЛ АНОНС ЛИСТИНГА [{symbol}] 🚀\n"
                f"Метка времени: {announce_str}\n"
                f"{'='*60}\n"
            )

    async def run(self):
        tuner = AdaptiveCooldownTuner(self.cooldown_sec)
        
        logger.info(f"🚀 МОНИТОРИНГ UPBIT ЗАПУЩЕН (CURL_CFFI Mode)")
        logger.info(f"   - Слотов в пуле (IP): {len(self.session_manager.proxies)}")
        logger.info(f"   - Начальный кулдаун: {tuner.current_cooldown:.1f}с | Min: {MIN_COOLDOWN_SEC}с | Шаг: {COOLDOWN_STEP_SEC}с")
        logger.info(f"   - Ускорение: каждые {N_STABLE_BEFORE_SPEEDUP} успехов подряд -1с кулдауна")
        logger.info(f"   - При 429: штраф {BANG_SLEEP_SEC}с + откат кулдауна")
        logger.info(f"   - ФАКТИЧЕСКИЙ ИНТЕРВАЛ: {self.session_manager.effective_interval:.2f}с")
        
        is_startup = True
        last_request_time = time.time()
        
        try:
            while True:
                start_time = time.time()
                time_since_last = start_time - last_request_time
                last_request_time = start_time
                
                notices = await self.fetch_latest_announcements(time_since_last)
                
                if notices is _EMPTY_POOL:
                    logger.error("❌ ПУЛ ПРОКСИ ИСТОЩЕН (Сеть полностью упала). Ждем 20 секунд...")
                    await asyncio.sleep(WAIT_FOR_GLOBAL_RETRY_SEC)
                    await self.session_manager.restore_initial_pool()
                    is_startup = True 
                    last_request_time = time.time()
                    continue

                if notices is _RATE_LIMIT:
                    reset_cd = tuner.on_rate_limit()
                    self.session_manager.cooldown = reset_cd
                    self.session_manager.effective_interval = self.session_manager._calc_interval()
                    logger.warning(f"♻️ Кулдаун сброшен до {reset_cd:.1f}с. Штрафной слип {BANG_SLEEP_SEC}с...")
                    await asyncio.sleep(BANG_SLEEP_SEC)
                    last_request_time = time.time()
                    continue
                
                for notice in notices:
                    n_id = notice.get("id")
                    if not n_id or n_id in self.seen_ids:
                        continue
                        
                    self.seen_ids.add(n_id)
                    title = notice.get("title", "")
                    
                    if any(kw in title for kw in self.keywords):
                        symbol = self._extract_symbol(title)
                        if symbol and symbol not in self.seen_symbols:
                            await self.process_new_listing(notice, symbol, is_startup)
                
                if is_startup:
                    logger.info("Синхронизация завершена. Ожидаю новые анонсы...")
                    is_startup = False

                # Адаптация кулдауна после успешного тика
                new_cd = tuner.on_success()
                if new_cd is not None:
                    self.session_manager.cooldown = new_cd
                    self.session_manager.effective_interval = self.session_manager._calc_interval()
                    logger.info(f"⚡ Кулдаун снижен до {new_cd:.1f}с (стрик {N_STABLE_BEFORE_SPEEDUP} успехов). Интервал: {self.session_manager.effective_interval:.2f}с")
                
                elapsed = time.time() - start_time
                sleep_time = max(0.0, self.session_manager.effective_interval - elapsed)
                await asyncio.sleep(sleep_time)
        finally:
            await self.session_manager.close_all()

# ==========================================
# 4. ТЕСТИРОВЩИК ЛИМИТОВ WAF (WAF PROBER)
# ==========================================
class WafLimitTester:
    def __init__(self, proxy: str | None = None, start_delay: float = 6.0, step: float = 0.5):
        self.proxy = proxy
        self.current_delay = start_delay
        self.step = step
        self.api_url = "https://api-manager.upbit.com/api/v1/announcements"
        
    async def run(self):
        logger.info(f"🧪 ЗАПУСК WAF PROBER (IP: {'Локальный' if not self.proxy else self.proxy})")
        logger.info(f"Начальная задержка: {self.current_delay}с. Шаг снижения: {self.step}с")
        
        proxy_dict = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        
        session = cffi_requests.AsyncSession(
            impersonate="chrome120", 
            proxies=proxy_dict,
            headers=DEFAULT_HEADERS
        )
        
        params = {"category": "trade", "page": 1, "per_page": 1, "os": "web"}
        
        try:
            while self.current_delay > 0:
                logger.info(f"⏳ Тест с паузой {self.current_delay:.1f} сек...")
                
                response = await session.get(self.api_url, params=params)
                status = response.status_code
                
                if status == 200:
                    logger.info(f"✅ Успех (HTTP 200) при {self.current_delay:.1f}с. Снижаем задержку.")
                    await asyncio.sleep(self.current_delay)
                    self.current_delay -= self.step
                elif status == 429:
                    logger.warning(f"🛑 БАН (HTTP 429) поймали на задержке: {self.current_delay:.1f}с!")
                    logger.info(f"🎯 ИТОГ: Физический предел для этого IP находится чуть выше {self.current_delay:.1f} секунд.")
                    break
                elif status == 403:
                    logger.error(f"🧱 Глухой блок от WAF (HTTP 403) при {self.current_delay:.1f}с. Отпечаток спалился или IP в блэклисте.")
                    break
                else:
                    logger.error(f"❓ Неизвестный статус {status}")
                    break
        finally:
            session.close()

# ==========================================
# 5. ТОЧКА ВХОДА
# ==========================================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-waf", action="store_true", help="Запустить скрипт поиска минимального пинга (WAF Prober)")
    args = parser.parse_args()

    # Если запускаем: python upbit_signal.py --test-waf
    if args.test_waf:
        tester = WafLimitTester(proxy=None, start_delay=6.0, step=0.5)
        try:
            asyncio.run(tester.run())
        except KeyboardInterrupt:
            logger.info("Тест прерван пользователем.")
    
    # Режим нормального парсинга: python upbit_signal.py
    else:
        async def dummy_on_signal(symbol: str):
            logger.info(f"🔔 СИГНАЛ ПЕРЕДАН ДАЛЬШЕ: {symbol}")

        MY_PROXIES = [None]
        TARGET_POLL_INTERVAL = 1.0 
        
        monitor = UpbitLiveMonitor(
            poll_interval_sec=TARGET_POLL_INTERVAL, 
            proxies=MY_PROXIES,
            on_signal=dummy_on_signal,
            cooldown_sec=5.0  # Будет подстраиваться из твоих тестов
        ) 
        
        try:
            asyncio.run(monitor.run())
        except KeyboardInterrupt:
            logger.info("Выход. Парсер остановлен.")


# ==========================================
# 5. MOCK ГЕНЕРАТОР (ДЛЯ ТЕСТОВ СИСТЕМЫ)
# ==========================================
class MockUpbitLiveMonitor(UpbitLiveMonitor):
    """
    Фейковый монитор для тестирования обработчиков сигналов.
    Не делает реальных HTTP-запросов, просто стреляет заданными тикерами.
    """
    def __init__(self, poll_interval_sec: float, on_signal=None, symbols_to_mock=None):
        # Намеренно не вызываем super().__init__(), чтобы не поднимать сессии curl_cffi
        self._on_signal = on_signal
        self.poll_interval = poll_interval_sec
        self.symbols_to_mock = symbols_to_mock or ["BTC", "ETH", "XRP", "SOL", "DOGE"]
        
    async def run(self):
        import random
        logger.info(f"🟢 ЗАПУЩЕН MOCK-ГЕНЕРАТОР СИГНАЛОВ. Интервал: {self.poll_interval} сек.")
        while True:
            await asyncio.sleep(self.poll_interval)
            symbol = random.choice(self.symbols_to_mock)
            logger.debug(f"🛠 [MOCK] Генерирую фейковый сигнал листинга для {symbol}...")
            if self._on_signal:
                await self._on_signal(symbol)

# python -m ENTRY.upbit_signal