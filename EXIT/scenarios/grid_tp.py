from decimal import Decimal, ROUND_DOWN
from typing import List, Dict, Any

class GridTPFactory:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.map = cfg.get("map", [])

    def calculate_grid(self, symbol: str, side: str, entry_price: float, total_qty: float, 
                       tick_size: float, lot_size: float, volume_24h_usd: float) -> List[Dict[str, Any]]:
        if total_qty <= 0:
            return []

        # Find the correct bucket
        selected_bucket = None
        for bucket in self.map:
            min_vol = bucket.get("min_vol", 0)
            max_vol = bucket.get("max_vol")
            
            if volume_24h_usd >= min_vol:
                if max_vol is None or volume_24h_usd < max_vol:
                    selected_bucket = bucket
                    break
        
        if not selected_bucket:
            # Fallback to the last bucket if volume is somehow out of bounds
            if self.map:
                selected_bucket = self.map[-1]
            else:
                return []

        levels = selected_bucket.get("levels", [])
        volumes = selected_bucket.get("volumes", [])

        if not levels or not volumes or len(levels) != len(volumes):
            return []

        orders = []
        accumulated_qty = Decimal("0")
        
        dec_total_qty = Decimal(str(total_qty))
        dec_lot_size = Decimal(str(lot_size)) if lot_size > 0 else Decimal("1")
        dec_tick_size = Decimal(str(tick_size)) if tick_size > 0 else Decimal("1")
        dec_entry_price = Decimal(str(entry_price))

        for idx, (level, vol_pct) in enumerate(zip(levels, volumes)):
            # Price calculation
            if side == "LONG":
                target_price = dec_entry_price * (Decimal("1") + Decimal(str(level)) / Decimal("100"))
            else:
                target_price = dec_entry_price * (Decimal("1") - Decimal(str(level)) / Decimal("100"))
            
            # Round price to tick_size
            rounded_price = (target_price / dec_tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * dec_tick_size

            # Quantity calculation
            if idx == len(levels) - 1:
                # Last order takes all remaining quantity to avoid dust
                cur_qty = dec_total_qty - accumulated_qty
            else:
                cur_qty = dec_total_qty * (Decimal(str(vol_pct)) / Decimal("100"))
                # Round qty to lot_size
                cur_qty = (cur_qty / dec_lot_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * dec_lot_size

            if cur_qty > 0:
                orders.append({
                    "idx": idx + 1,
                    "price": float(rounded_price),
                    "qty": float(cur_qty)
                })
                accumulated_qty += cur_qty

        return orders
