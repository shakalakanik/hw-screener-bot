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


def _base(card: dict) -> str:
    t = card.get("ticker") or ""
    if t.endswith("USDT"):
        return t[:-4]
    return t.split("-")[0] if "-" in t else t


def _d(card: dict) -> int:
    return 1 if card["side"] == "LONG" else 0


def dedup_list(cards: list[dict]) -> list[dict]:
    """dedupList(): повторы одного уровня (±1%) по монете+типу в пределах 48 ч → первый по времени."""
    ty = lambda c: c.get("strategy", "fbo")
    arr = sorted(cards, key=lambda c: (_base(c), ty(c), c["signal_ts"], c.get("_ord", (0, 0))))
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
            if b["signal_ts"] < best["signal_ts"] or (b["signal_ts"] == best["signal_ts"] and b["score"] > best["score"]):
                best = b
        out.append(best)
    out.sort(key=lambda c: -c["signal_ts"])
    return out


def recompute(card: dict, f: dict) -> dict | None:
    """recompute(): стоп = max(stop% ATR, 2%), тейк = tgt·R, исход по часовикам с безубытком."""
    e = card["last"]
    atr_d = card.get("atr_d") or 0
    is_ru = card.get("market") == "ru"
    # новый HTML: стоп не меньше 1% для акций РФ (2% крипта), удержание 96ч РФ / 168ч крипта
    risk = max((f["stop"] / 100.0) * atr_d, (0.01 if is_ru else 0.02) * e)
    hold = HOLD_RU if is_ru else HOLD
    if risk <= 0 or risk / e > MAXRISK:
        return None
    long_ = _d(card) == 1
    stop = e - risk if long_ else e + risk
    take = e + f["tgt"] * risk if long_ else e - f["tgt"] * risk
    be = e + BE_TRIGGER_R * risk if long_ else e - BE_TRIGGER_R * risk
    cur_stop, moved = stop, False
    outcome, exit_t, last_ts = "open", card["signal_ts"], card["signal_ts"]
    for (ts, o, h, l, c) in card.get("fwd") or []:
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
        if not moved and ((h >= be) if long_ else (l <= be)):
            cur_stop, moved = e, True
        last_ts = ts
    return {**card, "stop": stop, "take": take, "risk": risk, "rp": risk / e, "o": outcome, "xt": exit_t}


def _prio_key(c):  # orderRows: t desc, prio desc, base asc
    return (-c["signal_ts"], -c["score"], _base(c))


def _apply_coin_limit(rows):
    best = {}
    win = COIN_WINDOW_H * 3_600_000
    for s in rows:
        k = (_base(s), s["signal_ts"] // win)
        b = best.get(k)
        if (b is None or s["signal_ts"] < b["signal_ts"]
                or (s["signal_ts"] == b["signal_ts"] and (s["score"] > b["score"]
                    or (s["score"] == b["score"] and _base(s) < _base(b))))):
            best[k] = s
    keep = {id(v) for v in best.values()}
    return [s for s in rows if id(s) in keep]


def _cap_cluster(rows, cap):
    if not cap or cap >= 99:
        return sorted(rows, key=_prio_key)
    by = {}
    for s in rows:
        by.setdefault((s["signal_ts"] // 3_600_000, _d(s)), []).append(s)
    keep = []
    for g in by.values():
        g.sort(key=lambda s: (-s["score"], _base(s)))
        keep.extend(g[:int(cap)])
    return sorted(keep, key=_prio_key)


def _apply_sequential(rows, f):
    """applySequential() в режиме «Сигналы»: сделок в день + стоп-кран после серии стопов."""
    if f["daycap"] >= 99 and not f["streak"]:
        return rows
    asc = sorted(rows, key=lambda s: (s["signal_ts"], -s["score"], _base(s)))
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
        if not passes_html(c["feat"], f):
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
    asc = sorted(rows, key=lambda s: (s["signal_ts"], -s["score"], _base(s)))
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
                  market: str = "crypto", reg_ts=None) -> list[dict]:
    """1:1 bestPipeline(). Крипта: фильтры «авто» (часы 0-24 при выключенном «без ночи», только SHORT),
    источник — The best ЛП. Мосбиржа (HTML 7.02): фильтры = алгоритм MOEX «авто», источник — isBestRU.
    Затем бумага/монета 12ч → cap/час (cap||3) → серии → ≤14/нед (с учётом реестра reg_ts)."""
    from screener import _f_from_template, passes_html
    if market == "ru":
        f = _f_from_template({}, "ru", None)
        rows = [c for c in cards if c["signal_ts"] >= cutoff_ms and c.get("strategy") == "brk"
                and c.get("market") == "ru" and is_best_ru_card(c)]
    else:
        f = _f_from_template({} if no_night else {"hours": "0-24"}, "crypto", "fbo")
        rows = [c for c in cards if c["signal_ts"] >= cutoff_ms and c.get("strategy", "fbo") == "fbo"
                and c.get("market", "crypto") == "crypto" and is_best_card(c)]
    ok = []
    for c in rows:
        if not passes_html(c["feat"], f):
            continue
        r = recompute(c, f)
        if r is None or r["rp"] * 100 > f["maxrisk"]:
            continue
        ok.append(r)
    seq = _apply_sequential(_cap_cluster(_apply_coin_limit(ok), f["cap"] or 3), f)
    return _best_week_cap(seq, reg_ts)


def best_reg_key(c: dict) -> str:
    """Ключ реестра The best, как в HTML: base|t."""
    return f"{_base(c)}|{int(c['signal_ts'])}"


def best_key(c: dict) -> tuple:
    return (c["ticker"], c.get("strategy"), c["signal_ts"], c["side"], round(float(c["level"]), 10))
