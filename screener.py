"""screener.py — тянет данные с Bybit/OKX (auto) и MOEX ISS (ru), прогоняет hwv1.evaluate."""
import asyncio
import logging
import os
import time
from typing import Callable, Awaitable

import httpx

from hwv1 import Bar, evaluate
import storage
import algo as algo_mod
from moex import (
    fetch_imoex_ctx,
    fetch_tickers_moex,
    fetch_klines_moex_h1,
    moex_client,
    scan_one_moex,
    MOEX_MIN_TURN,
)

logger = logging.getLogger(__name__)

BYBIT_BASE = "https://api.bybit.com"
OKX_BASE = "https://www.okx.com"

# auto (default) | bybit | okx — как HTML select#src
DATA_SOURCE = (os.getenv("DATA_SOURCE") or "auto").strip().lower()
if DATA_SOURCE not in ("auto", "bybit", "okx"):
    DATA_SOURCE = "auto"

INTERVAL_MAP = {"D": "D", "H4": "240", "H1": "60", "M5": "5"}
# Bybit interval → OKX bar (1Dutc как HTML OKX.kline для суток; 4H/1H/5m)
OKX_BAR_MAP = {"D": "1Dutc", "240": "4H", "60": "1H", "5": "5m"}
LIMIT = 200  # баров за запрос

TOP_N = 300           # инструментов по обороту за сутки
MIN_VOL_USD_24H = 1_000_000.0   # жёсткий пол — меньше не берём вообще

# HTML post-filters: applyCoinLimit (12h) + capCluster (default 3)
COIN_WINDOW_H = 12
CAP_CLUSTER = 3

# Жёсткий потолок возраста сигнала для любой отправки в Telegram.
MAX_SIGNAL_AGE_H = 12
MAX_SIGNAL_AGE_MS = MAX_SIGNAL_AGE_H * 3_600_000
# Lookback детекции = max age (не ищем то, что всё равно не отправим).
SCAN_LOOKBACK_H = MAX_SIGNAL_AGE_H
# Cold-start watermark: не дампить историю — только последний час.
COLD_WATERMARK_LOOKBACK_MS = 3_600_000

# Активная биржа текущего скана: "bybit" | "okx"
_active_exchange = "bybit"


class BybitBlocked(Exception):
    """Bybit недоступен (403 / CloudFront geo-block)."""


