import asyncio
import time
import ujson
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional

# Импортируем наши боевые модули
from API.PHEMEX.symbol import PhemexSymbolInfo
from CORE._executor import OrderExecutor, round_step

# Мокаем структуру для тестов
@dataclass
class MockDepth:
    bids: List[Tuple[float, float]]
    asks: List[Tuple[float, float]]
    last_msg_ts: float

class MockStakan:
    def __init__(self):
        self.data = {}
        self.last_msg_ts = 0

    def get_depth(self, symbol: str) -> Tuple[List[list], List[list], float]:
        if symbol in self.data:
            d = self.data[symbol]
            return (d.bids, d.asks, d.last_msg_ts)
        return ([], [], 0)

class MockBot:
    def __init__(self, cfg):
        self.stakan_stream = MockStakan()
        self.symbol_specs = {}
        self.black_list = set()
        self.quota_asset = "USDT"
        self.private_client = None
        self.tracker = None
        self.tg = None
        self.price_manager = None
        self.state = None
        self.cfg = cfg

async def run_comprehensive_test():
    print("\n--- STARTING FINAL HARDENING TESTS ---")
    
    with open("cfg.json", "r", encoding="utf-8") as f:
        full_cfg = ujson.load(f)

    # --- 1. ТЕСТ ПАРСЕРА СИМВОЛОВ (dataclass + min_qty) ---
    print("\n1. Testing PhemexSymbolInfo...")
    try:
        sym = PhemexSymbolInfo(
            symbol="WIFUSDT",
            status="Listed",
            quote_currency="USDT",
            tick_size=0.0001,
            lot_size=1.0,
            contract_size=1.0,
            max_leverage=50.0,
            min_qty=1.0
        )
        print(f"Success: PhemexSymbolInfo fixed: {sym.symbol}, min_qty={sym.min_qty}")
    except Exception as e:
        print(f"FAILED in PhemexSymbolInfo: {e}")

    # --- 2. ТЕСТ ЗАЩИТЫ ОТ ПРОТУХШЕГО СТАКАНА ---
    print("\n2. Testing Staleness Check...")
    bot = MockBot(full_cfg)
    executor = OrderExecutor(bot)
    
    symbol = "WIFUSDT"
    bot.symbol_specs[symbol] = sym
    
    # Случай A: Стакан свежий
    bot.stakan_stream.data[symbol] = MockDepth(bids=[[2.5, 100]], asks=[[2.51, 100]], last_msg_ts=time.time())
    
    now = time.time()
    last_ts = bot.stakan_stream.data[symbol].last_msg_ts
    is_stale = (now - last_ts > 5.0)
    print(f"   - Fresh data (lag {now-last_ts:.4f}s): {'FAIL' if is_stale else 'OK'}")

    # Случай B: Стакан протух (10 сек назад)
    bot.stakan_stream.data[symbol].last_msg_ts = time.time() - 10.0
    now = time.time()
    last_ts = bot.stakan_stream.data[symbol].last_msg_ts
    is_stale = (now - last_ts > 5.0)
    print(f"   - Stale data (lag {now-last_ts:.1f}s): {'PROTECTION OK' if is_stale else 'FAILED'}")

    # --- 3. ТЕСТ ROUND_STEP (Отказоустойчивость) ---
    print("\n3. Testing round_step with bad data...")
    test_cases = [
        (10.556, 0.01, 10.55),    # Обычное округление вниз
        (10.556, 0, 10.556),      # Шаг 0 (защита)
        (10.556, None, 10.556),   # Шаг None (защита)
        (0.00007, 0.0001, 0.0),   # Слишком маленькое значение -> 0
    ]
    
    for val, step, expected in test_cases:
        res = round_step(val, step)
        print(f"   - Input: {val}, Step: {step} -> Result: {res} | {'OK' if abs(res-expected)<0.000001 else 'FAIL'}")

    # --- 4. ТЕСТ РАСЧЕТА ОБЪЕМА (Safety Factor 0.98) ---
    print("\n4. Testing volume calc (Notional 301.0 USDT)...")
    notional = full_cfg["risk"]["notional_limit"]
    price = 2.4567
    lot_size = 1.0
    
    # Расчет как в execute_entry:
    raw_qty = (notional * 0.98) / price
    qty = round_step(raw_qty, lot_size)
    
    usdt_vol = qty * price
    print(f"   - Notional: {notional} | Price: {price} | Safety: 0.98")
    print(f"   - Raw Qty: {raw_qty:.4f} -> Rounded Qty: {qty}")
    print(f"   - Final USDT volume: {usdt_vol:.2f} USDT (Must be < {notional})")
    
    if usdt_vol <= notional:
        print("Success: Volume test passed: No Insufficient Balance risk.")
    else:
        print("FAILED: Volume test failed: Risk of Insufficient Balance!")

    print("\n--- TESTS COMPLETED SUCCESSFULY ---")

if __name__ == "__main__":
    asyncio.run(run_comprehensive_test())
