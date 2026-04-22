# ============================================================
# FILE: CORE/bot_utils.py
# ROLE: Вспомогательные сервисы (BlackList, PriceCache, Reporters, Config)
# ============================================================
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, List, Tuple, TYPE_CHECKING
from ENTRY.signal_engine import SignalEngine
from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.orchestrator import TradingBot
    from API.PHEMEX.ticker import PhemexTickerAPI, TickerData
    from API.BINANCE.ticker import BinanceTickerAPI
    from ENTRY.pattern_math import EntrySignal

logger = UnifiedLogger("core")


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
                
            # if "OG" in full_sym: # -- так и оставить закомент.
            #     new_bl.append(full_sym.replace("OG", "0G"))
            # if "0G" in full_sym:
            #     new_bl.append(full_sym.replace("0G", "OG"))
                
        self.symbols = new_bl
        return self.symbols

    def update_and_save(self, raw_symbols: List[str]) -> Tuple[bool, str]:
        clean_symbols_for_cfg = []
        for sym in raw_symbols:
            sym = sym.upper().strip()
            if sym: clean_symbols_for_cfg.append(sym)
            
        self.load_from_config(clean_symbols_for_cfg)
        
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                c = json.load(f)
            c["black_list"] = clean_symbols_for_cfg
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(c, f, indent=4)
            return True, "✅ Список успешно обновлен."
        except Exception as e:
            return False, f"❌ Ошибка записи: {e}"


class PriceCacheManager:
    def __init__(self, binance_api: "BinanceTickerAPI", phemex_api: 'PhemexTickerAPI', upd_sec: float = 3.0):
        self.binance_api = binance_api
        self.phemex_api = phemex_api
        self.upd_sec = upd_sec
        self.binance_prices: Dict[str, float] = {}
        self.phemex_prices: Dict[str, float] = {}
        self.phemex_volumes: Dict[str, float] = {}   # <-- новое
        self._is_running = False

    async def _fetch(self):
        """Всегда запрашивает Phemex. Binance — опционально (но данные всё равно собираем)."""
        try:
            b_prices, p_tickers = await asyncio.gather(
                self.binance_api.get_all_prices(),
                self.phemex_api.get_all_tickers()
            )
            self.binance_prices = b_prices
            self.phemex_prices = {sym: t.price for sym, t in p_tickers.items()}
            self.phemex_volumes = {sym: t.volume_24h_usd for sym, t in p_tickers.items()}
        except Exception as e:
            logger.debug(f"Ошибка фонового обновления цен тикеров: {e}")

    async def warmup(self):
        """Первичное заполнение кэша перед стартом."""
        logger.info("⏳ PriceCacheManager: прогрев кэша...")
        await self._fetch()
        logger.info(f"✅ PriceCacheManager: прогрет. Phemex={len(self.phemex_prices)} монет, Binance={len(self.binance_prices)} монет.")

    async def loop(self):
        """Фоновый цикл периодического обновления."""
        self._is_running = True
        while self._is_running:
            await asyncio.sleep(self.upd_sec)
            await self._fetch()

    def stop(self):
        self._is_running = False

    def get_prices(self, symbol: str) -> Tuple[float, float]:
        return self.binance_prices.get(symbol, 0.0), self.phemex_prices.get(symbol, 0.0)

    def get_volume(self, symbol: str) -> float:
        """Объём 24ч в USD по символу Phemex."""
        return self.phemex_volumes.get(symbol, 0.0)
    

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

            # --- ПЕРЕЗАГРУЗКА ИНСТРУМЕНТОВ ВХОДА (ВКЛЮЧАЯ ФАНДИНГ V5) ---
            from ENTRY.funding_manager import FundingManager
            from ENTRY.signal_engine import SignalEngine
            
            # Тормозим старую таску фандинга, если есть
            if hasattr(self.tb, 'funding_manager'):
                self.tb.funding_manager.stop()
                
            old_task = getattr(self.tb, '_funding_task', None)
            if old_task and not old_task.done():
                old_task.cancel()

            # Создаем новый инстанс FundingManager
            self.tb.funding_manager = FundingManager(
                self.tb.cfg.get("entry", {}).get("pattern", {}), 
                self.tb.phemex_funding_api, 
                self.tb.binance_funding_api
            )
            
            # Пересоздаем движок сигналов (математика стакана обновится здесь же)
            self.tb.signal_engine = SignalEngine(self.tb.cfg["entry"], self.tb.funding_manager)

            # Перезапускаем луп фандинга
            if getattr(self.tb, '_is_running', False):
                self.tb._funding_task = asyncio.create_task(self.tb.funding_manager.run())

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
    def entry_signal(symbol: str, signal: EntrySignal, b_price: float, p_price: float) -> str:
        side_str = "🟢 LONG" if signal.side == "LONG" else "🔴 SHORT"
        return (
            f"<b>#{symbol}</b> | {side_str}\n"
            f"Вход: <b>{signal.price}</b>\n"
            f"Binance: {b_price} | Phemex: {p_price}"
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