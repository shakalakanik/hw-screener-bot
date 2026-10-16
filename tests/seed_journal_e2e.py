"""(часовой пояс машины = MSK, как у ISS) Сид реальных строк журнала (SBER, BTCUSDT по реальным свечам) для debug-пользователя 77 в тестовой БД."""
import os, sys, json, time, urllib.request
os.environ.setdefault("DB_PATH", "/tmp/hw_e2e_bot.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import storage
H = 3600000; now = int(time.time() * 1000)
def get(u): return json.loads(urllib.request.urlopen(u, timeout=20).read())
def bybit(ago):
    r = get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=60&end={now-ago*H}&limit=1")["result"]["list"][0]
    return int(r[0]), float(r[4])
def iss(ago):
    d = time.strftime("%Y-%m-%d", time.gmtime((now - ago * H) / 1000 + 3 * 3600))
    j = get(f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/SBER/candles.json?interval=60&from={d}&iss.meta=off")["candles"]
    r = j["data"][0]; c = j["columns"]
    return int(time.mktime(time.strptime(r[c.index("begin")], "%Y-%m-%d %H:%M:%S")) * 1000), float(r[c.index("close")])
with storage._conn() as c:
    storage._ensure_signal_journal(c); c.execute("DELETE FROM signal_journal WHERE chat_id=77")
for mkt, tick, f in (("ru", "SBER", iss), ("crypto", "BTCUSDT", bybit)):
    for ago, dd in ((300, 0.002), (30 if mkt == "ru" else 20, 0.05)):
        ts, e = f(ago)
        print(storage.add_signal_journal(77, {"market": mkt, "ticker": tick, "side": "LONG", "strategy": "fbo", "signal_ts": ts,
              "entry": e, "last": e, "stop": e * (1 - dd), "take": e * (1 + 3 * dd), "level": e, "score": 6.0}), mkt, ago)
