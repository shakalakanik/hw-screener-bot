"""moex.py — клиент MOEX ISS (TQBR) для скринера.

Порт логики из HTML MOEX/issFetch/issCandles/issBars/m5.
Опционально: MOEX_PROXY или HTTPS_PROXY — httpx proxy URL (если ISS режет non-RU IP).
По умолчанию — прямой доступ (с Railway EU ISS отвечает 200).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from hwv1 import Bar

logger = logging.getLogger(__name__)

ISS = "https://iss.moex.com/iss"
MSK_MS = 3 * 3_600_000
ISS_DELAY_MS = 15 * 60_000

# HTML MKT_STATE.ru.vol = '10' → 10 млн ₽/сут
MOEX_MIN_TURN = float(os.getenv("MOEX_MIN_TURN") or 10_000_000)
MOEX_TOP_N = int(os.getenv("MOEX_TOP_N") or 300)


def _proxy_url() -> str | None:
    return (os.getenv("MOEX_PROXY") or os.getenv("HTTPS_PROXY") or "").strip() or None


def moex_client(**kwargs) -> httpx.AsyncClient:
    """httpx client; proxy from MOEX_PROXY / HTTPS_PROXY if set."""
    proxy = _proxy_url()
    opts: dict[str, Any] = {"timeout": 30.0, **kwargs}
    if proxy:
        opts["proxy"] = proxy
    return httpx.AsyncClient(**opts)


async def iss_fetch(client: httpx.AsyncClient, url: str, tries: int = 3) -> dict:
    """Как HTML issFetch: 3 попытки с backoff 300*(i+1) мс."""
    last_err: Exception | None = None
    for i in range(tries):
        try:
            r = await client.get(url)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
            return r.json()
        except Exception as e:
            last_err = e
            if i < tries - 1:
                await asyncio.sleep(0.3 * (i + 1))
    raise last_err or RuntimeError("iss_fetch failed")


def msk_to_utc_ms(s: str) -> int:
    """'YYYY-MM-DD HH:MM:SS' (MSK wall) → UTC epoch ms. Как HTML mskToUtc."""
    parts = s.split(" ")
    d = parts[0]
    t = parts[1] if len(parts) > 1 else "00:00:00"
    y, m, dd = map(int, d.split("-"))
    hh, mi, ss = (list(map(int, t.split(":"))) + [0, 0, 0])[:3]
    utc_naive = datetime(y, m - 1 + 1, dd, hh or 0, mi or 0, ss or 0, tzinfo=timezone.utc)
    # Date.UTC(y,m-1,dd,...) - MSK → subtract 3h from the MSK wall clock treated as UTC
    return int(utc_naive.timestamp() * 1000) - MSK_MS


def msk_str(ts_ms: int) -> str:
    """UTC ms → 'YYYY-MM-DD HH:MM:SS' in MSK wall. Как HTML mskStr."""
    return datetime.fromtimestamp((ts_ms + MSK_MS) / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


async def iss_candles(
    client: httpx.AsyncClient,
    path: str,
    interval: int,
    from_ts: int,
    till_ts: int | None = None,
    max_pages: int = 4,
) -> list[list]:
    """Свечи ISS: [open, close, high, low, value, volume, begin, end], по 500/стр."""
    out: list[list] = []
    start = 0
    for _ in range(max_pages):
        u = (
            f"{ISS}{path}/candles.json?iss.meta=off&interval={interval}"
            f"&from={quote(msk_str(from_ts))}"
        )
        if till_ts is not None:
            u += f"&till={quote(msk_str(till_ts))}"
        u += f"&start={start}"
        d = await iss_fetch(client, u)
        rows = (d.get("candles") or {}).get("data") or []
        out.extend(rows)
        if len(rows) < 500:
            break
        start += len(rows)
    return out


def iss_bars(rows: list[list], interval: str, now_ms: int) -> list[Bar]:
    """interval '1d' | '1h'. Дневка → trade-date 03:00 MSK (как HTML)."""
    seen: dict[int, Bar] = {}
    for r in rows:
        begin = r[6]
        if interval == "1d":
            ts = msk_to_utc_ms(str(begin)[:10] + " 03:00:00")
            dur = 86_400_000
        else:
            ts = msk_to_utc_ms(str(begin))
            dur = 3_600_000
        confirmed = (ts + dur + ISS_DELAY_MS) <= now_ms
        seen[ts] = Bar(
            ts=ts,
            o=float(r[0] or 0),
            h=float(r[2] or 0),
            l=float(r[3] or 0),
            c=float(r[1] or 0),
            vol_quote=float(r[4] or 0),
            confirmed=confirmed,
        )
    return sorted(seen.values(), key=lambda b: b.ts)


def _h1_to_h4(h1: list[Bar]) -> list[Bar]:
    """Синтез H4 из H1 (на случай если когда-нибудь понадобится; evaluate PIT H4 игнорит)."""
    buckets: dict[int, list[Bar]] = {}
    for b in h1:
        key = (b.ts // (4 * 3_600_000)) * (4 * 3_600_000)
        buckets.setdefault(key, []).append(b)
    out: list[Bar] = []
    for ts, group in sorted(buckets.items()):
        out.append(Bar(
            ts=ts,
            o=group[0].o,
            h=max(x.h for x in group),
            l=min(x.l for x in group),
            c=group[-1].c,
            vol_quote=sum(x.vol_quote for x in group),
            confirmed=group[-1].confirmed,
        ))
    return out


async def fetch_tickers_moex(client: httpx.AsyncClient) -> list[dict]:
    """TQBR EQIN: turn=max(VALTODAY, prev VALUE), last. Shape как crypto tickers."""
    u = (
        f"{ISS}/engines/stock/markets/shares/boards/TQBR/securities.json"
        f"?iss.meta=off&iss.only=securities,marketdata"
        f"&securities.columns=SECID,PREVPRICE,INSTRID"
        f"&marketdata.columns=SECID,VALTODAY,LAST"
    )
    d = await iss_fetch(client, u)

    prev: dict[str, float] = {}
    try:
        for s in range(0, 3000, 100):
            hj = await iss_fetch(
                client,
                f"{ISS}/history/engines/stock/markets/shares/boards/TQBR/securities.json"
                f"?iss.meta=off&history.columns=SECID,VALUE&start={s}",
                tries=2,
            )
            hist = (hj.get("history") or {}).get("data") or []
            for r in hist:
                prev[r[0]] = float(r[1] or 0)
            cursor = ((hj.get("history.cursor") or {}).get("data") or [[0, 0, 0]])[0]
            if s + cursor[2] >= cursor[1] or not hist:
                break
    except Exception as e:
        logger.warning("MOEX history turnover skip: %s", e)

    px: dict[str, float] = {}
    eq: set[str] = set()
    for r in (d.get("securities") or {}).get("data") or []:
        px[r[0]] = float(r[1] or 0)
        if r[2] == "EQIN":
            eq.add(r[0])

    out: list[dict] = []
    for r in (d.get("marketdata") or {}).get("data") or []:
        sym = r[0]
        if sym not in eq:
            continue
        turn = max(float(r[1] or 0), prev.get(sym, 0.0))
        last = float(r[2] or 0) or px.get(sym, 0.0)
        if turn < MOEX_MIN_TURN or last <= 0:
            continue
        out.append({
            "symbol": sym,
            "inst_id": sym,
            "lastPrice": last,
            "bid1Price": last,
            "ask1Price": last,
            "turnover24h": turn,  # ₽
            "exchange": "moex",
            "market": "ru",
        })
    out.sort(key=lambda t: t["turnover24h"], reverse=True)
    return out[:MOEX_TOP_N]


async def fetch_klines_moex_d1(
    client: httpx.AsyncClient, sym: str, limit: int = 200
) -> list[Bar]:
    now = int(time.time() * 1000)
    path = f"/engines/stock/markets/shares/boards/TQBR/securities/{sym}"
    rows = await iss_candles(
        client, path, 24, now - int(limit * 1.5) * 86_400_000, None, 4
    )
    return iss_bars(rows, "1d", now)


async def fetch_klines_moex_h1(
    client: httpx.AsyncClient, sym: str, limit: int = 400
) -> list[Bar]:
    now = int(time.time() * 1000)
    path = f"/engines/stock/markets/shares/boards/TQBR/securities/{sym}"
    pages = min(40, int(limit * 0.75 / 500) + 2)
    rows = await iss_candles(client, path, 60, now - limit * 3_600_000, None, pages)
    return iss_bars(rows, "1h", now)


async def fetch_klines_moex_m5(
    client: httpx.AsyncClient, sym: str, limit: int = 300, end_ts: int | None = None
) -> list[Bar]:
    """Агрегация из минуток, как HTML MOEX.m5."""
    path = f"/engines/stock/markets/shares/boards/TQBR/securities/{sym}"
    end = end_ts or int(time.time() * 1000)
    rows = await iss_candles(client, path, 1, end - 2 * 86_400_000, end, 8)
    if len(rows) < limit * 5 * 0.6:
        rows = await iss_candles(client, path, 1, end - 5 * 86_400_000, end, 16)
    buckets: dict[int, Bar] = {}
    for r in rows:
        t = (msk_to_utc_ms(str(r[6])) // 300_000) * 300_000
        o, h, l, c = float(r[0] or 0), float(r[2] or 0), float(r[3] or 0), float(r[1] or 0)
        if t not in buckets:
            buckets[t] = Bar(ts=t, o=o, h=h, l=l, c=c, vol_quote=0.0, confirmed=True)
        else:
            b = buckets[t]
            b.h = max(b.h, h)
            b.l = min(b.l, l)
            b.c = c
    bars = sorted(buckets.values(), key=lambda b: b.ts)
    return bars[-limit:]


async def scan_one_moex(
    client: httpx.AsyncClient,
    ticker: dict,
    lookback_hours: int = 12,
) -> list[dict]:
    """Скан одной бумаги MOEX → карточки с market=ru."""
    from hwv1 import evaluate

    symbol = ticker["symbol"]
    try:
        last = float(ticker.get("lastPrice", 0))
        bid = float(ticker.get("bid1Price", 0) or 0)
        ask = float(ticker.get("ask1Price", 0) or 0)
        vol24 = float(ticker.get("turnover24h", 0))

        d1, h1, m5 = await asyncio.gather(
            fetch_klines_moex_d1(client, symbol, 200),
            fetch_klines_moex_h1(client, symbol, 400),
            fetch_klines_moex_m5(client, symbol, 300),
        )
        # H4: evaluate PIT строит уровни с пустым H4; синтез на всякий случай
        h4 = _h1_to_h4(h1)

        # vol в ₽; PARAMS.vol_usd_min=1e6 — при MOEX_MIN_TURN≥1e6 уже отфильтровано.
        result = evaluate(
            symbol, last, bid, ask, d1, h4, h1, m5, vol24,
            fbo_threshold=0.20, no_night=True, lookback_hours=lookback_hours,
        )
        cards = result.get("cards", [])
        for c in cards:
            c["market"] = "ru"
            c["exchange"] = "moex"
        return cards
    except Exception as e:
        logger.debug("scan_one_moex %s error: %s", symbol, e)
        return []
