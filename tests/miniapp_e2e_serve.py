import os, sys, time, asyncio
sys.path.insert(0, "/workspace/hwbot-3mkt")
os.environ.setdefault("DB_PATH", "/workspace/mtest/t.db")
os.environ["ALLOW_DEBUG_WEBAPP"] = "1"
os.environ.setdefault("TG_BOT_TOKEN", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
import storage, webapp_server
from aiohttp import web
if os.path.exists(os.environ["DB_PATH"]): os.remove(os.environ["DB_PATH"])
storage.init()
import watch_tomb; watch_tomb._READY = False; watch_tomb.install()   # БД пересоздана после импорта webapp_server
U = 42
now = int(time.time() * 1000)
ids = {}
for m, tk, n in (("crypto", "BTCUSDT", 12), ("ru", "SBER", 12), ("algo", "ETHUSDT", 12)):
    for i in range(n):
        card = {"market": m, "ticker": tk if i else tk, "signal_ts": now - (i + 1) * 3600_000 - hash(m) % 1000,
                "strategy": "algo" if m == "algo" else "fbo", "side": "long", "entry": 100, "last": 100, "stop": 98, "take": 106,
                "level": 99}
        jid, _ = storage.add_signal_journal(U, card)
        ids.setdefault(m, []).append(jid)
open("/workspace/mtest/ids.txt", "w").write(repr(ids))
web.run_app(webapp_server.create_app(), host="127.0.0.1", port=8811, print=None)
