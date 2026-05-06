import asyncio
import ujson
import os
import sys
from decimal import Decimal, ROUND_DOWN
from curl_cffi.requests import AsyncSession

# Добавляем корень проекта в путь, чтобы импортировать наши модули если надо
sys.path.append(os.getcwd())

async def test_wif_qty():
    log_file = "logs/qty_test.log"
    os.makedirs("logs", exist_ok=True)
    
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("\n" + "="*60 + "\n")
        f.write(f"🚀 ЗАПУСК ТЕСТА РАСЧЕТА ОБЪЕМА (WIFUSDT)\n")
        
        async with AsyncSession(impersonate="chrome120") as session:
            # 1. Запрос спецификаций
            url = "https://api.phemex.com/public/products"
            resp = await session.get(url)
            data = resp.json()
            
            # Ищем WIFUSDT
            products = data.get("data", {}).get("perpProductsV2", [])
            wif_spec = next((p for p in products if p["symbol"] == "WIFUSDT"), None)
            
            if not wif_spec:
                f.write("❌ WIFUSDT не найден в списке perpProductsV2\n")
                return

            f.write(f"✅ Спецификации WIFUSDT получены:\n")
            f.write(f"   - qtyStepSize: {wif_spec.get('qtyStepSize')}\n")
            f.write(f"   - minQty (Phemex): {wif_spec.get('minOrderQty')}\n")
            
            # 2. Параметры теста
            notional_usd = 301.0
            safety_factor = 0.98
            # Допустим цена WIF сейчас ~0.22 (возьмем из тикера если сможем, или просто для теста)
            # Но лучше возьмем реальную цену из тикера для честности
            ticker_url = "https://api.phemex.com/md/ticker/24hr?symbol=WIFUSDT"
            t_resp = await session.get(ticker_url)
            t_data = t_resp.json()
            current_price = float(t_data.get("data", {}).get("last", 0.22))
            
            f.write(f"\n📊 ВХОДНЫЕ ДАННЫЕ:\n")
            f.write(f"   - Целевой объем: ${notional_usd}\n")
            f.write(f"   - Коэффициент запаса: {safety_factor}\n")
            f.write(f"   - Текущая цена (last): {current_price}\n")
            
            # 3. Расчет как в боте
            lot_size = float(wif_spec.get("qtyStepSize", 1.0))
            
            # Метод из бота (копия для чистоты теста)
            def round_step(value, step):
                val_d = Decimal(str(value))
                step_d = Decimal(str(step))
                rounded = (val_d / step_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_d
                return float(rounded)

            raw_qty = (notional_usd * safety_factor) / current_price
            final_qty = round_step(raw_qty, lot_size)
            final_vol = final_qty * current_price
            
            f.write(f"\n🧪 ХОД РАСЧЕТА:\n")
            f.write(f"   1. Чистый Qty (без округления): {raw_qty}\n")
            f.write(f"   2. Округление по lot_size ({lot_size}): {final_qty}\n")
            f.write(f"   3. Итоговый объем в USDT: {final_vol:.4f}\n")
            
            if final_vol <= notional_usd:
                f.write(f"\n✅ РЕЗУЛЬТАТ: ОБЪЕМ КОРРЕКТЕН. {final_vol:.4f} <= {notional_usd}\n")
            else:
                f.write(f"\n❌ РЕЗУЛЬТАТ: ПЕРЕЛИВ! {final_vol:.4f} > {notional_usd}\n")
            
            f.write("="*60 + "\n")

    print(f"Тест завершен. Результаты в {log_file}")

if __name__ == "__main__":
    asyncio.run(test_wif_qty())
