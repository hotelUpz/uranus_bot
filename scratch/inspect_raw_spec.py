import asyncio
import ujson
import os
from curl_cffi.requests import AsyncSession

async def inspect_wif_raw():
    async with AsyncSession(impersonate="chrome120") as session:
        url = "https://api.phemex.com/public/products"
        resp = await session.get(url)
        data = resp.json()
        
        products = data.get("data", {}).get("perpProductsV2", [])
        wif_spec = next((p for p in products if p["symbol"] == "WIFUSDT"), None)
        
        if wif_spec:
            print("\n--- RAW WIF SPEC ---")
            print(ujson.dumps(wif_spec, indent=4))
            print("--------------------\n")
        else:
            print("WIFUSDT not found in perpProductsV2")

if __name__ == "__main__":
    asyncio.run(inspect_wif_raw())
