"""Крипта: доставка только при показанном времени сигнала 10:00–22:00 МСК (включительно); ночные не
помечаются отправленными; MOEX не затронут. Запуск: python tests/test_send_hours.py"""
import os, sys, tempfile, asyncio
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
import storage, screener, html_pipeline
storage.init()
fails = 0
def chk(n, c, x=""):
    global fails; fails += not c; print(("OK   " if c else "FAIL ") + n + ("" if c else f"  {x}"))
def ts_shown(h):  # показанное время h:00 МСК 13.10 → signal_ts = открытие бара = h-1 МСК
    return int(datetime(2026, 10, 13, (h - 1 - 3) % 24, tzinfo=timezone.utc).timestamp() * 1000) - (86400000 if h - 4 < 0 else 0)
for h, ok in [(9, False), (10, True), (16, True), (22, True), (23, False), (0, False), (6, False)]:
    chk(f"shown {h:02d}:00 → {ok}", screener.crypto_send_hour_ok(ts_shown(h)) == ok)
# доставка: ручной скан в 10:00 с окном 5ч — уходит только 10:00; ночные не помечены отправленными
NOW = ts_shown(10) + 3600_000 + 40_000
cards = [{"ticker": f"C{h}USDT", "side": "SHORT", "strategy": "fbo", "market": "crypto", "level": 1.0 + h,
          "last": 1.0, "stop": 1.02, "take": 0.94, "score": 5, "signal_ts": ts_shown(h), "feat": {}} for h in (6, 7, 8, 9, 10)]
cards.append({**cards[0], "ticker": "SBER", "market": "ru", "level": 300.0})
html_pipeline.apply_all = lambda cs, *a, **k: [dict(c) for c in cs]
html_pipeline.best_pipeline = lambda *a, **k: []
storage.is_duplicate = lambda *a, **k: False
sent = []
async def on_sig(c, ids): sent.append(c)
flt = lambda cid: {"markets": ["crypto", "ru"], "_multi": [
    {"_name": "Авто", "_market": "crypto", "_strategy": "fbo", "filters": {}},
    {"_name": "Авто", "_market": "ru", "_strategy": "fbo", "filters": {}}]}
st = {}
asyncio.run(screener._deliver_html(cards, [1], flt, NOW, 12, on_sig, None, st, min_send_ts=NOW - 5 * 3600_000 - 3600_000))
chk("manual 10:00: only C10 + MOEX sent", sorted(c["ticker"] for c in sent) == ["C10USDT", "SBER"], [c["ticker"] for c in sent])
chk("night counted", st[1]["night"] == 4, st)
chk("night not marked sent", not storage.was_sent_to_chat(1, "C9USDT", ts_shown(9), "fbo", "SHORT"))
print("FAILS", fails); sys.exit(1 if fails else 0)
