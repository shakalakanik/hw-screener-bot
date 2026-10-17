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
import time
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
    "vol_usd_min": 1_000_000.0,   # как V0.PARAMS в HTML 7.92 (отбор $5M — MIN_VOL_USD_24H)
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
HOLD_CRYPTO, HOLD_RU = 168, 96   # HTML 6.94: MOEX — 4 суток


def hold_h(market: str) -> int:
    """getHold(): удержание 48ч для акций РФ, 168ч для крипты."""
    return HOLD_RU if market == "ru" else HOLD_CRYPTO


def _ru_friday_late(ts_ms: int) -> bool:
    """Пятница ≥13:00 МСК — сигнал не берём (уйдёт на выходные). JS: getUTCDay()==5 && hl>=13."""
    import time as _t
    g = _t.gmtime(ts_ms / 1000)
    hl = (g.tm_hour + 3) % 24
    return ((g.tm_wday + 1) % 7) == 5 and hl >= 13


def ru_weekend(ts_ms: int) -> bool:
    """17.10: Мосбиржа — сигналы только в будни. Торги выходного дня (сб/вс по МСК, ISS отдаёт их свечи в TQBR)
    сигналов не дают: в HTML 7.71 позиции закрываются в пт 20:00 (ruFriCut), с пт 13:00 сигналы не берутся
    («уйдут на выходные»), свечи выходного дня считаются «редкими тонкими» и не показываются."""
    import time as _t
    return _t.gmtime(int(ts_ms) / 1000 + 3 * 3600).tm_wday >= 5


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


