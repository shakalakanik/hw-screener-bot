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
    risk = max((f["stop"] / 100.0) * atr_d, 0.02 * e)
    if risk <= 0 or risk / e > MAXRISK:
        return None
    long_ = _d(card) == 1
    stop = e - risk if long_ else e + risk
    take = e + f["tgt"] * risk if long_ else e - f["tgt"] * risk
    be = e + BE_TRIGGER_R * risk if long_ else e - BE_TRIGGER_R * risk
    cur_stop, moved = stop, False
    outcome, exit_t, last_ts = "open", card["signal_ts"], card["signal_ts"]
    for (ts, o, h, l, c) in card.get("fwd") or []:
        if (ts - card["signal_ts"]) / 3_600_000 > HOLD:
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
              cutoff_ms: int, week_max: int | None) -> list[dict]:
    """renderScan(): окно → стратегия → фильтры шаблона → лимиты → недельный лимит."""
    f = _f_from_template(raw_filters or {})
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
    seq = _apply_sequential(_cap_cluster(_apply_coin_limit(ok), f["cap"]), f)
    wm = week_max if week_max else 999
    return _weekly_best_cap(seq, wm) if wm < 999 else seq
