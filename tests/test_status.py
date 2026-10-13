"""/status и кнопка «📊 Статус» рендерятся без ошибок: крипта / MOEX / алго, с подпиской и без.
Запуск: python tests/test_status.py"""
import os, sys, tempfile, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("TG_BOT_TOKEN", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
from unittest.mock import MagicMock, AsyncMock
import storage, bot
storage.init()
fails = 0
def chk(n, c, x=""):
    global fails; fails += not c; print(("OK   " if c else "FAIL ") + n + ("" if c else f"  {x}"))
def msg(cid, text):
    m = MagicMock(); m.chat.id = cid; m.text = text; m.answer = AsyncMock(); return m
cid = 100
for mkt in ("crypto", "ru", "algo"):
    for sub in (False, True):
        cid += 1
        storage.set_market_enabled(cid, mkt, True)
        if mkt != "algo":
            storage.set_auto_mode(cid, mkt, True)
        if mkt == "crypto":
            storage.set_the_best(cid, sub)
        (bot._subscribers.add if sub else bot._subscribers.discard)(cid)
        for text in ("/status", "📊 Статус"):
            m = msg(cid, text)
            try:
                asyncio.run(bot.cmd_status(m)); err = None
            except Exception as e:
                err = repr(e)
            out = m.answer.call_args[0][0] if m.answer.called else ""
            chk(f"{mkt} sub={sub} {text}", err is None and "Статус" in out and
                ("✅ активна" if sub else "❌ остановлена") in out and "Только лучш" not in out, err or out)
print("FAILS", fails); sys.exit(1 if fails else 0)
