"""Мосбиржа (HTML 7.0x): алгоритм «авто» MOEX, правила индекса, The Best MOEX (отдельный ⭐ в фильтре),
реестр The best для недельного лимита. Запуск: python tests/test_moex_best.py"""
import os, sys, tempfile, asyncio, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("TG_BOT_TOKEN", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
import storage, bot, screener, html_pipeline, hwv1, ai_chat, moex
from datetime import datetime, timezone
D0 = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)   # чт 00:00 UTC
storage.init()
fails = 0
def chk(n, c, x=""):
    global fails; fails += not c; print(("OK   " if c else "FAIL ") + n + ("" if c else f"  {x}"))

# ── переключатели: крипта и Мосбиржа независимы; «авто» MOEX по умолчанию — пробой
CH = 901
chk("ru the best default off", storage.get_the_best(CH, "ru") is False)
storage.set_the_best(CH, True, "ru")
chk("ru on, crypto stays off", storage.get_the_best(CH, "ru") and not storage.get_the_best(CH))
chk("auto strategy ru default brk", storage.get_auto_strategy(CH, "ru") == "brk")
chk("auto strategy crypto default fbo", storage.get_auto_strategy(CH, "crypto") == "fbo")

# ── «авто» на Мосбирже = RU_AUTO, явные значения шаблона важнее
f = screener._f_from_template({}, "ru", "brk")
chk("ru auto: str4 cross2 acc1 7-19 base4 3R", (f["str"], f["cross"], f["acc"], f["hours"], f["rubase"], f["tgt"]) == (4.0, 2.0, 1.0, "7-19", 4.0, 3.0), f)
chk("ru auto: _ra marks auto fields", f["_ra"].get("side") == 1 and f["_ra"].get("str") == 1)
f2 = screener._f_from_template({"str": "5", "side": "0"}, "ru", "brk")
chk("ru template overrides", f2["str"] == 5.0 and f2["side"] == 0.0 and "str" not in f2["_ra"])
fc = screener._f_from_template({}, "crypto", "fbo")
chk("crypto unchanged", "_ra" not in fc and fc["hours"] == "9-23" and fc["str"] == 0.0)

def S(**k):
    s = {"type": "brk", "mkt": "ru", "d": 0, "k": "prev low", "p": 0, "strength": 4, "crosses": 1, "touched8": 0,
         "acc": 1, "dist_atr": 0.2, "overshoot_atr": 0.2, "vol_contraction": 0.9, "vol_mult": 2.0, "bias_ok": 1,
         "level_age_h": 30, "t": D0 + 9 * 3600_000, "free_zone_h": 99, "wall_r": 9, "day_pos": 0.5,
         "body_ratio": 0.6, "wick_ratio": 0.1, "brk_bars": 1, "vol_ret_brk": 1, "dow": 3, "ema_ok": 1,
         "approach_atr": 1, "atr_ratio": 0.2, "lvl_density": 1, "round_dist": 0.5, "green_ratio": 0.5,
         "d1_streak": 0, "atrD_pct": 0.03, "ru_base8": 4, "ru_ixS": 7, "ru_ixExt": 0, "ru_sa50": -0.02,
         "ru_clu": 3, "ru_sl200": 0.1}
    s.update(k); return s
P = screener.passes_html
chk("short, index 7/7, below SMA50 → pass", P(S(), f))
chk("index overheated → reject", not P(S(ru_ixExt=1), f))
chk("short 7/7 above SMA50 → reject", not P(S(ru_sa50=0.01), f))
chk("short 7/7 lone breakout (clu<2) → reject", not P(S(ru_clu=1), f))
chk("short 7/7: level 3, no acc, base 2 ok", P(S(strength=3, acc=0, ru_base8=2), f))
chk("short mixed index → reject", not P(S(ru_ixS=4), f))
chk("short vs rising index needs H1 downtrend", not P(S(ru_ixS=0, ru_sl200=0.1), f) and P(S(ru_ixS=0, ru_sl200=0.3, crosses=1, vol_mult=1.6), f))
chk("short vs rising index: ≤1 cross", not P(S(ru_ixS=0, ru_sl200=0.3, crosses=2), f))
chk("short vs rising index: vol ≥1.5", not P(S(ru_ixS=0, ru_sl200=0.3, vol_mult=1.2), f))
chk("long needs stock ≥6% over SMA50", not P(S(d=1, k="prev high", ru_sa50=0.03), f) and P(S(d=1, k="prev high", ru_sa50=0.07), f))
chk("long, falling index → reject", not P(S(d=1, k="prev high", ru_sa50=0.07, ru_ixS=0), f))
chk("long, mixed index, 5d high → reject", not P(S(d=1, k="5d high", ru_sa50=0.07, ru_ixS=4), f))
chk("hours 07–19 (bar open)", not P(S(t=D0 + 17 * 3600_000), f))
chk("no IMOEX → index rules skipped", P(S(ru_ixS=None, ru_ixExt=None, ru_sa50=0.5), f))
chk("IMOEX filter = SMA50 side", not P(S(ru_ix50=-0.01), {**f, "ruimoex": 1.0}) and P(S(ru_ix50=0.02), {**f, "ruimoex": 1.0}))

