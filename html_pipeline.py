"""html_pipeline.py — точная копия пост-обработки сигналов из HW_FBO_scanner_6.html.

Цепочка, как в renderScan()/applyAll():
  scanSymbol → dedupList (по монете)  → окно (t ≥ cutoff) → стратегия
  → passes(шаблон) → оценка → recompute (стоп/тейк/исход) → «макс. риск %»
  → applyCoinLimit (1 монета / 12 ч) → capCluster (cap шаблона на час+сторону)
  → applySequential («сделок в день», «пауза после серии стопов»)
  → weeklyBestCap (N лучших по оценке за неделю).
Сверено с JS на одинаковых свечах (test: /tmp/cmp).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from screener import _f_from_template, passes_html

HOLD = 168
HOLD_RU = 96   # HTML 6.94: MOEX — 4 суток
SHORT_WINDOW_H = 168   # окно скана < недели → «не более 10 сигналов в час» без недельного/score-капа
MAXRISK = 0.20
BE_TRIGGER_R = 1.5
COIN_WINDOW_H = 12
HALF_H = 1_800_000
LONG_HOUR_CAP = 4   # HTML 7.50: не больше 4 ЛП-лонгов крипты в час (самые ликвидные)
RU_STOP_ATR = 25    # HTML 7.36: MOEX — стоп не меньше 25% ATR дня
RU_FRI_H = 20       # HTML 7.60: MOEX — выход в пятницу 20:00 МСК
# HTML 7.37/7.38 RU_SW: режим «Свинг» Мосбиржи
RU_SW = {"stopH": 50, "stopMaxPct": 0.02, "atrMin": 0.025, "d1Str": 4, "holdH": 168, "beH": 0,
         "holdD": 96, "beD": 1.5, "ix20": 0.04}


def _is_ru_brk(card: dict) -> bool:
    return card.get("strategy") == "brk" and card.get("market") == "ru"


def sw_of(card: dict, f: dict | None) -> int:
    """HTML swOf(): 0 — интрадей, 1 — часовой пробой со свинг-выходом, 2 — дневной шорт.
    Сигнал скана следует режиму пользователя (f["_swing"]); сохранённая сделка — своему swm."""
    if not _is_ru_brk(card):
        return 0
    if (card.get("feat") or {}).get("sw") == 2 or card.get("sw") == 2:
        return 2
    swm = card.get("swm", (card.get("feat") or {}).get("swm"))
    if swm is not None:
        return 1 if swm else 0
    return 1 if (f or {}).get("_swing") else 0


def be_r_of(card: dict) -> float:
    """HTML beROf(t): свинг часовой пробой MOEX — 0 (без безубытка), дневной шорт — 1.5, иначе BE_TRIGGER_R."""
    sw = sw_of(card, None)
    return RU_SW["beH"] if sw == 1 else RU_SW["beD"] if sw == 2 else BE_TRIGGER_R


def be_level(card: dict):
    """HTML fmtBE(t): цена, на которой стоп переносится в точку входа (+beR·R от входа в сторону тейка). None — без безубытка."""
    try:
        e, st = float(card.get("last", card.get("entry"))), float(card["stop"])
    except (TypeError, ValueError, KeyError):
        return None
    be_r = be_r_of(card)
    if not be_r > 0:
        return None
    risk = abs(e - st)
    long_ = str(card.get("side", "")).upper() == "LONG" if card.get("side") else _d(card) == 1
    return e + be_r * risk if long_ else e - be_r * risk


def fmt_html(v) -> str:
    """HTML fmt(v)."""
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1000:
        return f"{v:.1f}"
    if a >= 1:
        return f"{v:.3f}"
    if a >= 0.01:
        return f"{v:.5f}"
    if not a:
        return "0.000"
    m, ex = f"{v:.3e}".split("e")   # JS toPrecision(4): экспонента только при порядке < −6
    ex = int(ex)
    return f"{m}e{ex}" if ex < -6 else f"{v:.{3 - ex}f}"


def sw_risk_cap(card: dict, risk: float, f: dict | None) -> float:
    return min(risk, RU_SW["stopMaxPct"] * card["last"]) if sw_of(card, f) == 1 else risk


def swing_gate(card: dict, f: dict):
    """passes() 7.37/7.38: дневной шорт — только в «Свинге» (+ сторона), прочие фильтры к нему не применяются;
    в «Свинге» часовые пробои MOEX — только у бумаг с ATR дня ≥2.5%. None — решают обычные фильтры."""
    feat = card.get("feat") or {}
    if feat.get("sw") == 2:
        side = f.get("side")
        return bool(f.get("_swing")) and (side is None or _d(card) == int(side))
    if f.get("_swing") and _is_ru_brk(card) and feat.get("swm") != 0 \
            and feat.get("atrD_pct") is not None and feat["atrD_pct"] < RU_SW["atrMin"]:
        return False
    return None


def _base(card: dict) -> str:
    t = card.get("ticker") or ""
    if t.endswith("USDT"):
        return t[:-4]
    return t.split("-")[0] if "-" in t else t


def _d(card: dict) -> int:
    return 1 if card["side"] == "LONG" else 0


def sig_end(c: dict) -> int:
    """HTML 7.58 sigEnd(): момент входа — закрытие часа или середина часа (30-минутка)."""
    return int(c["signal_ts"]) + (HALF_H if c.get("src30") else 3_600_000)


# HTML 7.86: конвейер считается на окне + 7 суток разгона (склейка уровня, «монета раз в 12 ч», паузы, лимиты),
# показывается/отправляется только окно — короткое окно = точная часть длинного.
SCAN_WARM_H = 168


def warm_cut(win_h: int, now_ms: int) -> int:
    return int(now_ms) - (int(win_h) + SCAN_WARM_H) * 3_600_000


def stop_min_c(f: dict | None) -> float:
    """HTML 7.81/7.85 stopMinC(f): пол стопа крипты, % цены (поле шаблона «stopmin», по умолчанию 2)."""
    v = (f or {}).get("stopmin")
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 0.0
    return (v if v > 0 else 2.0) / 100.0


def _msk_dw_hm(ts_ms: int) -> tuple[int, int]:
    """День недели JS (вс=0) и минуты от полуночи по МСК."""
    import time as _t
    g = _t.gmtime(int(ts_ms) / 1000 + 3 * 3600)
    return (g.tm_wday + 1) % 7, g.tm_hour * 60 + g.tm_min


def crypto_thu_fri_block(card: dict) -> bool:
    """HTML 7.90/7.91 cryptoThuFriBlock(s) — ТОЛЬКО крипта, ТОЛЬКО ложный пробой ШОРТ, время входа sigEnd по МСК:
    • чт 16:00 — пт 16:00 МСК: сигнала нет;
    • сб 06:00–12:00 МСК: только при свободном пути до «стены» ≥ 7R (wall_r ≥ 7).
    Остальные дни/часы, лонги, пробои, Мосбиржа — без изменений."""
    if card.get("market", "crypto") != "crypto" or card.get("strategy", "fbo") != "fbo":
        return False
    if _d(card) != 0:
        return False
    dw, hm = _msk_dw_hm(sig_end(card))
    if (dw == 4 and hm >= 16 * 60) or (dw == 5 and hm < 16 * 60):
        return True
    if dw == 6 and 6 * 60 <= hm < 12 * 60:
        w = (card.get("feat") or {}).get("wall_r")
        if not (w is not None and w >= 7):
            return True
    return False


def ru_off_day(card: dict) -> bool:
    """HTML 7.75 offDay(s,'ru'): MOEX — сигнал (время входа sigEnd, МСК) в субботу или воскресенье."""
    dw, _ = _msk_dw_hm(sig_end(card))
    return dw in (0, 6)


def no_off(cards: list[dict], market: str) -> list[dict]:
    """HTML 7.75 noOff(LAST): для MOEX — убрать сигналы выходных до всех конвейеров."""
    return [c for c in cards if not ru_off_day(c)] if market == "ru" else cards


def dedup_list(cards: list[dict]) -> list[dict]:
    """dedupList(): повторы одного уровня (±1%) по монете+типу в пределах 48 ч → самый ранний по моменту входа."""
    ty = lambda c: c.get("strategy", "fbo")
    arr = sorted(cards, key=lambda c: (_base(c), ty(c), sig_end(c), c.get("_ord", (0, 0))))
    groups = []
    cur = None
    for c in arr:
        if (cur and _base(c) == cur["base"] and ty(c) == cur["type"]
                and abs(c["level"] - cur["lv"]) / max(abs(cur["lv"]), 1e-12) < 0.01
                and (c["signal_ts"] - cur["lastT"]) <= 48 * 3_600_000):
            cur["items"].append(c)
            cur["lastT"] = c["signal_ts"]
        else:
            if cur:
                groups.append(cur)
            cur = {"base": _base(c), "type": ty(c), "lv": c["level"], "lastT": c["signal_ts"], "items": [c]}
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        best = g["items"][0]
        for b in g["items"][1:]:
            if sig_end(b) < sig_end(best) or (sig_end(b) == sig_end(best) and b["score"] > best["score"]):
                best = b
        out.append(best)
    out.sort(key=lambda c: -sig_end(c))
    return out


def stop_k(card: dict, k: float, f: dict | None = None) -> float:
    """HTML stopK(): MOEX — интрадей: стоп не меньше 25% ATR дня; свинг: 50% ATR; дневной шорт: 1% от входа."""
    if card.get("market") == "ru" and card.get("strategy") != "algo":
        w = sw_of(card, f)
        if w == 2:
            return 0
        return max(float(k or 0), RU_SW["stopH"] if w == 1 else RU_STOP_ATR)
    return k


def ru_fri_cut(card: dict):
    """HTML 7.60 ruFriCut(): MOEX — ближайшая пятница 20:00 МСК после входа (сделка закрывается)."""
    if card.get("market") != "ru" or card.get("strategy") == "algo":
        return None
    ent = sig_end(card)
    d = datetime.fromtimestamp((ent + 3 * 3_600_000) / 1000, tz=timezone.utc)
    add = (5 - ((d.weekday() + 1) % 7) + 7) % 7
    midnight = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    cut = midnight + add * 86_400_000 + RU_FRI_H * 3_600_000 - 3 * 3_600_000
    if cut <= ent:
        cut += 7 * 86_400_000
    return cut


def recompute(card: dict, f: dict) -> dict | None:
    """recompute(): стоп = max(stop% ATR, 2%), тейк = tgt·R, исход по часовикам с безубытком."""
    e = card["last"]
    atr_d = card.get("atr_d") or 0
    is_ru = card.get("market") == "ru"
    # новый HTML: стоп не меньше 1% для акций РФ (2% крипта), удержание 96ч РФ / 168ч крипта
    sw = sw_of(card, f)   # HTML 7.37
    risk = sw_risk_cap(card, max((stop_k(card, f["stop"], f) / 100.0) * atr_d, (0.01 if is_ru else stop_min_c(f)) * e), f)
    hold = RU_SW["holdH"] if sw == 1 else RU_SW["holdD"] if sw == 2 else (HOLD_RU if is_ru else HOLD)
    be_r = RU_SW["beH"] if sw == 1 else RU_SW["beD"] if sw == 2 else BE_TRIGGER_R
    fri = ru_fri_cut(card)   # HTML 7.60
    if risk <= 0 or risk / e > MAXRISK:
        return None
    long_ = _d(card) == 1
    stop = e - risk if long_ else e + risk
    take = e + f["tgt"] * risk if long_ else e - f["tgt"] * risk
    be = e + be_r * risk if long_ else e - be_r * risk
    cur_stop, moved = stop, False
    outcome, exit_t, last_ts = "open", card["signal_ts"], card["signal_ts"]
    for (ts, o, h, l, c) in card.get("fwd") or []:
        if fri is not None and ts >= fri:
            outcome, exit_t = "expired", last_ts
            break
        if (ts - card["signal_ts"]) / 3_600_000 > hold:
            outcome, exit_t = "expired", last_ts
            break
        exit_t = ts
        hs = (l <= cur_stop) if long_ else (h >= cur_stop)
        ht = (h >= take) if long_ else (l <= take)
        if hs:
            outcome = "be" if moved else "stop"
            break
        if ht:
            outcome = "take"
            break
        if be_r > 0 and not moved and ((h >= be) if long_ else (l <= be)):
            cur_stop, moved = e, True
        last_ts = ts
    if outcome == "open" and fri is not None and (card.get("fwd") or []) and last_ts + 3_600_000 >= fri:
        outcome, exit_t = "expired", last_ts   # HTML 7.60: пятница 20:00 наступила
    out = {**card, "stop": stop, "take": take, "risk": risk, "rp": risk / e, "o": outcome, "xt": exit_t}
    if _is_ru_brk(card) and sw != 2:
        out["swm"] = 1 if sw == 1 else 0
    return out


def algo_reject(card: dict, f: dict) -> bool:
    """Новые правила passes() HTML 7.51–7.59 (до фильтров шаблона).
    • ЛП: стоп обязан быть ЗА уровнем (шорт — стоп не ниже уровня, лонг — не выше), иначе сигнала нет.
    • ЛП-лонги крипты: BTC-поводырь не медвежий, монета не падает 2+ дня, ≤4 ч за уровнем, корр. с BTC ≥0.5."""
    s = card.get("feat") or {}
    mkt = card.get("market", "crypto")
    if card.get("strategy", "fbo") == "fbo" and mkt != "us":
        lv, e, a = s.get("lv", card.get("level")), card.get("last"), card.get("atr_d")
        if lv is not None and e is not None and a is not None:
            ru = mkt == "ru"
            risk = sw_risk_cap(card, max((stop_k(card, f["stop"], f) / 100.0) * a, (0.01 if ru else stop_min_c(f)) * e), f)
            if _d(card) == 1:
                if e - risk > lv:
                    return True
            elif e + risk < lv:
                return True
    if mkt == "crypto" and card.get("strategy", "fbo") == "fbo" and _d(card) == 1:
        if s.get("btcBias") is not None and s["btcBias"] < 0:
            return True
        if s.get("d1_streak") is not None and s["d1_streak"] <= -2:
            return True
        if s.get("brk_run") is not None and s["brk_run"] + 1 > 4:
            return True
        if s.get("cbtc") is not None and s["cbtc"] < 0.5:
            return True
    return False


def _prio_key(c):  # orderRows: sigEnd desc, prio desc, base asc
    return (-sig_end(c), -c["score"], _base(c))


def _apply_coin_limit(rows):
    best = {}
    win = COIN_WINDOW_H * 3_600_000
    for s in rows:
        k = (_base(s), "d1" if (s.get("feat") or {}).get("sw") == 2 else "", s["signal_ts"] // win)   # 7.37
        b = best.get(k)
        if (b is None or s["signal_ts"] < b["signal_ts"]
                or (s["signal_ts"] == b["signal_ts"] and (s["score"] > b["score"]
                    or (s["score"] == b["score"] and _base(s) < _base(b))))):
            best[k] = s
    keep = {id(v) for v in best.values()}
    return [s for s in rows if id(s) in keep]


def _cap_cluster(rows, cap):
    """HTML 7.33/7.50 capCluster(): лимит «сигналов в час» снят; у крипты — не больше 4 ЛП-лонгов в час
    (самые ликвидные: оборот за сутки turnD, затем posMax, затем тикер). cap оставлен для совместимости."""
    by, out = {}, []
    for s in rows:
        if s.get("market", "crypto") == "crypto" and s.get("strategy", "fbo") == "fbo" and _d(s) == 1:
            by.setdefault(s["signal_ts"] // 3_600_000, []).append(s)
        else:
            out.append(s)
    for g in by.values():
        g.sort(key=lambda s: (-((s.get("feat") or {}).get("turnD") or 0),
                              -((s.get("feat") or {}).get("vol5mAvg") or 0), _base(s)))
        out.extend(g[:LONG_HOUR_CAP])
    return sorted(out, key=_prio_key)


def _apply_sequential(rows, f):
    """applySequential() в режиме «Сигналы»: сделок в день + стоп-кран после серии стопов."""
    if f["daycap"] >= 99 and not f["streak"]:
        return rows
    asc = sorted(rows, key=lambda s: (sig_end(s), -s["score"], _base(s)))
    out, per_day, pending = [], {}, []
    streak, paused_until = 0, 0
    for t in asc:
        if f["streak"] and pending:
            pending.sort(key=lambda x: x.get("xt") or x["signal_ts"])
            while pending and (pending[0].get("xt") or pending[0]["signal_ts"]) <= t["signal_ts"]:
                x = pending.pop(0)
                if x["o"] == "stop":
                    streak += 1
                    if streak >= f["streak"]:
                        paused_until = (x.get("xt") or x["signal_ts"]) + f["pause"] * 3_600_000
                        streak = 0
                elif x["o"] in ("take", "be"):
                    streak = 0
        if t["signal_ts"] < paused_until:
            continue
        day = t["signal_ts"] // 86_400_000
        if f["daycap"] < 99 and per_day.get(day, 0) >= f["daycap"]:
            continue
        per_day[day] = per_day.get(day, 0) + 1
        if f["streak"]:
            pending.append(t)
        out.append(t)
    return sorted(out, key=_prio_key)


def iso_week_key(ts_ms: int) -> str:
    """isoWeekKey() из HTML — ровно та же формула (важно для границ недель)."""
    d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    y = d.year
    jan4 = datetime(y, 1, 4, tzinfo=timezone.utc)
    jan4_ms = int(jan4.timestamp() * 1000)
    js_day = (jan4.weekday() + 1) % 7
    import math
    wk = math.ceil(((ts_ms - jan4_ms) / 86_400_000 + js_day + 1) / 7)
    return f"{y}-{wk}"


def _weekly_best_cap(rows, week_max):
    if not week_max or week_max >= 999:
        return rows
    by = {}
    for t in rows:
        by.setdefault(iso_week_key(t["signal_ts"]), []).append(t)
    keep = []
    for g in by.values():
        g = sorted(g, key=lambda s: -(s["score"] or 0))   # стабильная, как Array.sort
        keep.extend(g[:week_max])
    return sorted(keep, key=_prio_key)


def apply_all(cards: list[dict], raw_filters: dict, strategy: str,
              cutoff_ms: int, week_max: int | None, win_h: int | None = None,
              market: str | None = None) -> list[dict]:
    """renderScan(): окно → стратегия → фильтры шаблона → лимиты → недельный лимит."""
    f = _f_from_template(raw_filters or {}, market, strategy)
    rows = [c for c in cards if c["signal_ts"] >= cutoff_ms]
    if strategy != "both":
        rows = [c for c in rows if c.get("strategy", "fbo") == strategy]
    ok = []
    for c in rows:
        if crypto_thu_fri_block(c):   # HTML 7.90/7.91 passes(): первое правило
            continue
        if algo_reject(c, f):
            continue
        g = swing_gate(c, f)
        if g is False or (g is None and not passes_html(c["feat"], f)):
            continue
        r = recompute(c, f)
        if r is None or r["rp"] * 100 > f["maxrisk"]:
            continue
        ok.append(r)
    # Новый HTML applyAll(scanMode): окно скана < 168ч → capH = min(cap||10, 10), недельный
    # лимит и score-ранжирование пропускаются; окно ≥ 168ч → cap||3 и недельный топ-N.
    short = win_h is not None and win_h < SHORT_WINDOW_H
    cap_h = min(f["cap"] or 10, 10) if short else (f["cap"] or 3)
    seq = _apply_sequential(_cap_cluster(_apply_coin_limit(ok), cap_h), f)
    if short:
        return seq
    wm = week_max if week_max else 999
    return _weekly_best_cap(seq, wm) if wm < 999 else seq


# ═══ The best (HTML 6.93 bestPipeline, режим «edge») ═══════════════════════════
def is_best_card(card: dict) -> bool:
    """isBestFBO(): ЛП-шорт, D1-серия вверх ≥2, объём часа перед сигналом ≤ среднего за 3 ч, без шипа H1 ≥8×."""
    from hwv1 import best_verdict
    if card.get("strategy", "fbo") != "fbo":
        return False
    feat = dict(card.get("feat") or {})
    feat["type"] = "fbo"
    feat.setdefault("side", "short" if card.get("side") == "SHORT" else "long")
    return best_verdict(feat) is None


def _best_week_cap(rows, reg_ts=None):
    """bestWeekCap(): по времени; хвост прокола > BEST_POKE стопов — пропуск; ≤ BEST_WEEK за ISO-неделю.
    HTML 7.01: счёт недели начинается с уже отобранных раньше (реестр The best) — те, что раньше первого
    сигнала списка и в той же неделе. reg_ts — время сигналов из реестра этого рынка."""
    from hwv1 import BEST_POKE, BEST_WEEK, best_poke
    asc = sorted(rows, key=lambda s: (sig_end(s), -s["score"], _base(s)))
    cnt, keep = {}, []
    if asc and reg_ts:
        t0 = asc[0]["signal_ts"]
        w0 = iso_week_key(t0)
        for v in reg_ts:
            if v < t0 and iso_week_key(v) == w0:
                cnt[w0] = cnt.get(w0, 0) + 1
    for t in asc:
        feat = t.get("feat") or {}
        pr = best_poke({"pk_ext": feat.get("pk_ext"), "st": t.get("stop"), "e": t.get("last")})
        if (pr or 0) > BEST_POKE:
            continue
        k = iso_week_key(t["signal_ts"])
        if cnt.get(k, 0) >= BEST_WEEK:
            continue
        cnt[k] = cnt.get(k, 0) + 1
        keep.append(t)
    return sorted(keep, key=_prio_key)


def is_best_ru_card(card: dict) -> bool:
    from hwv1 import is_best_ru
    return card.get("strategy") == "brk" and is_best_ru(card.get("feat") or {})


def best_pipeline(cards: list[dict], cutoff_ms: int, no_night: bool = False,
                  market: str = "crypto", reg_ts=None, swing: bool = False) -> list[dict]:
    """1:1 bestPipeline(). Крипта: фильтры «авто» (часы 0-24 при выключенном «без ночи», только SHORT),
    источник — The best ЛП. Мосбиржа (HTML 7.02): фильтры = алгоритм MOEX «авто», источник — isBestRU.
    Затем бумага/монета 12ч → cap/час (cap||3) → серии → ≤14/нед (с учётом реестра reg_ts)."""
    from screener import _f_from_template, passes_html
    if market == "ru":
        f = _f_from_template({"_ru_mode": "swing"} if swing else {}, "ru", None)
        rows = [c for c in cards if c["signal_ts"] >= cutoff_ms and c.get("strategy") == "brk"
                and c.get("market") == "ru" and is_best_ru_card(c)]
    else:
        f = _f_from_template({} if no_night else {"hours": "0-24"}, "crypto", "fbo")
        rows = [c for c in cards if c["signal_ts"] >= cutoff_ms and c.get("strategy", "fbo") == "fbo"
                and c.get("market", "crypto") == "crypto" and is_best_card(c)]
    ok = []
    for c in rows:
        if crypto_thu_fri_block(c):   # HTML 7.90/7.91 passes(): первое правило
            continue
        if algo_reject(c, f):
            continue
        g = swing_gate(c, f)
        if g is False or (g is None and not passes_html(c["feat"], f)):
            continue
        r = recompute(c, f)
        if r is None or r["rp"] * 100 > f["maxrisk"]:
            continue
        ok.append(r)
    seq = _apply_sequential(_cap_cluster(_apply_coin_limit(ok), f["cap"] or 3), f)
    return _best_week_cap(seq, reg_ts)


def apply_all_w(cards: list[dict], raw_filters: dict, strategy: str, now_ms: int, win_h: int,
                week_max: int | None = 0, market: str | None = None) -> list[dict]:
    """HTML 7.86 renderScan: applyAll на окне с разгоном (win_h + 168 ч), показ — только окно win_h."""
    cut = int(now_ms) - int(win_h) * 3_600_000
    rows = apply_all(cards, raw_filters, strategy, warm_cut(win_h, now_ms), week_max, win_h=win_h, market=market)
    return [c for c in rows if c["signal_ts"] >= cut]


def best_pipeline_w(cards: list[dict], now_ms: int, win_h: int, no_night: bool = False, market: str = "crypto",
                    reg_ts=None, swing: bool = False) -> tuple[list[dict], list[dict]]:
    """HTML 7.86: bestPipeline на окне с разгоном → (все отобранные — для реестра, только окно — показ/★)."""
    cut = int(now_ms) - int(win_h) * 3_600_000
    full = best_pipeline(cards, warm_cut(win_h, now_ms), no_night, market=market, reg_ts=reg_ts, swing=swing)
    return full, [c for c in full if c["signal_ts"] >= cut]


def best_reg_key(c: dict) -> str:
    """Ключ реестра The best, как в HTML: base|t."""
    return f"{_base(c)}|{int(c['signal_ts'])}"


def best_key(c: dict) -> tuple:
    return (c["ticker"], c.get("strategy"), c["signal_ts"], c["side"], round(float(c["level"]), 10))
