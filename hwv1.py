#!/usr/bin/env python3
"""HWv1.0 — портировано 1:1 из HW_FBO_scanner_6.html (функция scanSymbol).

ЧЁТКОЕ РАЗДЕЛЕНИЕ СТРАТЕГИЙ — не путать между собой:

  evaluate_brk()  — «Пробой» (Герчик): сигнал на первом закрытии часа ЗА
                     уровнем, при накоплении на H1 перед пробоем. Модель
                     НЕ участвует (в HTML для этой ветки p:0 — комментарий
                     "модель обучена на ложных пробоях, для пробоя не применяется").
                     card["strategy"] = "brk"

  evaluate_fbo()  — «Ложный пробой» (FBO): сигнал на первом часе ВОЗВРАТА
                     цены за уровень после пробоя. Использует Random Forest
                     (300 деревьев, файл model.json) для скоринга p (0..1);
                     сигнал проходит только если p >= порога (по умолчанию 0.30).
                     card["strategy"] = "fbo"

  evaluate()      — точка входа: гоняет ОБЕ функции и возвращает карточки
                     из обеих, каждая с полем "strategy" — дальше их можно
                     фильтровать по стратегии (bot.py / screener.py это делают).

Модель (model.json) — обученные веса, перенесены как есть, без переобучения.
Импорт: from hwv1 import Bar, evaluate, evaluate_brk, evaluate_fbo
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Optional

VERSION = "HWv1.0"

PARAMS = {
    "atr_n": 14,
    "d1_min": 45,
    "h1_min": 120,
    "work_days": 26,
    "swing_lr": 2,
    "universe_n": 200,
    "vol_usd_min": 1_000_000.0,
    "merge_atr": 0.06,
    "merge_pct": 0.001,
    "zone_atr": 0.03,
    "zone_pct": 0.0005,
    "impulse_d1_atr": 2.2,
    "strength_min": 3,          # как V0.PARAMS.strength_min в HTML — уровни слабее отбрасываются
    "max_crosses": 2,           # «запилов уровня» после формирования (правило для «Пробоя»)
    "acc_hours": (8, 10, 12, 16),
    "acc_range_atr": 0.50,
    "acc_close_atr": 0.35,
    "acc_drift_atr": 0.32,
    "acc_mid_atr": 0.60,
    "lookback_fbo": 6,          # LOOKBACK в HTML — окно проверки пробоя для «Ложного пробоя»
    "max_risk_pct": 0.20,       # MAXRISK — риск не более 20% цены, иначе сделка отбрасывается
}

_MODEL_PATH = os.path.join(os.path.dirname(__file__), "model.json")
_MODEL = None  # ленивая загрузка


def _load_model() -> dict:
    global _MODEL
    if _MODEL is None:
        with open(_MODEL_PATH, "r", encoding="utf-8") as f:
            _MODEL = json.load(f)
    return _MODEL


@dataclass
class Bar:
    ts: int            # ms since epoch
    o: float
    h: float
    l: float
    c: float
    vol_quote: float = 0.0
    confirmed: bool = True


@dataclass
class Level:
    price: float
    kind: str
    formed_ts: int
    lo: float
    hi: float
    strength: int = 3
    touches: int = 1
    chopped: bool = False
    impulse_mid: bool = False
    from_prev_day: bool = False


# ═════════════════════════════ базовые утилиты (общие) ═══════════════════════

def last_closed(bars: list[Bar]) -> list[Bar]:
    if bars and not bars[-1].confirmed:
        return bars[:-1]
    return bars


def atr(bars: list[Bar], n: int = PARAMS["atr_n"]) -> float:
    """Уайлдер (RMA), как ta.atr() в TradingView — 1:1 с atr() в HTML."""
    w = last_closed(bars)
    if len(w) < 2:
        return 0.0
    tr = []
    for i in range(1, len(w)):
        p, b = w[i - 1], w[i]
        tr.append(max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c)))
    if len(tr) < n:
        return sum(tr) / len(tr) if tr else 0.0
    rma = sum(tr[:n]) / n
    for i in range(n, len(tr)):
        rma = rma + (tr[i] - rma) / n
    return rma


def d1_bias(d1: list[Bar]) -> str:
    if len(d1) < 16:
        return "flat"
    last, ago = d1[-1].c, d1[-8].c
    hh = max(b.h for b in d1[-8:])
    ll = min(b.l for b in d1[-8:])
    prev_hh = max(b.h for b in d1[-16:-8])
    prev_ll = min(b.l for b in d1[-16:-8])
    up = last > ago and hh >= prev_hh * 0.998
    down = last < ago and ll <= prev_ll * 1.002
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return "flat"


def swings(bars: list[Bar], left: int, right: int):
    highs, lows = [], []
    n = len(bars)
    for i in range(left, n - right):
        h, l = bars[i].h, bars[i].l
        ok_h = all(h >= bars[i - k].h for k in range(1, left + 1)) and \
               all(h >= bars[i + k].h for k in range(1, right + 1))
        ok_l = all(l <= bars[i - k].l for k in range(1, left + 1)) and \
               all(l <= bars[i + k].l for k in range(1, right + 1))
        if ok_h:
            highs.append((i, h))
        if ok_l:
            lows.append((i, l))
    return highs, lows


def zone_crosses(bars: list[Bar], lo: float, hi: float) -> int:
    n = 0
    for b in bars:
        body_lo, body_hi = min(b.o, b.c), max(b.o, b.c)
        if body_lo < lo and body_hi > hi:
            n += 2
        elif b.h >= lo and b.l <= hi:
            n += 1
    return n


def d1_chopped(d1: list[Bar], lo: float, hi: float) -> bool:
    win = d1[-24:]
    if len(win) < 8:
        return False
    above = sum(1 for b in win if b.c > hi)
    below = sum(1 for b in win if b.c < lo)
    if above >= 4 and below >= 4:
        return True
    bodies = sum(1 for b in win if min(b.o, b.c) < lo and max(b.o, b.c) > hi)
    return bodies >= 3 or zone_crosses(win, lo, hi) >= 10


def inside_fresh_impulse(d1: list[Bar], h4: list[Bar], price: float, atr_d: float) -> bool:
    if atr_d <= 0 or len(d1) < 5:
        return False
    tail = d1[-5:]
    rng = max(b.h for b in tail) - min(b.l for b in tail)
    if rng < PARAMS["impulse_d1_atr"] * atr_d:
        return False
    lo, hi = min(b.l for b in tail), max(b.h for b in tail)
    if not (lo + 0.15 * rng < price < hi - 0.15 * rng):
        return False
    last3 = h4[-3:] if len(h4) >= 3 else tail
    a4 = atr(h4) if len(h4) > 5 else atr_d
    return sum(1 for b in last3 if (b.h - b.l) > 1.6 * a4) >= 2


def count_close_crosses(bars: list[Bar], price: float, after_ts: int) -> int:
    n = 0
    prev = None
    for b in bars:
        if b.ts <= after_ts:
            prev = b.c
            continue
        if prev is None:
            prev = b.c
            continue
        if (prev - price) * (b.c - price) < 0:
            n += 1
        if min(b.o, b.c) < price < max(b.o, b.c):
            n += 1
        prev = b.c
    return n


def accumulation(h1: list[Bar], level: float, atr_d: float) -> tuple[bool, str]:
    """Накопление на H1 у уровня — те же условия что и V0.accumulation в HTML."""
    if atr_d <= 0 or len(h1) < 8:
        return False, "мало H1"
    best = None
    for n in PARAMS["acc_hours"]:
        if len(h1) < n:
            break
        win = h1[-n:]
        hi, lo = max(b.h for b in win), min(b.l for b in win)
        width = hi - lo
        if width > PARAMS["acc_range_atr"] * atr_d:
            if n == 8:
                return False, f"8ч диапазон {width/atr_d:.2f} ATR"
            break
        close_rng = max(b.c for b in win) - min(b.c for b in win)
        drift = abs(win[-1].c - win[0].c)
        if close_rng > PARAMS["acc_close_atr"] * atr_d or drift > PARAMS["acc_drift_atr"] * atr_d:
            if n == 8:
                return False, f"ход close {drift/atr_d:.2f} ATR, не полка"
            break
        mid = (hi + lo) / 2
        touched = lo <= level <= hi
        if abs(mid - level) > PARAMS["acc_mid_atr"] * atr_d and not touched:
            if n == 8:
                return False, "сжатие не у уровня"
            break
        best = n
    if not best:
        return False, "нет сжатия ≥8ч"
    return True, f"{best}ч сжатие у уровня"


def build_levels(d1: list[Bar], h4: Optional[list[Bar]] = None) -> list[Level]:
    """1:1 с V0.buildLevels в HTML — отсекает lv.strength < PARAMS['strength_min'] (как HTML)."""
    if len(d1) < 12:
        return []
    atr_d = atr(d1)
    last = d1[-1].c
    work = d1[-PARAMS["work_days"]:] if len(d1) >= PARAMS["work_days"] else d1
    raw: list[Level] = []

    def add(price: float, kind: str, bar: Bar, touches: int = 1):
        if price <= 0:
            return
        pad = max(PARAMS["zone_atr"] * atr_d, price * PARAMS["zone_pct"])
        raw.append(Level(
            price=price, kind=kind, formed_ts=bar.ts,
            lo=price - pad, hi=price + pad, touches=touches,
            chopped=d1_chopped(d1, price - pad, price + pad),
            impulse_mid=inside_fresh_impulse(d1, h4, price, atr_d) if h4 else False,
            from_prev_day=kind.startswith("prev"),
        ))

    prev = d1[-2]
    add(prev.h, "prev high", prev, 2)
    add(prev.l, "prev low", prev, 2)
    window5 = d1[-6:-1] if len(d1) >= 6 else d1[:-1]
    if window5:
        hi_bar = max(window5, key=lambda b: b.h)
        lo_bar = min(window5, key=lambda b: b.l)
        add(hi_bar.h, "5d high", hi_bar, 2)
        add(lo_bar.l, "5d low", lo_bar, 2)
    lr = PARAMS["swing_lr"]
    sh, sl = swings(work, lr, lr)
    for i, price in sh:
        add(price, "swing D1 high", work[i], 1)
    for i, price in sl:
        add(price, "swing D1 low", work[i], 1)

    raw.sort(key=lambda x: -x.price)
    merge_tol = max(PARAMS["merge_atr"] * atr_d, last * PARAMS["merge_pct"])
    priority = ["5d high", "5d low", "prev high", "prev low", "swing D1 high", "swing D1 low"]
    merged: list[Level] = []
    for lv in raw:
        if merged and abs(lv.price - merged[-1].price) <= merge_tol:
            m = merged[-1]
            kinds = {m.kind, lv.kind}
            pick = next((k for k in priority if k in kinds), m.kind)
            if pick == lv.kind:
                m.price, m.kind, m.lo, m.hi = lv.price, lv.kind, lv.lo, lv.hi
                m.formed_ts = lv.formed_ts or m.formed_ts
            m.touches = max(m.touches, lv.touches) + 1
            m.chopped = m.chopped or lv.chopped
            m.from_prev_day = m.from_prev_day or lv.from_prev_day
            m.impulse_mid = m.impulse_mid or lv.impulse_mid
        else:
            merged.append(lv)

    kept = []
    for lv in merged:
        s = 3
        if lv.kind.startswith("5d"):
            s += 2
        if lv.from_prev_day:
            s += 1
        if lv.touches >= 3:
            s += 1
        if lv.chopped:
            s -= 2
        if lv.impulse_mid:
            s -= 3
        lv.strength = max(0, min(5, s))
        if lv.impulse_mid or lv.strength < PARAMS["strength_min"]:
            continue
        if abs(lv.price - last) > 3.0 * atr_d:
            continue
        kept.append(lv)
    return kept


def _side_of(kind: str) -> str:
    return "long" if "high" in kind else "short"


def _risk_stop_take(entry: float, atr_d: float, side: str, stop_atr_frac: float, target_r: float):
    """1:1 с recompute() в HTML: риск = max(stop_atr_frac * ATR_D, 2% цены), тейк = target_r * risk."""
    risk = max(stop_atr_frac * atr_d, 0.02 * entry)
    if risk <= 0 or risk / entry > PARAMS["max_risk_pct"]:
        return None
    if side == "long":
        stop = entry - risk
        take = entry + target_r * risk
    else:
        stop = entry + risk
        take = entry - target_r * risk
    return stop, take, risk


# ═══════════════════════════ стратегия «Пробой» (strategy="brk") ═════════════


# ── Мосбиржа: правила нового HTML (scanSymbol, MKT==='ru') ──────────────────
HOLD_CRYPTO, HOLD_RU = 168, 48


def hold_h(market: str) -> int:
    """getHold(): удержание 48ч для акций РФ, 168ч для крипты."""
    return HOLD_RU if market == "ru" else HOLD_CRYPTO


def _ru_friday_late(ts_ms: int) -> bool:
    """Пятница ≥13:00 МСК — сигнал не берём (уйдёт на выходные). JS: getUTCDay()==5 && hl>=13."""
    import time as _t
    g = _t.gmtime(ts_ms / 1000)
    hl = (g.tm_hour + 3) % 24
    return ((g.tm_wday + 1) % 7) == 5 and hl >= 13


def _ru_brk_reject(h1cut: list, bar, atr_d: float) -> bool:
    """Пробой РФ: 10:00 МСК, слабый объём (<0.5 медианы 20), ATR D1 <1%, тело <40%."""
    import time as _t
    hl = (_t.gmtime(bar.ts / 1000).tm_hour + 3) % 24
    if hl == 10:
        return True
    vv = sorted((b.vol_quote or 0) for b in h1cut[-21:-1])
    v_med20 = vv[len(vv) // 2] if vv else 0
    if v_med20 > 0 and (bar.vol_quote or 0) < 0.5 * v_med20:
        return True
    if atr_d > 0 and (atr_d / bar.c) < 0.01:
        return True
    rng = bar.h - bar.l
    if rng > 0 and abs(bar.c - bar.o) / rng < 0.40:
        return True
    return False


def _ru_feats(h1cut: list, bar, side: str, ctx: dict) -> dict:
    w = h1cut[-6:-1]
    sq = 0
    if len(w) >= 5:
        r = [b.h - b.l for b in w]
        sq = 1 if (r[4] < r[0] and r[3] < r[0] and r[4] < r[1]) else 0
    rng = bar.h - bar.l
    if rng > 0:
        cp = (bar.c >= bar.l + rng * 2 / 3) if side == "long" else (bar.c <= bar.l + rng / 3)
        cp = 1 if cp else 0
    else:
        cp = 0
    ia, ir, sr = ctx.get("imoexAbove"), ctx.get("imoexRet5"), ctx.get("sym5ret")
    im = (1 if (ia if side == "long" else (not ia)) else 0) if ia is not None else 0
    rs = (1 if ((sr - ir) > 0 if side == "long" else (sr - ir) < 0) else 0) if ir is not None else 0
    return {"ru_squeeze": sq, "ru_close_pos": cp, "ru_imoex_ok": im, "ru_rel_str": rs}


def _is_night_msk(ts_ms: int) -> bool:
    """1:1 с HTML: hl=(UTCHours+3)%24; ночь — НЕ (9<=hl<23), т.е. час НЕ входит в 09:00–23:00 МСК."""
    from datetime import datetime, timezone
    hl = (datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour + 3) % 24
    return not (9 <= hl < 23)


def _pit_day_state(d1c: list[Bar], h1c: list[Bar], bar: Bar) -> tuple[float, list[Level], str]:
    """Point-in-time D1 levels for bar's UTC day — 1:1 with scanSymbol day-cache in HTML.

    cut = closed D1 bars fully before bar.ts; then push a synthetic intraday day bar
    aggregated from H1 of that UTC day up to bar. Levels via build_levels(cut, []) —
    empty H4 list matches HTML V0.buildLevels(cut, []).
    """
    day_start = (bar.ts // 86_400_000) * 86_400_000
    cut = [b for b in d1c if b.ts + 86_400_000 <= bar.ts]
    td = [b for b in h1c if day_start <= b.ts <= bar.ts]
    if td:
        cut = list(cut)
        cut.append(Bar(
            ts=day_start,
            o=td[0].o,
            h=max(x.h for x in td),
            l=min(x.l for x in td),
            c=bar.c,
            vol_quote=0.0,
            confirmed=True,
        ))
    if len(cut) < 40:
        return 0.0, [], "flat"
    atr_d = atr(cut)
    if atr_d <= 0:
        return 0.0, [], "flat"
    return atr_d, build_levels(cut, []), d1_bias(cut)



# ═════════════ признаки для оценки сигнала 0–10 (порт из HW_FBO_scanner_6.html) ═════════════

_SCORE_PATH = os.path.join(os.path.dirname(__file__), "score_model.json")
_SCORE = None


def _load_score_model() -> dict:
    global _SCORE
    if _SCORE is None:
        with open(_SCORE_PATH, "r", encoding="utf-8") as f:
            _SCORE = json.load(f)
    return _SCORE


def _free_zone(h1c: list, formed_ts: int, br_side: str, level: float) -> float:
    before = [b for b in h1c if b.ts < formed_ts][-720:]
    for k in range(len(before) - 1, -1, -1):
        c = before[k].c
        if (c > level) if br_side == "long" else (c < level):
            return (formed_ts - before[k].ts) / 3_600_000
    return (formed_ts - before[0].ts) / 3_600_000 if before else 9999.0


def _wall_dist_r(levels: list, cur, side: str, px: float, risk: float) -> float:
    if risk <= 0:
        return 99.0
    best = None
    for L in levels:
        if L.price == cur.price:
            continue
        beyond = (L.price > px) if side == "long" else (L.price < px)
        if not beyond:
            continue
        d = abs(L.price - px)
        if best is None or d < best:
            best = d
    return 99.0 if best is None else best / risk


def _day_pos(h1c: list, i: int, px: float) -> float:
    day_start = (h1c[i].ts // 86_400_000) * 86_400_000
    td = []
    k = i
    while k >= 0 and h1c[k].ts >= day_start:
        td.append(h1c[k])
        k -= 1
    if not td:
        return 0.5
    hi, lo = max(b.h for b in td), min(b.l for b in td)
    return (px - lo) / (hi - lo) if hi > lo else 0.5


def _vol_ret_brk(win: list, bar, br_side: str, level: float) -> float:
    br = [b for b in win if ((b.c > level) if br_side == "long" else (b.c < level))]
    if not br:
        return 1.0
    mx = max((b.vol_quote or 0) for b in br)
    return (bar.vol_quote or 0) / mx if mx > 0 else 1.0


def _ema_side(h1cut: list, side: str) -> bool:
    n = 50
    w = h1cut[-n - 1:-1]
    if len(w) < n:
        return True
    k = 2 / (n + 1)
    e = w[0].c
    for b in w:
        e = b.c * k + e * (1 - k)
    px = h1cut[-1].c
    return px >= e if side == "long" else px <= e


def _approach_atr(h1cut: list, a_h1: float) -> float:
    w = h1cut[-5:-1]
    if len(w) < 4 or a_h1 <= 0:
        return 0.0
    return (max(b.h for b in w) - min(b.l for b in w)) / a_h1


def _round_dist(px: float) -> float:
    if px <= 0:
        return 0.5
    mag = 10 ** math.floor(math.log10(px))
    step = mag / 2
    x = px / step
    return abs(x - math.floor(x + 0.5))


def _green_ratio(h1cut: list) -> float:
    w = h1cut[-25:-1]
    if not w:
        return 0.5
    return sum(1 for b in w if b.c >= b.o) / len(w)


def _d1_streak(d1c: list, ts: int) -> int:
    d = [b for b in d1c if b.ts + 86_400_000 <= ts]
    if len(d) < 2:
        return 0
    up = d[-1].c >= d[-1].o
    s = 0
    for b in reversed(d):
        if (b.c >= b.o) == up:
            s += 1
        else:
            break
    return s if up else -s


def _extra_feats(h1c, h1_upto, i, bar, lv, levels, wall_side, wall_risk, ema_side,
                 wick_side_long, br_side, win, pit_atr_d, a_h1, d1c) -> dict:
    rng = bar.h - bar.l
    if rng > 0:
        body = abs(bar.c - bar.o) / rng
        if wick_side_long:
            wick = (min(bar.o, bar.c) - bar.l) / rng
        else:
            wick = (bar.h - max(bar.o, bar.c)) / rng
    else:
        body = wick = 0.0
    return {
        "free_zone_h": _free_zone(h1c, lv.formed_ts, br_side, lv.price),
        "wall_r": _wall_dist_r(levels, lv, wall_side, bar.c, wall_risk),
        "day_pos": _day_pos(h1c, i, bar.c),
        "body_ratio": body,
        "wick_ratio": wick,
        "ema_ok": 1 if _ema_side(h1_upto, ema_side) else 0,
        "approach_atr": _approach_atr(h1_upto, a_h1),
        "atr_ratio": (a_h1 / pit_atr_d) if pit_atr_d > 0 else 0.0,
        "lvl_density": sum(1 for L in levels if abs(L.price - lv.price) <= pit_atr_d),
        "round_dist": _round_dist(lv.price),
        "green_ratio": _green_ratio(h1_upto),
        "d1_streak": _d1_streak(d1c, bar.ts),
    }


def _score_feat_num(feat: dict, key: str) -> float:
    try:
        v = float(feat.get(key) or 0)
    except (TypeError, ValueError):
        v = 0.0
    return v if math.isfinite(v) else 0.0


def _false_break_extra(feat: dict) -> list[float]:
    """Шесть признаков только для ложного пробоя — 1:1 с smExtra() в HTML.

    Сторона из side_long: signal_score вызывается до записи поля d в карточку.
    Если side_long ещё нет (повторный скоринг карточки), берётся d === 1, как в JS.
    """
    if "side_long" in feat:
        side = 1.0 if feat.get("side_long") else -1.0
    else:
        side = 1.0 if feat.get("d") == 1 else -1.0
    p_clip = min(_score_feat_num(feat, "p"), 0.6)
    return [
        1.0 if _score_feat_num(feat, "brk_bars") >= 3 else 0.0,
        1.0 if min(_score_feat_num(feat, "dist_atr"), 2.0) > 0.6 else 0.0,
        1.0 if (
            _score_feat_num(feat, "vol_mult") > 8
            or _score_feat_num(feat, "atr_ratio") > 0.45
            or _score_feat_num(feat, "overshoot_atr") > 2.5
        ) else 0.0,
        1.0 if (
            _score_feat_num(feat, "v6_d1Against") > 0
            or _score_feat_num(feat, "d1_streak") * side <= -3
        ) else 0.0,
        1.0 if (
            _score_feat_num(feat, "strength") >= 4
            and _score_feat_num(feat, "crosses") <= 1
        ) else 0.0,
        p_clip * p_clip * 10.0,
    ]


def signal_score(feat: dict, strategy: str) -> float:
    """Оценка сигнала 0–10 — 1:1 с signalScore() в HW_FBO_scanner_6.html.

    Логистическая регрессия по 32 признакам + флаг типа (отдельная для ЛП и для пробоя).
    У ложного пробоя (модель с ext) к ним добавляются 6 признаков smExtra.
    Затем интерполяция по ступеням PAV и нормировка на 0–10.
    """
    sm = _load_score_model()
    m = sm["B"] if strategy == "brk" else sm["F"]
    x = []
    for k in sm["feats"]:
        try:
            v = float(feat.get(k, 0))
        except (TypeError, ValueError):
            v = 0.0
        x.append(v if math.isfinite(v) else 0.0)
    x.append(1.0 if strategy == "brk" else 0.0)
    if m.get("ext"):
        x.extend(_false_break_extra(feat))
    z = m["b0"]
    for j in range(len(x)):
        sd = m["sd"][j] or 1
        z += m["w"][j] * max(-3.0, min(3.0, (x[j] - m["mu"][j]) / sd))
    p = 1.0 / (1.0 + math.exp(-z))
    st = m["st"]
    if p <= st[0]["c"]:
        v = st[0]["v"]
    elif p >= st[-1]["c"]:
        v = st[-1]["v"]
    else:
        v = st[-1]["v"]
        for j in range(1, len(st)):
            if p <= st[j]["c"]:
                a, b = st[j - 1], st[j]
                v = a["v"] + (b["v"] - a["v"]) * (p - a["c"]) / ((b["c"] - a["c"]) or 1)
                break
    frac = max(0.0, min(1.0, (v - sm["vmin"]) / (sm["vmax"] - sm["vmin"])))
    return math.floor(frac * 100 + 0.5) / 10



def _card_feat(sf: dict, strategy: str, side: str, ts: int, kind: str, p, score: float) -> dict:
    """Сырые признаки сигнала для фильтров шаблона (порт passes() из HTML) и оценки."""
    import time as _t
    d = dict(sf)
    d.update({
        "type": strategy, "d": 1 if side == "long" else 0,
        "dow": (_t.gmtime(ts / 1000).tm_wday + 1) % 7,   # JS getUTCDay: вс=0
        "p": p if p is not None else 0, "k": kind, "t": ts, "_sc": score,
    })
    return d


def evaluate_brk(
    ticker: str,
    d1c: list[Bar],
    h4c: list[Bar],
    h1c: list[Bar],
    atr_d: float,
    levels: list[Level],
    bias: str,
    lookback_hours: int = 24,
    no_night: bool = False,
    market: str = "crypto",
    ru_ctx: Optional[dict] = None,
) -> list[dict]:
    """
    Сигнал в момент первого закрытия часа за уровнем (текущий час — за уровнем,
    предыдущий — ещё нет). 1:1 с веткой type:'brk' в scanSymbol(), но проверяет
    не только последний час, а последние lookback_hours часов — чтобы скан раз
    в 15 минут не пропускал сигнал, случившийся между запусками.

    no_night: как opt.noNight в HTML — пропускает ТОЛЬКО те часы сигнала, что
    попадают в 23:00–09:00 МСК, а не весь скан целиком (это не то же самое,
    что не сканировать вообще ночью).
    """
    # atr_d/levels/bias args ignored — PIT day-cache like HTML scanSymbol.
    _ = (atr_d, levels, bias)
    cards: list[dict] = []
    if len(h1c) < 2:
        return cards

    # HTML: fromIdx = max(60, h1c.length - opt.scanBars)
    start_idx = max(60, len(h1c) - lookback_hours)
    cache_day = None
    pit_levels: list[Level] = []
    pit_atr_d = 0.0
    pit_bias = "flat"

    for i in range(start_idx, len(h1c)):
        bar = h1c[i]
        if no_night and _is_night_msk(bar.ts):
            continue
        if market == "ru" and _ru_friday_late(bar.ts):
            continue
        dk = bar.ts // 86_400_000
        if dk != cache_day:
            cache_day = dk
            pit_atr_d, pit_levels, pit_bias = _pit_day_state(d1c, h1c, bar)
        if not pit_levels:
            continue

        prev_b = h1c[i - 1]
        h1_upto = h1c[: i + 1]   # только то, что было известно к моменту bar
        a_h1 = atr(h1_upto[-60:]) if len(h1_upto) >= 60 else atr(h1_upto)
        if a_h1 <= 0:
            continue

        vw = sorted(b.vol_quote for b in h1_upto[-25:-1] if b.vol_quote > 0)
        v_med = vw[len(vw) // 2] if vw else 0.0

        for li, lv in enumerate(pit_levels):
            side = _side_of(lv.kind)

            def beyond(b: Bar, _price=lv.price, _side=side) -> bool:
                return b.c > _price if _side == "long" else b.c < _price

            if not (beyond(bar) and not beyond(prev_b) and bar.ts > lv.formed_ts):
                continue

            acc, acc_why = accumulation(h1_upto[:-1], lv.price, pit_atr_d)
            if not acc:
                continue

            cross_b = count_close_crosses(
                [b for b in h1_upto if lv.formed_ts < b.ts < bar.ts], lv.price, lv.formed_ts
            )
            if cross_b > PARAMS["max_crosses"]:
                continue

            bias_ok = not (
                (pit_bias == "up" and side == "short")
                or (pit_bias == "down" and side == "long")
            )
            if not bias_ok:
                continue
            if market == "ru" and _ru_brk_reject(h1_upto, bar, pit_atr_d):
                continue

            rst = _risk_stop_take(bar.c, pit_atr_d, side, stop_atr_frac=0.10, target_r=3.0)
            if rst is None:
                continue
            stop, take, risk = rst

            win8b = h1_upto[-9:-1]
            min_risk_b = max(0.10 * pit_atr_d, 0.02 * bar.c)
            sf = {
                "strength": lv.strength, "crosses": cross_b,
                "touched8": 1 if (len(win8b) >= 8 and min(b.l for b in win8b) <= lv.price <= max(b.h for b in win8b)) else 0,
                "acc": 1,
                "level_age_h": (bar.ts - lv.formed_ts) / 3_600_000,
                "vol_contraction": _vol_contraction(h1_upto),
                "vol_mult": (bar.vol_quote / v_med) if v_med > 0 else 0.0,
                "overshoot_atr": abs(bar.c - lv.price) / a_h1,
                "dist_atr": abs(bar.c - lv.price) / a_h1,
                "bias_ok": 1, "p": 0,
                "brk_bars": 1, "vol_ret_brk": 1,
                "v6_fresh": 1, "v6_poke": 0, "v6_covers": 1,
                "v6_preAcc": 0, "v6_smooth": 0, "v6_d1Against": 0,
                "atrD_pct": pit_atr_d / bar.c,
            }
            sf.update(_extra_feats(
                h1c, h1_upto, i, bar, lv, pit_levels, side, min_risk_b, side,
                side == "long", side, [], pit_atr_d, a_h1, d1c,
            ))
            if market == "ru":
                sf.update(_ru_feats(h1_upto, bar, side, ru_ctx or {}))
            score = signal_score(sf, "brk")

            cards.append({
                "ticker": ticker,
                "version": VERSION,
                "score": score,
                "fwd": [(b.ts, b.o, b.h, b.l, b.c) for b in h1c[i + 1: i + 1 + hold_h(market)]],
                "_ord": (i, li),
                "feat": _card_feat(sf, "brk", side, bar.ts, lv.kind, 0, score),
                "strategy": "brk",
                "side": "LONG" if side == "long" else "SHORT",
                "status": "SIGNAL",
                "level": lv.price,
                "kind": lv.kind,
                "strength": lv.strength,
                "last": bar.c,
                "signal_ts": bar.ts,  # ms
                "dist_atr": abs(bar.c - lv.price) / a_h1,
                "atr_d": pit_atr_d,
                "atr_h1": a_h1,
                "stop": stop,
                "take": take,
                "risk": risk,
                "d1_bias": pit_bias,
                "crosses": cross_b,
                "prob": None,  # модель не применяется к «Пробою»
                "level_age_h": (bar.ts - lv.formed_ts) / 3_600_000,  # ms
                "vol_mult": (bar.vol_quote / v_med) if v_med > 0 else 0.0,
                "why": [acc_why, f"пробой {lv.kind} на закрытии часа", f"запилов после формирования: {cross_b}"],
            })

    cards.sort(key=lambda c: (-c["signal_ts"], -c["strength"], c["dist_atr"]))
    return cards


# ═══════════════════════════ стратегия «Ложный пробой» (strategy="fbo") ══════

def _model_predict(feat: dict) -> float:
    """1:1 с modelPredict() в HTML — прогон через 300 деревьев Random Forest."""
    model = _load_model()
    x = [
        feat["strength"], feat["crosses"], feat["touched8"], feat["acc"],
        min(feat["level_age_h"], 500) / 24, min(feat["vol_contraction"], 3),
        min(feat["vol_mult"], 10), min(feat["overshoot_atr"], 10),
        min(feat["dist_atr"], 10), feat["bias_ok"],
        min(feat["atrD_pct"], 0.3), min(feat["aH1_pct"], 0.1), feat["side_long"],
    ]
    for k in model["kinds"]:
        x.append(1 if feat["kind"] == k else 0)

    raw = model["init_raw"]
    for t in model["trees"]:
        n = 0
        left, right, feats, ths, vals = t["l"], t["r"], t["f"], t["th"], t["v"]
        while left[n] != -1:
            n = left[n] if x[feats[n]] <= ths[n] else right[n]
        raw += model["lr"] * vals[n]
    return 1.0 / (1.0 + math.exp(-raw))


def _vol_contraction(h1c: list[Bar]) -> float:
    short = atr(h1c[-9:-1], 8)
    long_ = atr(h1c[-49:-1], 48)
    return 1.0 if not long_ or long_ <= 0 else short / long_


def evaluate_fbo(
    ticker: str,
    d1c: list[Bar],
    h4c: list[Bar],
    h1c: list[Bar],
    atr_d: float,
    levels: list[Level],
    bias: str,
    threshold: Optional[float] = None,
    lookback_hours: int = 24,
    no_night: bool = False,
    market: str = "crypto",
) -> list[dict]:
    """
    Сигнал на первом часе возврата цены за уровень после пробоя (ложный пробой).
    1:1 с веткой type:'fbo' в scanSymbol(). Использует Random Forest (model.json)
    для скоринга p; сигнал проходит только при p >= threshold (по умолчанию
    0.20 — HTML DEF.thr).
    Проверяет последние lookback_hours часов, а не только текущий — иначе скан
    раз в 15 минут пропускал бы сигнал, случившийся между запусками.
    no_night — см. evaluate_brk: пропускает только часы сигнала в 23:00–09:00 МСК.
    """
    # atr_d/levels/bias args ignored — PIT day-cache like HTML scanSymbol.
    _ = (atr_d, levels, bias)
    cards: list[dict] = []
    _load_model()  # ensure model ready for _model_predict
    if threshold is None:
        threshold = 0.20  # HTML DEF.thr

    lookback = PARAMS["lookback_fbo"]
    if len(h1c) < lookback + 1:
        return cards

    # HTML: fromIdx = max(60, h1c.length - opt.scanBars)
    start_idx = max(60, len(h1c) - lookback_hours)
    cache_day = None
    pit_levels: list[Level] = []
    pit_atr_d = 0.0
    pit_bias = "flat"

    for hi_idx in range(start_idx, len(h1c)):
        bar = h1c[hi_idx]
        if no_night and _is_night_msk(bar.ts):
            continue
        if market == "ru" and _ru_friday_late(bar.ts):
            continue
        dk = bar.ts // 86_400_000
        if dk != cache_day:
            cache_day = dk
            pit_atr_d, pit_levels, pit_bias = _pit_day_state(d1c, h1c, bar)
        if not pit_levels:
            continue

        h1_upto = h1c[: hi_idx + 1]
        a_h1 = atr(h1_upto[-60:]) if len(h1_upto) >= 60 else atr(h1_upto)
        if a_h1 <= 0:
            continue

        vw = sorted(b.vol_quote for b in h1_upto[-25:-1] if b.vol_quote > 0)
        v_med = vw[len(vw) // 2] if vw else 0.0
        vc = _vol_contraction(h1_upto)

        w = h1_upto[-1 - lookback: -1]
        if len(w) != lookback:
            continue

        for li, lv in enumerate(pit_levels):
            br_side = _side_of(lv.kind)

            def beyond_br(b: Bar, _price=lv.price, _side=br_side) -> bool:
                return b.c > _price if _side == "long" else b.c < _price

            broke = any(beyond_br(b) for b in w)
            back = (bar.c < lv.price) if br_side == "long" else (bar.c > lv.price)
            if not (broke and back):
                continue
            if bar.ts <= lv.formed_ts:
                continue

            side = "short" if br_side == "long" else "long"   # сторона входа обратна стороне пробоя
            ext = max([b.h for b in w] + [bar.h]) if br_side == "long" else min([b.l for b in w] + [bar.l])

            # сигнал только на первом часе возврата (предыдущий час ещё был за уровнем)
            if not beyond_br(w[-1]):
                continue

            first_brk_idx = next((k for k, b in enumerate(w) if beyond_br(b)), None)
            if first_brk_idx is None:
                continue
            brk_bars = [b for b in w if beyond_br(b)]
            poke = max((b.h - b.l) for b in brk_bars) / a_h1 if brk_bars else 0.0

            min_risk = max(0.10 * pit_atr_d, 0.02 * bar.c)
            covers = (bar.c + min_risk > ext) if side == "short" else (bar.c - min_risk < ext)

            # индекс первого бара пробоя внутри h1_upto
            gi = len(h1_upto) - 1 - lookback + first_brk_idx
            pre = h1_upto[max(0, gi - 8): gi]
            pre_acc = 0
            if len(pre) == 8:
                hi_p, lo_p = max(b.h for b in pre), min(b.l for b in pre)
                near = all(abs(b.c - lv.price) <= 1.0 * a_h1 for b in pre)
                if hi_p - lo_p <= 2.0 * a_h1 and near:
                    pre_acc = 1

            ap = h1_upto[max(0, gi - 6): gi]
            smooth = 0
            if len(ap) == 6:
                avg_r = sum((b.h - b.l) for b in ap) / 6
                move = (1 if br_side == "long" else -1) * (ap[-1].c - ap[0].o)
                if avg_r < 0.8 * a_h1 and move > 0:
                    smooth = 1

            dd = [b for b in d1c if b.ts + 86_400_000 <= bar.ts][-3:]
            d1_against = 0
            if len(dd) == 3:
                hi_d, lo_d = max(b.h for b in dd), min(b.l for b in dd)
                side_ok = all(
                    (b.c >= lv.price - 0.3 * pit_atr_d) if br_side == "long"
                    else (b.c <= lv.price + 0.3 * pit_atr_d)
                    for b in dd
                )
                if hi_d - lo_d <= 0.9 * pit_atr_d and side_ok:
                    d1_against = 1

            win8 = h1_upto[-9:-1]
            touched8 = 1 if (len(win8) >= 8 and min(b.l for b in win8) <= lv.price <= max(b.h for b in win8)) else 0
            acc_res, _ = accumulation(h1_upto[:-1], lv.price, pit_atr_d)

            cross = count_close_crosses(
                [b for b in h1_upto if lv.formed_ts < b.ts <= bar.ts], lv.price, lv.formed_ts
            )
            bias_ok = not (
                (pit_bias == "up" and side == "short")
                or (pit_bias == "down" and side == "long")
            )

            feat = {
                "strength": lv.strength,
                "crosses": cross,
                "touched8": touched8,
                "acc": 1 if acc_res else 0,
                "level_age_h": (bar.ts - lv.formed_ts) / 3_600_000,  # ms
                "vol_contraction": vc,
                "vol_mult": (bar.vol_quote / v_med) if v_med > 0 else 0.0,
                "overshoot_atr": abs(ext - lv.price) / a_h1,
                "dist_atr": abs(bar.c - lv.price) / a_h1,
                "bias_ok": 1 if bias_ok else 0,
                "atrD_pct": pit_atr_d / bar.c,
                "aH1_pct": a_h1 / bar.c,
                "side_long": 1 if side == "long" else 0,
                "kind": lv.kind,
            }
            p = _model_predict(feat)
            if p < threshold:
                continue

            rst = _risk_stop_take(bar.c, pit_atr_d, side, stop_atr_frac=0.10, target_r=3.0)
            if rst is None:
                continue
            stop, take, risk = rst

            sf = dict(feat)
            sf.update({
                "p": p,
                "brk_bars": sum(1 for b in w if beyond_br(b)),
                "vol_ret_brk": _vol_ret_brk(w, bar, br_side, lv.price),
                "v6_fresh": 1, "v6_poke": poke, "v6_covers": 1 if covers else 0,
                "v6_preAcc": pre_acc, "v6_smooth": smooth, "v6_d1Against": d1_against,
            })
            sf.update(_extra_feats(
                h1c, h1_upto, hi_idx, bar, lv, pit_levels, side,
                max(0.10 * pit_atr_d, 0.02 * bar.c), side,
                side == "long", br_side, w, pit_atr_d, a_h1, d1c,
            ))
            score = signal_score(sf, "fbo")

            cards.append({
                "ticker": ticker,
                "version": VERSION,
                "score": score,
                "fwd": [(b.ts, b.o, b.h, b.l, b.c) for b in h1c[hi_idx + 1: hi_idx + 1 + hold_h(market)]],
                "_ord": (hi_idx, li),
                "feat": _card_feat(sf, "fbo", side, bar.ts, lv.kind, p, score),
                "strategy": "fbo",
                "side": "LONG" if side == "long" else "SHORT",
                "status": "SIGNAL",
                "level": lv.price,
                "kind": lv.kind,
                "strength": lv.strength,
                "last": bar.c,
                "signal_ts": bar.ts,  # ms
                "dist_atr": feat["dist_atr"],
                "atr_d": pit_atr_d,
                "atr_h1": a_h1,
                "stop": stop,
                "take": take,
                "risk": risk,
                "d1_bias": pit_bias,
                "crosses": cross,
                "prob": p,
                "level_age_h": feat["level_age_h"],
                "vol_mult": feat["vol_mult"],
                "poke_atr": poke,
                "covers_wick": covers,
                "pre_accumulation": pre_acc,
                "smooth_approach": smooth,
                "d1_against": d1_against,
                "why": [f"ложный пробой {lv.kind}, модель p={p:.2f}", f"запилов: {cross}"],
            })

    cards.sort(key=lambda c: (-c["signal_ts"], -(c["prob"] or 0), c["dist_atr"]))
    return cards

# ═══════════════════════════ единая точка входа ═══════════════════════════

def evaluate(
    ticker: str,
    last: float,
    bid: float,
    ask: float,
    d1: list[Bar],
    h4: list[Bar],
    h1: list[Bar],
    m5: Optional[list[Bar]] = None,
    vol_usd_24h: float = 0.0,
    strategies: tuple[str, ...] = ("brk", "fbo"),
    fbo_threshold: Optional[float] = None,
    lookback_hours: int = 24,
    no_night: bool = False,
    market: str = "crypto",
    ru_ctx: Optional[dict] = None,
) -> dict:
    """
    Точка входа для скринера. Уровни считаются point-in-time по UTC-дню внутри
    evaluate_brk/evaluate_fbo (как scanSymbol day-cache в HTML): на каждом часе
    lookback — cut = закрытые D1 до bar.ts + синтетический intraday день из H1,
    build_levels(cut, []), затем BRK+FBO с этими уровнями. Не один build_levels
    на полный d1/h4 для всех часов.

    strategies: подмножество ("brk",), ("fbo",) или ("brk", "fbo") — что сканировать.
    lookback_hours: сколько последних часов проверять на сигнал (по умолчанию 24 —
        чтобы скан раз в 15 минут не пропускал сигнал, случившийся между запусками).
    no_night: как чекбокс «без ночи» в HTML — пропускает часы 23:00–09:00 МСК
        поштучно, а не весь скан целиком.
    fbo_threshold: пол модели для FBO; None → 0.20 (HTML DEF.thr).
    """
    d1c, h4c, h1c = last_closed(d1), last_closed(h4), last_closed(h1)
    out = {"version": VERSION, "ticker": ticker, "ok": False, "cards": [], "reason": ""}

    if vol_usd_24h and vol_usd_24h < PARAMS["vol_usd_min"]:
        out["reason"] = "оборот < $1M"
        return out
    if len(h1c) < PARAMS["h1_min"] or len(d1c) < PARAMS["d1_min"] or last <= 0:
        out["reason"] = "мало баров"
        return out

    # Метаданные по полному ряду (для out); сигналы используют PIT внутри BRK/FBO.
    atr_d = atr(d1c)
    if atr_d <= 0:
        out["reason"] = "ATR=0"
        return out

    out["ok"] = True
    out["atr_d"] = atr_d
    out["d1_bias"] = d1_bias(d1c)

    cards: list[dict] = []
    # Пустые levels/bias — evaluate_* строят PIT сами (args игнорируются).
    if "brk" in strategies:
        cards.extend(evaluate_brk(
            ticker, d1c, h4c, h1c, atr_d, [], "flat",
            lookback_hours=lookback_hours, no_night=no_night, market=market, ru_ctx=ru_ctx,
        ))
    if "fbo" in strategies:
        cards.extend(evaluate_fbo(
            ticker, d1c, h4c, h1c, atr_d, [], "flat",
            threshold=fbo_threshold, lookback_hours=lookback_hours, no_night=no_night, market=market,
        ))

    out["cards"] = cards
    return out


if __name__ == "__main__":
    m = _load_model()
    print(VERSION, "params", PARAMS)
    print("model loaded:", len(m["trees"]), "trees, threshold", m.get("threshold"))
