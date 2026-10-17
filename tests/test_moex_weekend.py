"""MOEX: сигналы только в будни (сб/вс МСК — торги выходного дня — не шлются и не помечаются), плановый скан
MOEX в сб/вс не запускается; крипта в выходные не затронута. Запуск: python tests/test_moex_weekend.py"""
import os, sys, tempfile, asyncio
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
import storage, screener, html_pipeline, hwv1
storage.init()
MSK = timezone(timedelta(hours=3))
fails = 0
def chk(n, c, x=""):
    global fails; fails += not c; print(("OK   " if c else "FAIL ") + n + ("" if c else f"  {x}"))
ms = lambda y, mo, d, h, mi=0: int(datetime(y, mo, d, h, mi, tzinfo=MSK).timestamp() * 1000)
# 16.10.2026 — пятница, 17 — суббота, 18 — воскресенье, 19 — понедельник
for (d, h, exp) in [(16, 12, False), (16, 23, False), (17, 0, True), (17, 12, True), (18, 18, True), (18, 23, True), (19, 0, False), (19, 10, False)]:
    chk(f"ru_weekend {d}.10 {h:02d}:00 МСК → {exp}", hwv1.ru_weekend(ms(2026, 10, d, h)) == exp)
chk("пт 13:00 — по-прежнему «пятница поздно»", hwv1._ru_friday_late(ms(2026, 10, 16, 13)) and not hwv1._ru_friday_late(ms(2026, 10, 16, 12)))
for (d, h, exp) in [(16, 13, True), (17, 13, False), (18, 10, False), (19, 10, True)]:
    chk(f"плановый скан MOEX {d}.10 {h}:16 → {exp}", screener.moex_scan_day_ok(ms(2026, 10, d, h, 16) / 1000) == exp)
# доставка в субботу 17.10 13:16 (ровно как в проде): MOEX-бар 12:00 сб — не шлётся; крипта 12:00 сб — шлётся
NOW = ms(2026, 10, 17, 13, 16)
mk = lambda t, m, ts: {"ticker": t, "side": "LONG", "strategy": "fbo", "market": m, "level": 100.0, "last": 1.0,
                       "stop": 0.98, "take": 1.06, "score": 5, "signal_ts": ts, "bar_ts": ts, "feat": {}}
cards = [mk("SBER", "ru", ms(2026, 10, 17, 12)), mk("GAZP", "ru", ms(2026, 10, 17, 10)),
         mk("LKOH", "ru", ms(2026, 10, 16, 11)),           # пятница 11:00 — будний, в окне 12 ч? нет: старше → too_old
         mk("BTCUSDT", "crypto", ms(2026, 10, 17, 11))]
html_pipeline.apply_all = lambda cs, *a, **k: [dict(c) for c in cs]
html_pipeline.best_pipeline = lambda *a, **k: []
storage.is_duplicate = lambda *a, **k: False
sent = []
async def on_sig(c, ids): sent.append(c)
flt = lambda cid: {"markets": ["crypto", "ru"], "_multi": [
    {"_name": "Авто", "_market": "crypto", "_strategy": "fbo", "filters": {}},
    {"_name": "Авто", "_market": "ru", "_strategy": "fbo", "filters": {}}]}
st = {}
asyncio.run(screener._deliver_html(cards, [1], flt, NOW, 12, on_sig, None, st,
                                   min_send_ts=screener.send_min_ts(NOW, screener.MANUAL_SEND_HOURS)))
chk("сб 13:16 ручной скан: MOEX не уходит, крипта уходит", sorted(c["ticker"] for c in sent) == ["BTCUSDT"], [c["ticker"] for c in sent])
chk("MOEX выходного дня не помечен отправленным", not storage.was_sent_to_chat(1, "SBER", ms(2026, 10, 17, 12), "fbo", "LONG"))
# будний день: тот же MOEX-сигнал в понедельник уходит
sent.clear(); NOW2 = ms(2026, 10, 19, 13, 16)
asyncio.run(screener._deliver_html([mk("SBER", "ru", ms(2026, 10, 19, 12))], [2], flt, NOW2, 12, on_sig, None, {},
                                   min_send_ts=screener.send_min_ts(NOW2, 3)))
chk("пн 13:16: MOEX уходит", [c["ticker"] for c in sent] == ["SBER"], sent)
print("FAILS", fails); sys.exit(1 if fails else 0)
