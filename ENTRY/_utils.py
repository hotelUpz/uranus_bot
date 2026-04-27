# # ============================================================
# # FILE: ENTRY/_utils.py
# # ROLE: UPBIT SIGNAL
# # ============================================================

import datetime

def get_upbit_status_and_sleep_time():
    """
    Проверяет текущее время по Сеулу (KST).
    Возвращает: (is_working_window: bool, sleep_seconds: float)
    Рабочее окно: Пн-Пт, 08:30 - 18:30 (KST).
    """
    # KST фиксированно UTC+9, без переходов на летнее время
    kst_tz = datetime.timezone(datetime.timedelta(hours=9))
    now_kst = datetime.datetime.now(kst_tz)
    
    # Границы рабочего дня (с небольшим запасом по краям)
    start_h, start_m = 8, 30
    end_h, end_m = 18, 30
    
    is_weekday = now_kst.weekday() < 5  # 0-4 это Пн-Пт
    curr_time = now_kst.time()
    start_time = datetime.time(start_h, start_m)
    end_time = datetime.time(end_h, end_m)
    
    # Мы в активной фазе?
    is_working_hours = start_time <= curr_time <= end_time
    
    if is_weekday and is_working_hours:
        return True, 0.0
        
    # Если мы тут, значит время нерабочее. Вычисляем следующий старт.
    next_start = now_kst.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    
    # Если рабочий день уже закончился, переносим старт на завтра
    if curr_time >= end_time:
        next_start += datetime.timedelta(days=1)
        
    # Если после всех переносов попадаем на субботу (5) или воскресенье (6),
    # крутим дни вперед до понедельника (0)
    while next_start.weekday() >= 5:
        next_start += datetime.timedelta(days=1)
        
    # Считаем точную разницу в секундах до следующего старта
    sleep_secs = (next_start - now_kst).total_seconds()
    return False, sleep_secs