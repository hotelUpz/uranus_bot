# import time
# from curl_cffi import requests

# # Твои прокси в формате ip:port:user:pass
# RAW_PROXIES = [
#     "203.227.45.166:6014:smart-rvjai9lybinm:Y03Lp1sVqvdmikHm",
#     "203.227.190.239:6014:smart-rvjai9lybinm:Y03Lp1sVqvdmikHm"
# ]

#         # "proxies": [
#         #     "http://smart-rvjai9lybinm:Y03Lp1sVqvdmikHm@203.227.45.166:6014",
#         #     "http://smart-rvjai9lybinm:Y03Lp1sVqvdmikHm@203.227.190.239:6014"
#         # ],

# # Боевые заголовки из проекта
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

# def check_upbit(label, proxies=None):
#     print(f"\n--- Checking UPBIT: {label} ---")
#     url = "https://api-manager.upbit.com/api/v1/announcements"
#     params = {"category": "trade", "page": 1, "per_page": 1, "os": "web"}
    
#     try:
#         start = time.perf_counter()
#         r = requests.get(url, 
#                         params=params, 
#                         headers=HEADERS, 
#                         proxies=proxies, 
#                         timeout=10, 
#                         impersonate="chrome120")
#         rtt = (time.perf_counter() - start) * 1000
        
#         if r.status_code == 200:
#             print(f"  Upbit Status: 200 OK (SUCCESS!) | Time: {rtt:.0f}ms")
#         elif r.status_code == 403:
#             print(f"  Upbit Status: 403 Forbidden (IP BANNED) | Time: {rtt:.0f}ms")
#         elif r.status_code == 429:
#             print(f"  Upbit Status: 429 Rate Limit | Time: {rtt:.0f}ms")
#         else:
#             print(f"  Upbit Status: {r.status_code} | Time: {rtt:.0f}ms")
            
#     except Exception as e:
#         print(f"  Upbit FAILED: {e}")

# if __name__ == "__main__":
#     for p in RAW_PROXIES:
#         try:
#             parts = p.split(':')
#             if len(parts) != 4: continue
#             ip, port, user, pwd = parts
#             proxy_url = f"http://{user}:{pwd}@{ip}:{port}"
#             proxies = {"http": proxy_url, "https": proxy_url}
#             check_upbit(f"PROXY: {ip}", proxies)
#         except Exception as e:
#             print(f"ERROR: {e}")