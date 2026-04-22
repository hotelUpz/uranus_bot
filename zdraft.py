# # # ============================================================
# # # FILE: ENTRY/upbit_signal.py
# # # ============================================================

# # from __future__ import annotations

# # import asyncio
# # import aiohttp
# # import logging
# # import re
# # import json
# # import os
# # import time
# # from datetime import datetime, timezone, timedelta

# # from c_log import UnifiedLogger

# # # ==========================================
# # # 1. НАСТРОЙКИ ЛОГИРОВАНИЯ
# # # ==========================================
# # logger = UnifiedLogger("upbit_signal")

# # # ==========================================
# # # 2. УМНЫЙ МЕНЕДЖЕР ПРОКСИ И СЕССИЙ
# # # ==========================================
# # WAIT_FOR_GLOBAL_RETRY_SEC = 20
# # _COOLDOWN = object()  # Sentinel: все слоты на перезарядке
# # _EMPTY_POOL = object() # Sentinel: нет прокси в пуле

# # class SmartSessionManager:
# #     def __init__(self, proxies: list, cooldown_sec: float = 8.0, poll_interval_sec: float = 1.0):
# #         """
# #         Менеджер ротации прокси, который кэширует aiohttp-сессии.
# #         null/None = локальный IP (без прокси).
# #         """
# #         raw_proxies = [p for p in proxies]
# #         if None not in raw_proxies and not raw_proxies:
# #             raw_proxies = [None]
            
# #         self.initial_proxies = list(raw_proxies)
# #         self.proxies = list(raw_proxies)
        
# #         self.sessions: dict[str | None, aiohttp.ClientSession] = {}
# #         self.last_used: dict[str | None, float] = {p: 0.0 for p in self.proxies}
        
# #         self.cooldown = cooldown_sec
# #         self.poll_interval = poll_interval_sec
# #         self.effective_interval = self._calc_interval()

# #     def _calc_interval(self) -> float:
# #         count = len(self.proxies)
# #         if count == 0: 
# #             return self.cooldown
# #         return max(self.poll_interval, self.cooldown / count)

# #     async def get_ready_session(self) -> tuple[str | None, aiohttp.ClientSession | None] | object:
# #         if not self.proxies:
# #             return _EMPTY_POOL

# #         now = time.time()
# #         for proxy in self.proxies:
# #             if now - self.last_used.get(proxy, 0.0) >= self.cooldown:
# #                 self.last_used[proxy] = now
                
# #                 # Создаем сессию, если её нет или она закрыта
# #                 if proxy not in self.sessions or self.sessions[proxy].closed:
# #                     connector = aiohttp.TCPConnector(force_close=False, limit=100)
# #                     self.sessions[proxy] = aiohttp.ClientSession(connector=connector)
                    
# #                 return proxy, self.sessions[proxy]
                
# #         return _COOLDOWN

# #     async def remove_proxy(self, proxy: str | None):
# #         if proxy in self.proxies:
# #             self.proxies.remove(proxy)
            
# #         if proxy in self.sessions:
# #             try:
# #                 await self.sessions[proxy].close()
# #             except Exception:
# #                 pass
# #             del self.sessions[proxy]
            
# #         if proxy in self.last_used:
# #             del self.last_used[proxy]
            
# #         old_interval = self.effective_interval
# #         self.effective_interval = self._calc_interval()
# #         logger.warning(f"🗑 Прокси '{proxy or 'Localhost'}' удален. Осталось слотов: {len(self.proxies)}. Интервал: {old_interval:.2f}с -> {self.effective_interval:.2f}с")

# #     async def restore_initial_pool(self):
# #         """Восстанавливает исходный пул прокси при полном падении сети"""
# #         await self.close_all()
# #         self.proxies = list(self.initial_proxies)
# #         self.last_used = {p: 0.0 for p in self.proxies}
# #         self.effective_interval = self._calc_interval()
# #         logger.info(f"🔄 Пул прокси восстановлен до {len(self.proxies)} слотов.")

