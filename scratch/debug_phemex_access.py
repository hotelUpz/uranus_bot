import asyncio
import ujson
from curl_cffi.requests import AsyncSession

async def debug_phemex():
    async with AsyncSession(impersonate="chrome120") as session:
        url = "https://api.phemex.com/public/products"
        print(f"Testing Symbols: {url}")
        try:
            resp = await session.get(url, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                root = data.get("data", {})
                items = root.get("perpProductsV2", []) or root.get("perpProducts", [])
                
                usdt_pairs = []
                other_pairs = {}
                
                for item in items:
                    q = str(item.get("quoteCurrency") or item.get("settleCurrency") or "").upper()
                    if q == "USDT":
                        usdt_pairs.append(item.get("symbol"))
                    else:
                        other_pairs[q] = other_pairs.get(q, 0) + 1
                
                print(f"Total Perpetual items: {len(items)}")
                print(f"USDT pairs found: {len(usdt_pairs)}")
                print(f"Other currencies: {other_pairs}")
                
                if usdt_pairs:
                    print(f"First 10 USDT symbols: {usdt_pairs[:10]}")
                
                # Проверка конкретного WIFUSDT
                wif = [i for i in items if i.get("symbol") == "WIFUSDT"]
                if wif:
                    print(f"Found WIFUSDT! Data: {ujson.dumps(wif[0], indent=2)}")
                else:
                    print("WIFUSDT NOT FOUND in the list!")
            else:
                print(f"HTTP Error: {resp.status_code}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(debug_phemex())
