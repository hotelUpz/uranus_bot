# ============================================================
# FILE: ENTRY/upbit_signal.py
# ============================================================

from __future__ import annotations

import asyncio
import aiohttp
import logging
import re
import json
import os
import time
from datetime import datetime, timezone, timedelta

from c_log import UnifiedLogger

# ==========================================
# 1. НАСТРОЙКИ ЛОГИРОВАНИЯ
# ==========================================
logger = UnifiedLogger("upbit_signal")

# ==========================================
# 2. УМНЫЙ МЕНЕДЖЕР ПРОКСИ (ЗАЩИТА ОТ 429)
# ==========================================
_COOLDOWN = object()  # Sentinel: все слоты на перезарядке

class SmartProxyManager:
    def __init__(self, proxies: list, cooldown_sec: float = 8.0):
        """
        Менеджер ротации прокси с кулдауном.

        proxies: список адресов. null/None = локальный IP (без прокси).
        Пример: [null, "http://user:pass@1.2.3.4:8080"] — 2 слота,
        один из них — локальная машина.

        cooldown_sec: минимальная пауза перед повторным использованием
        одного слота. Upbit банит быстрее 1 запроса в 10 сек на IP.
        Для poll_interval_sec=1 нужно floor(cooldown_sec / poll_interval_sec) + 1 слотов.
        """
        # Ключ = адрес (str или None), значение = timestamp последнего использования
        self.proxies: dict = {proxy: 0.0 for proxy in proxies}
        self.cooldown = cooldown_sec

    def get_ready_proxy(self):
        """
        Возвращает:
          - str  — адрес прокси
          - None — слот «локальный IP» готов к использованию
          - _COOLDOWN — все слоты на перезарядке, пропустить такт
        """
        if not self.proxies:
            # Список пуст — всегда работаем с локального IP без кулдауна
            return None

        now = time.time()
        for proxy, last_used in self.proxies.items():
            if now - last_used >= self.cooldown:
                self.proxies[proxy] = now
                return proxy   # None или строка — оба валидны для aiohttp

        return _COOLDOWN

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
        
        # Инициализация менеджера прокси
        self.proxy_manager = SmartProxyManager(
            proxies=proxies,
            cooldown_sec=cooldown_sec,
        )
        
        self.keywords = [
            "Market Support for",
            "신규 거래지원", 
            "디지털 자산 추가"
        ]
        
        self.seen_symbols: set[str] = set()
        self.seen_ids: set[int] = set()
        self.signals_log: list[dict] = []
        
        self._load_cache()

    def _load_cache(self):
        """Загружает базу уже известных монет, чтобы не спамить сигналы при рестарте."""
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
        """Сохраняет лог в JSON."""
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.signals_log, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Ошибка сохранения JSON: {e}")

    def _parse_iso_to_ms(self, dt_str: str) -> int:
        """Переводит корейское время (KST) в UTC миллисекунды."""
        if not dt_str: return 0
        KST = timezone(timedelta(hours=9)) # Upbit отдает время в KST (UTC+9)
        try:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
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
        """Достает тикер монеты из заголовка."""
        match = re.search(r"\(([^)]+)\)", title)
        if match:
            symbol = match.group(1).strip().upper()
            return re.sub(r"\(.*\)", "", symbol).strip() or None
        if "for" in title.lower():
            idx = title.lower().find("for")
            tail = title[idx + 3:].strip()
            if tail: return tail.split()[0].upper()
        return None

    async def fetch_latest_announcements(self, session: aiohttp.ClientSession) -> list[dict]:
        """Запрашивает API через свободный прокси."""
        proxy = self.proxy_manager.get_ready_proxy()

        # Все слоты на перезарядке — пропускаем такт
        if proxy is _COOLDOWN:
            return []

        params = {"category": "trade", "page": 1, "per_page": 10, "os": "web"}
        
        try:
            # timeout=3 важен, чтобы зависший прокси не застопорил весь парсер
            async with session.get(self.api_url, params=params, proxy=proxy, timeout=aiohttp.ClientTimeout(total=3)) as response:
                if response.status == 429:
                    logger.warning(f"HTTP 429 (Бан IP): Прокси {proxy or 'Локальный IP'}.")
                    return []
                if response.status != 200:
                    logger.error(f"HTTP Error {response.status} на прокси {proxy}")
                    return []
                data = await response.json()
                return data.get("data", {}).get("notices", [])
        except Exception as e:
            # Прокси могут отваливаться по таймауту, это нормально, просто игнорируем
            return []

    async def process_new_listing(self, notice: dict, symbol: str, is_startup: bool = False):
        """Обрабатывает найденный анонс и сохраняет сигнал."""
        title = notice.get("title", "")
        listed_at = notice.get("listed_at")
        
        if not listed_at:
            return

        announce_ms = self._parse_iso_to_ms(listed_at)
        announce_str = datetime.fromtimestamp(announce_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        phemex_symbol = f"{symbol}USDT"
        
        signal_status = "INITIAL_SIGNAL" if is_startup else "NEW_SIGNAL"
        
        signal_data = {
            "symbol": symbol,
            "phemex_symbol": phemex_symbol,
            "announce_ts_ms": announce_ms,
            "announce_ts_str": announce_str,
            "title": title,
            "status": signal_status
        }
        
        self.seen_symbols.add(symbol)
        self.signals_log.append(signal_data)
        self._save_cache()

        if not is_startup and self._on_signal:
            await self._on_signal(symbol)   # <-- бросаем наружу
            logger.debug(
                f"\n{'='*60}\n"
                f"🚀 СИГНАЛ ПАМПА: ВЫШЕЛ АНОНС ЛИСТИНГА [{symbol}] 🚀\n"
                f"Метка времени: {announce_str}\n"
                f"{'='*60}\n"
            )

    async def run(self):
        """Главный бесконечный цикл."""
        proxy_count = len(self.proxy_manager.proxies)
        
        # Математика "как для взрослых": 
        # Если прокси мало, мы физически не можем стучать чаще чем (cooldown / count)
        if proxy_count > 0:
            actual_limit = self.cooldown_sec / proxy_count
            effective_interval = max(self.poll_interval, actual_limit)
        else:
            effective_interval = self.cooldown_sec

        logger.info(f"🚀 МОНИТОРИНГ UPBIT ЗАПУЩЕН")
        logger.info(f"   - Желаемый интервал: {self.poll_interval}с")
        logger.info(f"   - Слотов в пуле (IP): {proxy_count}")
        logger.info(f"   - Кулдаун на 1 IP: {self.cooldown_sec}с")
        
        if effective_interval > self.poll_interval:
            logger.warning(f"   - ⚠️ ФАКТИЧЕСКИЙ ИНТЕРВАЛ: {effective_interval}с (ограничено кол-вом прокси)")
        else:
            logger.info(f"   - ФАКТИЧЕСКИЙ ИНТЕРВАЛ: {effective_interval}с")

        is_startup = True
        
        # Keep-Alive соединение для ускорения запросов
        connector = aiohttp.TCPConnector(force_close=False, limit=100)
        async with aiohttp.ClientSession(connector=connector) as session:
            while True:
                start_time = time.time()
                
                notices = await self.fetch_latest_announcements(session)
                
                for notice in notices:
                    n_id = notice.get("id")
                    if not n_id or n_id in self.seen_ids:
                        continue
                        
                    self.seen_ids.add(n_id)
                    title = notice.get("title", "")
                    
                    if any(kw in title for kw in self.keywords):
                        symbol = self._extract_symbol(title)
                        if not symbol:
                            continue
                            
                        if symbol not in self.seen_symbols:
                            await self.process_new_listing(notice, symbol, is_startup)
                
                if is_startup:
                    logger.info("Синхронизация завершена. Ожидаю новые анонсы...")
                    is_startup = False
                
                # Высчитываем, сколько еще нужно поспать до эффективного интервала
                elapsed = time.time() - start_time
                sleep_time = max(0.0, effective_interval - elapsed)
                await asyncio.sleep(sleep_time)

# # ==========================================
# # 4. ЗАПУСК
# # ==========================================
# if __name__ == "__main__":
#     # Список твоих IPv4 прокси. 
#     # Формат: 'http://login:pass@ip:port' или 'socks5://login:pass@ip:port'
#     # Чтобы протестировать без прокси (с локального IP), оставь список пустым: MY_PROXIES = []
#     MY_PROXIES = [
#         # "http://user1:pass1@192.168.1.1:8000",
#         # "http://user2:pass2@192.168.1.2:8000",
#     ]
    
#     # ИНТЕРВАЛ В СЕКУНДАХ. 
#     # Если прокси пустой (работаем с локального IP) - ставь не меньше 11.0
#     # Если есть 20+ прокси - ставь 0.5
#     TARGET_POLL_INTERVAL = 8.0 if not MY_PROXIES else 1.0
    
#     monitor = UpbitLiveMonitor(poll_interval_sec=TARGET_POLL_INTERVAL, proxies=MY_PROXIES) 
    
#     try:
#         asyncio.run(monitor.run())
#     except KeyboardInterrupt:
#         logger.info("Выход. Парсер остановлен.")

# ==========================================
# 5. MOCK ГЕНЕРАТОР (ДЛЯ ТЕСТОВ)
# ==========================================
class MockUpbitLiveMonitor(UpbitLiveMonitor):
    def __init__(self, poll_interval_sec: float, on_signal=None, symbols_to_mock=None):
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