"""algo.py — рынок «Крипта (Алго)» (MKT='algo' в HTML): 1:1 порт runAlgoScan()/algoLoad()/
computeIndicators()/passesOneInd()/algoSim()/algoRun() из нового HTML-скринера.

Каждый закрытый H1-бар топ-N монет (Bybit, фолбэк OKX), где выполнены выбранные в шаблоне
индикаторные условия → сигнал. На одной монете новый сигнал только после закрытия
предыдущей сделки (open → монета занята до конца окна). Лимиты «монета 12ч / в час /
недельный» к алго НЕ применяются (applyAll в HTML для MKT==='algo' возвращает все строки).
Стоп = max(1.5·ATR14(H1), 2% цены), тейк 3R, безубыток после +1.5R, удержание 168ч.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

logger = logging.getLogger(__name__)

IND_OPTIONS = [
    ("ema50", "EMA 50", ["any", "above", "below"]),
    ("ema100", "EMA 100", ["any", "above", "below"]),
    ("ema200", "EMA 200", ["any", "above", "below"]),
    ("rsi", "RSI(20)", ["any", "oversold", "neutral", "overbought"]),
    ("macd", "MACD", ["any", "bull", "bear"]),
    ("st", "SuperTrend", ["any", "bull", "bear"]),
    ("bb", "Bollinger", ["any", "above", "inside", "below"]),
    ("kc", "Keltner", ["any", "above", "inside", "below"]),
    ("stoch", "Stoch(14)", ["any", "oversold", "neutral", "overbought"]),
    ("adx", "ADX", ["any", "strong", "weak"]),
    ("di", "DI+/DI−", ["any", "diplus", "diminus"]),
    ("vol", "Объём vs SMA20", ["any", "above", "below"]),
    ("slope", "Наклон EMA20", ["any", "up", "down"]),
]
TARGET_R = 3.0
BE_TRIGGER_R = 1.5
HOLD_CRYPTO = 168
MAXRISK = 0.20
MIN_SCAN_H = 24          # runAlgoScan: hours = max(24, windowHours())
INF = float("inf")


def _jsum(xs):
    s = 0.0
    for x in xs:
        s += x
    return s


def compute_indicators(h1cut: list, bar) -> dict | None:
    closes = [b.c for b in h1cut] + [bar.c]
    n = len(closes)
    if n < 30:
        return None

    def ema_of(p):
        if n < p:
            return None
        k = 2 / (p + 1)
        e = _jsum(closes[:p]) / p
        for i in range(p, n):
            e = closes[i] * k + e * (1 - k)
        return e

    ema50, ema100, ema200, c = ema_of(50), ema_of(100), ema_of(200), bar.c
    rsi = None
    if n >= 22:
        sl = closes[-21:]
        g = l = 0.0
        for i in range(1, len(sl)):
            d = sl[i] - sl[i - 1]
            if d > 0:
                g += d
            else:
                l -= d
        ag, al = g / 20, l / 20
        rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    macd = None
    if n >= 26:
        k12, k26 = 2 / 13, 2 / 27
        e12 = _jsum(closes[:12]) / 12
        e26 = _jsum(closes[:26]) / 26
        for i in range(12, n):
            e12 = closes[i] * k12 + e12 * (1 - k12)
        for i in range(26, n):
            e26 = closes[i] * k26 + e26 * (1 - k26)
        macd = e12 - e26
    st = None
    if len(h1cut) >= 14:
        sl = h1cut[-7:]
        atr7 = _jsum(b.h - b.l for b in sl) / 7
        lb = h1cut[-1]
        st = 1 if c > (lb.h + lb.l) / 2 - 3 * atr7 else 0
    bb = None
    if n >= 20:
        sl = closes[-20:]
        sma = _jsum(sl) / 20
        std = math.sqrt(_jsum((x - sma) ** 2 for x in sl) / 20)
        u, lo = sma + 2 * std, sma - 2 * std
        if u != lo:
            bb = (c - lo) / (u - lo)
    kc = None
    if n >= 20 and len(h1cut) >= 10:
        e20 = ema_of(20)
        atr10 = _jsum(b.h - b.l for b in h1cut[-10:]) / 10
        u, lo = e20 + 2 * atr10, e20 - 2 * atr10
        if e20 and u != lo:
            kc = (c - lo) / (u - lo)
    stoch = None
    if len(h1cut) >= 14:
        sl = h1cut[-14:]
        hh, ll = max(b.h for b in sl), min(b.l for b in sl)
        stoch = 50.0 if hh == ll else (c - ll) / (hh - ll) * 100
    adx = di = None
    if len(h1cut) >= 15:
        sl = h1cut[-15:]
        tr = dmp = dmm = 0.0
        for i in range(1, len(sl)):
            b, p = sl[i], sl[i - 1]
            tr += max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c))
            up, dn = b.h - p.h, p.l - b.l
            if up > dn and up > 0:
                dmp += up
            if dn > up and dn > 0:
                dmm += dn
        dip = dmp / tr * 100 if tr > 0 else 0
        dim = dmm / tr * 100 if tr > 0 else 0
        adx = abs(dip - dim) / (dip + dim) * 100 if dip + dim > 0 else 0
        di = 1 if dip > dim else 0
    vol = None
    if len(h1cut) >= 20:
        sma_v = _jsum(b.vol_quote for b in h1cut[-20:]) / 20
        vol = 1 if bar.vol_quote > sma_v else 0
    slope = None
    if n >= 22:
        e20n = ema_of(20)
        cl2 = closes[:-2]
        n2 = len(cl2)
        if n2 >= 20:
            k = 2 / 21
            e2 = _jsum(cl2[:20]) / 20
            for i in range(20, n2):
                e2 = cl2[i] * k + e2 * (1 - k)
            if e20n is not None:
                slope = 1 if e20n > e2 else -1
    sgn = lambda e: None if e is None else (1 if c > e else -1)
    return {"ind_ema50": sgn(ema50), "ind_ema100": sgn(ema100), "ind_ema200": sgn(ema200),
            "ind_rsi": rsi, "ind_macd": macd, "ind_st": st, "ind_bb": bb, "ind_kc": kc,
            "ind_stoch": stoch, "ind_adx": adx, "ind_diBull": di, "ind_volAbove": vol,
            "ind_emaSlope": slope}


def passes_one_ind(s: dict, key: str, opt: str) -> bool:
    if opt == "any":
        return True
    g = s.get
    if key in ("ema50", "ema100", "ema200"):
        v = g("ind_" + key)
        return (v > 0 if opt == "above" else v < 0) if v is not None else True
    if key == "rsi":
        v = g("ind_rsi")
        if v is None:
            return True
        return v < 35 if opt == "oversold" else v > 65 if opt == "overbought" else 35 <= v <= 65
    if key == "macd":
        v = g("ind_macd")
        if v is None:
            return True
        return v > 0 if opt == "bull" else v < 0
    if key == "st":
        v = g("ind_st")
        return (v == 1 if opt == "bull" else v == 0) if v is not None else True
    if key in ("bb", "kc"):
        v = g("ind_" + key)
        if v is None:
            return True
        return v > 1 if opt == "above" else v < 0 if opt == "below" else 0 <= v <= 1
    if key == "stoch":
        v = g("ind_stoch")
        if v is None:
            return True
        return v < 25 if opt == "oversold" else v > 75 if opt == "overbought" else 25 <= v <= 75
    if key == "adx":
        v = g("ind_adx")
        return (v > 22 if opt == "strong" else v < 20) if v is not None else True
    if key == "di":
        v = g("ind_diBull")
        return (v == 1 if opt == "diplus" else v == 0) if v is not None else True
    if key == "vol":
        v = g("ind_volAbove")
        return (v == 1 if opt == "above" else v == 0) if v is not None else True
    if key == "slope":
        v = g("ind_emaSlope")
        return (v == 1 if opt == "up" else v == -1) if v is not None else True
    return True


def algo_sim(h1: list, i: int, d: int, risk: float) -> dict:
    e = h1[i].c
    long_ = d == 1
    stop = e - risk if long_ else e + risk
    take = e + TARGET_R * risk if long_ else e - TARGET_R * risk
    be = e + BE_TRIGGER_R * risk if long_ else e - BE_TRIGGER_R * risk
    cur, moved, o, xt, last_c, last_ts = stop, False, "open", h1[i].ts, e, h1[i].ts
    for j in range(i + 1, len(h1)):
        b = h1[j]
        if (b.ts - h1[i].ts) / 3_600_000 > HOLD_CRYPTO:
            o, xt = "expired", last_ts
            break
        xt = b.ts
        hs = b.l <= cur if long_ else b.h >= cur
        ht = b.h >= take if long_ else b.l <= take
        if hs:
            o = "be" if moved else "stop"
            break
        if ht:
            o = "take"
            break
        if not moved and (b.h >= be if long_ else b.l <= be):
            cur, moved = e, True
        last_c, last_ts = b.c, b.ts
    r = (TARGET_R if o == "take" else -1 if o == "stop" else 0 if o == "be"
         else ((last_c - e if long_ else e - last_c) / risk) if o == "expired" else 0)
    return {"o": o, "r": r, "xt": xt, "st": stop, "tk": take}


def build_rows(coins: list[dict], hours: int, now_ms: int) -> list[dict]:
    """algoLoad(): строки по каждому закрытому бару окна (coins уже в порядке оборота)."""
    frm = now_ms - hours * 3_600_000
    rows = []
    for ci, cn in enumerate(coins):
        h1 = cn["h1"]
        for i in range(210, len(h1)):
            bar = h1[i]
            if bar.ts < frm:
                continue
            ind = compute_indicators(h1[max(0, i - 300):i], bar)
            if not ind:
                continue
            tr = 0.0
            for j in range(i - 13, i + 1):
                tr += h1[j].h - h1[j].l
            atr = tr / 14
            risk = max(1.5 * atr, 0.02 * bar.c)
            if risk / bar.c > MAXRISK:
                continue
            L, S = algo_sim(h1, i, 1, risk), algo_sim(h1, i, 0, risk)
            rows.append({"c": ci, "i": i, "t": bar.ts, "e": bar.c, "risk": risk, "atr": atr, "ind": ind,
                         "o": [S["o"], L["o"]], "r": [S["r"], L["r"]], "xt": [S["xt"], L["xt"]],
                         "st": [S["st"], L["st"]], "tkp": [S["tk"], L["tk"]]})
    rows.sort(key=lambda r: (r["t"], r["c"]))
    return rows


def template_combo(filters: dict) -> tuple[list[tuple[str, str]], list[int]]:
    """algoPanelCombo() из сохранённого алго-шаблона {ind_<key>: opt, ind_side}."""
    f = filters or {}
    pairs = []
    for key, _lbl, opts in IND_OPTIONS:
        v = f.get("ind_" + key, "any")
        if v in opts and v != "any":
            pairs.append((key, v))
    sv = f.get("ind_side", "any")
    sides = [1] if sv == "long" else [0] if sv == "short" else [0, 1]
    return pairs, sides


_OPT_RU = {"above": "цена выше", "below": "цена ниже", "oversold": "перепродан", "neutral": "нейтр.",
           "overbought": "перекуплен", "bull": "бычий", "bear": "медвежий", "inside": "внутри",
           "strong": "сильный", "weak": "слабый", "diplus": "DI+ выше", "diminus": "DI− выше",
           "up": "вверх", "down": "вниз"}


def combo_label(pairs, side) -> str:
    lbl = {k: l for k, l, _ in IND_OPTIONS}

    def ru(v, key):
        if key in ("bb", "kc"):
            return {"above": "выше верхней", "below": "ниже нижней", "inside": "внутри канала"}.get(v, v)
        if key == "vol":
            return {"above": "выше среднего", "below": "ниже среднего"}.get(v, v)
        if key in ("rsi", "stoch"):
            return {"oversold": "перепродан", "neutral": "нейтральный", "overbought": "перекуплен"}.get(v, v)
        return _OPT_RU.get(v, v)
    s = ", ".join(f"{lbl[k]}: {ru(v, k)}" for k, v in pairs)
    return ("ЛОНГ · " if side == 1 else "ШОРТ · " if side == 0 else "") + (s or "без условий")


def select_signals(rows: list[dict], coins: list[dict], pairs, sides) -> list[dict]:
    """runAlgoScan(): маска комбинации → algoRun(slots=∞) по каждой стороне → algoMaterialize."""
    out = []
    if not pairs:
        return out
    match = [all(passes_one_ind(r["ind"], k, v) for k, v in pairs) for r in rows]
    for side in sides:
        busy: dict[int, float] = {}
        lbl = combo_label(pairs, side)
        for idx, r in enumerate(rows):
            if not match[idx]:
                continue
            c, t = r["c"], r["t"]
            if c in busy and busy[c] > t:
                continue
            busy[c] = INF if r["o"][side] == "open" else r["xt"][side]
            cn = coins[c]
            out.append({"type": "algo", "base": cn["base"], "sym": cn["sym"], "t": t, "d": side,
                        "e": r["e"], "st": r["st"][side], "tk": r["tkp"][side], "o": r["o"][side],
                        "xt": r["xt"][side], "rp": r["risk"] / r["e"], "k": lbl, "atr": r["atr"]})
    out.sort(key=lambda s: -s["t"])
    return out


def to_card(s: dict, tpl_name: str = "") -> dict:
    """Сигнал алго → карточка бота (тот же формат, что у crypto/ru)."""
    long_ = s["d"] == 1
    risk = abs(s["e"] - s["st"])
    return {
        "ticker": s["sym"], "market": "algo", "exchange": s.get("exchange", "bybit"),
        "strategy": "algo", "side": "LONG" if long_ else "SHORT", "status": "SIGNAL",
        "level": s["e"], "kind": "algo", "strength": 0, "last": s["e"], "signal_ts": s["t"],
        "stop": s["st"], "take": s["tk"], "risk": risk, "rp": s["rp"], "score": 0.0,
        "prob": None, "atr_h1": s.get("atr"), "atr_d": 0.0, "dist_atr": 0.0, "crosses": 0,
        "vol_mult": 0.0, "level_age_h": 0.0, "d1_bias": "flat", "o": s["o"],
        "algo_combo": s["k"], "matched_template": tpl_name,
        "why": [f"алго: {s['k']}"],
    }


async def load_coins(client, *, top_n: int, min_vol: float, hours: int, source: str | None = None,
                     progress=None) -> tuple[list[dict], str]:
    """algoLoad(): тикеры (Bybit → OKX), оборот ≥ minVol, топ-N, H1 hours+320, ≥230 закрытых."""
    import screener as S
    tks = await S.fetch_tickers(client, source=source, min_vol=None, top_n=1_000_000)
    uni = [t for t in tks if float(t.get("turnover24h") or 0) >= min_vol]
    uni.sort(key=lambda t: -float(t.get("turnover24h") or 0))
    uni = uni[:max(1, int(top_n))]
    need = hours + 320
    got: dict[str, dict] = {}

    async def one(t):
        sym = t["symbol"]
        try:
            h = await S.fetch_klines(client, sym, "60", limit=need, inst_id=t.get("inst_id") or sym,
                                     html_pages=True)
            h = [b for b in h if b.confirmed]
            if len(h) >= 230:
                got[sym] = {"sym": t.get("inst_id") or sym, "base": S._card_base({"ticker": sym}),
                            "ticker": sym, "h1": h}
        except Exception as e:
            logger.debug("algo kline %s: %s", sym, e)

    for i in range(0, len(uni), 6):
        await asyncio.gather(*[one(t) for t in uni[i:i + 6]])
        if progress:
            await progress(min(i + 6, len(uni)), len(uni))
    coins = [got[t["symbol"]] for t in uni if t["symbol"] in got]
    return coins, ("OKX" if S._active_exchange == "okx" else "Bybit")


async def prepare(client, *, top_n: int, min_vol: float, win_h: int, source: str | None = None,
                  progress=None) -> dict:
    """Один раз на скан: монеты + строки индикаторов. Окно = max(24, win_h) — как runAlgoScan."""
    hours = max(MIN_SCAN_H, int(win_h))
    coins, ex = await load_coins(client, top_n=top_n, min_vol=min_vol, hours=hours, source=source,
                                 progress=progress)
    rows = build_rows(coins, hours, int(time.time() * 1000))
    logger.info("Скан algo: %d монет (%s), окно %dч, строк %d", len(coins), ex, hours, len(rows))
    return {"coins": coins, "rows": rows, "exchange": ex, "hours": hours}


def cards_for(ctx: dict | None, filters: dict, tpl_name: str = "") -> list[dict]:
    """Сигналы одного алго-шаблона (пустой набор индикаторов → нет скана, как в HTML)."""
    if not ctx:
        return []
    pairs, sides = template_combo(filters)
    if not pairs:
        return []
    out = []
    coins = ctx["coins"]
    for s in select_signals(ctx["rows"], coins, pairs, sides):
        c = to_card(s, tpl_name)
        c["ticker"] = coins_ticker(coins, s["base"])
        c["exchange"] = ctx["exchange"].lower()
        out.append(c)
    return out


def coins_ticker(coins, base):
    for c in coins:
        if c["base"] == base:
            return c["ticker"]
    return base
