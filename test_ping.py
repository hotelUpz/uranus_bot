# # ============================================================
# # FILE: test_ping.py
# # ROLE: Замер пинга до Upbit Announcements API
# # ============================================================

# import asyncio
# import time
# import statistics
# from curl_cffi import requests as cffi_requests

# API_URL = "https://api-manager.upbit.com/api/v1/announcements"
# PARAMS  = {"category": "trade", "page": 1, "per_page": 10, "os": "web"}

# HEADERS = {
#     "accept": "application/json, text/plain, */*",
#     "accept-language": "en-US,en;q=0.9,ru;q=0.8",
#     "origin": "https://upbit.com",
#     "referer": "https://upbit.com/",
#     "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
#     "sec-ch-ua-mobile": "?0",
#     "sec-ch-ua-platform": '"Windows"',
#     "sec-fetch-dest": "empty",
#     "sec-fetch-mode": "cors",
#     "sec-fetch-site": "same-site",
# }

# ROUNDS     = 20       # Количество замеров
# PAUSE_SEC  = 0.5      # Пауза между запросами (чтобы не словить 429)

# async def main():
#     session = cffi_requests.AsyncSession(
#         impersonate="chrome120",
#         headers=HEADERS,
#         timeout=10.0
#     )

#     latencies = []
#     errors    = 0

#     print(f"\n  Upbit Ping Test  |  {ROUNDS} rounds  |  pause {PAUSE_SEC}s\n" + "-" * 52)

#     for i in range(1, ROUNDS + 1):
#         t0 = time.perf_counter()
#         try:
#             resp = await session.get(API_URL, params=PARAMS)
#             elapsed_ms = (time.perf_counter() - t0) * 1000

#             status = resp.status_code
#             if status == 200:
#                 notices = resp.json().get("data", {}).get("notices", [])
#                 latencies.append(elapsed_ms)
#                 print(f"  [{i:>2}/{ROUNDS}]  {elapsed_ms:>7.1f} ms  |  HTTP 200  |  notices: {len(notices)}")
#             elif status == 429:
#                 errors += 1
#                 print(f"  [{i:>2}/{ROUNDS}]  {elapsed_ms:>7.1f} ms  |  HTTP 429 RATE LIMITED")
#             else:
#                 errors += 1
#                 print(f"  [{i:>2}/{ROUNDS}]  {elapsed_ms:>7.1f} ms  |  HTTP {status}")

#         except Exception as e:
#             elapsed_ms = (time.perf_counter() - t0) * 1000
#             errors += 1
#             print(f"  [{i:>2}/{ROUNDS}]  {elapsed_ms:>7.1f} ms  |  ERROR: {e}")

#         if i < ROUNDS:
#             await asyncio.sleep(PAUSE_SEC)

#     # --- Итоги ---
#     print("\n" + "=" * 52)
#     if latencies:
#         avg  = statistics.mean(latencies)
#         med  = statistics.median(latencies)
#         mn   = min(latencies)
#         mx   = max(latencies)
#         p95  = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) >= 2 else mx
#         std  = statistics.stdev(latencies) if len(latencies) >= 2 else 0.0

#         print(f"  Успешных:   {len(latencies)} / {ROUNDS}")
#         print(f"  Ошибок:     {errors}")
#         print(f"  ---")
#         print(f"  Avg:        {avg:.1f} ms")
#         print(f"  Median:     {med:.1f} ms")
#         print(f"  Min:        {mn:.1f} ms")
#         print(f"  Max:        {mx:.1f} ms")
#         print(f"  P95:        {p95:.1f} ms")
#         print(f"  StdDev:     {std:.1f} ms")
#     else:
#         print(f"  Все {ROUNDS} запросов завершились с ошибкой.")
#     print("=" * 52 + "\n")

#     await session.close()

# if __name__ == "__main__":
#     asyncio.run(main())
