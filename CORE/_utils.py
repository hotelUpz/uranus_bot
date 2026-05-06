# ============================================================
# FILE: CORE/bot_utils.py
# ROLE: Вспомогательные сервисы (BlackList, PriceCache, Reporters, Config)
# ============================================================
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List, Tuple, TYPE_CHECKING
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.orchestrator import TradingBot
    from API.PHEMEX.ticker import PhemexTickerAPI, TickerData
    from ENTRY.signal_engine import EntrySignal, SignalEngine

logger = UnifiedLogger("core")

PRICE_WARMUP_POLL_SEC = 0.01 # Пауза при прогреве кэша цен


class BlackListManager:
    """Управление черным списком: парсинг, квотирование, сохранение."""
    def __init__(self, cfg_path: Path | str, quota_asset: str = "USDT"):
        self.cfg_path = Path(cfg_path)
        self.quota_asset = quota_asset.upper()
        self.symbols: List[str] = []

    def load_from_config(self, raw_symbols: List[str]) -> List[str]:
        new_bl = []
        for sym in raw_symbols:
            sym = sym.upper().strip()
            if not sym: continue
            
            full_sym = sym if sym.endswith(self.quota_asset) else sym + self.quota_asset
            if full_sym not in new_bl:
                new_bl.append(full_sym)
                
        self.symbols = new_bl
        return self.symbols

    async def is_blacklisted(self, symbol: str) -> bool:
        return self.is_blacklisted_sync(symbol)

    def is_blacklisted_sync(self, symbol: str) -> bool:
        return symbol.upper() in self.symbols

    async def update_and_save(self, raw_symbols: List[str]) -> Tuple[bool, str]:
        clean_symbols_for_cfg = []
        for sym in raw_symbols:
            sym = sym.upper().strip()
            if sym: clean_symbols_for_cfg.append(sym)
            
        self.load_from_config(clean_symbols_for_cfg)
        
        def _save():
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                c = json.load(f)
            c["black_list"] = clean_symbols_for_cfg
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(c, f, indent=4, ensure_ascii=False)

        try:
            await asyncio.to_thread(_save)
            return True, "✅ Список успешно обновлен."
        except Exception as e:
            return False, f"❌ Ошибка записи: {e}"


class PriceCacheManager:
    def __init__(self, phemex_api, upd_sec: float, bot_ref):
        self.phemex_api = phemex_api
        self.upd_sec = upd_sec
        self.bot = bot_ref  # Ссылка на оркестратор для проверки stop_another_request
        self._is_running = False
        self.phemex_prices = {}
        self.phemex_volumes = {}
        
        # Минутные снапшоты для защиты от импульса
        self.minute_prices = {}       # Текущая минута (базис)
        self.prev_minute_prices = {}  # Предыдущая минута (для нулевой секунды)

    async def warmup(self):
        try:
            tickers = await self.phemex_api.get_all_tickers()
            self.phemex_prices = {sym: t.price for sym, t in tickers.items()}
            self.phemex_volumes = {sym: t.volume_24h_usd for sym, t in tickers.items()}
            # Инициализируем минутный базис при старте
            self.minute_prices = self.phemex_prices.copy()
            self.prev_minute_prices = self.phemex_prices.copy()
        except Exception as e:
            logger.error(f"Warmup API Error: {e}")

    async def _minute_snapshot_loop(self):
        """Цикл, который делает слепок цен ровно в 00 секунд каждой минуты."""
        logger.info("[PRICE] Запущен цикл минутных снапшотов.")
        while self._is_running:
            try:
                now = time.time()
                # Считаем, сколько осталось до начала следующей минуты
                sleep_time = 60 - (now % 60)
                await asyncio.sleep(sleep_time)

                # Ровно 00 секунд (или около того)
                tickers = await self.phemex_api.get_all_tickers()
                if tickers:
                    self.prev_minute_prices = self.minute_prices.copy()
                    self.minute_prices = {sym: t.price for sym, t in tickers.items()}
                    logger.debug(f"[PRICE] Обновлен минутный Snapshot для {len(self.minute_prices)} пар.")
            except Exception as e:
                logger.error(f"[PRICE] Ошибка минутного снапшота: {e}")
                await asyncio.sleep(1)

    async def loop(self):
        self._is_running = True
        # Запускаем минутный цикл фоном
        asyncio.create_task(self._minute_snapshot_loop())
        
        while self._is_running:
            # Тормозим обновление цен, пока идет боевой ордер!
            if self.bot.stop_another_request:
                await asyncio.sleep(PRICE_WARMUP_POLL_SEC)
                continue

            try:
                tickers = await self.phemex_api.get_all_tickers()
                for sym, t in tickers.items():
                    self.phemex_prices[sym] = t.price
                    self.phemex_volumes[sym] = t.volume_24h_usd
            except Exception:
                pass
            await asyncio.sleep(self.upd_sec)

    def get_minute_baseline(self, symbol: str) -> float:
        """Возвращает цену базиса (начало минуты). Если сейчас первые 2 сек минуты — берем прошлую минуту."""
        now = time.time()
        if (now % 60) < 2.0:
            return self.prev_minute_prices.get(symbol, self.minute_prices.get(symbol, 0.0))
        return self.minute_prices.get(symbol, 0.0)

    def get_prices(self, symbol: str) -> tuple[float, float]:
        # Возвращаем (Binance, Phemex). Т.к. Binance нет, возвращаем дважды Phemex
        p = self.phemex_prices.get(symbol, 0.0)
        return p, p

    def get_volume(self, symbol: str) -> float:
        return self.phemex_volumes.get(symbol, 0.0)

    def stop(self):
        self._is_running = False
    

