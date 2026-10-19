"""Крипта: доставка = HTML «без ночи» (8.01 scanSymbol: час открытия H1-бара МСК в [9, 23)); 30-минутка — по своему
часовому бару. Входы 09:30 … 23:00 МСК. Ночные не помечаются отправленными; MOEX не затронут.
19.10: PEPE ЛП-шорт, 30-минутка 09:00–09:30 → уходит подписке на скане 09:30:40. Запуск: python tests/test_send_hours.py"""
import os, sys, tempfile, asyncio
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
import storage, screener, html_pipeline, hwv1
storage.init()
fails = 0
def chk(n, c, x=""):
    global fails; fails += not c; print(("OK   " if c else "FAIL ") + n + ("" if c else f"  {x}"))
MSK = timezone(timedelta(hours=3))
bar = lambda d, h, m=0: int(datetime(2026, 10, d, h, m, tzinfo=MSK).timestamp() * 1000)   # открытие бара, МСК
# часовые бары (вход = открытие + 1ч) и 30-минутки (вход = открытие + 30м)
for (h, m), ok in [((8, 0), False), ((8, 30), False), ((9, 0), True), ((12, 30), True), ((22, 0), True), ((22, 30), True),
                   ((23, 0), False), ((23, 30), False), ((0, 0), False), ((6, 0), False)]:
    ts = bar(19, h, m)
    chk(f"bar {h:02d}:{m:02d} МСК → send {ok}", screener.crypto_send_hour_ok(ts) == ok)
    chk(f"bar {h:02d}:{m:02d}: same as engine noNight (hwv1._is_night_msk of H1 bar)",
        screener.crypto_send_hour_ok(ts) == (not hwv1._is_night_msk(ts // 3_600_000 * 3_600_000)))
html_pipeline.apply_all = lambda cs, *a, **k: [dict(c) for c in cs]
html_pipeline.best_pipeline = lambda *a, **k: []
storage.is_duplicate = lambda *a, **k: False
mk = lambda t, ts, src30=False, mkt="crypto": {"ticker": t, "side": "SHORT", "strategy": "fbo", "market": mkt, "level": 1.0 + len(t),
    "last": 1.0, "stop": 1.02, "take": 0.94, "score": 5, "signal_ts": ts, "src30": src30, "feat": {}}
flt = lambda cid: {"markets": ["crypto", "ru"], "_multi": [
    {"_name": "Авто", "_market": "crypto", "_strategy": "fbo", "filters": {}},
    {"_name": "Авто", "_market": "ru", "_strategy": "fbo", "filters": {}}]}
def run(cards, now, hours, cid):
    sent, st = [], {}
    async def on_sig(c, ids): sent.append(c)
    asyncio.run(screener._deliver_html(cards, [cid], flt, now, 12, on_sig, None, st, min_send_ts=screener.send_min_ts(now, hours)))
    return sorted(c["ticker"] for c in sent), st.get(cid, {})
# 1) подписка, скан 19.10 09:30:40: PEPE (30-минутка 09:00) уходит; 30-минутка 08:30 (вход 09:00) — ночь
s, st = run([mk("PEPEUSDT", bar(19, 9, 0), True), mk("EARLYUSDT", bar(19, 8, 30), True)], bar(19, 9, 30) + 40_000, 1, 1)
chk("09:30:40 subscription: PEPE 09:30 sent, 09:00 entry is night", s == ["PEPEUSDT"] and st.get("night") == 1, (s, st))
chk("night not marked sent", not storage.was_sent_to_chat(1, "EARLYUSDT", bar(19, 8, 30), "fbo", "SHORT"))
# 2) подписка, 10:00:40: часовой бар 09:00 (вход 10:00) уходит
s, st = run([mk("H9USDT", bar(19, 9))], bar(19, 10) + 40_000, 1, 2)
chk("10:00:40: H1 bar 09:00 sent", s == ["H9USDT"], s)
# 3) вечер: 22:30 (30м, вход 23:00) и бар 22:00 (вход 23:00) уходят на 23:00:40; бар 23:00 (вход 00:00) — ночь
s, st = run([mk("E2230USDT", bar(19, 22, 30), True), mk("E22USDT", bar(19, 22)), mk("E23USDT", bar(19, 23))], bar(20, 0) + 40_000, 2, 3)
chk("evening: 22:00 / 22:30 bars sent, 23:00 bar night", s == ["E2230USDT", "E22USDT"] and st.get("night") == 1, (s, st))
# 4) ручной скан (12ч) 10:00: ночные 06–08 не уходят, 09:xx уходят; MOEX не затронут
cards = [mk(f"C{h}USDT", bar(19, h)) for h in (6, 7, 8, 9)] + [mk("SBER", bar(19, 7), mkt="ru")]
s, st = run(cards, bar(19, 10) + 40_000, screener.MANUAL_SEND_HOURS, 4)
chk("manual 10:00: C9 + MOEX (night rule crypto only)", s == ["C9USDT", "SBER"] and st.get("night") == 3, (s, st))
print("FAILS", fails); sys.exit(1 if fails else 0)