# #     async def close_all(self):
# #         for s in self.sessions.values():
# #             if not s.closed:
# #                 await s.close()
# #         self.sessions.clear()

# # # ==========================================
# # # 3. ОСНОВНОЙ КЛАСС МОНИТОРИНГА
# # # ==========================================
# # class UpbitLiveMonitor:
# #     def __init__(self, poll_interval_sec: float, proxies: list,
# #                  on_signal=None,
# #                  cooldown_sec: float = 8.0,
# #                  cache_file: str = "live_signals.json"):
# #         self._on_signal = on_signal
# #         self.poll_interval = poll_interval_sec
# #         self.api_url = "https://api-manager.upbit.com/api/v1/announcements"
# #         self.cache_file = cache_file
# #         self.cooldown_sec = cooldown_sec
        
# #         self.session_manager = SmartSessionManager(
# #             proxies=proxies,
# #             cooldown_sec=cooldown_sec,
# #             poll_interval_sec=poll_interval_sec
# #         )
        
# #         self.keywords = [
# #             "Market Support for",
# #             "신규 거래지원", 
# #             "디지털 자산 추가"
# #         ]
        
# #         self.seen_symbols: set[str] = set()
# #         self.seen_ids: set[int] = set()
# #         self.signals_log: list[dict] = []
        
# #         self._load_cache()

# #     def _load_cache(self):
# #         if os.path.exists(self.cache_file):
# #             try:
# #                 with open(self.cache_file, "r", encoding="utf-8") as f:
# #                     self.signals_log = json.load(f)
                    
# #                 for signal in self.signals_log:
# #                     if "symbol" in signal:
# #                         self.seen_symbols.add(signal["symbol"])
                        
# #                 logger.info(f"💾 Восстановлено {len(self.seen_symbols)} монет из кеша.")
# #             except Exception as e:
# #                 logger.error(f"Ошибка чтения кеша: {e}")

# #     def _save_cache(self):
# #         try:
# #             with open(self.cache_file, "w", encoding="utf-8") as f:
# #                 json.dump(self.signals_log, f, ensure_ascii=False, indent=4)
# #         except Exception as e:
# #             logger.error(f"Ошибка сохранения JSON: {e}")

# #     def _parse_iso_to_ms(self, dt_str: str) -> int:
# #         if not dt_str: return 0
# #         KST = timezone(timedelta(hours=9)) 
# #         try:
# #             dt = datetime.fromisoformat(dt_str)
# #             if dt.tzinfo is None:
# #                 dt = dt.replace(tzinfo=KST)
# #             return int(dt.timestamp() * 1000)
# #         except Exception:
# #             try:
# #                 clean = dt_str[:19].replace("T", " ")
# #                 dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
# #                 dt = dt.replace(tzinfo=KST)
# #                 return int(dt.timestamp() * 1000)
# #             except Exception: 
# #                 return 0

# #     def _extract_symbol(self, title: str) -> str | None:
# #         match = re.search(r"\(([^)]+)\)", title)
# #         if match:
# #             symbol = match.group(1).strip().upper()
# #             return re.sub(r"\(.*\)", "", symbol).strip() or None
# #         if "for" in title.lower():
# #             idx = title.lower().find("for")
# #             tail = title[idx + 3:].strip()
# #             if tail: return tail.split()[0].upper()
# #         return None

# #     async def fetch_latest_announcements(self, time_since_last: float) -> list[dict]:
# #         res = await self.session_manager.get_ready_session()
        
# #         if res is _EMPTY_POOL:
# #             return _EMPTY_POOL
            
# #         if res is _COOLDOWN:
# #             return []

# #         proxy, session = res
# #         params = {"category": "trade", "page": 1, "per_page": 10, "os": "web"}
        
