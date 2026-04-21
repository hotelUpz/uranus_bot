from __future__ import annotations
from typing import TYPE_CHECKING
from c_log import UnifiedLogger



if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition

logger = UnifiedLogger("exit")

class ExtrimeClose:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg["enable"]
        self.retry_ttl = cfg["retry_ttl"]
        self.retry_num = cfg["retry_num"]
        self.bid_to_ask_orientation = cfg["bid_to_ask_orientation"]
        self.increase_fraction = cfg["increase_fraction"] / 100.0

    def scen_extrime_analyze(self, current_price: float, pos: ActivePosition, now: float) -> float | None:
        if not self.enable: return None
        if pos.current_qty <= 0.0: return None        
        if now - pos.last_extrime_try_ts < self.retry_ttl: return None # 
        
        if not current_price: return None

        if str(self.retry_num).lower() != "inf" and pos.extrime_retries_count >= int(self.retry_num):
            logger.warning(f"Осторожно! extrime_retries_count >= retry_num, но позиция {pos.symbol} не закрыта!")
            return None

        mid = current_price
        # Имитируем спред как 0.1% от цены для агрессивного выхода
        simulated_spread = mid * 0.001
        
        base_price = mid + (simulated_spread * self.bid_to_ask_orientation) if pos.side == "LONG"\
            else mid - (simulated_spread * self.bid_to_ask_orientation)
        shift = simulated_spread * self.increase_fraction * pos.extrime_retries_count
        
        return base_price - shift if pos.side == "LONG" else base_price + shift