async def _http_get_retry(client: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
    """GET с повтором на сетевых сбоях/таймаутах. HTML (fetch без таймаута) почти никогда не теряет
    инструмент из-за сети; без повтора бот мог молча выкинуть монету из скана → расхождение с HTML."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            r = await client.get(url, params=params, timeout=20)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return r
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            await asyncio.sleep(0.5 * (attempt + 1))
    raise last_exc or RuntimeError(f"GET failed: {url}")


async def _get_bybit(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    r = await _http_get_retry(client, f"{BYBIT_BASE}{path}", params)
    body = r.text or ""
    if r.status_code == 403 or (
        "CloudFront" in body and "block" in body.lower()
    ):
        raise BybitBlocked(f"Bybit HTTP {r.status_code}: geo/CloudFront block")
    r.raise_for_status()
    return r.json()


async def _get_okx(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    r = await _http_get_retry(client, f"{OKX_BASE}{path}", params)
    r.raise_for_status()
    data = r.json()
    if str(data.get("code", "")) not in ("0", "0.0"):
        raise RuntimeError(f"OKX {path}: {data.get('msg') or data.get('code')}")
    return data


def _okx_inst_to_symbol(inst_id: str) -> str:
    """BTC-USDT-SWAP → BTCUSDT (как Bybit-стиль для карточек/дедупа)."""
    # HTML: base = instId.split('-')[0]; API sym остаётся instId
    parts = inst_id.split("-")
    if len(parts) >= 2 and parts[1] == "USDT":
        return f"{parts[0]}USDT"
    return inst_id.replace("-SWAP", "").replace("-", "")


async def fetch_tickers_bybit(
    client: httpx.AsyncClient,
    min_vol: float | None = None,
    top_n: int | None = None,
) -> list[dict]:
    """Топ-N USDT-perp Bybit по обороту за сутки."""
    floor = MIN_VOL_USD_24H if min_vol is None else float(min_vol)
    n = TOP_N if top_n is None else max(1, int(top_n))
    data = await _get_bybit(client, "/v5/market/tickers", {"category": "linear"})
    items = data.get("result", {}).get("list", [])
    usdt = [
        {
            "symbol": t["symbol"],
            "inst_id": t["symbol"],
            "lastPrice": t.get("lastPrice", 0),
            "bid1Price": t.get("bid1Price", 0),
            "ask1Price": t.get("ask1Price", 0),
            "turnover24h": float(t.get("turnover24h", 0) or 0),
            "exchange": "bybit",
        }
        for t in items
        if t["symbol"].endswith("USDT")
        and float(t.get("turnover24h", 0) or 0) >= floor
    ]
    usdt.sort(key=lambda t: t["turnover24h"], reverse=True)
    return usdt[:n]


async def fetch_tickers_okx(
    client: httpx.AsyncClient,
    min_vol: float | None = None,
    top_n: int | None = None,
) -> list[dict]:
    """Топ-N USDT-SWAP OKX. Оборот: volCcy24h * last (как HTML OKX.tickers)."""
    floor = MIN_VOL_USD_24H if min_vol is None else float(min_vol)
    n = TOP_N if top_n is None else max(1, int(top_n))
    data = await _get_okx(
        client, "/api/v5/market/tickers", {"instType": "SWAP"}
    )
    items = data.get("data") or []
    out = []
    for t in items:
        inst = t.get("instId") or ""
        if not inst.endswith("-USDT-SWAP"):
            continue
        last = float(t.get("last") or 0)
        # HTML: turn = volCcy24h * last (volCcy24h ≈ base currency volume)
        turn = float(t.get("volCcy24h") or 0) * last
        if turn < floor:
            continue
        out.append({
            "symbol": _okx_inst_to_symbol(inst),  # BTCUSDT для evaluate/карточек
            "inst_id": inst,                       # BTC-USDT-SWAP для candles API
            "lastPrice": last,
            "bid1Price": float(t.get("bidPx") or 0),
            "ask1Price": float(t.get("askPx") or 0),
            "turnover24h": turn,
            "exchange": "okx",
        })
    out.sort(key=lambda t: t["turnover24h"], reverse=True)
    return out[:n]


async def fetch_tickers(
    client: httpx.AsyncClient,
    source: str | None = None,
    min_vol: float | None = None,
    top_n: int | None = None,
) -> list[dict]:
    """Выбрать источник по DATA_SOURCE (или source); в auto — Bybit, при 403 → OKX."""
    global _active_exchange
    pref = (source or DATA_SOURCE or "auto").strip().lower()
    if pref not in ("auto", "bybit", "okx"):
        pref = "auto"

    if pref == "okx":
        _active_exchange = "okx"
        return await fetch_tickers_okx(client, min_vol=min_vol, top_n=top_n)

    if pref == "bybit":
        _active_exchange = "bybit"
        return await fetch_tickers_bybit(client, min_vol=min_vol, top_n=top_n)

    # auto: Bybit → OKX (HTML pickSource)
    try:
        tickers = await fetch_tickers_bybit(client, min_vol=min_vol, top_n=top_n)
        if tickers:
            _active_exchange = "bybit"
            return tickers
    except BybitBlocked as e:
        logger.warning("Bybit недоступен (%s) — переключаюсь на OKX", e)
    except Exception as e:
        # В auto любая ошибка тикеров Bybit → пробуем OKX (как HTML catch)
        logger.warning("Bybit tickers error (%s) — пробую OKX", e)

    _active_exchange = "okx"
    return await fetch_tickers_okx(client, min_vol=min_vol, top_n=top_n)


_BAR_MS = {"D": 86_400_000, "240": 4 * 3_600_000, "60": 3_600_000, "5": 300_000}


def _html_confirm(bars: list[Bar], interval: str) -> list[Bar]:
    """confirmed как в HTML: (ts + длительность бара) <= Date.now()."""
    dur = _BAR_MS.get(interval)
    if dur is None:
        if bars:
            bars[-1].confirmed = False
        return bars
    now = int(time.time() * 1000)
    for b in bars:
        b.confirmed = (b.ts + dur) <= now
    return bars


async def fetch_klines_bybit(
    client: httpx.AsyncClient, symbol: str, interval: str, limit: int = LIMIT,
    *, html_pages: bool = False,
) -> list[Bar]:
    """Bybit kline. html_pages=True — ровно как HTML Bybit.kline: страницы по 1000,
    пока не набрано limit (end = min(ts)−1), confirmed = ts+dur ≤ now."""
    if not html_pages:
        data = await _get_bybit(
            client,
            "/v5/market/kline",
            {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
        )
        pages = [data.get("result", {}).get("list", [])]
    else:
        pages, seen_n, end = [], set(), None
        for _p in range(-(-limit // 1000) + 1):
            params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": 1000}
            if end:
                params["end"] = end
            data = await _get_bybit(client, "/v5/market/kline", params)
            page = data.get("result", {}).get("list", []) if str(data.get("retCode", 0)) == "0" else []
            if not page:
                break
            pages.append(page)
            seen_n.update(int(r[0]) for r in page)
            if len(seen_n) >= limit:
                break
            end = min(int(r[0]) for r in page) - 1
    seen: dict[int, list] = {}
    for page in pages:
        for row in page:
            seen[int(row[0])] = row
    bars = []
    for ts_k in sorted(seen):
        ts, o, h, l, c, vol, vol_q = (list(seen[ts_k]) + ["0"] * 7)[:7]
        bars.append(Bar(
            ts=int(ts),  # ms
            o=float(o), h=float(h), l=float(l), c=float(c),
            vol_quote=float(vol_q),
            confirmed=True,
        ))
    if html_pages:
        return _html_confirm(bars, interval)
    if bars:
        bars[-1].confirmed = False
    return bars


async def fetch_klines_okx(
    client: httpx.AsyncClient, inst_id: str, interval: str, limit: int = LIMIT
) -> list[Bar]:
    """OKX candles; ts в ms. bar: 1Dutc|4H|1H|5m. vol_quote = k[7] ?? k[6] (HTML)."""
    bar = OKX_BAR_MAP.get(interval, interval)
    # OKX отдаёт до 300 за запрос, новейшие первыми. Пагинация как HTML OKX.kline:
    # страницы по 300, пока не набрано limit (HTML: D1 → 1 стр., H1 304 → 2 стр. = 600).
    seen: dict = {}
    after = None
    for _p in range(-(-limit // 300) + 1):
        params = {"instId": inst_id, "bar": bar, "limit": 300}
        if after:
            params["after"] = after
        data = await _get_okx(client, "/api/v5/market/candles", params)
        page = data.get("data") or []
        if not page:
            break
        for row in page:
            seen[int(row[0])] = row
        if len(seen) >= limit:
            break
        after = min(int(r[0]) for r in page)
    raw = [seen[k] for k in sorted(seen, reverse=True)]
    bars = []
    for row in reversed(raw):
        # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        padded = list(row) + ["0"] * 9
        ts, o, h, l, c = padded[0], padded[1], padded[2], padded[3], padded[4]
        vol_ccy, vol_quote = padded[6], padded[7]
        vq = float(vol_quote or 0) or float(vol_ccy or 0)
        bars.append(Bar(
            ts=int(ts),  # ms — как Bybit
            o=float(o), h=float(h), l=float(l), c=float(c),
            vol_quote=vq,
            confirmed=True,
        ))
    # HTML OKX.kline: confirmed = (ts + dur) <= Date.now()
    return _html_confirm(bars, interval)


async def fetch_klines(
    client: httpx.AsyncClient, symbol: str, interval: str, limit: int = LIMIT,
    inst_id: str | None = None, *, html_pages: bool = False,
) -> list[Bar]:
    if _active_exchange == "okx":
        return await fetch_klines_okx(client, inst_id or symbol, interval, limit)
    return await fetch_klines_bybit(client, symbol, interval, limit, html_pages=html_pages)


def html_avg_turn(d1: list[Bar], win_h: int, now_ms: int | None = None) -> float:
    """Средний дневной оборот для отбора инструментов — как HTML (кнопка «Сканировать»):
    закрытые D1 внутри окна; если их меньше 3 — последние 3 закрытых (фикс окна 24 ч)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    closed = d1[:-1] if (d1 and not d1[-1].confirmed) else list(d1)
    w0 = [b for b in closed if b.ts >= now_ms - int(win_h) * 3_600_000]
    w = w0 if len(w0) >= 3 else closed[-3:]
    return (sum((b.vol_quote or 0) for b in w) / len(w)) if w else 0.0


async def scan_one(
    client: httpx.AsyncClient,
    ticker: dict,
    *,
    lookback_hours: int | None = None,
    no_night: bool = True,
) -> list[dict]:
    """Проверить один инструмент, вернуть список карточек (может быть пустым)."""
    symbol = ticker["symbol"]
    inst_id = ticker.get("inst_id") or symbol
    lb = SCAN_LOOKBACK_H if lookback_hours is None else max(1, int(lookback_hours))

    try:
        last = float(ticker.get("lastPrice", 0))
        bid = float(ticker.get("bid1Price", 0) or 0)
        ask = float(ticker.get("ask1Price", 0) or 0)
        vol24 = float(ticker.get("turnover24h", 0))

        # Глубина истории — ровно как в HTML: ex.kline(sym,'1d',min(450,ceil(win/24)+60)) и
        # ex.kline(sym,'1h',min(9100,win+280)); Bybit — страницами по 1000, OKX — по 300.
        # От глубины зависят ATR (RMA с начала ряда) и «свободная зона» — поэтому 1:1.
        d1n = min(450, -(-lb // 24) + 60)
        need = min(9100, lb + 280)
        d1, h1 = await asyncio.gather(
            fetch_klines(client, symbol, "D", limit=d1n, inst_id=inst_id, html_pages=True),
            fetch_klines(client, symbol, "60", limit=need, inst_id=inst_id, html_pages=True),
        )

        # Порог модели в скане не применяем (как scanSymbol в HTML) — решает шаблон.
        # no_night — чекбокс «без ночи» в HTML (по умолчанию включён).
        result = evaluate(
            symbol, last, bid, ask, d1, [], h1, [], 0,
            fbo_threshold=0.0, no_night=bool(no_night), lookback_hours=lb + 8,
        )
        from html_pipeline import dedup_list
        cards = dedup_list(result.get("cards", []))
        # Для отбора инструментов: средний дневной оборот за окно (HTML perCoin.avg)
        turn = html_avg_turn(d1, lb)
        return {"cards": cards, "turn": turn, "base": _card_base({"ticker": symbol})}
    except Exception as e:
        logger.warning("scan_one %s error: %s", symbol, e)
        return {"cards": [], "turn": 0.0, "base": ""}


# ── Точный порт фильтров шаблона из HTML (readF + passes + recompute) ───────────
_F_DEF = {
    "thr": 0.20, "stop": 10.0, "tgt": 3.0, "str": 0.0, "cross": 99.0, "touch": None, "acc": None,
    "dist": 99.0, "over": 0.0, "vc": None, "vol": 0.0, "bias": None, "side": None, "cap": 3,
    "age": 0.0, "kind": None, "maxrisk": 99.0, "hours": "9-23", "daycap": 99, "streak": 0,
    "pause": 24, "free": 0.0, "wall": 0.0, "daypos": None, "body": 0.0, "wick": 0.0,
    "brkbars": None, "volret": 0.0, "dow": None, "ema": None, "appr": None, "atrr": None,
    "dens": 99.0, "round": None, "green": None, "streakd": 0.0, "atrd": None, "minn": 8,
    "cov": None, "preacc": None, "smooth": None, "d1ag": None, "poke": 0.0, "msc": 0.0,
    "rusqueeze": None, "ruclose": None, "ruimoex": None, "rurelstr": None,
}
_F_STR = ("kind", "hours", "daypos", "dow", "appr", "atrr", "round", "green", "atrd")
_F_INT = ("touch", "acc", "bias", "side", "ema", "brkbars", "cov", "preacc", "smooth", "d1ag")
MAXRISK = 0.20  # как MAXRISK в HTML: риск > 20% цены — сделка отбрасывается


def _f_from_template(raw: dict, market: str | None = None) -> dict:
    """Как readF() в HTML: отсутствующее/«auto» значение → DEF.
    applyMarket(): для акций РФ DEF.hours = '0-24' (все часы торгов), для крипты '9-23'."""
    f = {}
    for k, dv in _F_DEF.items():
        if k == "hours" and market == "ru":
            dv = "0-24"
        v = raw.get(k, "auto") if isinstance(raw, dict) else "auto"
        if v is None or v == "auto":
            f[k] = dv
        elif k in _F_STR:
            f[k] = str(v)
        elif k in _F_INT:
            try:
                f[k] = float(v)
            except (TypeError, ValueError):
                f[k] = dv
        else:
            try:
                f[k] = float(v)
            except (TypeError, ValueError):
                f[k] = dv
    return f


def _sfx(v: str, x: float, kind: str) -> bool:
    """Селекторы вида s0.04 / f0.08: s — меньше, f — больше."""
    try:
        lim = float(v[1:])
    except ValueError:
        return True
    if v[0] == "s":
        return x < lim
    if v[0] == "f":
        return x > lim
    return True


def passes_html(s: dict, f: dict) -> bool:
    """1:1 с passes(s,f) из HW_FBO_scanner_6.html. s — card['feat'], f — из _f_from_template."""
    is_b = s.get("type") == "brk"
    if not is_b and s.get("p", 0) < f["thr"]:
        return False
    if f["msc"] > 0 and s.get("_sc", 0) < f["msc"]:
        return False
    if s["strength"] < f["str"]:
        return False
    if s["crosses"] > f["cross"]:
        return False
    if f["touch"] is not None and s["touched8"] != f["touch"]:
        return False
    if f["acc"] is not None and s["acc"] != f["acc"]:
        return False
    if s["dist_atr"] > f["dist"]:
        return False
    if not is_b and s["overshoot_atr"] < f["over"]:
        return False
    if f["vc"] is not None:
        if f["vc"] >= 99:
            if s["vol_contraction"] <= 1.0:
                return False
        elif s["vol_contraction"] >= f["vc"]:
            return False
    if s["vol_mult"] < f["vol"]:
        return False
    if f["bias"] is not None and s["bias_ok"] != f["bias"]:
        return False
    if f["side"] is not None and s["d"] != f["side"]:
        return False
    if s["level_age_h"] < f["age"]:
        return False
    if f["kind"]:
        k = s.get("k") or ""
        if f["kind"] == "swing" and "swing" not in k:
            return False
        if f["kind"] == "5d" and "5d" not in k:
            return False
        if f["kind"] == "prev" and "prev" not in k:
            return False
    if f["hours"] and f["hours"] != "0-24":
        try:
            a, b = (int(x) for x in f["hours"].split("-"))
            hl = (time.gmtime(s["t"] / 1000).tm_hour + 3) % 24
            if not (a <= hl < b):
                return False
        except ValueError:
            pass
    if s["free_zone_h"] < f["free"]:
        return False
    if f["wall"] and s["wall_r"] < f["wall"]:
        return False
    if f["daypos"]:
        d = s["day_pos"]
        if f["daypos"] == "lo" and not d < 0.3:
            return False
        if f["daypos"] == "mid" and not (0.3 <= d <= 0.7):
            return False
        if f["daypos"] == "hi" and not d > 0.7:
            return False
        if f["daypos"] == "edge" and not (d < 0.25 or d > 0.75):
            return False
    if s["body_ratio"] < f["body"]:
        return False
    if s["wick_ratio"] < f["wick"]:
        return False
    if not is_b and f["brkbars"] is not None:
        bb = s["brk_bars"]
        if f["brkbars"] == 4:
            if bb < 4:
                return False
        elif f["brkbars"] == 1:
            if bb != 1:
                return False
        elif bb > f["brkbars"]:
            return False
    if not is_b and s["vol_ret_brk"] < f["volret"]:
        return False
    if f["dow"]:
        d = s["dow"]
        if f["dow"] == "work" and d in (0, 6):
            return False
        if f["dow"] == "mid" and not (2 <= d <= 4):
            return False
        if f["dow"] == "weekend" and d not in (0, 6):
            return False
    if f["ema"] is not None and s["ema_ok"] != f["ema"]:
        return False
    if f["appr"] and not _sfx(f["appr"], s["approach_atr"], "appr"):
        return False
    if f["atrr"] and not _sfx(f["atrr"], s["atr_ratio"], "atrr"):
        return False
    if s["lvl_density"] > f["dens"]:
        return False
    if f["round"]:
        if f["round"] == "far":
            if s["round_dist"] <= 0.20:
                return False
        else:
            try:
                if s["round_dist"] > float(f["round"]):
                    return False
            except ValueError:
                pass
    if f["green"]:
        g = s["green_ratio"]
        with_side = g > 0.55 if s["d"] == 1 else g < 0.45
        if f["green"] == "with" and not with_side:
            return False
        if f["green"] == "against" and with_side:
            return False
        if f["green"] == "flat" and not (0.4 <= g <= 0.6):
            return False
    if f["streakd"] and abs(s["d1_streak"]) < f["streakd"]:
        return False
    if not is_b:
        if f["cov"] is not None and s["v6_covers"] != f["cov"]:
            return False
        if f["preacc"] is not None and s["v6_preAcc"] != f["preacc"]:
            return False
        if f["smooth"] is not None and s["v6_smooth"] != f["smooth"]:
            return False
        if f["d1ag"] is not None and s["v6_d1Against"] != f["d1ag"]:
            return False
        if f["poke"] and (s.get("v6_poke") or 0) < f["poke"]:
            return False
    if f["atrd"]:
        a = s["atrD_pct"]
        if f["atrd"] == "m" and not (0.04 <= a <= 0.08):
            return False
        if f["atrd"][0] == "s" and not a < float(f["atrd"][1:]):
            return False
        if f["atrd"][0] == "f" and not a > float(f["atrd"][1:]):
            return False
    # РУ-фильтры нового HTML — только для акций Мосбиржи (s.mkt==='ru'); «1» = фильтр включён.
    # HTML: if(+f.rusqueeze===1&&!s.ru_squeeze) return false; (readF → parseFloat('1') = 1).
    if s.get("mkt") == "ru":
        for fk, sk in (("rusqueeze", "ru_squeeze"), ("ruclose", "ru_close_pos"),
                       ("ruimoex", "ru_imoex_ok"), ("rurelstr", "ru_rel_str")):
            v = f.get(fk)
            try:
                on = v is not None and v != "" and float(v) == 1
            except (TypeError, ValueError):
                on = False
            if on and not s.get(sk):
                return False
    return True


def template_risk(card: dict, f: dict) -> tuple[float, float, float] | None:
    """recompute() из HTML: риск = max(stop% * ATR_D, 2% цены); тейк = tgt*R. → (stop, take, risk)."""
    e = float(card["last"])
    atr_d = float(card.get("atr_d") or 0)
    min_pct = 0.01 if card.get("market") == "ru" else 0.02   # новый HTML: 1% РФ / 2% крипта
    risk = max((f["stop"] / 100.0) * atr_d, min_pct * e)
    if risk <= 0 or risk / e > MAXRISK:
        return None
    long_ = card["side"] == "LONG"
    stop = e - risk if long_ else e + risk
    take = e + f["tgt"] * risk if long_ else e - f["tgt"] * risk
    return stop, take, risk


def retarget_card(card: dict, raw_filters: dict) -> dict:
    """Пересчитать стоп/тейк карточки под настройки «Стоп % ATR» шаблона."""
    f = _f_from_template(raw_filters)
    r = template_risk(card, f)
    if r is None:
        return card
    stop, take, risk = r
    return {**card, "stop": stop, "take": take, "risk": risk}


def _matches_single(card: dict, f: dict) -> bool:
    """
    Проверить карточку против одного набора фильтров.
    Понимает два формата:
      1) старый упрощённый (sides, strength_min, dist_atr_max, bias_filter)
      2) реальные ключи шаблона из HTML (thr, stop, str, cross, dist, age,
         maxrisk, bias, side, kind — без префикса sc_/bt_, как сохраняет HTML)
    Значения отсутствующих ключей — «авто» (как HTML readF→DEF), кроме FBO thr:
    отсутствующий/auto thr = 0.20 (HTML DEF.thr), не «фильтр выключен».
    """
    side_map = {"1": "LONG", "0": "SHORT"}

    # ── старый формат ──
    if "sides" in f:
        if card["side"] not in f.get("sides", ["LONG", "SHORT"]):
            return False
    if "strength_min" in f and card.get("strength", 0) < f["strength_min"]:
        return False
    if "dist_atr_max" in f and card.get("dist_atr", 0) > f["dist_atr_max"]:
        return False
    if f.get("bias_filter"):
        bias = card.get("d1_bias", "flat")
        if card["side"] == "LONG" and bias != "up":
            return False
        if card["side"] == "SHORT" and bias != "down":
            return False

    # ── точный порт passes() из HTML, если у карточки есть сырые признаки ──
    _legacy = any(k in f for k in ("sides", "strength_min", "dist_atr_max", "bias_filter"))
    if card.get("feat") is not None and not _legacy:
        f2 = _f_from_template(f)
        rr = template_risk(card, f2)
        if rr is None:
            return False
        if (rr[2] / float(card["last"])) * 100 > f2["maxrisk"]:
            return False
        return passes_html(card["feat"], f2)

    # ── реальные ключи шаблона HTML (FIDS) ──
    # FBO thr: как HTML passes()+DEF — если thr отсутствует / auto / None → 0.20 (DEF.thr),
    # НЕ «фильтр выключен». Для brk порог модели не применяется.
    if card.get("strategy") == "fbo":
        thr_raw = f.get("thr", 0.20)
        if thr_raw in (None, "auto"):
            thr = 0.20
        else:
            try:
                thr = float(thr_raw)
            except (TypeError, ValueError):
                thr = 0.20
        if (card.get("prob") or 0) < thr:
            return False

    if "str" in f and f["str"] not in (None, "auto"):
        try:
            if card.get("strength", 0) < float(f["str"]):
                return False
        except (TypeError, ValueError):
            pass

    if "cross" in f and f["cross"] not in (None, "auto"):
        try:
            if card.get("crosses", 0) > float(f["cross"]):
                return False
        except (TypeError, ValueError):
            pass

    if "dist" in f and f["dist"] not in (None, "auto"):
        try:
            if card.get("dist_atr", 0) > float(f["dist"]):
                return False
        except (TypeError, ValueError):
            pass

    if "age" in f and f["age"] not in (None, "auto"):
        try:
            if card.get("level_age_h", 0) < float(f["age"]):
                return False
        except (TypeError, ValueError):
            pass

    if "maxrisk" in f and f["maxrisk"] not in (None, "auto"):
        try:
            entry = card.get("last", 0)
            risk_pct = (abs(entry - card.get("stop", entry)) / entry * 100) if entry else 0
            if risk_pct > float(f["maxrisk"]):
                return False
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    if "bias" in f and f["bias"] not in (None, "auto"):
        bias = card.get("d1_bias", "flat")
        want_with_trend = f["bias"] in (1, "1")
        side = card["side"]
        trend_side = "LONG" if bias == "up" else ("SHORT" if bias == "down" else None)
        if trend_side is not None:
            if want_with_trend and side != trend_side:
                return False
            if not want_with_trend and side == trend_side:
                return False

    if "side" in f and f["side"] not in (None, "auto"):
        wanted = side_map.get(str(f["side"]))
        if wanted and card["side"] != wanted:
            return False

    if "kind" in f and f["kind"] not in (None, "auto"):
        wanted_kind = f["kind"]
        card_kind = card.get("kind", "")
        kind_map = {"swing": "swing D1", "5d": "5d", "prev": "prev"}
        prefix = kind_map.get(wanted_kind, wanted_kind)
        if not card_kind.startswith(prefix):
            return False

    return True


def match_filter(card: dict, f: dict) -> str | None:
    """
    Проверить карточку против фильтра пользователя.
    Возвращает имя совпавшего шаблона, либо '' для старого формата, либо None если не подошло.
    """
    # Рынок карточки должен быть среди выбранных рынков
    card_market = card.get("market", "crypto")
    markets = f.get("markets")
    if markets and card_market not in markets:
        return None

    if "_multi" in f:
        # Формат из HTML: список {_name, _market, _strategy, filters}. Сигнал проходит если
        # совпал хотя бы с одним активным шаблоном С ТЕМ ЖЕ рынком И стратегией.
        card_strategy = card.get("strategy", "brk")
        for tpl in f["_multi"]:
            name = tpl.get("_name", "")
            tpl_market = tpl.get("_market", "crypto")
            if tpl_market != card_market:
                continue
            tpl_strategy = tpl.get("_strategy", "fbo")
            if tpl_strategy != "both" and tpl_strategy != card_strategy:
                continue
            if _matches_single(card, tpl.get("filters", tpl)):
                return name
        return None

    # Старый формат (без шаблонов из HTML)
    return "" if _matches_single(card, f) else None

def _template_conc_limit(f: dict, tpl_name: str) -> int:
    """Достать _conc (макс. одновременно открытых сигналов) выбранного шаблона. 0 = без лимита."""
    for tpl in f.get("_multi", []):
        if tpl.get("_name") == tpl_name:
            try:
                return int(tpl.get("filters", {}).get("_conc") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def concurrency_allows(chat_id: int, tpl_name: str, f: dict) -> bool:
    """True если можно слать новый сигнал по этому шаблону (лимит «Одновременно в рынке» не превышен)."""
    _remember_templates(chat_id, f)
    limit = _template_conc_limit(f, tpl_name)
    if limit <= 0:
        return True
    return storage.count_open_signals(chat_id, tpl_name) < limit


# Оставлен для обратной совместимости, если где-то ещё вызывается напрямую
def apply_filter(card: dict, f: dict) -> bool:
    return match_filter(card, f) is not None


def _card_base(card: dict) -> str:
    t = card.get("ticker") or ""
    if t.endswith("USDT"):
        return t[:-4]
    return t.split("-")[0] if "-" in t else t


def _apply_coin_limit(pending: list[dict], win_h: int = COIN_WINDOW_H) -> list[dict]:
    """HTML applyCoinLimit: 1 сигнал на base-монету за окно win_h часов (в рамках скана)."""
    best: dict[str, dict] = {}
    win_ms = win_h * 3_600_000
    for item in pending:
        card = item["card"]
        ts = int(card.get("signal_ts") or 0)
        base = _card_base(card)
        key = f"{base}|{ts // win_ms if win_ms else 0}"
        prev = best.get(key)
        if prev is None:
            best[key] = item
            continue
        p_ts = int(prev["card"].get("signal_ts") or 0)
        # приоритет как prioOf() в HTML — оценка 0–10, затем вероятность модели
        p_prob = (prev["card"].get("score") or 0, prev["card"].get("prob") or 0)
        c_prob = (card.get("score") or 0, card.get("prob") or 0)
        # первый по времени; при равном ts — выше оценка, затем base
        if ts < p_ts or (ts == p_ts and (c_prob > p_prob or (c_prob == p_prob and base < _card_base(prev["card"])))):
            best[key] = item
    keep = set(id(v) for v in best.values())
    return [p for p in pending if id(p) in keep]


def _cap_cluster(pending: list[dict], cap: int = CAP_CLUSTER) -> list[dict]:
    """HTML capCluster: max `cap` сигналов на (час, сторона)."""
    if not cap or cap >= 99:
        return pending
    by: dict[str, list[dict]] = {}
    for item in pending:
        card = item["card"]
        ts = int(card.get("signal_ts") or 0)
        side = card.get("side") or ""
        k = f"{ts // 3_600_000}|{side}"
        by.setdefault(k, []).append(item)
    keep = []
    for group in by.values():
        group.sort(
            key=lambda it: (
                -(it["card"].get("score") or 0),
                -(it["card"].get("prob") or 0),
                _card_base(it["card"]),
            )
        )
        keep.extend(group[:cap])
    # сохранить относительный порядок исходного списка
    keep_ids = set(id(x) for x in keep)
    return [p for p in pending if id(p) in keep_ids]


def _best_only_rank(card: dict) -> tuple:
    """Ключ ранжирования «лучший» (больше = лучше).

    Порядок:
      1) prob (float, default 0)
      2) strength (int 1–5, default 0)
      3) ближе вход: ниже dist_atr (default большой → хуже)
      4) новее signal_ts
    Тот же дух, что у _cap_cluster / coin_limit.
    """
    dist = card.get("dist_atr")
    if dist is None:
        dist = 1e9
    return (
        float(card.get("score") or 0),
        float(card.get("prob") or 0),
        int(card.get("strength") or 0),
        -float(dist),
        int(card.get("signal_ts") or 0),
    )


def _apply_best_only(
    pending: list[dict],
    chat_best_only: Callable[[int], dict],
) -> list[dict]:
    """Если у чата best_only[market]=True — оставить только 1 лучший сигнал
    этого рынка для чата; убрать chat_id из остальных by_template.

    Независимые настройки crypto / ru. Чаты с выкл. — без изменений.
    """
    if not pending or chat_best_only is None:
        return pending

    all_chats: set[int] = set()
    for item in pending:
        for chats in (item.get("by_template") or {}).values():
            all_chats.update(chats)
    if not all_chats:
        return pending

    settings = {cid: (chat_best_only(cid) or {}) for cid in all_chats}

    # Кандидаты: (chat_id, market) → индексы pending, где чат фигурирует
    candidates: dict[tuple[int, str], list[int]] = {}
    for idx, item in enumerate(pending):
        market = item["card"].get("market", "crypto")
        if market not in ("crypto", "ru"):
            market = "crypto"
        chats_in: set[int] = set()
        for chat_list in (item.get("by_template") or {}).values():
            chats_in.update(chat_list)
        for cid in chats_in:
            if (settings.get(cid) or {}).get(market):
                candidates.setdefault((cid, market), []).append(idx)

    keep_for: dict[tuple[int, str], int] = {}
    for key, idxs in candidates.items():
        if len(idxs) == 1:
            keep_for[key] = idxs[0]
        else:
            keep_for[key] = max(idxs, key=lambda i: _best_only_rank(pending[i]["card"]))

    if not keep_for:
        return pending

    out: list[dict] = []
    for idx, item in enumerate(pending):
        market = item["card"].get("market", "crypto")
        if market not in ("crypto", "ru"):
            market = "crypto"
        new_by: dict = {}
        for tpl, chats in (item.get("by_template") or {}).items():
            filtered = []
            for cid in chats:
                if (settings.get(cid) or {}).get(market):
                    if keep_for.get((cid, market)) == idx:
                        filtered.append(cid)
                else:
                    filtered.append(cid)
            if filtered:
                new_by[tpl] = filtered
        if new_by:
            out.append({"card": item["card"], "by_template": new_by})

    dropped = len(pending) - len(out)
    if dropped or any(True for _ in keep_for):
        logger.info(
            "best_only: pending %d → %d (правил чат+рынок: %d)",
            len(pending), len(out), len(keep_for),
        )
    return out


def _week_start_ms(ts_ms: int) -> int:
    """Понедельник 00:00 UTC недели, в которую попал сигнал."""
    d = ts_ms // 86_400_000
    wd = (d + 3) % 7          # 1970-01-01 — четверг → понедельник = 0
    return (d - wd) * 86_400_000


def _apply_week_cap(pending: list[dict]) -> list[dict]:
    """«N лучших за неделю» (как sc_weekmax в HTML): для каждой пары чат+рынок+неделя
    отправляем не больше N сигналов, лучшие по оценке; уже отправленные за неделю
    уменьшают остаток. N задаётся на чат (/weekcap), 0 = без лимита."""
    if not pending:
        return pending
    chats: set[int] = set()
    for item in pending:
        for cl in (item.get("by_template") or {}).values():
            chats.update(cl)
    caps = {cid: storage.get_week_cap(cid) for cid in chats}
    groups: dict[tuple[int, str, int], list[int]] = {}
    for idx, item in enumerate(pending):
        card = item["card"]
        market = card.get("market", "crypto")
        ws = _week_start_ms(int(card.get("signal_ts") or 0))
        cin: set[int] = set()
        for cl in (item.get("by_template") or {}).values():
            cin.update(cl)
        for cid in cin:
            if caps.get(cid, 0) > 0:
                groups.setdefault((cid, market, ws), []).append(idx)
    keep: dict[tuple[int, str, int], set[int]] = {}
    for (cid, market, ws), idxs in groups.items():
        remaining = caps[cid] - storage.count_signals_in_week(cid, market, ws)
        if remaining <= 0:
            keep[(cid, market, ws)] = set()
        else:
            best = sorted(idxs, key=lambda i: _best_only_rank(pending[i]["card"]), reverse=True)[:remaining]
            keep[(cid, market, ws)] = set(best)
    if not keep:
        return pending
    out: list[dict] = []
    for idx, item in enumerate(pending):
        card = item["card"]
        market = card.get("market", "crypto")
        ws = _week_start_ms(int(card.get("signal_ts") or 0))
        new_by: dict = {}
        for tpl, cl in (item.get("by_template") or {}).items():
            kept = [cid for cid in cl
                    if (cid, market, ws) not in keep or idx in keep[(cid, market, ws)]]
            if kept:
                new_by[tpl] = kept
        if new_by:
            out.append({"card": card, "by_template": new_by})
    if len(out) != len(pending):
        logger.info("week_cap: pending %d → %d", len(pending), len(out))
    return out


# «🤖 Авто» = встроенный шаблон HTML «Авто — все фильтры сброшены» (BUILTIN_TPL = {}):
# все фильтры в «auto» → html_pipeline.apply_all берёт значения DEF, ровно как HTML.
AUTO_TEMPLATE_NAME = storage.AUTO_TEMPLATE_NAME
AUTO_FILTERS: dict = {}


def auto_template_entry(market: str, strategy: str = "fbo") -> dict:
    """Элемент _multi для рынка в режиме «Авто» (crypto | ru)."""
    return {"_name": AUTO_TEMPLATE_NAME, "_market": market, "_strategy": strategy,
            "filters": dict(AUTO_FILTERS), "_auto": True}


_TPL_FILTERS: dict[tuple[int, str], dict] = {}


def _remember_templates(chat_id: int, f: dict):
    for tpl in f.get("_multi", []):
        _TPL_FILTERS[(chat_id, tpl.get("_name", ""))] = tpl.get("filters", {})


def _card_for_template(card: dict, tpl_name: str, chat_ids: list[int]) -> dict:
    """Карточка с матч-шаблоном и стопом/тейком, пересчитанными под «Стоп % ATR» шаблона."""
    out = {**card, "matched_template": tpl_name}
    for cid in chat_ids:
        raw = _TPL_FILTERS.get((cid, tpl_name))
        if raw is not None:
            out = {**retarget_card(out, raw), "matched_template": tpl_name}
            break
    return out


def _needed_markets(subscribers: list[int], chat_filters: Callable[[int], dict]) -> set[str]:
    """Какие рынки реально нужны по активным фильтрам подписчиков.

    Пустой набор = нечего сканировать (нет активных шаблонов ни у кого).
    """
    needed: set[str] = set()
    for chat_id in subscribers:
        f = chat_filters(chat_id)
        markets = f.get("markets")
        if markets is None:
            markets = ["crypto"]
        multi = f.get("_multi")
        if multi:
            # «Крипта (Алго)» — только по явно выбранному алго-шаблону с ≥1 индикатором
            tpl_mkts = {t.get("_market", "crypto") for t in multi
                        if t.get("_market") != "algo" or (not t.get("_auto") and algo_mod.template_combo(t.get("filters") or {})[0])}
            for m in markets:
                if m in tpl_mkts:
                    needed.add(m)
        else:
            needed.update(m for m in markets if m != "algo")   # без шаблонов алго не сканируется никогда
    return needed




# HTML noStocks — американские акции/ETF на крипто-биржах (как isStock в HTML)
_STOCK_BASES = frozenset(
    "QQQ SPY SPX IWM DIA GLD SLV USO ARKK TQQQ SQQQ SOXL SOXS TSLA AAPL NVDA MSFT META "
    "AMZN GOOGL GOOG COIN HOOD MSTR CRCL ORCL AMD NFLX BMNR SPCX SKHYNIX DRAM PLTR AVGO "
    "INTC BABA NIO GME AMC UNH JPM V MA DIS BA WMT XOM CVX PFE KO PEP MCD NKE SBUX CRM "
    "ADBE CSCO QCOM TXN MU SMCI ARM DELL UBER ABNB SQ PYPL SHOP SNOW NET DDOG CRWD RBLX "
    "DKNG F GM T VZ".split()
)
_STOCK_PREFIXES = ("NCCO", "NCSI", "CSOP")


def _is_stock_base(symbol: str) -> bool:
    base = (symbol or "").upper()
    if base.endswith("USDT"):
        base = base[:-4]
    if base in _STOCK_BASES:
        return True
    return any(base.startswith(p) for p in _STOCK_PREFIXES)


# ── Выбор инструментов и отправка — как в HTML (кнопка «Сканировать» / renderScan) ─────


def _select_universe(results: list, n_inst: int, min_vol: float) -> list[dict]:
    """HTML: kept = монеты со средним дневным оборотом за период ≥ minVol, по убыванию, топ n_inst."""
    rows = [r for r in results if isinstance(r, dict)]
    rows = [r for r in rows if (r.get("turn") or 0) >= min_vol]
    rows.sort(key=lambda r: -(r.get("turn") or 0))
    out = []
    for r in rows[:n_inst]:
        out.extend(r.get("cards") or [])
    return out


async def _scan_crypto_html(
    client: httpx.AsyncClient,
    *,
    n_inst: int,
    min_vol: float,
    win_h: int,
    no_night: bool = True,
    no_stocks: bool = True,
    source: str | None = None,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> list[dict]:
    """Ровно как HTML: тикеры с turn ≥ minVol×0.5 и last>0, без акций, топ nInst по обороту 24ч →
    скан каждого (окно win_h) → итоговый список по среднему дневному обороту за период ≥ minVol."""
    tks = await fetch_tickers(client, source=source, min_vol=min_vol * 0.5, top_n=1_000_000)
    tks = [t for t in tks if float(t.get("lastPrice") or 0) > 0]
    if no_stocks:
        tks = [t for t in tks if not _is_stock_base(t.get("symbol", ""))]
    tks = tks[:max(1, int(n_inst))]
    logger.info("Скан crypto: %d инструментов через %s (окно %dч)",
                len(tks), "OKX" if _active_exchange == "okx" else "Bybit", win_h)
    results: list = []
    total = max(1, len(tks))
    for i in range(0, len(tks), 20):
        results += await asyncio.gather(
            *[scan_one(client, t, lookback_hours=win_h, no_night=no_night) for t in tks[i:i + 20]],
            return_exceptions=True,
        )
        if progress:
            await progress(min(i + 20, total), total)
        await asyncio.sleep(0.3)
    cards = _select_universe(results, n_inst, min_vol)
    for c in cards:
        c["market"] = "crypto"
    return cards


def _week_quota_filter(chat_id: int, cards: list[dict], week_max: int) -> list[dict]:
    """Бот-правило (в HTML его нет, т.к. HTML видит всю неделю сразу): не больше week_max
    отправленных сигналов на рынок за ISO-неделю HTML (isoWeekKey) — с учётом уже
    отправленных ранее. Из новых берутся лучшие по оценке."""
    if not week_max or week_max >= 999 or not cards:
        return cards
    from html_pipeline import iso_week_key
    groups: dict[tuple[str, str], list[dict]] = {}
    for c in cards:
        groups.setdefault((c.get("market", "crypto"), iso_week_key(int(c["signal_ts"]))), []).append(c)
    keep: list[dict] = []
    for (mkt, wk), grp in groups.items():
        since = min(int(c["signal_ts"]) for c in grp) - 8 * 86_400_000
        try:
            already = sum(1 for ts in storage.list_sent_signal_ts(chat_id, mkt, since)
                          if iso_week_key(int(ts)) == wk)
        except Exception as e:
            logger.debug("week quota lookup failed: %s", e)
            already = 0
        remaining = week_max - already
        if remaining <= 0:
            continue
        keep.extend(sorted(grp, key=_best_only_rank, reverse=True)[:remaining])
    return keep


async def _deliver_html(
    all_cards: list[dict],
    subscribers: list[int],
    chat_filters: Callable[[int], dict],
    now_ms: int,
    win_h: int,
    on_signal: Callable[[dict, list[int]], Awaitable[None]],
    chat_best_only: Callable[[int], dict] | None = None,
    stats: dict | None = None,
    algo_ctx: dict | None = None,
) -> int:
    """Для каждого чата и каждого его активного шаблона — ровно тот список, что показал бы
    раздел «Сигналы» в HTML за последние win_h часов (html_pipeline.apply_all).

    Поверх — только бот-правила: не старше MAX_SIGNAL_AGE_H, без повторов (история отправок
    чата + общий дедуп уровня ±0.5%/36ч), лимит одновременных по шаблону, «только лучший»
    по рынку, недельная квота с учётом уже отправленного. Отправка — от старых к новым."""
    from html_pipeline import apply_all

    cutoff = now_ms - win_h * 3_600_000
    by_market: dict[str, list[dict]] = {}
    for c in all_cards:
        by_market.setdefault(c.get("market", "crypto"), []).append(c)

    # Дедуп уровня — снимок ДО рассылки, чтобы отправка первому чату не блокировала второй.
    dup_cache: dict[tuple, bool] = {}

    def _is_dup(c: dict) -> bool:
        k = (c["ticker"], c["side"], round(float(c["level"]), 12), c.get("strategy", "brk"))
        if k not in dup_cache:
            try:
                dup_cache[k] = storage.is_duplicate(c["ticker"], c["side"], c["level"],
                                                    c.get("strategy", "brk"))
            except Exception:
                dup_cache[k] = False
        return dup_cache[k]

    sent = 0
    for chat_id in subscribers:
        f = chat_filters(chat_id) or {}
        markets = f.get("markets") or ["crypto"]
        multi = f.get("_multi") or [{"_name": "", "_market": m, "_strategy": "both", "filters": {}}
                                    for m in markets if m != "algo"]   # алго: без авто/дефолта
        # Недельный лимит в боте УБРАН (решение пользователя): «макс. сделок в неделю» — только
        # фильтр бэктеста HTML. Количество сигналов бота ограничивают только шаблоны/сценарии.
        week_max = 0
        chosen: dict[tuple, dict] = {}
        for tpl in multi:
            mkt = tpl.get("_market", "crypto")
            if mkt not in markets:
                continue
            name = tpl.get("_name", "")
            raw = tpl.get("filters") or {}
            if mkt == "algo":
                # «Крипта (Алго)»: только явный шаблон (никакого Авто); HTML applyAll для algo
                # отдаёт все сигналы шаблона без лимитов
                if tpl.get("_auto") or not name:
                    continue
                rows = [c for c in algo_mod.cards_for(algo_ctx, raw, name) if c["signal_ts"] >= cutoff]
                for c in rows:
                    key = (c["ticker"], "algo", c["signal_ts"], c["side"], round(c["level"], 10))
                    if key not in chosen:
                        chosen[key] = {**c, "matched_template": name, "template_thr": None}
                continue
            rows = apply_all(by_market.get(mkt, []), raw, tpl.get("_strategy", "both"), cutoff, week_max,
                             win_h=win_h, market=mkt)
            thr = raw.get("thr")
            try:
                thr = float(thr) if thr not in (None, "auto") else None
            except (TypeError, ValueError):
                thr = None
            for c in rows:
                key = (c["ticker"], c.get("strategy"), c["signal_ts"], c["side"], round(c["level"], 10))
                if key not in chosen:
                    chosen[key] = {**c, "matched_template": name, "template_thr": thr}

        # Счётчики для прозрачности (ручной «Скан» пишет итог: найдено как в HTML → отправлено).
        st = {"found": len(chosen), "too_old": 0, "already": 0, "level_dup": 0,
              "conc": 0, "best_only": 0, "sent": 0,
              "exchange": ("OKX" if _active_exchange == "okx" else "Bybit")}
        fresh = []
        for c in chosen.values():
            if now_ms - int(c["signal_ts"]) > MAX_SIGNAL_AGE_MS:
                st["too_old"] += 1
            elif storage.was_sent_to_chat(chat_id, c["ticker"], c["signal_ts"],
                                          c.get("strategy", "brk"), c["side"]):
                st["already"] += 1
            elif _is_dup(c):
                st["level_dup"] += 1
            else:
                fresh.append(c)
        n0 = len(fresh)
        fresh = [c for c in fresh if concurrency_allows(chat_id, c["matched_template"], f)]
        st["conc"] = n0 - len(fresh)

        # «Только лучший» (настройка бота, в HTML её нет) — по оценке, отдельно для рынка
        if chat_best_only is not None and fresh:
            bo = chat_best_only(chat_id) or {}
            keep = []
            for mkt in {c.get("market", "crypto") for c in fresh}:
                grp = [c for c in fresh if c.get("market", "crypto") == mkt]
                keep.extend([max(grp, key=_best_only_rank)] if bo.get(mkt) else grp)
            st["best_only"] = len(fresh) - len(keep)
            fresh = keep


        fresh.sort(key=lambda c: c["signal_ts"])
        for c in fresh:
            await on_signal(c, [chat_id])
            storage.mark_sent(c["ticker"], c["side"], c["level"], c.get("strategy", "brk"))
            sent += 1
            st["sent"] += 1
        if any(st[k] for k in ("too_old", "level_dup", "conc", "best_only")):
            logger.info("deliver chat=%s: %s", chat_id, st)
        if stats is not None:
            stats[chat_id] = st
    return sent


async def run_scan(
    on_signal: Callable[[dict, list[int]], Awaitable[None]],
    subscribers: list[int],
    chat_filters: Callable[[int], dict],
    incremental: bool = True,
    chat_best_only: Callable[[int], dict] | None = None,
    stats: dict | None = None,
) -> int:
    """Автоскан: как кнопка «Сканировать» в HTML с окном MAX_SIGNAL_AGE_H (12 ч),
    TOP_N инструментов, от MIN_VOL_USD_24H.

    Каждый чат получает ровно те сигналы, что HTML показал бы по его шаблонам за
    это окно, — кроме уже присланных ранее. incremental оставлен для совместимости.
    """
    now_ms = int(time.time() * 1000)
    win_h = MAX_SIGNAL_AGE_H
    needed = _needed_markets(subscribers, chat_filters)
    if not needed:
        logger.info("Скан пропущен: нет рынков с активными шаблонами")
        return 0

    all_cards: list[dict] = []
    if "crypto" in needed:
        async with httpx.AsyncClient() as client:
            all_cards += await _scan_crypto_html(
                client, n_inst=TOP_N, min_vol=MIN_VOL_USD_24H, win_h=win_h,
                no_night=True, no_stocks=True,
            )
    if "ru" in needed:
        async with moex_client() as client:
            try:
                tickers_ru = await fetch_tickers_moex(client)
            except Exception as e:
                logger.warning("MOEX tickers failed: %s", e)
                tickers_ru = []
            imoex_ctx = await fetch_imoex_ctx(client)
            results = []
            for i in range(0, len(tickers_ru), 6):
                results += await asyncio.gather(
                    *[scan_one_moex(client, t, lookback_hours=win_h, no_night=False, imoex_ctx=imoex_ctx)
                      for t in tickers_ru[i:i + 6]],
                    return_exceptions=True,
                )
                await asyncio.sleep(0.5)
            cc = _select_universe(results, 10_000, 0)
            for c in cc:
                c["market"] = "ru"
            all_cards += cc

    algo_ctx = None
    if "algo" in needed:
        try:
            async with httpx.AsyncClient() as client:
                algo_ctx = await algo_mod.prepare(client, top_n=TOP_N, min_vol=MIN_VOL_USD_24H, win_h=win_h)
        except Exception as e:
            logger.warning("algo scan failed: %s", e)

    sent = await _deliver_html(all_cards, subscribers, chat_filters, now_ms, win_h,
                               on_signal, chat_best_only, stats, algo_ctx=algo_ctx)
    logger.info("Скан: карточек %d → отправлено %d", len(all_cards), sent)
    return sent


async def run_manual_scan(
    on_signal: Callable[[dict, list[int]], Awaitable[None]],
    chat_id: int,
    *,
    market: str = "crypto",
    strategy: str = "fbo",
    source: str = "auto",
    min_vol: float = MIN_VOL_USD_24H,
    n_inst: int = TOP_N,
    win_h: int = SCAN_LOOKBACK_H,
    no_night: bool = True,
    no_stocks: bool = True,
    template_name: str = "",
    filters: dict | None = None,
    on_progress: Callable[[dict], Awaitable[None]] | None = None,
    chat_best_only: Callable[[int], dict] | None = None,
) -> int:
    """Серверный скан с параметрами формы HTML + выбранный шаблон (тот же конвейер, что HTML).

    (Mini App сам сканирует в браузере и в Telegram ничего не шлёт; эта функция — для /scan-подобных
    вызовов и для теста паритета.) Окно = min(win_h, MAX_SIGNAL_AGE_H). Отправка: oldest → newest.
    """
    market = "ru" if market == "ru" else "crypto"
    strategy = strategy if strategy in ("fbo", "brk", "both") else "fbo"
    filters = dict(filters or {})
    tpl_name = (template_name or "").strip() or "manual"
    n_inst = max(1, min(int(n_inst or TOP_N), 600))
    min_vol = float(min_vol if min_vol is not None else MIN_VOL_USD_24H)
    # noNight / noStocks только для crypto (как HTML fldNight)
    use_no_night = bool(no_night) if market == "crypto" else False
    use_no_stocks = bool(no_stocks) if market == "crypto" else False

    chat_filter = {
        "markets": [market],
        "_multi": [{
            "_name": tpl_name,
            "_market": market,
            "_strategy": strategy,
            "filters": filters,
        }],
    }

    async def _progress(pct: float, message: str, **extra):
        if on_progress:
            payload = {"progress": round(pct, 1), "message": message, **extra}
            try:
                await on_progress(payload)
            except Exception:
                pass

    async def _p(done: int, total: int):
        await _progress(5 + 85 * done / max(1, total), f"{done}/{total}")

    now_ms = int(time.time() * 1000)
    lookback = max(1, min(int(win_h or MAX_SIGNAL_AGE_H), MAX_SIGNAL_AGE_H))
    await _progress(1, "загрузка тикеров…")
    all_cards: list[dict] = []
    if market == "crypto":
        async with httpx.AsyncClient() as client:
            all_cards = await _scan_crypto_html(
                client, n_inst=n_inst, min_vol=min_vol, win_h=lookback,
                no_night=use_no_night, no_stocks=use_no_stocks, source=source, progress=_p,
            )
    else:
        async with moex_client() as client:
            try:
                tickers_ru = await fetch_tickers_moex(client)
            except Exception as e:
                logger.warning("MOEX tickers failed (manual): %s", e)
                tickers_ru = []
            imoex_ctx = await fetch_imoex_ctx(client)
            # как HTML: turn ≥ minVol×0.5, топ nInst по обороту
            tickers_ru = [t for t in tickers_ru if float(t.get("turnover24h") or 0) >= min_vol * 0.5]
            tickers_ru.sort(key=lambda t: -float(t.get("turnover24h") or 0))
            tickers_ru = tickers_ru[:n_inst]
            total = max(1, len(tickers_ru))
            results = []
            for i in range(0, len(tickers_ru), 6):
                results += await asyncio.gather(
                    *[scan_one_moex(client, t, lookback_hours=lookback, no_night=use_no_night,
                                    imoex_ctx=imoex_ctx)
                      for t in tickers_ru[i:i + 6]],
                    return_exceptions=True,
                )
                await _p(min(i + 6, total), total)
                await asyncio.sleep(0.5)
            all_cards = _select_universe(results, n_inst, min_vol)
            for c in all_cards:
                c["market"] = "ru"

    await _progress(92, f"отбор по шаблону ({len(all_cards)} сигналов до фильтров)…")
    sent = await _deliver_html(
        all_cards, [chat_id], lambda _cid: chat_filter, now_ms, lookback,
        on_signal, chat_best_only,
    )
    await _progress(100, f"готово, отправлено {sent}", n_sent=sent)
    return sent



# ── Автопроверка исхода отправленных сигналов (тейк/стоп) ────────────────────
# Не влияет на сам скан — отдельный проход по открытым записям signal_history.
# Конвенция при "прокол в одном баре сразу через оба уровня": считаем стоп
# (консервативно, чтобы не завышать винрейт).

OUTCOME_HORIZON_MS = 7 * 24 * 3_600_000  # не решилось за 7 суток → expired


async def _fetch_outcome_bars(
    crypto_client: httpx.AsyncClient, moex_c: httpx.AsyncClient, row: dict,
) -> list[Bar]:
    ticker = row["ticker"]
    if row.get("market") == "ru":
        return await fetch_klines_moex_h1(moex_c, ticker, limit=200)
    return await fetch_klines_bybit(crypto_client, ticker, "60", limit=200)


def _resolve_outcome(row: dict, bars: list[Bar]) -> tuple[str, int, float] | None:
    """Вернуть (status, outcome_ts, outcome_price) если решилось, иначе None."""
    side = row["side"]
    stop, take = float(row["stop"]), float(row["take"])
    signal_ts = int(row["signal_ts"])
    relevant = [b for b in bars if b.ts >= signal_ts]
    for b in relevant:
        hit_take = (b.h >= take) if side == "LONG" else (b.l <= take)
        hit_stop = (b.l <= stop) if side == "LONG" else (b.h >= stop)
        if hit_take and hit_stop:
            return ("loss", b.ts, stop)  # оба в одном баре — консервативно стоп
        if hit_stop:
            return ("loss", b.ts, stop)
        if hit_take:
            return ("win", b.ts, take)
    return None


async def check_signal_outcomes(limit: int = 300) -> dict:
    """Проверить открытые сигналы из signal_history, обновить исход. Вызывать раз в скан."""
    rows = storage.get_open_signal_rows(limit=limit)
    if not rows:
        return {"checked": 0, "resolved": 0}

    resolved = 0
    now_ms = int(time.time() * 1000)
    async with httpx.AsyncClient() as crypto_client, moex_client() as moex_c:
        for row in rows:
            try:
                bars = await _fetch_outcome_bars(crypto_client, moex_c, row)
            except Exception as e:
                logger.debug("outcome fetch failed %s: %s", row.get("ticker"), e)
                continue
            if not bars:
                continue
            outcome = _resolve_outcome(row, bars)
            if outcome:
                status, ts, price = outcome
                storage.update_signal_outcome(row["id"], status, ts, price)
                resolved += 1
            elif now_ms - int(row["signal_ts"]) > OUTCOME_HORIZON_MS:
                last = bars[-1]
                storage.update_signal_outcome(row["id"], "expired", last.ts, last.c)
                resolved += 1
    return {"checked": len(rows), "resolved": resolved}
