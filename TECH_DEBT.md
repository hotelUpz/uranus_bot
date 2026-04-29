# Uranus Bot — TECH DEBT


## CRITICAL

1.
# # ============================================================
# # FILE: ENTRY/upbit_signal.py
# # ROLE: UPBIT SIGNAL
# # ============================================================

Сегодня еще раз просрали сигнал. Благо что монета отсутствовала на бирже, но сам сигнлал по монете BLEND был.

Отработка на сервере Railway (дальше буду с коробки ибо это капздец):

`

Starting Container
2026-04-28 19:44:40 | INFO | main | [LEV] Скип установки (Leverage & Margin), так как set_previous == false
2026-04-28 19:44:40 | INFO | main | [TG] Запуск супервизора Telegram...
🎛 <b>[ТЕКУЩИЙ КОНФИГ БОТА]</b>
🔹 <b>[BASIC APP SETTINGS]</b>
🔹 <b>[RISK MANAGEMENT AND LIMITS]</b>
  ▪️ name: <b>Uranos Bot</b>
  🔹 <b>[LEVERAGE]</b>
  ▪️ max_active_positions: <b>1</b>
    ▪️ set_previous: <b>OFF</b>
🔹 <b>[EXCHANGE ACCESS (API KEYS)]</b>
    ▪️ used_by_cache: <b>OFF</b>
  ▪️ api_key: <b></b>
  ▪️ api_secret: <b></b>
  ▪️ error: <b>ON</b>
🔹 <b>[TELEGRAM NOTIFICATIONS]</b>
  ▪️ max_lines: <b>1500</b>
2026-04-28 19:45:00 | INFO | bot | [RUN] Инициализация систем...
  ▪️ enable: <b>ON</b>
  ▪️ timezone: <b>UTC</b>
2026-04-28 19:45:00 | INFO | bot | [CFG] БОТ ЗАПУЩЕН С НАСТРОЙКАМИ
  ▪️ token: <b></b>
▪️ black_list: <b>0 элементов</b>
  ▪️ chat_id: <b></b>
▪️ quota_asset: <b>USDT</b>
🔹 <b>[LOGGING SETTINGS]</b>
  ▪️ debug: <b>ON</b>
  ▪️ info: <b>ON</b>
  ▪️ warning: <b>ON</b>
    ▪️ val: <b>22</b>
    ▪️ delay_sec: <b>0.3</b>
    ▪️ margin_mode: <b>2</b>
  ▪️ notional_limit: <b>301.0</b>
🔹 <b>[UPBIT MONITORING (SIGNAL SOURCE)]</b>
  ▪️ use_mock: <b>OFF</b>
      ▪️ percent: <b>50.0</b>
      ▪️ stabilization_sec: <b>0</b>
      ▪️ ttl_sec: <b>0.0</b>
    🔹 <b>[GRID_TP]</b>
      ▪️ enable: <b>ON</b>
    🔹 <b>[STOP_LOSS]</b>
      ▪️ enable: <b>ON</b>
      ▪️ map: <b>3 элементов</b>
  ▪️ max_place_order_retries: <b>1</b>
      ▪️ trailing: <b>ON</b>
  ▪️ min_order_life_sec: <b>0.1</b>
  🔹 <b>[SCENARIOS]</b>
    🔹 <b>[TTL_MARKET_CLOSE]</b>
      ▪️ enable: <b>ON</b>
      ▪️ ttl_sec: <b>4500</b>
────────────────────────────────────────
2026-04-28 19:45:01 | INFO | bot | [OK] Стейт синхронизирован. В памяти 0 активных позиций.
2026-04-28 19:45:01 | INFO | bot | [WS] Инициализация стрима стаканов для 517 пар...
2026-04-28 19:45:01 | INFO | bot | [REST] Запрашиваем Equity с биржи для инициализации аудита...
2026-04-28 19:45:01 | INFO | bot | [USD] Стартовый баланс зафиксирован: 71.07 USDT
2026-04-28 19:45:01 | INFO | api | 🌐 Stakan WS подключен: на 40 монет.
  ▪️ poll_interval_sec: <b>0.25</b>
  ▪️ min_cooldown_sec: <b>0.25</b>
  ▪️ proxies: <b>1 элементов</b>
  ▪️ default_side: <b>long</b>
  ▪️ bang_sleep_sec: <b>30</b>
  🔹 <b>[ADAPTIVE]</b>
    ▪️ enabled: <b>OFF</b>
    ▪️ step: <b>0.1</b>
    ▪️ n_stable: <b>10</b>
  🔹 <b>[PROBER]</b>
    ▪️ start_delay: <b>1.0</b>
    ▪️ step: <b>0.1</b>
    ▪️ hits_per_step: <b>100</b>
🔹 <b>[ENTRY LOGIC (SNIPER)]</b>
  ▪️ max_place_order_retries: <b>1</b>
  ▪️ mode: <b>limit</b>
  ▪️ limit_distance_pct: <b>5.0</b>
  ▪️ signal_timeout_sec: <b>0.01</b>
  🔹 <b>[PATTERN]</b>
    ▪️ upbit_signal: <b>ON</b>
🔹 <b>[EXIT LOGIC (TAKE-PROFITS AND STOPS)]</b>
2026-04-28 19:45:01 | INFO | bot | [WS] Прогрев кэша цен и фандинга...
2026-04-28 19:45:01 | INFO | api | 🌐 Stakan WS подключен: на 37 монет.
2026-04-28 19:45:02 | INFO | bot | [LOOP] Главная торговая живолупа (Game Loop) запущена.
2026-04-28 19:45:02 | INFO | bot | [WAIT] Ожидание готовности систем (Timeout: 30.0s)...
2026-04-28 19:45:02 | INFO | api | 🔐 Private WS Подключен. Отправляем авторизацию...
2026-04-28 19:45:02 | INFO | api | ✅ Авторизация успешна! Подписываемся на канал (aop_p.subscribe)...
2026-04-28 19:45:02 | INFO | api | 🎯 Подписка ПРИНЯТА БИРЖЕЙ! Канал USDT-фьючерсов открыт.
2026-04-28 19:45:02 | INFO | bot | [WS] Подписан на приватный канал. Запуск синхронизации стейта (Recover)...
2026-04-28 19:45:03 | INFO | bot | [FAST] Пре-фетч цен завершен. Получено 530 котировок.
2026-04-28 19:45:04 | INFO | bot | [OK] Системы готовы за 1.5с. Данные получены.
2026-04-28 19:45:04 | INFO | upbit_signal | [SYNC] Собираем базу текущих новостей перед запуском...
2026-04-28 19:45:04 | INFO | bot | 📊 Цикл отчетов запущен (каждые 6.0ч)
2026-04-28 19:45:04 | INFO | upbit_signal | [OK] Синхронизация завершена (собрано 10 ID). Ждем новые анонсы...
2026-04-28 19:45:04 | INFO | upbit_signal | [Localhost] Биржа спит. Анабиоз на 3.75 ч.
   Засыпаем:    2026-04-28 19:45:04 UTC | 2026-04-29 04:45:04 KST
   Пробуждение: 2026-04-28 23:30:00 UTC | 2026-04-29 08:30:00 KST
2026-04-28 19:45:04 | INFO | upbit_signal | [RUN] STARTING WORKERS (STATIC)
2026-04-28 19:45:04 | INFO | upbit_signal |   Target interval: 0.25s
2026-04-28 19:45:04 | INFO | upbit_signal |   Cooldown per slot: 0.25s
2026-04-28 23:30:00 | INFO | upbit_signal | [Localhost] ПРОБУЖДЕНИЕ. Факт: 2026-04-28 23:30:00 UTC | 2026-04-29 08:30:00 KST. Возобновляем парсинг...
2026-04-29 02:23:37 | INFO | upbit_signal | [UPBIT] Закрытие сессий мониторинга...
2026-04-29 07:22:44 | ERROR | api | Error fetching tickers: Failed to perform, curl: (28) Operation timed out after 10000 milliseconds with 0 bytes received. See https://curl.se/libcurl/c/libcurl-errors.html first for more details.


`


Предлагаю получить тебе сырой ответ от upbit и сохранить его в джейсон (единожды) -- чтобы и мне было видно, потом надстройку в коде которая отвечала за такое сохранение удалить -- чтобы не мешалась и не дергала лишние ресурсы.

Тщательно изучить всю структуру ответа, все ключи (без фантазий и догадок). На основании этого ответа проанализировать и понять где мы снова проебались. Закрвыть вопрос раз и навсегда чтобы сигналы больше не пропускались.

Кстати в закомете ключевого файла upbit_signal.py я сохранил старый артефакт в закоменте -- где мы проебали аж 2 сигнала в свое время.

Еще смотри. Прокси никакие не использую, юзаю только напрпямую  ссервера, ошибок парса вроде 429 не было, планировщик вроде тоже отрабатывал во-время.

Смотри, я не поленился и запустил код. Он сразу выдал файл live_signals.json. На этом этапе все хорошо.

был сигнал:
        "symbol": "BLEND",
        "announce_ts_ms": 1777429411000,
        "announce_ts_str": "2026-04-29 02:23:31 UTC",
        "received_ts_ms": 1777462792940,
        "received_ts_str": "2026-04-29 11:39:52.940000 ",
        "latency_sec": 33381.94,
        "status": "INIT"