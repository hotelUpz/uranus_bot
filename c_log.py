# ============================================================
# FILE: c_log.py
# ROLE: Унифицированный логгер. Методы: debug, info, warning, error, exception.
# ============================================================
from __future__ import annotations

import inspect
import logging
import sys
import time
from datetime import datetime
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

import pytz

from consts import LOG_DEBUG, LOG_ERROR, LOG_INFO, LOG_WARNING, MAX_LOG_LINES, TIME_ZONE

TZ = pytz.timezone(TIME_ZONE)

# --- АНТИСПАМ НАСТРОЙКИ ---
ANTI_SPAM_COOLDOWN_SEC = 60.0
_SPAM_CACHE: dict[int, float] = {}


class _TzFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, TZ)
        # Добавляем миллисекунды вручную
        s = dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")
        return f"{s}.{int(record.msecs):03d}"


class UnifiedLogger:
    def __init__(self, name: str, log_dir: str = "./logs", max_lines: int = MAX_LOG_LINES, context: Optional[str] = None):
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log_path = Path(log_dir) / f"{name}.log"
        approx_line_len = 350
        max_bytes = max(100_000, approx_line_len * max_lines)

        base_logger = logging.getLogger(name)
        base_logger.setLevel(logging.DEBUG)
        base_logger.propagate = False

        if not base_logger.handlers:
            # Создаем единый форматер для всех выводов
            formatter = _TzFormatter("%(asctime)s | %(levelname)s | %(context)s | %(message)s", "%Y-%m-%d %H:%M:%S")

            # 1. Обработчик для записи в файл
            file_handler = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=2, encoding="utf-8")
            file_handler.setFormatter(formatter)
            base_logger.addHandler(file_handler)

            # 2. Обработчик для вывода в консоль
            if LOG_DEBUG: 
                console_handler = logging.StreamHandler(sys.stdout)
                console_handler.setFormatter(formatter)
                base_logger.addHandler(console_handler)

        self._logger = logging.LoggerAdapter(base_logger, extra={"context": context or name})

    def _is_spam(self, msg: str) -> bool:
        """
        Проверяет, не дублируется ли сообщение в течение ANTI_SPAM_COOLDOWN_SEC.
        Возвращает True, если это спам (надо пропустить).
        """
        now = time.time()
        
        # Защита от утечки памяти: чистим старые хеши, если кэш разросся
        if len(_SPAM_CACHE) > 1000:
            keys_to_del = [k for k, v in _SPAM_CACHE.items() if now - v > ANTI_SPAM_COOLDOWN_SEC]
            for k in keys_to_del:
                del _SPAM_CACHE[k]
                
        # Хешируем само сообщение (оно уже без метки времени)
        msg_hash = hash(msg)
        
        if msg_hash in _SPAM_CACHE:
            if now - _SPAM_CACHE[msg_hash] < ANTI_SPAM_COOLDOWN_SEC:
                return True
                
        _SPAM_CACHE[msg_hash] = now
        return False

    def debug(self, msg: str, *args, **kwargs) -> None:
        # Для дебага иногда нужны дубли (например, поллинг), можно отключить антиспам при желании,
        # но сейчас фильтруем и его тоже.
        if LOG_DEBUG and not self._is_spam(msg):
            self._logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs) -> None:
        if LOG_INFO and not self._is_spam(msg):
            self._logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs) -> None:
        if LOG_WARNING and not self._is_spam(msg):
            self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs) -> None:
        if LOG_ERROR and not self._is_spam(msg):
            self._logger.error(msg, *args, **kwargs)

    def exception(self, msg: str, *args, **kwargs) -> None:
        if LOG_ERROR and not self._is_spam(msg):
            self._logger.exception(msg, *args, **kwargs)

    def total_exception_decor(self, func, context: Optional[Any] = None):
        if getattr(func, "_is_wrapped", False):
            return func

        if context is not None:
            target_logger = logging.LoggerAdapter(self._logger.logger, extra={"context": context})
        else:
            target_logger = self._logger

        if hasattr(func, "__call__"):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except Exception:
                    target_logger.exception("Unhandled async exception in %s", getattr(func, "__qualname__", repr(func)))
                    return None

            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except Exception:
                    target_logger.exception("Unhandled sync exception in %s", getattr(func, "__qualname__", repr(func)))
                    return None

            wrapper = async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper
            wrapper._is_wrapped = True
            return wrapper
        return func