def _ru_brk_flags(h1cut: list, bar, atr_d: float) -> dict:
    """Флаги пробоя РФ (HTML 7.03): open — 10:00 МСК, low_atr — ATR D1 <1% (оба отсекают);
    low_vol — объём <0.5 медианы 20, weak — тело <40% (только пометки)."""
    hl = (time.gmtime(bar.ts / 1000).tm_hour + 3) % 24
    vv = sorted((b.vol_quote or 0) for b in h1cut[-21:-1])
    v_med20 = vv[len(vv) // 2] if vv else 0
    rng = bar.h - bar.l
    return {"open": hl == 10,
            "low_vol": v_med20 > 0 and (bar.vol_quote or 0) < 0.5 * v_med20,
            "low_atr": atr_d > 0 and (atr_d / bar.c) < 0.01,
            "weak": rng > 0 and abs(bar.c - bar.o) / rng < 0.40}


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


# ═══ MOEX 7.0x (HTML e32370…): режим индекса IMOEX, признаки тренда, оценка и The best MOEX ═══
RU_LONG_SA50 = 0.06
RU_BEST = {"ix24": 0.011, "sl200": 0.22}
RU_IX = {"ext_macd": 1.5, "ext_day": 0.017, "ext_rsi": 72}
RU_TB = {"hour": 12, "clu": 4, "dm": 0.3}
RU_AUTO = {"str": 4.0, "cross": 2.0, "acc": 1.0, "hours": "7-19", "rubase": 4.0}
RU_TGT = 3.0   # «Тейк (Мосбиржа)» по умолчанию 3R (решение пользователя: без настройки)
_RU_TASTE = None


def _ru_taste() -> dict:
    global _RU_TASTE
    if _RU_TASTE is None:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ru_taste.json")) as fh:
            _RU_TASTE = json.load(fh)
    return _RU_TASTE


def _ema_list(x: list, n: int) -> list:
    a = 2 / (n + 1)
    e = [0.0] * len(x)
    if not x:
        return e
    e[0] = x[0]
    for k in range(1, len(x)):
        e[k] = e[k - 1] + a * (x[k] - e[k - 1])
    return e


def _rsi_list(x: list, n: int) -> list:
    r = [0.0] * len(x)
    au = ad = 1e-9
    if x:
        r[0] = 50.0
    for k in range(1, len(x)):
        d = x[k] - x[k - 1]
        au += ((d if d > 0 else 0) - au) / n
        ad += ((-d if d < 0 else 0) - ad) / n
        r[k] = 100 - 100 / (1 + au / max(ad, 1e-12))
    return r


def ru_ix_prep(d1: list | None, h1: list | None):
    """ruIxPrep(): d1 — закрытые дневки IMOEX, h1 — подтверждённые часовики IMOEX."""
    if not d1 or not h1 or len(d1) < 70 or len(h1) < 260:
        return None
    hc = [b.c for b in h1]
    dc = [b.c for b in d1]
    e12, e26, e200 = _ema_list(hc, 12), _ema_list(hc, 26), _ema_list(hc, 200)
    tr = []
    for k, b in enumerate(h1):
        pc = h1[k - 1].c if k else h1[0].c
        tr.append(max(b.h - b.l, abs(b.h - pc), abs(b.l - pc)))
    d12, d26 = _ema_list(dc, 12), _ema_list(dc, 26)
    return {"hts": [b.ts for b in h1], "hc": hc, "E200": e200, "M": [e12[k] - e26[k] for k in range(len(hc))],
            "ATRH": _ema_list(tr, 27), "dts": [b.ts for b in d1], "dc": dc, "D20": _ema_list(dc, 20),
            "D50": _ema_list(dc, 50), "DM": [d12[k] - d26[k] for k in range(len(dc))], "DR": _rsi_list(dc, 14)}


def ru_ix_state(P, ts: int, sg: int):
    if not P:
        return None
    import bisect
    k = bisect.bisect_right(P["hts"], ts) - 1
    if k < 240:
        return None
    kd = bisect.bisect_right([t + 86_400_000 for t in P["dts"]], ts)
    if kd < 60:
        return None
    x, j = P["hc"][k], kd - 1
    rs = P["DR"][j] if sg > 0 else 100 - P["DR"][j]
    S = int(sg * (x / P["D20"][j] - 1) > 0) + int(sg * (x / P["D50"][j] - 1) > 0) \
        + int(sg * (P["D20"][j] / P["D20"][j - 5] - 1) > 0) + int(sg * (P["E200"][k] / P["E200"][k - 40] - 1) > 0) \
        + int(sg * (x / P["E200"][k] - 1) > 0) + int(sg * P["DM"][j] > 0) + int(rs > 50)
    ext = (sg * P["M"][k] / P["ATRH"][k] > RU_IX["ext_macd"]) or (sg * (x / P["dc"][j] - 1) > RU_IX["ext_day"]) \
        or (rs > RU_IX["ext_rsi"])
    return {"ru_ixS": S, "ru_ixExt": 1 if ext else 0}


def ix_sma50_side(d1: list | None, ts: int, br_side: str):
    if not d1:
        return None
    c = []
    for b in d1:
        if b.ts + 86_400_000 <= ts:
            c.append(b.c)
        else:
            break
    if len(c) < 50:
        return None
    sma = sum(c[-50:]) / 50
    r = c[-1] / sma - 1
    return r if br_side == "long" else -r


def _h1_emas(h1c: list):
    n = len(h1c)
    if not n:
        return None, None
    e50, e200 = [0.0] * n, [0.0] * n
    e50[0] = e200[0] = h1c[0].c
    for k in range(1, n):
        e50[k] = e50[k - 1] + 2 / 51 * (h1c[k].c - e50[k - 1])
        e200[k] = e200[k - 1] + 2 / 201 * (h1c[k].c - e200[k - 1])
    return e50, e200


def ru_brk_feats(h1c, i, bar, br_side, atr_d, E50, E200, ix_h1) -> dict:
    sg = 1 if br_side == "long" else -1
    o: dict = {}
    if atr_d > 0 and E200 and i >= 41:
        o["ru_sl200"] = sg * (E200[i - 1] - E200[i - 41]) / atr_d
        o["ru_e50200"] = sg * (E50[i - 1] - E200[i - 1]) / atr_d
    if atr_d > 0 and i >= 25:
        o["ru_run24"] = sg * (h1c[i - 1].c - h1c[i - 25].c) / atr_d
    if atr_d > 0 and i >= 8:
        w = h1c[i - 8:i]
        o["ru_b8w"] = (max(b.h for b in w) - min(b.l for b in w)) / atr_d
    w = h1c[max(0, i - 60):i]
    a_h = (sum(b.h - b.l for b in w) / len(w)) if w else 0
    o["ru_brng"] = (bar.h - bar.l) / a_h if a_h > 0 else 0
    r = bar.h - bar.l
    o["ru_cpos"] = (((bar.c - bar.l) / r) if sg > 0 else ((bar.h - bar.c) / r)) if r > 0 else 0.5
    if ix_h1:
        import bisect
        k = bisect.bisect_right([b.ts for b in ix_h1], bar.ts) - 1
        if k >= 25:
            o["ru_ix24"] = sg * (ix_h1[k - 1].c / ix_h1[k - 25].c - 1)
    return o


def _ru_hour(s: dict) -> int:
    return (time.gmtime(s["t"] / 1000).tm_hour + 3) % 24


def is_best_ru(s: dict) -> bool:
    """isBestRU(): шорт-пробой ≥12:00 МСК и (≥4 пробоев вниз по списку за 3 ч или акция сегодня −0.3 ATR)."""
    if s.get("type") != "brk" or s.get("d") != 0:
        return False
    if _ru_hour(s) < RU_TB["hour"]:
        return False
    clu, dm = s.get("ru_clu"), s.get("ru_dm")
    return (clu is not None and clu >= RU_TB["clu"]) or (dm is not None and dm > RU_TB["dm"])


def _ru_best_conds(s: dict) -> int:
    a, b = s.get("ru_ix24"), s.get("ru_sl200")
    return int(a is not None and a < RU_BEST["ix24"]) + int(b is not None and b >= RU_BEST["sl200"])


def _num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def score_ru(s: dict):
    """scoreRU(): ярус по условиям The best MOEX + похожесть на сделки пользователя (RU_TASTE)."""
    T = _ru_taste()
    side = 1 if s.get("d") == 1 else -1
    hr = _ru_hour(s)
    x = [_num(s.get(k)) for k in ("ru_sl200", "ru_brng", "ru_b8w", "ru_e50200", "vol_mult", "ru_run24",
                                  "ru_cpos", "ru_base8")] + [hr, _num(s.get("crosses")), side]
    if any(v is None for v in x) or s.get("ru_ix24") is None:
        return None
    z = T["B"]
    for j, v in enumerate(x):
        v = min(T["hi"][j], max(T["lo"][j], v))
        z += T["W"][j] * (v - T["mu"][j]) / T["sd"][j]
    q = T["qs"]
    n = len(q) - 1
    if z <= q[0]:
        p = 0.0
    elif z >= q[n]:
        p = 1.0
    else:
        p = 1.0
        for j in range(1, n + 1):
            if z <= q[j]:
                p = ((j - 1) + (z - q[j - 1]) / max(q[j] - q[j - 1], 1e-12)) / n
                break
    nc = 2 if is_best_ru(s) else (1 if (_ru_best_conds(s) >= 1 or (s.get("d") == 0 and (
        s.get("ru_ixS") == 7 or hr >= RU_TB["hour"]))) else 0)
    clu = s.get("ru_clu")
    if nc == 2 and hr >= 13 and s.get("ru_ixS") == 7 and clu is not None and clu >= 7:
        return 9.5 + 0.5 * p
    return 8 + 1.45 * p if nc == 2 else (5 + 2.9 * p if nc == 1 else 4.9 * p)


def ru_cluster(cards: list[dict]) -> None:
    """ruCluster(): для каждого пробоя — сколько пробоев той же стороны по всему списку за 3 ч (включая этот).
    cards — карточки всех бумаг после dedupList (как raw в HTML); пишет feat["ru_clu"]."""
    import bisect
    for d in (0, 1):
        ts = sorted(c["feat"]["t"] for c in cards if c.get("strategy") == "brk" and c["feat"].get("d") == d
                    and c["feat"].get("sw") != 2)   # 7.37: дневные шорты в «пробоях за 3 ч» не считаются
        for c in cards:
            if c.get("strategy") == "brk" and c["feat"].get("d") == d and c["feat"].get("sw") != 2:
                t = c["feat"]["t"]
                c["feat"]["ru_clu"] = bisect.bisect_left(ts, t + 1) - bisect.bisect_left(ts, t - 2 * 3_600_000)


def ru_rescore(card: dict) -> None:
    """signalScore() для пробоя MOEX — после ruCluster (оценка зависит от ru_clu)."""
    if card.get("strategy") != "brk":
        return
    if card["feat"].get("sw") == 2:   # HTML 7.37: дневной шорт — средний сигнал
        card["score"] = 5
        card["feat"]["_sc"] = 5
        return
    v = score_ru(card["feat"])
    if v is None:
        return
    sc = math.floor(v * 10 + 0.5) / 10
    card["score"] = sc
    card["feat"]["_sc"] = sc


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


def signal_score(feat: dict, strategy: str, market: str = "crypto") -> float:
    """Оценка сигнала 0–10 — 1:1 с signalScore() в HW_FBO_scanner_6.html.

    Логистическая регрессия по 32 признакам + флаг типа (отдельная для ЛП и для пробоя).
    У ложного пробоя (модель с ext) к ним добавляются 6 признаков smExtra.
    Затем интерполяция по ступеням PAV и нормировка на 0–10.
    """
    if strategy != "brk":
        if market != "ru":
            v6 = score_v6(feat)   # HTML 6.93: у ЛП на крипте — ярусная оценка v6
            if v6 is not None:
                return math.floor(v6 * 10 + 0.5) / 10
        r = hw_score(feat)   # HTML 6.78.3: у ЛП оценка v4 (похожесть на эталоны)
        if r is not None:
            return math.floor(r["score"] * 10 + 0.5) / 10
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



# ═══ Оценка ЛП v5/v6 (HTML 6.88–6.93), только крипта ═══
_SCORE_V5 = None


def _load_score_v5() -> dict:
    global _SCORE_V5
    if _SCORE_V5 is None:
        with open(os.path.join(os.path.dirname(__file__), "score_v5.json"), "r", encoding="utf-8") as fh:
            _SCORE_V5 = json.load(fh)
    return _SCORE_V5


def score_v5_raw(s: dict):
    """1:1 scoreV5raw(): логистическая регрессия на рангах → перцентиль 0..10. None — нет признаков."""
    S = _load_score_v5()
    vol5 = s.get("vol5mAvg")
    appr = s.get("approach_atr")
    if appr is None:
        appr = s.get("appr")
    v = {"lv_touch24": s.get("lv_touch24"), "ap03": s.get("ap03"), "near12": s.get("near12"),
         "approach_atr": appr, "lv_touch": s.get("lv_touch"), "mv12": s.get("mv12"),
         "log_vol5m": (math.log10(max(vol5, 1)) if vol5 is not None else None), "v6_poke": s.get("v6_poke")}
    z = S["b"]
    import bisect
    for j, k in enumerate(S["K"]):
        x = v[k]
        if x is None:
            return None
        try:
            x = float(x)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(x):
            return None
        q = S["Q"][j]
        z += S["w"][j] * bisect.bisect_right(q, x) / len(q)
    P = S["P"]
    if z <= P[0]:
        return 0.0
    if z >= P[-1]:
        return 10.0
    for i in range(1, len(P)):
        if z <= P[i]:
            return ((i - 1) + (z - P[i - 1]) / ((P[i] - P[i - 1]) or 1)) * 10 / (len(P) - 1)
    return 10.0


def _risk_d(s: dict):
    st, e, a = s.get("st"), s.get("e"), s.get("atrD")
    if st is not None and e is not None:
        return abs(st - e)
    if a is not None and e is not None:
        return max(0.10 * a, 0.02 * e)
    return None


def score_v6(s: dict):
    """1:1 scoreV6(): шорт с D1-серией ≥2, vtr ≤1.0, без шипа/хвоста → 8+0.2·v5; иначе ≤7.9."""
    T = score_v5_raw(s)
    if T is None:
        return None
    side = s.get("side") or ("short" if s.get("d") == 0 else "long")
    d = _risk_d(s)
    g = s.get("g_spk")
    spike = g is not None and g >= 8
    pk = s.get("pk_ext")
    poke = (abs(pk - s["e"]) / d > 3) if (pk is not None and d) else False
    ds, vtr = s.get("d1_streak"), s.get("vtr")
    if side == "short" and ds is not None and vtr is not None and ds >= 2 and vtr <= 1.0 and not spike and not poke:
        return 8 + 0.2 * T
    return max(0.0, min(7.9, 0.79 * T - (3 if spike else 0) - (2 if poke else 0)))


_P_TIERS = [0.15, 0.18, 0.20, 0.22, 0.245, 0.30]


def _p_pct(s: dict) -> float:
    try:
        p = float(s.get("p"))
        if not math.isfinite(p):
            p = 0.2
    except (TypeError, ValueError):
        p = 0.2
    P = _P_TIERS
    if p <= P[0]:
        return 0.0
    for i in range(1, len(P)):
        if p <= P[i]:
            return ((i - 1) + (p - P[i - 1]) / (P[i] - P[i - 1])) / (len(P) - 1)
    return 1.0


def _side(s: dict) -> str:
    return s.get("side") or ("short" if s.get("d") == 0 else "long")


def score_v7(s: dict):
    """HTML 7.39 scoreV7(): ЛП-шорты крипты, ярусы «близость к The best»."""
    if _side(s) != "short":
        return None
    if s.get("d1_streak") is None or s.get("vtr") is None:
        return None
    d = _risk_d(s)
    pk = s.get("pk_ext")
    poke = abs(pk - s["e"]) / d if (pk is not None and d) else 0
    g = s.get("g_spk")
    spike = g is not None and g >= 8
    D1, VOL = s["d1_streak"] >= 2, s["vtr"] <= 1.0
    tier = 0 if (poke > 3 or (not D1 and not VOL and spike)) else (3 if (D1 and VOL) else (2 if (D1 or VOL) else 1))
    return [0, 4, 6, 8][tier] + [3.9, 1.9, 1.9, 2.0][tier] * _p_pct(s)


def score_v8_pts(s: dict):
    """HTML 7.42 scoreV8pts()."""
    for k in ("d1_streak", "lv_touch24", "lvl_density", "crosses", "brk_run"):
        if s.get(k) is None:
            return None
    d = _risk_d(s)
    pk = s.get("pk_ext")
    poke = abs(pk - s["e"]) / d if (pk is not None and d) else 0
    g = s.get("g_spk")
    bad = poke > 3 or (g is not None and g >= 8)
    return (2 * (s["d1_streak"] >= 2) + (s["lv_touch24"] == 0) + (s["lvl_density"] <= 2)
            + (s["crosses"] <= 4) + (s["brk_run"] <= 2) - (2 if bad else 0))


def score_v8(s: dict):
    """HTML 7.42 scoreV8(): ЛП-шорты крипты, «похожесть на The best» по баллам."""
    if _side(s) != "short":
        return None
    pt = score_v8_pts(s)
    if pt is None:
        return None
    B = ([0, 3.9] if pt <= 0 else [4, 4.6] if pt == 1 else [4.7, 5.3] if pt == 2 else [5.4, 5.9] if pt == 3
         else [6, 7.9] if pt == 4 else [8, 8.9] if pt == 5 else [9, 10])
    return B[0] + (B[1] - B[0]) * _p_pct(s)


def score_long(s: dict):
    """HTML 7.49 scoreLong(): ЛП-лонги крипты."""
    if _side(s) != "long":
        return None
    ds = s.get("d1_streak")
    if ds is None:
        return None
    vp = s.get("v6_poke")
    pt = -1 if ds <= -2 else 2 * (ds >= 2) + (1 if (vp is not None and vp <= 1.385) else 0)
    B = [0, 3.9] if pt < 0 else [4, 5.9] if pt == 0 else [6, 7.9] if pt == 1 else [8, 10]
    return B[0] + (B[1] - B[0]) * _p_pct(s)


def _r1(v: float) -> float:
    return math.floor(v * 10 + 0.5) / 10


def signal_score_show(feat: dict, strategy: str, market: str = "crypto") -> float:
    """HTML 7.71 signalScore(): у ЛП крипты — scoreLong (лонги) → v8 → v7 → v6; остальное — как раньше.
    Порядок/приоритет сигналов по-прежнему по v6 (prioOf, HTML 7.39) — это поле card['score']."""
    if strategy != "brk" and market != "ru":
        for fn in (score_long, score_v8, score_v7):
            v = fn(feat)
            if v is not None:
                return _r1(v)
    return signal_score(feat, strategy, market)


def turn_day_at(h1c: list, i: int) -> float:
    """HTML 7.10 turnDayAt(): оборот за 24 часа до сигнала включительно."""
    v = 0.0
    for k in range(max(0, i - 23), i + 1):
        if 0 <= k < len(h1c):
            v += h1c[k].vol_quote or 0
    return v


# ═══ The best (HTML 6.93, режим «edge») ═══
BEST_SPK = 8
BEST_POKE = 3
BEST_WEEK = 14
EDGE_D1 = 2
EDGE_VTR = 1.0


def best_verdict(s: dict):
    """1:1 bestVerdict() (BEST_MODE='edge'): None — проходит, иначе причина."""
    if (s.get("type") or "fbo") != "fbo":
        return "notfbo"
    side = s.get("side") or ("short" if s.get("d") == 0 else "long")
    if side != "short":
        return "notshort"
    ds, vtr = s.get("d1_streak"), s.get("vtr")
    if ds is None or vtr is None:
        return "nodata"
    if ds < EDGE_D1:
        return "d1"
    if vtr > EDGE_VTR:
        return "vol"
    g = s.get("g_spk")
    if g is not None and g >= BEST_SPK:
        return "spike"
    return None


def best_poke(s: dict):
    pk, st, e = s.get("pk_ext"), s.get("st"), s.get("e")
    if pk is None or st is None or e is None:
        return None
    d = abs(st - e)
    return abs(pk - e) / d if d > 0 else None


# ═══ Оценка ложного пробоя v4 (HW_FBO_scanner 6.78.3): «похожесть на эталонные сделки» ═══
_HW_SCORE_PATH = os.path.join(os.path.dirname(__file__), "hw_score.json")
_HW_SCORE = None


def _load_hw_score() -> dict:
    global _HW_SCORE
    if _HW_SCORE is None:
        with open(_HW_SCORE_PATH, "r", encoding="utf-8") as fh:
            _HW_SCORE = json.load(fh)
    return _HW_SCORE


def fbo_shape(h1c: list, i: int, L: float, br_side: str, formed_ts: int, atr_d: float, v_med: float) -> dict:
    """1:1 с fboShape() в HTML: признаки уровня/подхода/прокола по H1."""
    dr = 1 if br_side == "long" else -1
    H, D = 3_600_000, 86_400_000

    def beyond(c):
        return dr * (c - L) > 0

    j = i - 1
    if beyond(h1c[j].c):
        while j - 1 >= 0 and beyond(h1c[j - 1].c) and h1c[j - 1].ts > formed_ts:
            j -= 1
    else:
        f = -1
        for k in range(i - 6, i):
            if k >= 0 and beyond(h1c[k].c):
                f = k
                break
        j = f if f >= 0 else i - 1
    bstart = h1c[j].ts
    touch = touch24 = 0
    prom = None
    for k in range(0, j):
        b = h1c[k]
        if b.ts <= formed_ts or b.ts < formed_ts + D or b.ts >= bstart:
            continue
        if b.l <= L <= b.h:
            touch += 1
            if b.ts >= bstart - 24 * H:
                touch24 += 1
        away = dr * (L - (b.l if dr > 0 else b.h))
        if prom is None or away > prom:
            prom = away
    if prom is None:
        prom = 0.0

    ap03 = 200.0
    for k in range(j - 1, max(-1, j - 200), -1):
        if dr * (L - h1c[k].c) >= 0.3 * atr_d:
            ap03 = (bstart - h1c[k].ts) / H
            break
    near = sum(1 for k in range(max(0, j - 12), j) if abs(h1c[k].c - L) <= 0.25 * atr_d)
    c12 = h1c[max(0, j - 12)].c
    vmax = 0.0
    for k in range(j, i + 1):
        vmax = max(vmax, h1c[k].vol_quote or 0.0)
    # HTML 6.93: «дёрганость» (самый большой H1-бар за 48ч / медианный бар за 167ч),
    # экстремум прокола за 3 бара (вкл. бар сигнала), затухание объёма перед сигналом.
    rngs = sorted(h1c[k].h - h1c[k].l for k in range(max(0, i - 167), i))
    nn = len(rngs)
    med = (rngs[(nn - 1) // 2] if nn % 2 else (rngs[nn // 2 - 1] + rngs[nn // 2]) / 2) if nn else 0.0
    spk = 0.0
    for k in range(max(0, i - 48), i):
        spk = max(spk, h1c[k].h - h1c[k].l)
    pk = None
    for k in range(max(0, i - 2), i + 1):
        v = h1c[k].h if dr > 0 else h1c[k].l
        pk = v if pk is None else (max(pk, v) if dr > 0 else min(pk, v))
    vtr = None
    if i >= 4:
        vb = (h1c[i - 2].vol_quote or 0) + (h1c[i - 3].vol_quote or 0) + (h1c[i - 4].vol_quote or 0)
        if vb > 0:
            vtr = (h1c[i - 1].vol_quote or 0) / (vb / 3)
    return {
        "g_spk": spk / med if med > 0 else None, "pk_ext": pk, "vtr": vtr,
        "lvB": bstart, "lv_touch": touch, "lv_touch24": touch24, "ap03": ap03, "near12": near,
        "mv12": dr * (L - c12) / atr_d if atr_d > 0 else 0.0,
        "vbrk": vmax / v_med if v_med > 0 else 0.0,
        "lv_prom": prom / atr_d if atr_d > 0 else 0.0,
        "brk_run": i - j,
        "ret_d": dr * (L - h1c[i].c) / atr_d if atr_d > 0 else 0.0,
    }


def _hw_ln(x) -> float:
    return math.log1p(max(0.0, x))


def hw_x(s: dict):
    for k in ("lv_touch", "ap03", "near12", "lv_prom", "brk_run"):
        if s.get(k) is None:
            return None
    g = lambda k: _score_feat_num(s, k)
    return [
        1.0 if s.get("strength") == 5 else 0.0, 1.0 if s.get("strength") == 4 else 0.0,
        _hw_ln(g("lv_touch24")), _hw_ln(g("lv_touch")), g("near12") / 12, _hw_ln(g("ap03")),
        _hw_ln(g("vbrk")), min(g("v6_poke"), 6), min(g("lv_prom"), 5), min(g("dist_atr"), 3),
        min(g("ret_d"), 1), min(g("crosses"), 16) / 4, _hw_ln(g("brk_run")),
    ]


def hw_score(s: dict):
    """1:1 с hwScore(): итог = max(модель, память по эталонам), 0..10. None — нет признаков."""
    x = hw_x(s)
    if x is None:
        return None
    S = _load_hw_score()
    z = [(v - S["mu"][j]) / S["sd"][j] for j, v in enumerate(x)]
    dec = S["b"] + sum(S["w"][j] * z[j] for j in range(len(z)))
    pv, val = S["pv"], S["val"]
    if dec <= pv[0]:
        model = val[0]
    elif dec >= pv[-1]:
        model = val[-1]
    else:
        model = val[-1]
        for k in range(1, len(pv)):
            if dec <= pv[k]:
                model = val[k - 1] + (val[k] - val[k - 1]) * (dec - pv[k - 1]) / ((pv[k] - pv[k - 1]) or 1)
                break
    dmin, near = 1e9, -1
    for i, r in enumerate(S["lib"]):
        q = math.sqrt(sum((z[j] - r[j]) ** 2 for j in range(len(z))) / len(z))
        if q < dmin:
            dmin, near = q, i
    mem = 10 - 3 * dmin / S["memR"] if dmin <= S["memR"] else 0.0
    return {"score": max(0.0, min(10.0, max(model, mem))), "model": model, "mem": mem,
            "near": S["lab"][near], "dist": dmin}


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

    is_ru = market == "ru"
    rc = ru_ctx or {}
    E50, E200 = _h1_emas(h1c) if is_ru else (None, None)
    day_sa50 = day_prev_c = None
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
            if is_ru:
                cut_d = [b for b in d1c if b.ts + 86_400_000 <= bar.ts]
                day_sa50 = (cut_d[-1].c / (sum(b.c for b in cut_d[-50:]) / 50) - 1) if len(cut_d) >= 50 else None
                day_prev_c = cut_d[-1].c if cut_d else None
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
            if not acc and not is_ru:   # HTML 7.05: на MOEX пробой без накопления записывается (acc:0)
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
            ru_flags = _ru_brk_flags(h1_upto, bar, pit_atr_d) if is_ru else None
            if is_ru and (ru_flags["open"] or ru_flags["low_atr"]):
                continue   # HTML 7.03: слабое тело / малый объём — не отсекаются, а помечаются

            rst = _risk_stop_take(bar.c, pit_atr_d, side, stop_atr_frac=0.10, target_r=3.0)
            if rst is None:
                continue
            stop, take, risk = rst

            win8b = h1_upto[-9:-1]
            min_risk_b = max(0.10 * pit_atr_d, 0.02 * bar.c)
            sf = {
                "strength": lv.strength, "crosses": cross_b,
                "touched8": 1 if (len(win8b) >= 8 and min(b.l for b in win8b) <= lv.price <= max(b.h for b in win8b)) else 0,
                "acc": 1 if acc else 0,
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
                "lv": lv.price, "e": bar.c, "atrD": pit_atr_d, "turnD": turn_day_at(h1c, i),
            }
            sf.update(_extra_feats(
                h1c, h1_upto, i, bar, lv, pit_levels, side, min_risk_b, side,
                side == "long", side, [], pit_atr_d, a_h1, d1c,
            ))
            if market == "ru":
                sf.update(_ru_feats(h1_upto, bar, side, rc))
                sf["ru_base8"] = sum(1 for b in h1_upto[-9:-1] if abs(b.c - lv.price) <= 0.3 * pit_atr_d)
                sf["ru_ix50"] = ix_sma50_side(rc.get("imoexD1"), bar.ts, side)
                sf["ru_sa50"] = day_sa50
                sf["ru_dm"] = ((1 if side == "long" else -1) * (bar.c - day_prev_c) / pit_atr_d
                               if (day_prev_c is not None and pit_atr_d > 0) else None)
                sf["ru_weak"] = 1 if ru_flags["weak"] else 0
                sf["ru_lowvol"] = 1 if ru_flags["low_vol"] else 0
                sf.update(ru_brk_feats(h1c, i, bar, side, pit_atr_d, E50, E200, rc.get("imoexH1")))
                sf.update(ru_ix_state(rc.get("ixP"), bar.ts, 1 if side == "long" else -1) or {})
            score = signal_score(sf, "brk")

            cards.append({
                "ticker": ticker,
                "version": VERSION,
                "score": score,
                "fwd": [(b.ts, b.o, b.h, b.l, b.c) for b in h1c[i + 1: i + 1 + (RU_HOLD_BARS if market == "ru" else hold_h(market))]],
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
            if is_ru:
                cards[-1]["feat"]["mkt"] = "ru"
                ru_rescore(cards[-1])   # HTML 7.02: оценка MOEX (до ruCluster — как prioOf в dedupList)

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
            sf.update(fbo_shape(h1c, hi_idx, lv.price, br_side, lv.formed_ts, pit_atr_d, v_med))
            sf.update({"vol5mAvg": v_med / 12, "atrD": pit_atr_d, "e": bar.c, "side": side,
                       "lv": lv.price, "turnD": turn_day_at(h1c, hi_idx)})
            score = signal_score(sf, "fbo", market)            # приоритет (HTML prioOf: v6)
            score_show = signal_score_show(sf, "fbo", market)  # показ/фильтр «мин. оценка» (HTML signalScore)

            cards.append({
                "ticker": ticker,
                "version": VERSION,
                "score": score,
                "fwd": [(b.ts, b.o, b.h, b.l, b.c) for b in h1c[hi_idx + 1: hi_idx + 1 + (RU_HOLD_BARS if market == "ru" else hold_h(market))]],
                "_ord": (hi_idx, li),
                "feat": _card_feat(sf, "fbo", side, bar.ts, lv.kind, p, score_show),
                "score_show": score_show,
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
    k30: Optional[list[Bar]] = None,
) -> dict:
    """
    k30: 30-минутки (HTML 7.58) — ложный пробой ловится на середине часа (только крипта, только ЛП).
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

    if k30 and "fbo" in strategies and market != "ru":
        cards.extend(_half_hour_fbo(ticker, d1c, h4c, h1, h1c, k30, fbo_threshold,
                                    lookback_hours, no_night, market))
    out["cards"] = cards
    return out


HALF_H = 1_800_000


def _half_hour_fbo(ticker, d1c, h4c, h1, h1c, k30, threshold, lookback_hours, no_night, market) -> list[dict]:
    """HTML 7.58 scanSymbol(): для каждого часа окна (и формирующегося) первая 30-минутка часа — как
    закрытая часовая свеча; ЛП на ней = сигнал с середины часа (_src=30, вход в t+30 мин).
    Свечи вперёд: вторая половина часа + следующие часы."""
    by = {b.ts: b for b in k30}
    start = max(60, len(h1c) - lookback_hours)
    form_h = h1[-1].ts if (h1 and not h1[-1].confirmed) else None
    hold = hold_h(market)
    out = []
    for i in range(start, len(h1c) + 1):
        H = h1c[i].ts if i < len(h1c) else form_h
        if H is None:
            continue
        a = by.get(H)
        if not a or not a.confirmed:
            continue
        pb = Bar(ts=H, o=a.o, h=a.h, l=a.l, c=a.c, vol_quote=a.vol_quote or 0, confirmed=True)
        part = h1c[:i] + [pb]
        sg = evaluate_fbo(ticker, d1c, h4c, part, 0.0, [], "flat", threshold=threshold,
                          lookback_hours=1, no_night=no_night, market=market)
        b2 = by.get(H + HALF_H)
        fwd = ([(b2.ts, b2.o, b2.h, b2.l, b2.c)] if (b2 and b2.confirmed) else []) + \
              [(b.ts, b.o, b.h, b.l, b.c) for b in h1c[i + 1: i + 1 + hold]]
        for c in sg:
            if c["signal_ts"] != H:
                continue
            c["src30"] = True
            c["feat"]["_src"] = 30
            c["fwd"] = fwd
            c["_ord"] = (i, c["_ord"][1], 30)
            out.append(c)
    return out


if __name__ == "__main__":
    m = _load_model()
    print(VERSION, "params", PARAMS)
    print("model loaded:", len(m["trees"]), "trees, threshold", m.get("threshold"))


# ═══ HTML 7.49 / 7.55: BTC-поводырь и связанность с BTC (для ЛП-лонгов крипты) ═══
def btc_bias_compute(h1: list) -> dict | None:
    """btcBiasCompute(): пробой/ЛП вчерашнего и 5-дневного хая/лоя BTC на H1; состояние держится 12 ч.
    h1 — закрытые часовики BTC (Bar). Возвращает {ts: -1/0/1}."""
    if not h1 or len(h1) < 200:
        return None
    D = 86_400_000
    days: dict[int, list] = {}
    for b in h1:
        k = (b.ts // D) * D
        x = days.get(k)
        if x is None:
            days[k] = [b.h, b.l]
        else:
            x[0] = max(x[0], b.h)
            x[1] = min(x[1], b.l)
    dk = sorted(days)
    lv = {}
    for i in range(1, len(dk)):
        p = days[dk[i - 1]]
        h5 = l5 = None
        if i >= 5:
            h5 = max(days[dk[j]][0] for j in range(i - 5, i))
            l5 = min(days[dk[j]][1] for j in range(i - 5, i))
        lv[dk[i]] = (p[0], p[1], h5, l5)
    out = {}
    cur, age = 0, 999
    for i in range(1, len(h1)):
        b, pc = h1[i], h1[i - 1].c
        L = lv.get((b.ts // D) * D)
        sg = 0
        if L:
            for H, Lo in ((L[0], L[1]), (L[2], L[3])):
                if H is None:
                    continue
                if b.c > H and pc <= H:
                    sg += 1
                if b.h > H and b.c < H and pc < H:
                    sg -= 1
                if b.c < Lo and pc >= Lo:
                    sg -= 1
                if b.l < Lo and b.c > Lo and pc > Lo:
                    sg += 1
        if sg != 0:
            cur, age = (1 if sg > 0 else -1), 0
        else:
            age += 1
        out[b.ts] = cur if age < 12 else 0
    return out


def cbtc_tag(cards: list[dict], all_closes: dict) -> None:
    """cbtcTag(): корреляция часовых лог-доходностей монеты и BTC за 168 ч до свечи сигнала (ЛП-лонги)."""
    B = all_closes.get("BTC")
    if not B or len(B) < 200:
        return
    bi = {ts: i for i, (ts, _c) in enumerate(B)}
    cache: dict = {}
    for card in cards:
        s = card.get("feat") or {}
        if card.get("market", "crypto") != "crypto" or card.get("strategy", "fbo") != "fbo" or s.get("d") != 1:
            continue
        t = card.get("ticker") or ""
        base = t[:-4] if t.endswith("USDT") else (t.split("-")[0] if "-" in t else t)
        C = all_closes.get(base)
        if not C or len(C) < 200:
            continue
        if base not in cache:
            cache[base] = {ts: i for i, (ts, _c) in enumerate(C)}
        ci, j = cache[base].get(card["signal_ts"]), bi.get(card["signal_ts"])
        if ci is None or j is None:
            continue
        xs, ys = [], []
        for k in range(168):
            if ci - k < 0 or ci - k - 1 < 0:
                break
            a, a0 = C[ci - k], C[ci - k - 1]
            jb, jb0 = bi.get(a[0]), bi.get(a0[0])
            if jb is None or jb0 is None:
                continue
            xs.append(math.log(a[1] / a0[1]))
            ys.append(math.log(B[jb][1] / B[jb0][1]))
        if len(xs) < 134:
            continue
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxy = sxx = syy = 0.0
        for k in range(n):
            dx, dy = xs[k] - mx, ys[k] - my
            sxy += dx * dy
            sxx += dx * dx
            syy += dy * dy
        s["cbtc"] = sxy / math.sqrt(sxx * syy) if (sxx > 0 and syy > 0) else None


def btc_tag(cards: list[dict], bias: dict | None) -> None:
    """mkTag(): btcBias по времени сигнала (крипта)."""
    if not bias:
        return
    for card in cards:
        if card.get("market", "crypto") != "crypto":
            continue
        v = bias.get(card["signal_ts"])
        if v is not None:
            card.setdefault("feat", {})["btcBias"] = v


# ═══ HTML 7.37/7.38: дневные шорты Мосбиржи (режим «Свинг»), ruD1Signals ═══════════════
RU_SW_D1STR = 4
RU_SW_IX20 = 0.04
RU_HOLD_BARS = 200   # ruHoldBars(): свечей вперёд у сигнала MOEX — с запасом под свинг 7 суток


def ru_d1_signals(ticker: str, d1c: list, h1: list, h1c: list, from_idx: int, rc: dict | None) -> list[dict]:
    """1:1 ruD1Signals(): закрытие дня k под уровнем D1 силой ≥4 (вчера — над ним), IMOEX 7/7 вниз и не ниже
    SMA20 больше 4%. Вход — открытие первого часа следующего торгового дня; t = этот час − 1 ч; стоп 1%."""
    import bisect
    rc = rc or {}
    P, ixd = rc.get("ixP"), rc.get("imoexD1")
    out: list[dict] = []
    if not P or not ixd or len(d1c) < 50 or not h1c:
        return out
    t_from = h1c[min(from_idx, len(h1c) - 1)].ts
    h1_ts = [b.ts for b in h1]
    h1c_ts = [b.ts for b in h1c]
    for k in range(45, len(d1c)):
        today, prev = d1c[k], d1c[k - 1]
        j = bisect.bisect_left(h1_ts, today.ts + 86_400_000)
        if j >= len(h1):
            continue
        eb = h1[j]
        if eb.ts - today.ts > 5 * 86_400_000:
            continue
        t_sig = eb.ts - 3_600_000
        if t_sig < t_from:
            continue
        cut = d1c[: k + 1]
        a_d = atr(cut)
        if not (a_d > 0):
            continue
        best = None
        for L in build_levels(cut, []):
            if _side_of(L.kind) != "short":
                continue
            if not (today.c < L.price and prev.c >= L.price):
                continue
            if not (L.strength >= RU_SW_D1STR):
                continue
            ov = (L.price - today.c) / a_d
            if best is None or L.strength > best[0].strength or (L.strength == best[0].strength and ov < best[1]):
                best = (L, ov)
        if not best:
            continue
        st = ru_ix_state(P, t_sig, -1)
        if not st or st.get("ru_ixS") != 7:
            continue
        ic = [b for b in ixd if b.ts <= today.ts]
        if len(ic) < 20:
            continue
        c0 = ic[-1].c
        sma20 = sum(b.c for b in ic[-20:]) / 20
        ix20 = 1 - c0 / sma20
        if ix20 >= RU_SW_IX20:
            continue
        jc = bisect.bisect_left(h1c_ts, eb.ts)
        vw = sorted(v for v in ((b.vol_quote or 0) for b in h1c[max(0, jc - 24):jc]) if v > 0)
        v_med = vw[len(vw) // 2] if vw else 0
        L, ov = best
        k_lbl = "D1-закрытие · " + L.kind
        feat = {
            "type": "brk", "sw": 2, "t": t_sig, "d": 0, "side": "short", "lv": L.price, "k": k_lbl, "kind": L.kind,
            "e": eb.o, "atrD": a_d, "strength": L.strength, "crosses": 0, "touched8": 0, "acc": 0,
            "level_age_h": (t_sig - L.formed_ts) / 3_600_000, "vol_contraction": 1, "vol_mult": 1,
            "overshoot_atr": ov, "dist_atr": 0, "bias_ok": 1, "atrD_pct": a_d / eb.o, "aH1_pct": 0,
            "v6_fresh": 1, "v6_poke": 0, "v6_covers": 1, "v6_preAcc": 0, "v6_smooth": 0, "v6_d1Against": 0,
            "free_zone_h": 0, "wall_r": 99, "day_pos": 0, "body_ratio": 1, "wick_ratio": 0,
            "ru_ixS": st.get("ru_ixS"), "ru_ixExt": st.get("ru_ixExt"), "ru_ix20": ix20, "ru_d1close": today.c,
            "brk_bars": 1, "vol_ret_brk": 1, "dow": (time.gmtime(eb.ts / 1000).tm_wday + 1) % 7, "ema_ok": 1,
            "approach_atr": 0, "atr_ratio": 0, "lvl_density": 0, "round_dist": 0, "green_ratio": 0, "d1_streak": 0,
            "turn24": 0, "turnD": turn_day_at(h1c, jc - 1) if jc > 0 else 0, "p": 0,
            "vol5mAvg": v_med / 12, "posMax": (v_med / 12) * 0.10, "grouped": 1, "mkt": "ru", "_sc": 5,
        }
        out.append({
            "ticker": ticker, "market": "ru", "exchange": "moex", "sw": 2,
            "score": 5, "fwd": [(b.ts, b.o, b.h, b.l, b.c) for b in h1c[jc: jc + RU_HOLD_BARS]],
            "_ord": (jc, 0), "feat": feat, "strategy": "brk", "side": "SHORT", "status": "SIGNAL",
            "level": L.price, "kind": k_lbl, "strength": L.strength, "last": eb.o, "signal_ts": t_sig,
            "dist_atr": 0.0, "atr_d": a_d, "atr_h1": 0.0, "stop": eb.o * 1.01, "take": eb.o * 0.97, "risk": eb.o * 0.01,
            "d1_bias": "flat", "crosses": 0, "prob": None, "level_age_h": feat["level_age_h"], "vol_mult": 1.0,
            "why": ["дневной шорт (Свинг): закрытие дня под уровнем " + L.kind, "IMOEX 7/7 вниз"],
        })
    return out
