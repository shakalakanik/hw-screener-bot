"""MOEX: ISS задерживает свечу на 15 мин — скан в HH:00:40 не видит закрывшийся бар
(раньше все MOEX-сигналы уходили в too_old). Отдельный проход в HH:16:40."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import moex, screener, bot
H=3_600_000
hs=1_792_000_000_000//H*H  # начало часа
def bar_open_ok(now):
    rows=[[1,1,1,1,1,1,None]]
    from datetime import datetime,timezone,timedelta
    beg=datetime.fromtimestamp((hs-H)/1000,timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S")
    rows[0][6]=beg
    b=moex.iss_bars(rows,"1h",now)[-1]
    return b.confirmed and b.ts>=screener.send_min_ts(now,1)
assert not bar_open_ok(hs+40_000), "в HH:00:40 бар ещё не подтверждён"
assert bar_open_ok(hs+bot.MOEX_SCAN_DELAY_S*1000), "в HH:16:40 бар подтверждён и в окне отправки"
assert bot.next_scan_ts(hs/1000+1, bot.MOEX_SCAN_DELAY_S)==hs/1000+1000
assert "ru" not in bot.NON_RU_MARKETS
print("ok")
assert bot.MOEX_SEND_HOURS >= 2
# окно MOEX: бар, закрывшийся 2 часа назад (пропущенный прошлым сканом), всё ещё отправляется
assert hs-2*H >= screener.send_min_ts(hs+bot.MOEX_SCAN_DELAY_S*1000, bot.MOEX_SEND_HOURS)
print("ok2")
