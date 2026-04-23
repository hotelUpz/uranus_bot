# ============================================================
# FILE: utils.py
# ============================================================
import os
import json
from typing import Any
from decimal import Decimal, ROUND_HALF_DOWN

def round_step(value: float, step: float) -> float:
    if not step or step <= 0:
        return value
    val_d = Decimal(str(value))
    step_d = Decimal(str(step))
    quantized = (val_d / step_d).quantize(Decimal('1'), rounding=ROUND_HALF_DOWN) * step_d
    return float(quantized)

def float_to_str(value: float) -> str:
    return f"{Decimal(str(value)):f}"

def deep_update(d: dict, u: dict) -> dict:
    for k, v in u.items():
        if isinstance(v, dict) and k in d and isinstance(d[k], dict):
            deep_update(d[k], v)
        else:
            d[k] = v
    return d

def get_config_summary(cfg: dict) -> str:
    lines = []
    
    app = cfg.get("app", {})
    risk = cfg.get("risk", {})
    
    lines.append("🎛 <b>[APP & RISK]</b>")
    lines.append(f"Name: <b>{app.get('name', 'N/A')}</b> | Quota: <b>{cfg.get('quota_asset', 'N/A')}</b>")
    lines.append(f"Max Positions: <b>{app.get('max_active_positions', 'N/A')}</b> | Hedge Mode: <b>{risk.get('hedge_mode', 'N/A')}</b>")
    
    lev_cfg = risk.get('leverage', {})
    lev_val = lev_cfg.get('val', 'N/A') if isinstance(lev_cfg, dict) else lev_cfg
    margin_mod = lev_cfg.get('margin_mode', 'N/A') if isinstance(lev_cfg, dict) else 'N/A'
    
    lines.append(f"Leverage: <b>{lev_val}x</b> | Margin Mode: <b>{margin_mod}</b>")
    lines.append(f"Margin Size: <b>{risk.get('margin_size', 'N/A')}</b> | Notional Limit: <b>{risk.get('notional_limit', 'N/A')} USDT</b>")

    log = cfg.get("log", {})
    tg = cfg.get("tg", {})
    lines.append("\n📡 <b>[SYSTEM]</b>")
    lines.append(f"TG Enabled: <b>{tg.get('enable', 'N/A')}</b>")
    
    # Блок Upbit
    upb = cfg.get("upbit", {})
    lines.append("\n🚀 <b>[UPBIT SIGNAL]</b>")
    lines.append(f"Default Side: <b>{upb.get('default_side', 'LONG').upper()}</b>")
    lines.append(f"Poll Interval: <b>{upb.get('poll_interval_sec', 'N/A')}s</b> | Proxies: <b>{len(upb.get('proxies', []))}</b>")

    # Сценарии выхода
    exit_cfg = cfg.get("exit", {})
    scen = exit_cfg.get("scenarios", {})
    
    sl = scen.get("stop_loss", {})
    ttl = scen.get("ttl_market_close", {})
    grid = scen.get("grid_tp", {})

    lines.extend([
        "────────────────────────────────────────",
        "🔹 [EXIT STRATEGY]",
        f"   Grid TP:   {'ON' if grid.get('enable') else 'OFF'}",
        f"   Stop-Loss: {'ON' if sl.get('enable') else 'OFF'} ({sl.get('percent', 5.0)}% | {sl.get('ttl_sec', 0.0)}s)",
        f"   TTL Close: {'ON' if ttl.get('enable') else 'OFF'} ({ttl.get('ttl_sec', 3600)}s)",
        "────────────────────────────────────────"
    ])

    return "\n".join(lines)

def load_json(filepath: str, default: Any = None) -> Any:
    if not os.path.exists(filepath): return default if default is not None else {}
    try:
        with open(filepath, "r", encoding="utf-8") as f: return json.load(f)
    except Exception as e:
        return default if default is not None else {}

def save_json_safe(filepath: str, data: Any) -> None:
    tmp_file = f"{filepath}.{os.getpid()}.tmp"
    for attempt in range(3):
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            os.replace(tmp_file, filepath)
            return
        except Exception:
            import time
            time.sleep(0.05 * (attempt + 1))
        finally:
            if os.path.exists(tmp_file):
                try: os.remove(tmp_file)
                except: pass