# #         # 1-а попытка восстановления при ошибке
# #         for attempt in range(2):
# #             try:
# #                 async with session.get(self.api_url, params=params, proxy=proxy, timeout=aiohttp.ClientTimeout(total=3)) as response:
# #                     status = response.status
# #                     if status == 200:
# #                         logger.info(f"[{proxy or 'Localhost'}] Успех: HTTP 200 | Прошло с пред. запроса: {time_since_last:.3f}с")
# #                         data = await response.json()
# #                         return data.get("data", {}).get("notices", [])
                        
# #                     elif status == 429:
# #                         logger.warning(f"HTTP 429 (Бан IP): Прокси {proxy or 'Локальный IP'}. Будет пауза для этого слота.")
# #                         return []
                        
# #                     else:
# #                         if attempt == 0:
# #                             logger.warning(f"[{proxy or 'Localhost'}] HTTP Error {status}. Ретрай...")
# #                             await asyncio.sleep(0.5)
# #                             continue
# #                         else:
# #                             logger.error(f"[{proxy or 'Localhost'}] HTTP Error {status} (попытка 2). Удаляем прокси.")
# #                             await self.session_manager.remove_proxy(proxy)
# #                             return []

# #             except Exception as e:
# #                 if attempt == 0:
# #                     logger.warning(f"[{proxy or 'Localhost'}] Сетевая ошибка: {e}. Ретрай...")
# #                     await asyncio.sleep(0.5)
# #                 else:
# #                     logger.error(f"[{proxy or 'Localhost'}] Сетевой отказ (попытка 2): {e}. Удаляем прокси из пула.")
# #                     await self.session_manager.remove_proxy(proxy)
# #                     return []
                    
# #         return []

# #     async def process_new_listing(self, notice: dict, symbol: str, is_startup: bool = False):
# #         title = notice.get("title", "")
# #         listed_at = notice.get("first_listed_at") or notice.get("listed_at")
        
# #         if not listed_at:
# #             return

# #         announce_ms = self._parse_iso_to_ms(listed_at)
# #         announce_str = datetime.fromtimestamp(announce_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
# #         phemex_symbol = f"{symbol}USDT"
        
# #         signal_status = "INITIAL_SIGNAL" if is_startup else "NEW_SIGNAL"
        
# #         signal_data = {
# #             "symbol": symbol,
# #             "phemex_symbol": phemex_symbol,
# #             "announce_ts_ms": announce_ms,
# #             "announce_ts_str": announce_str,
# #             "title": title,
# #             "status": signal_status
# #         }
        
# #         self.seen_symbols.add(symbol)
# #         self.signals_log.append(signal_data)
# #         self._save_cache()

# #         if not is_startup and self._on_signal:
# #             await self._on_signal(symbol)
# #             logger.debug(
# #                 f"\n{'='*60}\n"
# #                 f"🚀 СИГНАЛ ПАМПА: ВЫШЕЛ АНОНС ЛИСТИНГА [{symbol}] 🚀\n"
# #                 f"Метка времени: {announce_str}\n"
# #                 f"{'='*60}\n"
# #             )

# #     async def run(self):
# #         logger.info(f"🚀 МОНИТОРИНГ UPBIT ЗАПУЩЕН")
# #         logger.info(f"   - Желаемый интервал: {self.poll_interval}с")
# #         logger.info(f"   - Слотов в пуле (IP): {len(self.session_manager.proxies)}")
# #         logger.info(f"   - Кулдаун на 1 IP: {self.cooldown_sec}с")
        
# #         if self.session_manager.effective_interval > self.poll_interval:
# #             logger.warning(f"   - ⚠️ ФАКТИЧЕСКИЙ ИНТЕРВАЛ: {self.session_manager.effective_interval:.2f}с (ограничено кол-вом прокси)")
# #         else:
# #             logger.info(f"   - ФАКТИЧЕСКИЙ ИНТЕРВАЛ: {self.session_manager.effective_interval:.2f}с")

