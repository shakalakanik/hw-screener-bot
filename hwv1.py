#!/usr/bin/env python3
"""HWv1.0 — отбор пробоя high/low дневки после сжатия.

Заморожено 10.09.2026. Эталон: ZRO, APE, BIO, GIGGLE.
Импорт: from hwv1 import Bar, evaluate
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

VERSION = "HWv1.0"

PARAMS = {
    "atr_n": 14,
    "d1_min": 30,
    "h4_min": 40,
    "h1_min": 40,
    "work_days": 26,
    "swing_lr": 2,
    "universe_n": 200,
    "vol_usd_min": 1_000_000.0,
    "spread_max": 0.0015,
    "merge_atr": 0.06,
    "merge_pct": 0.001,
    "zone_atr": 0.03,
    "zone_pct": 0.0005,
    "dist_atr": 0.50,
    "acc_hours": (8, 10, 12, 16),
    "acc_range_atr": 0.50,
    "acc_close_atr": 0.35,
    "acc_drift_atr": 0.32,
    "acc_mid_atr": 0.60,
    "max_crosses": 2,
    "impulse_d1_atr": 2.2,
    "strength_min": 3,
}


@dataclass
class Bar:
    ts: int
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
    notes: list[str] = field(default_factory=list)


def atr(bars: list[Bar], n: int = PARAMS["atr_n"]) -> float:
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        prev, b = bars[i - 1].c, bars[i]
        trs.append(max(b.h - b.l, abs(b.h - prev), abs(b.l - prev)))
    window = trs[-min(n, len(trs)) :]
    return sum(window) / len(window) if window else 0.0


def last_closed(bars: list[Bar]) -> list[Bar]:
    if bars and not bars[-1].confirmed:
        return bars[:-1]
    return bars


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
        if all(h >= bars[i - k].h for k in range(1, left + 1)) and all(
            h >= bars[i + k].h for k in range(1, right + 1)
        ):
            highs.append((i, h))
        if all(l <= bars[i - k].l for k in range(1, left + 1)) and all(
            l <= bars[i + k].l for k in range(1, right + 1)
        ):
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


def sawed(d1, h4, h1, m5, price: float, formed_ts: int) -> bool:
    cap = PARAMS["max_crosses"]
    return any(
        count_close_crosses(seq, price, formed_ts) > cap
        for seq in (d1, h4, h1, m5 or [])
    )


def accumulation(h1: list[Bar], level: float, lo: float, hi: float, atr_d: float) -> tuple[bool, str]:
    if atr_d <= 0 or len(h1) < 8:
        return False, "мало H1"
    best = None
    for n in PARAMS["acc_hours"]:
        if len(h1) < n:
            break
        win = h1[-n:]
        width = max(b.h for b in win) - min(b.l for b in win)
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
        mid = (max(b.h for b in win) + min(b.l for b in win)) / 2
        touched = min(b.l for b in win) <= level <= max(b.h for b in win)
        if abs(mid - level) > PARAMS["acc_mid_atr"] * atr_d and not touched:
            if n == 8:
                return False, "сжатие не у уровня"
            break
        best = n
    if not best:
        return False, "нет сжатия ≥8ч"
    return True, f"{best}ч сжатие у уровня"


def build_levels(d1: list[Bar], h4: list[Bar]) -> list[Level]:
    if len(d1) < 12:
        return []
    atr_d = atr(d1)
    last = d1[-1].c
    work = d1[-PARAMS["work_days"] :] if len(d1) >= PARAMS["work_days"] else d1
    raw: list[Level] = []

    def add(price: float, kind: str, bar: Bar, touches: int = 1):
        if price <= 0:
            return
        pad = max(PARAMS["zone_atr"] * atr_d, price * PARAMS["zone_pct"])
        raw.append(
            Level(
                price=price,
                kind=kind,
                formed_ts=bar.ts,
                lo=price - pad,
                hi=price + pad,
                touches=touches,
                chopped=d1_chopped(d1, price - pad, price + pad),
                impulse_mid=inside_fresh_impulse(d1, h4, price, atr_d) if h4 else False,
                from_prev_day=kind.startswith("prev"),
            )
        )

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

    raw.sort(key=lambda x: x.price)
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


def _side(kind: str) -> str:
    return "long" if "high" in kind else "short"


def _m5_wick_saw(m5: list[Bar], price: float, formed_ts: int) -> bool:
    """Пила фитилями на 5М после бара уровня."""
    through = 0
    sides = 0
    prev = None
    for b in m5:
        if b.ts <= formed_ts:
            continue
        if b.h >= price and b.l <= price:
            through += 1
        side = 1 if b.c > price else -1 if b.c < price else prev
        if prev is not None and side is not None and side != prev:
            sides += 1
        if side is not None:
            prev = side
    return through >= 5 or sides > 2


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
) -> dict:
    """Вернуть 0..N идей HWv1.0 по одному инструменту."""
    d1, h4, h1 = last_closed(d1), last_closed(h4), last_closed(h1)
    m5 = last_closed(m5 or [])
    out = {
        "version": VERSION,
        "ticker": ticker,
        "ok": False,
        "cards": [],
        "levels": [],
        "reason": "",
    }
    if vol_usd_24h and vol_usd_24h < PARAMS["vol_usd_min"]:
        out["reason"] = "оборот < $1M"
        return out
    if len(d1) < PARAMS["d1_min"] or len(h4) < PARAMS["h4_min"] or len(h1) < PARAMS["h1_min"] or last <= 0:
        out["reason"] = "мало баров"
        return out
    atr_d, atr_h4 = atr(d1), atr(h4)
    if atr_d <= 0:
        out["reason"] = "ATR=0"
        return out
    spread = (ask - bid) / last if ask and bid else 0.0
    if ask and bid and spread > PARAMS["spread_max"]:
        out["reason"] = "широкий спред"
        return out

    levels = build_levels(d1, h4)
    bias = d1_bias(d1)
    out["ok"] = True
    out["last"] = last
    out["atr_d"] = atr_d
    out["d1_bias"] = bias
    out["levels"] = [
        {"price": lv.price, "kind": lv.kind, "strength": lv.strength, "formed_ts": lv.formed_ts}
        for lv in levels
    ]

    cards = []
    for lv in levels:
        if abs(last - lv.price) > PARAMS["dist_atr"] * atr_d:
            continue
        side = _side(lv.kind)
        # WATCH только до пробоя: цена ещё не за уровнем
        if side == "long" and last > lv.price:
            continue
        if side == "short" and last < lv.price:
            continue
        if side == "long" and (d1[-1].c > lv.price or (h1 and h1[-1].c > lv.price)):
            continue
        if side == "short" and (d1[-1].c < lv.price or (h1 and h1[-1].c < lv.price)):
            continue
        if bias == "up" and side == "short":
            continue
        if bias == "down" and side == "long":
            continue
        if sawed(d1, h4, h1, m5, lv.price, lv.formed_ts):
            continue
        # доп. запил на 5М: много фитилей сквозь линию после бара уровня
        if m5 and _m5_wick_saw(m5, lv.price, lv.formed_ts):
            continue
        acc, acc_why = accumulation(h1, lv.price, lv.lo, lv.hi, atr_d)
        if not acc:
            continue
        pad = max((lv.hi - lv.lo) * 0.20, 0.10 * atr_h4, last * 0.001)
        if side == "long":
            stop = lv.lo - pad
            risk = max(last - stop, 1e-12)
            take = last + 3 * risk
        else:
            stop = lv.hi + pad
            risk = max(stop - last, 1e-12)
            take = last - 3 * risk
        cards.append(
            {
                "ticker": ticker,
                "version": VERSION,
                "side": "LONG" if side == "long" else "SHORT",
                "status": "WATCH",
                "level": lv.price,
                "kind": lv.kind,
                "strength": lv.strength,
                "last": last,
                "dist_atr": abs(last - lv.price) / atr_d,
                "atr_d": atr_d,
                "stop": stop,
                "take": take,
                "d1_bias": bias,
                "why": [acc_why, "high/low дневки", "≤0.5 ATR от уровня"],
            }
        )
    cards.sort(key=lambda c: (c["dist_atr"], -c["strength"]))
    out["cards"] = cards[:1]
    return out


if __name__ == "__main__":
    print(VERSION, "params", PARAMS)