class ConfigManager:
    def __init__(self, cfg_path: Path | str, tb: "TradingBot"):
        self.cfg_path = cfg_path
        self.tb = tb

    def reload_config(self) -> tuple[bool, str]:
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                new_cfg = json.load(f)

            self.tb.cfg = new_cfg
            self.tb.max_active_positions = self.tb.cfg.get("app", {}).get("max_active_positions", 1)
            
            self.tb.black_list = self.tb.bl_manager.load_from_config(self.tb.cfg.get("black_list", []))
            if hasattr(self.tb.state, 'black_list'):
                self.tb.state.black_list = self.tb.black_list

            # --- ПЕРЕЗАГРУЗКА ИНСТРУМЕНТОВ ВХОДА ---
            from ENTRY.signal_engine import SignalEngine
            self.tb.signal_engine = SignalEngine(self.tb.cfg["entry"])

            # --- ПЕРЕЗАГРУЗКА СЦЕНАРИЕВ ВЫХОДА ---
            exit_cfg = self.tb.cfg.get("exit", {})
            scen_cfg = exit_cfg.get("scenarios", {})
            
            from EXIT.scenarios.grid_tp import GridTPFactory
            
            self.tb.grid_tp_factory = GridTPFactory(scen_cfg.get("grid_tp", {}))

            return True, "Конфигурация успешно обновлена в памяти!"
        except Exception as e:
            logger.error(f"Config reload error: {e}")
            return False, f"Ошибка загрузки в память: {e}"


class Reporters:
    @staticmethod
    def entry_signal(symbol: str, signal: EntrySignal, bid_price: float, ask_price: float) -> str:
        arrow = "🟢 LONG" if signal.side == "LONG" else "🔴 SHORT"
        return (
            f"🚀 <b>[Upbit Signal]</b> {symbol} → {arrow}\n"
            f"Bid: {bid_price} | Ask: {ask_price}"
        )

    @staticmethod
    def extrime_alert(symbol: str, reason: str) -> str:
        return f"🚨 <b>ОТКАЗ API</b>\n#{symbol}\nЭкстрим ордера отклоняются: {reason}"

    @staticmethod
    def exit_success(pos_key: str, semantic: str, price: float, pnl: float = 0.0, emoji: str = "💵") -> str:
        # Форматируем PnL с плюсом для прибыли
        pnl_str = f"+{pnl:.4f}" if pnl > 0 else f"{pnl:.4f}"
        return (
            f"{emoji} <b>{semantic}</b>\n"
            f"#{pos_key}\n"
            f"Цена закрытия: <b>{price}</b>\n"
            f"PnL: <b>{pnl_str}$</b>"
        )