# #         is_startup = True
# #         last_request_time = time.time()
        
# #         try:
# #             while True:
# #                 start_time = time.time()
# #                 time_since_last = start_time - last_request_time
# #                 last_request_time = start_time
                
# #                 notices = await self.fetch_latest_announcements(time_since_last)
                
# #                 if notices is _EMPTY_POOL:
# #                     logger.error("❌ ПУЛ ПРОКСИ ИСТОЩЕН (Сеть полностью упала). Ждем 60 секунд перед восстановлением пула...")
# #                     await asyncio.sleep(WAIT_FOR_GLOBAL_RETRY_SEC)
# #                     await self.session_manager.restore_initial_pool()
# #                     is_startup = True # На всякий случай не шлем старые сигналы после рестарта сети
# #                     last_request_time = time.time()
# #                     continue
                
# #                 for notice in notices:
# #                     n_id = notice.get("id")
# #                     if not n_id or n_id in self.seen_ids:
# #                         continue
                        
# #                     self.seen_ids.add(n_id)
# #                     title = notice.get("title", "")
                    
# #                     if any(kw in title for kw in self.keywords):
# #                         symbol = self._extract_symbol(title)
# #                         if not symbol:
# #                             continue
                            
# #                         if symbol not in self.seen_symbols:
# #                             await self.process_new_listing(notice, symbol, is_startup)
                
# #                 if is_startup:
# #                     logger.info("Синхронизация завершена. Ожидаю новые анонсы...")
# #                     is_startup = False
                
# #                 elapsed = time.time() - start_time
# #                 sleep_time = max(0.0, self.session_manager.effective_interval - elapsed)
# #                 await asyncio.sleep(sleep_time)
# #         finally:
# #             await self.session_manager.close_all()

# # # ==========================================
# # # 4. ЗАПУСК ДЛЯ ТЕСТИРОВАНИЯ (РЕАЛЬНЫЙ РЕЖИМ)
# # # ==========================================
# # if __name__ == "__main__":
# #     # Тестовый коллбек
# #     async def dummy_on_signal(symbol: str):
# #         logger.info(f"🔔 ПОЛУЧЕН БОЕВОЙ СИГНАЛ: {symbol}! В этой сборке он только логируется.")

# #     # Пример использования с прокси и локалхостом:
# #     # MY_PROXIES = [None, "http://login:pass@1.2.3.4:8000"]
# #     # MY_PROXIES = ["http://4j4P77:4P116s@us-ca-sjc-06.oxylabs.io:20012"]
# #     MY_PROXIES = [None]
    
# #     TARGET_POLL_INTERVAL = 1.0 
    
# #     monitor = UpbitLiveMonitor(
# #         poll_interval_sec=TARGET_POLL_INTERVAL, 
# #         proxies=MY_PROXIES,
# #         on_signal=dummy_on_signal,
# #         cooldown_sec=6.0
# #     ) 
    
# #     try:
# #         asyncio.run(monitor.run())
# #     except KeyboardInterrupt:
# #         logger.info("Выход. Парсер остановлен.")

# # # ==========================================
# # # 5. MOCK ГЕНЕРАТОР (ДЛЯ ТЕСТОВ)
# # # ==========================================
# # class MockUpbitLiveMonitor(UpbitLiveMonitor):
# #     def __init__(self, poll_interval_sec: float, on_signal=None, symbols_to_mock=None):
# #         self._on_signal = on_signal
# #         self.poll_interval = poll_interval_sec
# #         self.symbols_to_mock = symbols_to_mock or ["BTC", "ETH", "XRP", "SOL", "DOGE"]
        
# #     async def run(self):
# #         import random
# #         logger.info(f"🟢 ЗАПУЩЕН MOCK-ГЕНЕРАТОР СИГНАЛОВ. Интервал: {self.poll_interval} сек.")
# #         while True:
# #             await asyncio.sleep(self.poll_interval)
# #             symbol = random.choice(self.symbols_to_mock)
# #             logger.debug(f"🛠 [MOCK] Генерирую фейковый сигнал листинга для {symbol}...")
# #             if self._on_signal:
# #                 await self._on_signal(symbol)


