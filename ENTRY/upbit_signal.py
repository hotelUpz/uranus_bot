# ============================================================
# FILE: ENTRY/upbit_signal.py
# ROLE: UPBIT SIGNAL — парсер анонсов Upbit (KISS Edition + FIX)
# ============================================================

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone, timedelta

from curl_cffi import requests as cffi_requests
from c_log import UnifiedLogger
from ENTRY._utils import get_upbit_status_and_sleep_time
from utils import save_json_safe

logger = UnifiedLogger("upbit_signal")

IS_PLANER = True
INJECT_FAKE_SIGNAL = False  # Для тестов переведи в True

# ==========================================
# КОНФИГ И КОНСТАНТЫ
# ==========================================
API_URL = "https://api-manager.upbit.com/api/v1/announcements"
PARAMS  = {"category": "trade", "page": 1, "per_page": 10, "os": "web"}

HEADERS = {
    "accept":            "application/json, text/plain, */*",
    "accept-language":   "en-US,en;q=0.9,ru;q=0.8",
    "origin":            "https://upbit.com",
    "referer":           "https://upbit.com/",
    "sec-fetch-dest":    "empty",
    "sec-fetch-mode":    "cors",
    "sec-fetch-site":    "same-site",
}

KEYWORDS = [
    "Listing", "Adds", "Market Support", "New Market", "KRW",
    "신규 거래지원", "디지털 자산 추가",
]

REQUEST_TIMEOUT_SEC = 6.0
MAX_STARTUP_SIGNAL_AGE_SEC = 600
MAX_BACKOFF_INTERVAL_SEC = 10.0  # Кап для бекоффа, чтобы не уснуть навсегда
_RATE_LIMITED = object()

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def _new_session() -> cffi_requests.AsyncSession:
    return cffi_requests.AsyncSession(
        impersonate="chrome120",
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT_SEC,
    )

def _parse_iso_to_ms(dt_str: str) -> int:
    if not dt_str: return 0
    KST = timezone(timedelta(hours=9))
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=KST)
        return int(dt.timestamp() * 1000)
    except Exception:
        try:
            clean = dt_str[:19].replace("T", " ")
            dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            return int(dt.timestamp() * 1000)
        except Exception: return 0

def _extract_symbol(title: str) -> str | None:
    match = re.search(r"\(([^)]+)\)", title)
    if match:
        symbol = match.group(1).strip().upper()
        return re.sub(r"\(.*\)", "", symbol).strip() or None
    if "for" in title.lower():
        idx = title.lower().find("for")
        tail = title[idx + 3:].strip()
        if tail: return tail.split()[0].upper()
    return None

# ==========================================
# ОСНОВНОЙ МОНИТОР
# ==========================================

