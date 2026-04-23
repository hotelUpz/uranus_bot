import asyncio
import time
from curl_cffi import requests

# Твои прокси в формате IP:PORT:USER:PASS
RAW_PROXIES = [
    "203.227.45.166:6014:smart-rvjai9lybinm:Y03Lp1sVqvdmikHm",
    "203.227.190.239:6014:smart-rvjai9lybinm:Y03Lp1sVqvdmikHm",
    None  # Локальный сервер (для сравнения)
]

def format_proxy(raw_proxy: str | None) -> dict | None:
    """Конвертирует формат ip:port:user:pass в словарь для curl_cffi"""
    if not raw_proxy:
        return None
        
    parts = raw_proxy.split(':')
    if len(parts) == 4:
        ip, port, user, pwd = parts
        proxy_url = f"http://{user}:{pwd}@{ip}:{port}"
        return {"http": proxy_url, "https": proxy_url}
    
    # Если прокси уже в правильном формате URL
    return {"http": raw_proxy, "https": raw_proxy}

async def check_proxy(raw_proxy: str | None):
    label = raw_proxy.split(':')[0] if raw_proxy else "Localhost"
    proxies = format_proxy(raw_proxy)
    
    print(f"🔄 Тестируем: {label} ...")
    
    # Поднимаем сессию с маскировкой под Chrome 120
    async with requests.AsyncSession(impersonate="chrome120", proxies=proxies, timeout=10.0) as session:
        
        # --- ШАГ 1: ПРОВЕРКА ЛОКАЦИИ И ПРОВАЙДЕРА ---
        try:
            resp_info = await session.get("http://ip-api.com/json/")
            data = resp_info.json()
            if data.get("status") == "success":
                real_ip = data.get("query")
                country = data.get("country")
                city = data.get("city")
                isp = data.get("isp")
                print(f"  📍 [INFO] IP: {real_ip} | Локация: {country}, {city} | ISP: {isp}")
            else:
                print(f"  📍 [INFO] Ошибка определения локации: {data}")
        except Exception as e:
            print(f"  ❌ [INFO] Не удалось проверить IP: {e}")


        # --- ШАГ 2: БОЕВОЙ ПИНГ ДО UPBIT ---
        upbit_url = "https://api-manager.upbit.com/api/v1/announcements"
        params = {"category": "trade", "page": 1, "per_page": 1, "os": "web"}
        
        # Делаем "прогревочный" запрос (инициализация TLS хэндшейка занимает время)
        try:
            await session.get(upbit_url, params=params)
        except Exception:
            pass

        # Делаем чистовой замер
        start_time = time.time()
        try:
            resp_upbit = await session.get(upbit_url, params=params)
            rtt_ms = (time.time() - start_time) * 1000
            
            if resp_upbit.status_code == 200:
                print(f"  ⚡ [PING] Upbit RTT: {rtt_ms:.2f} ms | Статус: 200 OK")
            else:
                print(f"  ⚠️ [PING] Upbit ответил статусом: {resp_upbit.status_code} | RTT: {rtt_ms:.2f} ms")
                
        except Exception as e:
            print(f"  ❌ [PING] Запрос к Upbit провалился: {e}")
            
    print("-" * 50)

async def main():
    print(f"🚀 Запуск проверки {len(RAW_PROXIES)} каналов...\n" + "="*50)
    
    # Можно запустить параллельно через asyncio.gather, 
    # но для чистоты замера пинга лучше прогнать их по очереди:
    for p in RAW_PROXIES:
        await check_proxy(p)

if __name__ == "__main__":
    asyncio.run(main())