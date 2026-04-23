# import asyncio
# import time
# import aiohttp

# # Импортируем твои модули
# from API.PHEMEX.order import PhemexPrivateClient
# from API.PHEMEX.test_order_fast import PhemexFastClient
# from API.PHEMEX.ticker import PhemexTickerAPI
# from utils import float_to_str

# # Конфигурация
# API_KEY = "094ade21-a721-4b34-b6c9-2c13a3aaa56f"
# API_SECRET = "jTOP6Z90qywJrFVmX-W5Q9XNaS2Lx34ujxJ9IlGMpkVjNmQ4YTc5ZS1lNTMzLTRjN2UtYTI0NC01NzJhMjUyMzBiNTk"
# SYMBOL = "ETHUSDT"
# AMOUNT_USD = 40.0

# async def get_target_price():
#     """Используем твой штатный метод для получения цены"""
#     api = PhemexTickerAPI()
#     try:
#         tickers = await api.get_all_tickers()
#         t_data = tickers.get(SYMBOL)
#         if not t_data:
#             raise ValueError(f"Не удалось найти цену для {SYMBOL}")
#         return t_data.price
#     finally:
#         await api.aclose()

# async def run_test_for_client(client_name, client, qty):
#     print(f"\n>>> Тестируем: {client_name}")
#     latencies = []

#     for i in range(1, 3):
#         print(f"  Итерация {i}...")
        
#         # 1. MARKET BUY
#         start_buy = time.perf_counter()
#         try:
#             await client.place_market_order(SYMBOL, "Buy", qty, "Long")
#             end_buy = time.perf_counter()
#             buy_time = (end_buy - start_buy) * 1000
#             latencies.append(buy_time)
#             print(f"    [BUY]  Успешно за {buy_time:.2f} ms")
#         except Exception as e:
#             print(f"    [BUY]  Ошибка: {e}")

#         await asyncio.sleep(1.5) # Пауза для стабильности

#         # 2. MARKET SELL (Close)
#         start_sell = time.perf_counter()
#         try:
#             await client.place_market_order(SYMBOL, "Sell", qty, "Long", reduce_only=True)
#             end_sell = time.perf_counter()
#             sell_time = (end_sell - start_sell) * 1000
#             latencies.append(sell_time)
#             print(f"    [SELL] Успешно за {sell_time:.2f} ms")
#         except Exception as e:
#             print(f"    [SELL] Ошибка: {e}")
        
#         await asyncio.sleep(1.5)

#     if not latencies:
#         return 0
#     return sum(latencies) / len(latencies)

# async def main():
#     print("🚀 Подготовка к тесту скорости...")
    
#     # 0. Расчет объема
#     try:
#         price = await get_target_price()
#         # Для ETH обычно 3 знака после запятой (0.001)
#         qty = round(AMOUNT_USD / price, 3)
#         print(f"Актуальная цена {SYMBOL}: {price} | Объем на ${AMOUNT_USD}: {qty}")
#     except Exception as e:
#         print(f"❌ Ошибка подготовки: {e}")
#         return


#     # --- ТЕСТ 1: CURL_CFFI (Теперь ПЕРВЫЙ) ---
#     client_fast = PhemexFastClient(API_KEY, API_SECRET)
#     try:
#         print("\n🔥 Прогрев CURL_CFFI (холостой запрос)...")
#         await client_fast._request("GET", "/g-accounts/accountPositions", query_no_q="currency=USDT")
        
#         avg_fast = await run_test_for_client("CURL_CFFI (Fast)", client_fast, qty)
#     finally:
#         await client_fast.close()

#     # 1. Тест AIOHTTP
#     async with aiohttp.ClientSession() as session:
#         client_aio = PhemexPrivateClient(API_KEY, API_SECRET, session)
#         avg_aio = await run_test_for_client("AIOHTTP (Стандарт)", client_aio, qty)

#     # ИТОГИ
#     print("\n" + "="*50)
#     print(f"РЕЗУЛЬТАТЫ СРАВНЕНИЯ (Средний RTT):")
#     print(f"  - aiohttp:   {avg_aio:.2f} ms")
#     print(f"  - fast:      {avg_fast:.2f} ms")
    
#     if avg_aio > 0 and avg_fast > 0:
#         diff = avg_aio - avg_fast
#         gain = (diff / avg_aio) * 100
#         if diff > 0:
#             print(f"\n✅ Fast-клиент быстрее на {diff:.2f} ms ({gain:.1f}%)")
#         else:
#             print(f"\n⚠️ Fast-клиент медленнее на {abs(diff):.2f} ms. Проверь нагрузку на CPU.")
#     print("="*50)

# if __name__ == "__main__":
#     asyncio.run(main())
