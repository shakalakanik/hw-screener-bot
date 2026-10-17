"""⭐ The Best (HTML 6.93): переключатель на чат, флаг is_best в журнале, карточка, доставка ВКЛ/ВЫКЛ,
оценка v6 / bestVerdict. Запуск: python tests/test_the_best.py"""
import os, sys, tempfile, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("TG_BOT_TOKEN", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
import storage, bot, screener, html_pipeline, hwv1, ai_chat
storage.init()
fails = 0
def chk(n, c, x=""):
    global fails; fails += not c; print(("OK   " if c else "FAIL ") + n + ("" if c else f"  {x}"))

CH = 777
chk("default off", storage.get_the_best(CH) is False)
storage.set_the_best(CH, True); chk("set on persists", storage.get_the_best(CH) is True)
storage.set_the_best(CH, False); chk("set off persists", storage.get_the_best(CH) is False)

now = int(time.time() * 1000)
base = {"ticker": "GRASSUSDT", "side": "SHORT", "market": "crypto", "strategy": "fbo", "level": 0.8442,
        "kind": "5d high", "last": 0.8192, "stop": 0.8356, "take": 0.7700, "score": 8.6, "prob": 0.2,
        "dist_atr": 0.7, "d1_bias": "up", "strength": 3, "signal_ts": now - 3600_000}
txt = bot._format_card({**base, "is_best": True})
chk("card: ⭐ THE BEST line first", txt.splitlines()[0] == "⭐ <b>THE BEST</b>", txt)
txt0 = bot._format_card(base)
chk("card: no star when not best", "THE BEST" not in txt0)
for bad in ("модель p=", "расст.", "[5d high]", "Тренд D1"):
    chk(f"card: removed {bad!r}", bad not in txt and bad not in txt0)
for good in ("Стоп-лосс", "(−", "Тейк-профит", "(+", "Оценка", "Сигнал:"):
    chk(f"card: kept {good!r}", good in txt0)

jid, _ = storage.add_signal_journal(CH, {**base, "is_best": True})
row = storage.get_signal_journal(jid)
chk("journal is_best=1", row and row["is_best"] is True and row["card"].get("is_best") is True, row)
jid2, _ = storage.add_signal_journal(CH, {**base, "ticker": "ENAUSDT"})
chk("journal is_best=0", storage.get_signal_journal(jid2)["is_best"] is False)
chk("best_sent_ts_since", storage.best_sent_ts_since(CH, now - 86400_000) == [base["signal_ts"]])

# bestVerdict / score v6
f = {"type": "fbo", "side": "short", "d1_streak": 2, "vtr": 0.9, "g_spk": 3}
chk("verdict pass", hwv1.best_verdict(f) is None)
chk("verdict d1", hwv1.best_verdict({**f, "d1_streak": 1}) == "d1")
chk("verdict vol", hwv1.best_verdict({**f, "vtr": 1.2}) == "vol")
chk("verdict spike", hwv1.best_verdict({**f, "g_spk": 8}) == "spike")
chk("verdict long", hwv1.best_verdict({**f, "side": "long"}) == "notshort")
sv = {"lv_touch24": 0, "ap03": 2, "near12": 1, "approach_atr": 3, "lv_touch": 0, "mv12": 1,
      "vol5mAvg": 50000, "v6_poke": 0.5, "e": 1.0, "atrD": 0.05, "pk_ext": 1.01}
v6 = hwv1.score_v6({**sv, **f})
chk("v6 tier ≥8 for edge short", v6 is not None and v6 >= 8, v6)
chk("v6 ≤7.9 otherwise", hwv1.score_v6({**sv, **f, "vtr": 2}) <= 7.9)

# доставка: ВКЛ — только The best (шаблоны крипты не применяются), ВЫКЛ — как обычно + is_best
c1 = {**base, "feat": {}, "signal_ts": now - 1800_000}
c2 = {**base, "ticker": "ENAUSDT", "level": 0.24, "feat": {}, "signal_ts": now - 1800_000}
html_pipeline.best_pipeline = lambda cards, cut, nn=False, market="crypto", reg_ts=None, **k: [dict(c1)] if market == "crypto" else []
html_pipeline.apply_all = lambda cards, raw, st, cut, wm, win_h=None, market=None, **k: [dict(c1), dict(c2)]
storage.is_duplicate = lambda *a, **k: False
screener.crypto_send_hour_ok = lambda ts: True   # часы доставки — в test_send_hours.py
sent = []
async def on_sig(c, ids): sent.append(c)
flt = lambda cid: {"markets": ["crypto"], "_multi": [{"_name": "Авто", "_market": "crypto", "_strategy": "fbo", "filters": {}}]}
async def run():
    return await screener._deliver_html([c1, c2], [CH], flt, now, 12, on_sig, None, None,
                                        min_send_ts=now - 2 * 3600_000)
storage.set_the_best(CH, False); sent.clear(); asyncio.run(run())
chk("OFF: both sent", sorted(c["ticker"] for c in sent) == ["ENAUSDT", "GRASSUSDT"], [c["ticker"] for c in sent])
chk("OFF: star only on best", {c["ticker"]: c.get("is_best") for c in sent} == {"GRASSUSDT": True, "ENAUSDT": False})
CH2 = 778
storage.set_the_best(CH2, True); sent.clear()
async def run2():
    return await screener._deliver_html([c1, c2], [CH2], flt, now, 12, on_sig, None, None,
                                        min_send_ts=now - 2 * 3600_000)
asyncio.run(run2())
chk("ON: only best", [c["ticker"] for c in sent] == ["GRASSUSDT"] and sent[0]["matched_template"] == screener.THE_BEST_NAME, sent)

# AI
chk("AI: the best on", ai_chat.parse_rules("включи the best") == [{"action": "set_the_best", "params": {"enabled": True}}])
chk("AI: the best off", ai_chat.parse_rules("the best выкл") == [{"action": "set_the_best", "params": {"enabled": False}}])
chk("AI: «только лучший» удалён (не the best)", ai_chat.parse_rules("крипта только лучший") is None)
storage.set_best_only(CH, "crypto", True)
chk("stored best_only ignored → off", storage.get_best_only(CH) == {m: False for m in storage.MARKETS})
chk("AI sanitize", ai_chat.sanitize_proposal({"action": "set_the_best", "params": {"enabled": "да"}}) is not None)
print("FAILS", fails); sys.exit(1 if fails else 0)