# ── The Best MOEX и оценка
b = S(t=D0 + 10 * 3600_000, ru_clu=4)   # 13:00 МСК, 4 пробоя вниз за 3 ч
chk("isBestRU: short ≥12:00 + cluster", hwv1.is_best_ru(b))
chk("isBestRU: before 12:00 → no", not hwv1.is_best_ru({**b, "t": D0 + 8 * 3600_000}))
chk("isBestRU: or stock −0.3 ATR today", hwv1.is_best_ru({**b, "ru_clu": 1, "ru_dm": 0.4}))
chk("isBestRU: long never", not hwv1.is_best_ru({**b, "d": 1}))
sc = {**b, "ru_brng": 1.2, "ru_b8w": 1.0, "ru_e50200": 0.2, "ru_run24": -0.5, "ru_cpos": 0.8, "ru_ix24": 0.0}
v = hwv1.score_ru(sc)
chk("score MOEX best tier 8–10", v is not None and 8 <= v <= 10, v)
chk("score MOEX: no IMOEX hourly → None (model fallback)", hwv1.score_ru({**sc, "ru_ix24": None}) is None)
chk("score MOEX low tier < 5", hwv1.score_ru({**sc, "d": 1, "t": D0 + 6 * 3600_000, "ru_ix24": 0.05, "ru_sl200": 0.0, "ru_ixS": 3}) < 5)

# ── ruCluster: пробои той же стороны за 3 ч по всему списку (включая этот)
H = 3600_000
cs = [{"strategy": "brk", "feat": {"t": t, "d": 0}} for t in (0, H, 2 * H, 3 * H)] + [{"strategy": "brk", "feat": {"t": 2 * H, "d": 1}}]
hwv1.ru_cluster(cs)
chk("ruCluster counts", [c["feat"]["ru_clu"] for c in cs] == [1, 2, 3, 3, 1], [c["feat"]["ru_clu"] for c in cs])

# ── недельный лимит The best учитывает реестр (HTML 7.01)
wk0 = int(datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp() * 1000)   # пн 00:00 UTC
rows = [{"ticker": f"C{i}", "signal_ts": wk0 + (20 + i) * H, "score": 5, "feat": {}, "stop": 1.02, "last": 1.0} for i in range(5)]
chk("week cap w/o registry keeps all", len(html_pipeline._best_week_cap(rows)) == 5)
reg = [wk0 + i * H for i in range(12)]
chk("week cap with 12 earlier → 2 left", len(html_pipeline._best_week_cap(rows, reg)) == 2)
chk("registry from previous week ignored", len(html_pipeline._best_week_cap(rows, [wk0 - 72 * H] * 20)) == 5)
storage.best_registry_add("ru", [("SBER|1", wk0 + H), ("SBER|1", wk0 + H), ("GAZP|2", wk0 + 2 * H)])
storage.best_registry_add("crypto", [("BTC|3", wk0 + 3 * H)])
chk("registry per market, unique", sorted(storage.best_registry_ts("ru", 0)) == [wk0 + H, wk0 + 2 * H])

# ── список MOEX: только 73 бумаги, в порядке списка
tk = [{"symbol": s} for s in ("SBER", "ZZZZ", "GAZP", "LKOH")]
chk("ru list filter/order", [t["symbol"] for t in moex.ru_list_tickers(tk)] == ["LKOH", "GAZP", "SBER"])
chk("ru list = 73", len(moex.RU_LIST_DEFAULT) == 73)
chk("hold MOEX 96h", hwv1.hold_h("ru") == 96 and html_pipeline.HOLD_RU == 96)

