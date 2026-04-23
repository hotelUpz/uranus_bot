import asyncio
from curl_cffi import requests

async def test_phemex():
    # Пробуем эндпоинт конфигурации V2
    url = "https://api.phemex.com/exchange/public/cfg/v2/products"
    print(f"Testing: {url}")
    try:
        resp = requests.get(url, impersonate="chrome120", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # В cfg/v2/products структура: {"code":0, "msg":"OK", "data": {"products": [...]}}
            products = data.get("data", {}).get("products", [])
            print(f"Total products: {len(products)}")
            
            if products:
                # Ищем BTCUSDT (Linear)
                for p in products:
                    if p.get("symbol") == "BTCUSDT":
                        print(f"FOUND BTCUSDT: {p}")
                        break
        else:
            print("Status:", resp.status_code)
            print("Body:", resp.text[:200])
    except Exception as e:
        print(f"FAILED: {e}")

if __name__ == "__main__":
    asyncio.run(test_phemex())