class UpbitLiveMonitor:
    def __init__(
        self,
        upbit_cfg: dict,
        on_signal=None,
        is_paused_func=None
    ):
        self._on_signal      = on_signal
        self._is_paused_func = is_paused_func
        self._is_running     = True

        self.base_interval    = upbit_cfg["poll_interval_sec"]
        self.current_interval = upbit_cfg["poll_interval_sec"]
        self.bang_sleep       = upbit_cfg["bang_sleep_sec"]
        self.bang_mult        = upbit_cfg["bang_backoff_mult"]

        self.seen_ids:     set[int] = set()
        self.seen_symbols: set[str] = set()

        self._start_time = time.time()
        self._fake_injected = False

    # def _get_fake_notice(self) -> dict:
    #     """Создает боевой фейковый сигнал с текущим временем KST."""
    #     kst = timezone(timedelta(hours=9))
    #     now_kst = datetime.now(kst).strftime("%Y-%m-%dT%H:%M:%S+09:00")
        
    #     return {
    #         "listed_at": now_kst,
    #         "first_listed_at": now_kst,
    #         "id": 999999,  # Заведомо уникальный ID, чтобы не пересекся с реальными
    #         "title": "Market Support for VASYAUSHLEPKIN(VASYAUSHLEPKIN) (KRW, BTC, USDT Market)",
    #         "category": "Trade",
    #         "need_new_badge": True,
    #         "need_update_badge": False
    #     }

    # async def _fetch_notices(self, session: cffi_requests.AsyncSession, return_full: bool = False):
    #     try:
    #         resp = await session.get(API_URL, params=PARAMS)
    #         if resp.status_code == 200:
    #             data = resp.json()
                
    #             # ---- ВРЕЗКА ФЕЙКЕРА ----
    #             if INJECT_FAKE_SIGNAL and not self._fake_injected:
    #                 # Вбрасываем фейк через 15 секунд после старта.
    #                 # За это время парсер уже снимет флаг is_startup и перейдет в боевой режим.
    #                 if time.time() - self._start_time > 30:
    #                     fake_notice = self._get_fake_notice()
                        
    #                     # Если ключа notices еще нет (на всякий случай защита структуры)
    #                     if "data" not in data: data["data"] = {}
    #                     if "notices" not in data["data"]: data["data"]["notices"] = []
                        
    #                     # Добавляем фейк в самое начало списка (как самую свежую новость)
    #                     data["data"]["notices"].insert(0, fake_notice)
    #                     self._fake_injected = True
    #                     logger.info("[TEST] 💉 Вброшен тестовый сигнал TESTCOIN(TST)!")
    #             # ------------------------

    #             return data if return_full else data.get("data", {}).get("notices", [])
                
    #         if resp.status_code == 429:
    #             return _RATE_LIMITED
    #         logger.error(f"[FETCH] Ошибка API, неверный статус: {resp.status_code}. Текст ответа: {resp.text[:100]}")
    #         return None
    #     except Exception as e:
    #         logger.error(f"[FETCH] Ошибка сетевого запроса: {e}")
    #         return None

    async def _fetch_notices(self, session: cffi_requests.AsyncSession, return_full: bool = False):
        try:
            resp = await session.get(API_URL, params=PARAMS)
            if resp.status_code == 200:
                data = resp.json()
                return data if return_full else data.get("data", {}).get("notices", [])
            if resp.status_code == 429:
                return _RATE_LIMITED
            logger.error(f"[FETCH] Ошибка API, неверный статус: {resp.status_code}. Текст ответа: {resp.text[:100]}")
            return None
        except Exception as e:
            logger.error(f"[FETCH] Ошибка сетевого запроса: {e}")
            return None

    async def _process_notice(self, notice: dict, is_startup: bool):
        n_id = notice.get("id")
        
        # 1. Защита от кривого JSON
        if not n_id:
            logger.warning(f"[SKIP] Анонс без 'id'. Дамп: {notice}")
            return
            
        # 2. Игнорируем то, что уже видели (БЕЗ логов, чтобы не спамить)
        if n_id in self.seen_ids:
            return

        self.seen_ids.add(n_id)
        title = notice.get("title", "")
        
        # 3. Фильтр по кейвордам
        if not any(kw in title for kw in KEYWORDS):
            # Новость новая, но не про листинг. Логируем для прозрачности на уровне info/debug.
            logger.info(f"[IGNORE] Не целевой анонс: '{title}'")
            return

        # 4. Извлечение тикера (КРИТИЧЕСКАЯ ТОЧКА)
        symbol = _extract_symbol(title)
        if not symbol:
            # АХТУНГ: Кейворд сработал, но тикер не найден! Возможно, биржа изменила формат заголовка.
            logger.error(f"[CRITICAL SKIP] Найдено ключевое слово, но не извлечен тикер! Заголовок: '{title}'")
            return

        # 5. Защита от дублей по тикеру
        if symbol in self.seen_symbols:
            logger.info(f"[SKIP] Анонс про {symbol}, но монета уже обрабатывалась. Заголовок: '{title}'")
            return

        # 6. Парсинг времени
        listed_at = notice.get("listed_at") or notice.get("first_listed_at")
        if listed_at:
            announce_ms = _parse_iso_to_ms(listed_at)
        else:
            logger.warning(f"[WARN] Нет времени listed_at для {symbol}. Используем текущее время. Заголовок: '{title}'")
            announce_ms = int(time.time() * 1000)
        
        # 7. Фильтр старья на старте
        if is_startup:
            age_sec = (int(time.time() * 1000) - announce_ms) / 1000.0
            if age_sec > MAX_STARTUP_SIGNAL_AGE_SEC:
                logger.info(f"[STARTUP SKIP] Слишком старый анонс {symbol} (возраст {int(age_sec)}с). Вносим в базу.")
                self.seen_symbols.add(symbol)
                return
            else:
                logger.info(f"[STARTUP KEEP] Найден свежий анонс {symbol} (возраст {int(age_sec)}с). Формируем сигнал!")

        # ---- БОЕВОЙ СИГНАЛ ----
        self.seen_symbols.add(symbol)
        
        if self._on_signal:
            await self._on_signal(symbol, announce_ms)

        logger.warning("="*60)
        logger.warning(f"🚀 SIGNAL: UPBIT LISTING [{symbol}] 🚀")
        logger.warning(f"Title: {title}")
        logger.warning("="*60)

    async def run(self):
        logger.info(f"[RUN] Upbit Monitor Start. Base Interval: {self.base_interval}s")
        session = _new_session()
        is_startup = True
        
        try:
            while self._is_running:
                # 1. Расписание (Анабиоз)
                if IS_PLANER:
                    is_active, sleep_time = get_upbit_status_and_sleep_time()
                    if not is_active:
                        logger.info(f"[SLEEP] Биржа закрыта. Спим {sleep_time/3600:.2f}ч...")
                        await session.close()
                        await asyncio.sleep(sleep_time)
                        session = _new_session()
                        is_startup = True
                        self.current_interval = self.base_interval
                        continue

                # 2. Пауза от оркестратора
                if self._is_paused_func and self._is_paused_func():
                    await asyncio.sleep(self.current_interval)
                    continue

                loop_start = time.monotonic()
                
                # 3. Синхронизация (Отрабатывает только 1 раз при запуске/просыпании)
                if is_startup:
                    full_data = await self._fetch_notices(session, return_full=True)
                    
                    if full_data is _RATE_LIMITED:
                        logger.warning(f"[SYNC] 429 Limit. Sleep {self.bang_sleep}s")
                        await asyncio.sleep(self.bang_sleep)
                        continue
                        
                    if full_data is None:
                        logger.error("[SYNC] Ошибка получения снапшота. Пауза 5с.")
                        await asyncio.sleep(5.0)
                        continue
                    
                    if isinstance(full_data, dict):
                        save_json_safe("upbit_snapshot.json", full_data)
                        notices = full_data.get("data", {}).get("notices", [])
                        
                        for notice in reversed(notices):
                            await self._process_notice(notice, is_startup=True)
                        
                        logger.info(f"[SYNC] Готово. В базе {len(self.seen_ids)} новостей и {len(self.seen_symbols)} тикеров.")
                        is_startup = False
                        self.current_interval = self.base_interval
                        await asyncio.sleep(self.current_interval)
                    continue

                # 4. Боевой мониторинг
                notices = await self._fetch_notices(session)

                if notices is _RATE_LIMITED:
                    self.current_interval = min(round(self.current_interval * self.bang_mult, 3), MAX_BACKOFF_INTERVAL_SEC)
                    logger.warning(f"[429] Rate Limit! Sleep {self.bang_sleep}s. New interval: {self.current_interval}s")
                    await asyncio.sleep(self.bang_sleep)
                    continue
                elif notices is None:
                    # Сетевая ошибка (таймаут, 502 и тд). Уже залогировано в _fetch_notices.
                    pass
                else:
                    # ФИКС: Обязательно сбрасываем бекофф при успешном запросе!
                    if self.current_interval > self.base_interval:
                        logger.info(f"[API] Связь стабильна. Возвращаем интервал с {self.current_interval}s на {self.base_interval}s")
                        self.current_interval = self.base_interval

                    for notice in reversed(notices):
                        await self._process_notice(notice, is_startup=False)

                # 5. Точный Sleep
                elapsed = time.monotonic() - loop_start
                await asyncio.sleep(max(0.0, self.current_interval - elapsed))
                print("tik")

        except asyncio.CancelledError:
            logger.info("[UPBIT] Получен сигнал отмены. Остановка парсера...")
            raise
        finally:
            await session.close()

    async def aclose(self):
        self._is_running = False


class MockUpbitLiveMonitor:
    """Генератор фейковых сигналов для тестов"""
    def __init__(self, poll_interval_sec: float, on_signal=None, is_paused_func=None):
        self.poll_interval = poll_interval_sec
        self._on_signal = on_signal
        self._is_paused_func = is_paused_func
        self._is_running = True

    async def run(self):
        import random
        logger.info("[MOCK] ГЕНЕРАТОР ЗАПУЩЕН")
        while self._is_running:
            await asyncio.sleep(self.poll_interval)
            if self._is_paused_func and self._is_paused_func(): continue
            sym = random.choice(["ETH", "BTC"])
            if self._on_signal:
                await self._on_signal(sym, int(time.time() * 1000))

    async def aclose(self):
        self._is_running = False