# ── доставка: ⭐ MOEX ВКЛ — только The best MOEX; ВЫКЛ — шаблон + строки The best со звёздой
now = int(time.time() * 1000)
mk = lambda t, ts, lv: {"ticker": t, "side": "SHORT", "market": "ru", "strategy": "brk", "level": lv, "kind": "prev low",
                        "last": lv * 0.99, "stop": lv, "take": lv * 0.97, "score": 6.0, "feat": {}, "signal_ts": ts,
                        "strength": 4, "dist_atr": 0.1, "d1_bias": "down"}
r1, r2 = mk("SBER", now - 1800_000, 300.0), mk("GAZP", now - 1800_000, 120.0)
orig_bp, orig_aa = html_pipeline.best_pipeline, html_pipeline.apply_all
html_pipeline.best_pipeline = lambda cards, cut, nn=False, market="crypto", reg_ts=None: [dict(r1)] if market == "ru" else []
html_pipeline.apply_all = lambda cards, raw, st, cut, wm, win_h=None, market=None: [dict(r2)] if market == "ru" else []
storage.is_duplicate = lambda *a, **k: False
sent = []
async def on_sig(c, ids): sent.append(c)
flt = lambda cid: {"markets": ["ru"], "_multi": [{"_name": "Авто", "_market": "ru", "_strategy": "brk", "filters": {}}]}
async def deliver(ch):
    sent.clear()
    await screener._deliver_html([r1, r2], [ch], flt, now, 12, on_sig, None, None, min_send_ts=now - 2 * 3600_000)
    return {c["ticker"]: (c.get("is_best"), c.get("matched_template")) for c in sent}
CH_OFF, CH_ON = 902, 903
storage.set_the_best(CH_ON, True, "ru")
got = asyncio.run(deliver(CH_OFF))
chk("MOEX OFF: template + best row with ⭐", got == {"GAZP": (False, "Авто"), "SBER": (True, "Авто")}, got)
got = asyncio.run(deliver(CH_ON))
chk("MOEX ON: only The best", got == {"SBER": (True, screener.THE_BEST_NAME)}, got)
html_pipeline.best_pipeline, html_pipeline.apply_all = orig_bp, orig_aa

# ── бот: кнопка ⭐ в фильтре Мосбиржи, AI
chk("AI: the best мосбиржа", ai_chat.parse_rules("the best мосбиржа вкл") == [{"action": "set_the_best", "params": {"enabled": True, "market": "ru"}}],
    ai_chat.parse_rules("the best мосбиржа вкл"))
chk("AI: the best (crypto default)", ai_chat.parse_rules("включи the best") == [{"action": "set_the_best", "params": {"enabled": True}}])
san = ai_chat.sanitize_proposal({"action": "set_the_best", "params": {"enabled": True, "market": "ru"}})
chk("AI sanitize keeps market", san and san["params"]["market"] == "ru", san)

class _Msg:
    def __init__(self): self.kb = None; self.chat = type("C", (), {"id": CH_OFF})()
    async def edit_text(self, *a, reply_markup=None, **k): self.kb = reply_markup
    async def answer(self, *a, reply_markup=None, **k): self.kb = reply_markup
class _Bot:
    async def send_message(self, *a, reply_markup=None, **k): _M.kb = reply_markup
_M = _Msg()
bot.bot = _Bot()
asyncio.run(bot._send_filter_menu(CH_OFF, edit_msg=_M, market="ru"))
cbs = [b.callback_data for row in (_M.kb.inline_keyboard if _M.kb else []) for b in row]
chk("MOEX filter has ⭐ The Best button", "the_best:ru" in cbs, cbs)
class _Call:
    data = "the_best:ru"; message = _M
    async def answer(self, *a, **k): pass
asyncio.run(bot.cb_the_best(_Call()))
chk("cb the_best:ru toggles only MOEX", storage.get_the_best(CH_OFF, "ru") and not storage.get_the_best(CH_OFF))
print("FAILS", fails); sys.exit(1 if fails else 0)
