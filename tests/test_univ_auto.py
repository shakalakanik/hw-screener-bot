"""HTML 7.93/7.94 (8.01): крипта — «Инструментов» = число монет с оборотом 24ч ≥ мин. оборота (20…600), перед каждым сканом.
Пример 19.10: ALTUSDT (Bybit ~4.2 млн $/сут) — в старом универсе (≥2.5M, топ-300), в новом нет."""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TG_BOT_TOKEN", "123456:ABCdefGHIjklMNOpqrSTUvwxYZ012345678")
import screener
fails = 0
def chk(n, c, x=""):
    global fails; fails += not c; print(("OK   " if c else "FAIL ") + n + ("" if c else f"  {x}"))
tk = lambda s, v: {"symbol": s, "inst_id": s, "lastPrice": 1.0, "turnover24h": v}
# 107 монет ≥5M, ещё 50 — 2.5…5M (как Bybit утром 19.10)
T = [tk(f"C{i}USDT", 6e6 + (200 - i) * 1e5) for i in range(107)] + [tk(f"L{i}USDT", 4.9e6 - i * 4e4) for i in range(49)] + [tk("ALTUSDT", 4.22e6)]
chk("univ_n_inst = count(turn ≥ 5M) = 107", screener.univ_n_inst(T, 5e6) == 107, screener.univ_n_inst(T, 5e6))
chk("clamp min 20", screener.univ_n_inst(T[:5], 5e6) == 20)
chk("clamp max 600", screener.univ_n_inst([tk(f"X{i}USDT", 9e6) for i in range(700)], 5e6) == 600)

scanned = []
async def fake_tickers(client, source=None, min_vol=None, top_n=None):
    return sorted([t for t in T if t["turnover24h"] >= (min_vol or 0)], key=lambda t: -t["turnover24h"])
async def fake_scan_one(client, t, **k):
    scanned.append(t["symbol"]); return {"base": t["symbol"][:-4], "cards": [], "turn": t["turnover24h"], "h1c": []}
screener.fetch_tickers, screener.scan_one = fake_tickers, fake_scan_one
async def run(n):
    scanned.clear(); await screener._scan_crypto_html(None, n_inst=n, min_vol=5e6, win_h=12); return list(scanned)
auto = asyncio.run(run(None))
chk("auto: scans top-107 by 24h turnover", len(auto) == 107 and "ALTUSDT" not in auto, len(auto))
old = asyncio.run(run(300))
chk("explicit 300 (old/univMaster='n'): ≥2.5M → 157 incl. ALT", len(old) == 157 and "ALTUSDT" in old, len(old))
src = open(os.path.join(os.path.dirname(__file__), "..", "screener.py")).read()
chk("scheduled crypto scan uses auto universe", "client, n_inst=None, min_vol=MIN_VOL_USD_24H, win_h=win_h," in src)
b = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
chk("manual API: crypto auto unless univMaster='n'", "!= \"n\":\n        n_inst = None" in b)
h = open(os.path.join(os.path.dirname(__file__), "..", "webapp", "screener.html")).read()
chk("Mini App engine: univSync before scan", "await Promise.race([univSync('перед сканом')" in h and "async function univSync(why)" in h)
print("FAILS", fails); sys.exit(1 if fails else 0)
