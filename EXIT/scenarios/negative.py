from __future__ import annotations

from typing import TYPE_CHECKING

from c_log import UnifiedLogger

if TYPE_CHECKING:
    from CORE.models_fsm import ActivePosition


logger = UnifiedLogger("negative")


class NegativeScenario:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enable = cfg["enable"]
        self.stab_neg = cfg["stabilization_ttl"]
        self.negative_spread_pct = cfg["negative_spread_pct"]
        self.negative_ttl = cfg["negative_ttl"]

    def scen_neg_analyze(self, current_price: float, pos: ActivePosition, now: float) -> str | None:
        if not self.enable:
            return None

        # Во время стабилизации мы "в безопасности", поэтому тащим якорь за собой
        if (now - pos.opened_at) < self.stab_neg: 
            pos.last_negative_check_ts = now
            return None

        # Check if in negative using current_price
        if pos.side == "LONG":
            # in LONG, we are in negative if current_price < entry_price * (1 - negative_spread_pct)
            is_negative = current_price < pos.entry_price * (1 - self.negative_spread_pct / 100)
        else:
            is_negative = current_price > pos.entry_price * (1 + self.negative_spread_pct / 100)

        if is_negative:
            # Мы в просадке! Якорь перестал обновляться.
            # Считаем чистое абсолютное время с момента, как всё стало плохо.
            if (now - pos.last_negative_check_ts) >= self.negative_ttl:
                return "NEGATIVE_TIMEOUT"
        else:
            # Мы в плюсе. Подтягиваем якорь к текущему моменту.
            pos.last_negative_check_ts = now

        return None