# # # python -m ENTRY.upbit_signal
# {
#     "app": {
#         "name": "Rucheiok Bot",
#         "max_active_positions": 1
#     },
#     "credentials": {
#         "api_key": "",
#         "api_secret": ""
#     },
#     "tg": {
#         "enable": true,
#         "token": "",
#         "chat_id": ""
#     },
#     "log": {
#         "debug": true,
#         "info": true,
#         "warning": true,
#         "error": true,
#         "max_lines": 1500,
#         "timezone": "Europe/Kyiv"
#     },
#     "black_list": [
#         "BTCUSDT"
#     ],
#     "quota_asset": "USDT",
#     "risk": {
#         "leverage": {
#             "set_previous": false,
#             "used_by_cache": false,
#             "val": 22,
#             "delay_sec": 0.3,
#             "margin_mode": 2
#         },
#         "notional_limit": 100.0
#     },
#     "upbit": {
#         "test_mode": true,
#         "symbols_to_mock": [
#             "BTC",
#             "ETH",
#             "SOL"
#         ],
#         "poll_interval_sec": 1.0,
#         "_proxy_hint": "null = локальный IP без прокси (валидный слот). Кол-во слотов = ceil(cooldown_sec / poll_interval_sec). Пример: poll=1, cooldown=8 → нужно 8 слотов [null, proxy1..proxy7].",
#         "cooldown_sec": 2.0,
#         "proxies": [
#             null
#         ],
#         "default_side": "long"
#     },
#     "entry": {
#         "signal_timeout_sec": 0.01,
#         "max_place_order_retries": 1,
#         "pattern": {
#             "upbit_signal": true,
#             "binance": {
#                 "enable": false,
#                 "min_price_spread_rate": 0.8,
#                 "update_prices_sec": 1.1,
#                 "spread_ttl_sec": 2
#             },
#             "funding_pattern1": {
#                 "enable": false,
#                 "check_interval_sec": 60,
#                 "skip_before_counter_sec": 1800,
#                 "threshold_pct": 1.0
#             },
#             "funding_pattern2": {
#                 "enable": false,
#                 "check_interval_sec": 60,
#                 "skip_before_counter_sec": 1800,
#                 "diff_threshold_pct": 0.05
#             }
#         }
#     },
#     "exit": {
#         "max_place_order_retries": 1,
#         "min_order_life_sec": 0.05,
#         "scenarios": {
#             "grid_tp": {
#                 "enable": true,
#                 "map": [
#                     {
#                         "comment": "< $500K — малоизвестный листинг, резкий памп, гасится быстро",
#                         "min_vol": 0,
#                         "max_vol": 500,
#                         "levels": [4, 10, 20, 40, 70],
#                         "volumes": [30, 25, 20, 15, 10]
#                     },
#                     {
#                         "comment": "$500K–$2M — среднеизвестная монета, умеренный памп",
#                         "min_vol": 500,
#                         "max_vol": 2000,
#                         "levels": [3, 8, 16, 30, 55],
#                         "volumes": [25, 25, 20, 20, 10]
#                     },
#                     {
#                         "comment": "> $2M — ликвидная монета, памп растянут",
#                         "min_vol": 2000,
#                         "max_vol": null,
#                         "levels": [2, 5, 12, 22, 40],
#                         "volumes": [20, 20, 20, 20, 20]
#                     }
#                 ]
#             },
#             "stop_loss": {
#                 "enable": false,
#                 "percent": 5.0,
#                 "ttl_sec": 0.0
#             },
#             "ttl_market_close": {
#                 "enable": true,
#                 "ttl_sec": 60
#             }
#         }
#     }
# }