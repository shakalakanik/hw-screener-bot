"""Уровень безубытка = HTML v8.01 fmtBE(t) (бот: Telegram-карточка, html_pipeline.be_level; Mini App: webapp fmtBE)
и тот же уровень, на котором recompute() переносит стоп (статус «безубыток»)."""
import json, os, re, subprocess, sys
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, R)
os.environ.setdefault("TG_BOT_TOKEN", "123456:ABCdefGHIjklMNOpqrSTUvwxYZ012345678")
os.environ.setdefault("DB_PATH", "/tmp/test_be_level.db")
import html_pipeline as hp
fails = 0
def chk(n, c, x=""):
    global fails; fails += not c; print(("OK   " if c else "FAIL ") + n + ("" if c else f"  {x}"))

# (card бота, t в нотации HTML)
cases = [
    ({"market": "crypto", "strategy": "fbo", "side": "SHORT", "last": 0.0000071234, "stop": 0.0000072650}, {}),   # PEPE-цены
    ({"market": "crypto", "strategy": "brk", "side": "LONG", "last": 0.4321, "stop": 0.4231}, {}),
    ({"market": "crypto", "strategy": "fbo", "side": "LONG", "last": 2.718, "stop": 2.66}, {}),
    ({"market": "crypto", "strategy": "brk", "side": "SHORT", "last": 67123.5, "stop": 68465.97}, {}),
    ({"market": "ru", "strategy": "fbo", "side": "LONG", "last": 301.55, "stop": 298.5}, {}),
    ({"market": "ru", "strategy": "brk", "side": "SHORT", "last": 6512.0, "stop": 6577.0, "swm": 0}, {"swm": 0}),
    ({"market": "ru", "strategy": "brk", "side": "LONG", "last": 158.2, "stop": 155.0, "swm": 1}, {"swm": 1}),   # свинг: без БУ
    ({"market": "ru", "strategy": "brk", "side": "SHORT", "last": 12.345, "stop": 12.468, "sw": 2, "feat": {"sw": 2}}, {"sw": 2}),
    ({"market": "algo", "strategy": "algo", "side": "LONG", "last": 1.0567, "stop": 1.0356}, {}),
]
js_in = [{"e": c["last"], "st": c["stop"], "d": 1 if c["side"] == "LONG" else 0, **x} for c, x in cases]
node = """const ref=require(process.argv[1]); const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8'); const g=re=>src.match(re)[0];
const web=new Function(g(/const BE_TRIGGER_R=[^\\n]*/)+'\\n'+g(/const RU_SW=[^\\n]*/)+'\\n'+g(/function swStored\\(t\\)[^\\n]*/)+'\\n'+g(/function beROf\\(t\\)[^\\n]*/)+'\\n'+g(/function fmt\\(v\\)\\{[\\s\\S]*?\\n\\}/)+'\\n'+g(/function fmtBE\\(t\\)\\{[\\s\\S]*?\\n\\}/)+'\\nreturn fmtBE;')();
const I=JSON.parse(process.argv[3]); console.log(JSON.stringify(I.map(t=>[ref.fmtBE(t),web(t)])));"""
out = json.loads(subprocess.check_output(["node", "-e", node, os.path.join(R, "tests/fixtures/html801_be.js"),
                                          os.path.join(R, "webapp/screener.html"), json.dumps(js_in)]))
for (c, _), (ref, web) in zip(cases, out):
    py = hp.fmt_html(hp.be_level(c))
    chk(f"{c['market']} {c['side']} {c['last']}: HTML={ref} bot={py} miniapp={web}", ref == py == web)

# Telegram-карточка: строка «🛡 Безубыток» между стопом и тейком, значение = HTML
import bot
card = {"ticker": "PEPEUSDT", "side": "SHORT", "market": "crypto", "strategy": "fbo", "last": 0.0000071234,
        "stop": 0.0000072650, "take": 0.0000066986, "level": 0.0000071, "score": 6.4, "signal_ts": 1792389600000, "prob": 0.61}
txt = bot._format_card(card)
lines = txt.split("\n")
i = next(k for k, l in enumerate(lines) if "Безубыток" in l)
chk("TG: Безубыток между стопом и тейком", "Стоп-лосс" in lines[i - 1] and "Тейк-профит" in lines[i + 1], txt)
chk("TG: значение как HTML", f"<code>{out[0][0]}</code>" in lines[i], lines[i])
sw = bot._format_card({**card, "ticker": "SBER", "market": "ru", "strategy": "brk", "swm": 1, "last": 158.2, "stop": 155.0, "take": 167.8, "level": 158})
chk("TG: свинг MOEX — «—»", "Безубыток: <code>—</code>" in sw, sw)
chk("TG: карточка разбирается обратно", (bot._parse_card_from_message(type("M", (), {"html_text": txt})()) or {}).get("ticker") == "PEPEUSDT")

# статус «безубыток» в боте срабатывает ровно на этом уровне
H = 3_600_000
base = {"market": "crypto", "strategy": "fbo", "side": "LONG", "last": 100.0, "atr_d": 10.0, "signal_ts": 0, "ticker": "AAAUSDT", "feat": {}}
f = {"stop": 20, "tgt": 3}
r0 = hp.recompute(dict(base, fwd=[]), f) if hasattr(hp, "recompute") else None
if r0:
    be = hp.be_level({**r0, "last": r0["last"], "stop": r0["stop"]})
    hit = hp.recompute(dict(base, fwd=[(H, 100, be, 100, be), (2 * H, be, be, 99.0, 99.5)]), f)
    miss = hp.recompute(dict(base, fwd=[(H, 100, be - 1e-6, 100, be - 1e-6), (2 * H, be, be, r0["stop"] - 0.01, 99.5)]), f)
    chk(f"recompute: касание {be} → безубыток", hit["o"] == "be", hit["o"])
    chk("recompute: недотянули → стоп", miss["o"] == "stop", miss["o"])
else:
    chk("recompute найден", False)
print("FAILS", fails); sys.exit(1 if fails else 0)
