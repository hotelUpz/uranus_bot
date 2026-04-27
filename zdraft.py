# {
#     "app": {
#         "name": "Uranos Bot",
#         "max_active_positions": 1
#     },
#     "credentials": {
#         "api_key": "",
#         "api_secret": ""
#     },
#     "tg": {
#         "enable": true,
#         "token": "",
#         "chat_id": ""
#     },
#     "log": {
#         "debug": true,
#         "info": true,
#         "warning": true,
#         "error": true,
#         "max_lines": 1500,
#         "timezone": "UTC"
#     },
#     "black_list": [
#     ],
#     "quota_asset": "USDT",
#     "risk": {
#         "leverage": {
#             "set_previous": false,
#             "used_by_cache": false,
#             "val": 22,
#             "delay_sec": 0.3,
#             "margin_mode": 2
#         },
#         "notional_limit": 301.0,
#         "_is_validate_notional_limit": false
#     },
#     "upbit": {
#         "use_mock": false,
#         "poll_interval_sec": 0.25,
#         "min_cooldown_sec": 0.25,
#         "proxies": [
#             null
#         ],
#         "default_side": "long",
#         "bang_sleep_sec": 30,
#         "adaptive": {
#             "enabled": false,
#             "step": 0.1,
#             "n_stable": 10
#         },
#         "prober": {
#             "start_delay": 1.0,
#             "step": 0.1,
#             "hits_per_step": 100
#         }
#     },
#     "entry": {
#         "_filds": {
#             "mode": "market | limit",
#             "limit_distance_pct": ""
#         },
#         "max_place_order_retries": 1,
#         "mode": "limit",
#         "limit_distance_pct": 5.0,
#         "signal_timeout_sec": 0.01,
#         "pattern": {
#             "upbit_signal": true
#         }
#     },
#     "exit": {
#         "max_place_order_retries": 1,
#         "min_order_life_sec": 0.05,
#         "scenarios": {
#             "grid_tp": {
#                 "enable": true,
#                 "map": [
#                     {
#                         "comment": "< $500K",
#                         "min_vol": 0,
#                         "max_vol": 500,
#                         "levels": [
#                             4,
#                             10,
#                             20,
#                             40,
#                             70
#                         ],
#                         "volumes": [
#                             20,
#                             20,
#                             20,
#                             20,
#                             20
#                         ]
#                     },
#                     {
#                         "comment": "$500K - $2M",
#                         "min_vol": 500,
#                         "max_vol": 2000,
#                         "levels": [
#                             3,
#                             8,
#                             16,
#                             30,
#                             55
#                         ],
#                         "volumes": [
#                             20,
#                             20,
#                             20,
#                             20,
#                             20
#                         ]
#                     },
#                     {
#                         "comment": "> $2M",
#                         "min_vol": 2000,
#                         "max_vol": null,
#                         "levels": [
#                             2,
#                             5,
#                             12,
#                             22,
#                             40
#                         ],
#                         "volumes": [
#                             20,
#                             20,
#                             20,
#                             20,
#                             20
#                         ]
#                     }
#                 ]
#             },
#             "stop_loss": {
#                 "enable": true,
#                 "trailing": true,
#                 "percent": 50.0,
#                 "stabilization_sec": 0,
#                 "ttl_sec": 0.0
#             },
#             "ttl_market_close": {
#                 "enable": true,
#                 "ttl_sec": 4500
#             }
#         }
#     }
# }