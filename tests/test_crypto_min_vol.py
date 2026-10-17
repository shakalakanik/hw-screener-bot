"""Крипта: оборот не ниже $5M/сут по умолчанию и всегда (бот + движок Mini App); MOEX не затронут.
Запуск: python tests/test_crypto_min_vol.py"""
import os, sys, re, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "t.db"))
import screener, hwv1
fails = 0
def chk(n, c, x=""):
    global fails; fails += not c; print(("OK   " if c else "FAIL ") + n + ("" if c else f"  {x}"))
chk("screener.MIN_VOL_USD_24H = 5M", screener.MIN_VOL_USD_24H == 5_000_000.0, screener.MIN_VOL_USD_24H)
chk("hwv1 PARAMS vol_usd_min = 1M (как V0 в HTML 7.92; пол 5M — в screener)", hwv1.PARAMS["vol_usd_min"] == 1_000_000.0)
rows = [{"cards": [{"ticker": t}], "turn": v, "base": t} for t, v in (("A", 4_999_999), ("B", 5_000_000), ("C", 1_200_000), ("D", 80e6))]
kept = [c["ticker"] for c in screener._select_universe(rows, 300, screener.MIN_VOL_USD_24H)]
chk("отбор: <5M отброшены, ≥5M оставлены (по убыванию)", kept == ["D", "B"], kept)
bot = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
chk("ручной скан API: дефолт minVol=5 и пол 5M", 'body.get("min_vol", 5)' in bot and "max(min_vol, screener.MIN_VOL_USD_24H)" in bot)
import inspect
chk("run_manual_scan по умолчанию = MIN_VOL_USD_24H", inspect.signature(screener.run_manual_scan).parameters["min_vol"].default == 5_000_000.0)
h = open(os.path.join(ROOT, "webapp", "screener.html"), encoding="utf-8").read()
chk("HTML: CRYPTO_MIN_VOL=5e6", "const CRYPTO_MIN_VOL=5e6;" in h)
chk("HTML: поле «Мин. оборот» = 5", re.search(r'id="minVol" type="number" value="5"', h) is not None)
chk("HTML: состояние крипты vol '5'", "crypto:{LAST:[],BT:[],EX:null,REGIME:null,vol:'5'}" in h)
chk("HTML: V0 vol_usd_min 5M", "vol_usd_min: 5_000_000" in h and "vol_usd_min: 1_000_000" not in h)
chk("HTML: скан крипты — пол 5M, MOEX без порога", "const minVol=ru?0:Math.max(CRYPTO_MIN_VOL," in h)
chk("HTML: остальные пути — пол только для крипты", h.count("MKT==='crypto'?Math.max(CRYPTO_MIN_VOL,") == 2)
print("FAILS", fails); sys.exit(1 if fails else 0)
