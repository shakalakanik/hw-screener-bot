"""screener.py — тянет данные с Bybit, прогоняет hwv1.evaluate, возвращает карточки."""
import asyncio
import logging
import time
from typing import Callable, Awaitable

import httpx

from hwv1 import Bar, evaluate
import storage

logger = logging.getLogger(__name__)

BYBIT_BASE = "https://api.bybit.com"

# Ночной фильтр: 23:00–09:00 UTC+3 (Vilnius) → 20:00–06:00 UTC
NIGHT_START_UTC = 20   # 23:00 Vilnius
NIGHT_END_UTC = 6      # 09:00 Vilnius

TOP30_MCAP = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "USDC", "ADA", "AVAX", "DOGE",
    "TRX", "DOT", "MATIC", "LINK", "SHIB", "TON", "ICP", "DAI", "LTC",
    "BCH", "UNI", "ATOM", "XLM", "ETC", "APT", "NEAR", "FIL", "VET",
    "HBAR", "ARB", "OP",
}

INTERVAL_MAP = {"D": "D", "H4": "240", "H1": "60", "M5": "5"}
LIMIT = 200  # баров за запрос


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    r = await client.get(f"{BYBIT_BASE}{path}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


async def fetch_tickers(client: httpx.AsyncClient) -> list[dict]:
    """Топ-200 USDT-perp по объёму."""
    data = await _get(client, "/v5/market/tickers", {"category": "linear"})
    items = data.get("result", {}).get("list", [])
    usdt = [t for t in items if t["symbol"].endswith("USDT")]
    usdt.sort(key=lambda t: float(t.get("turnover24h", 0)), reverse=True)
    return usdt[:200]


async def fetch_klines(
    client: httpx.AsyncClient, symbol: str, interval: str, limit: int = LIMIT
) -> list[Bar]:
    data = await _get(
        client,
        "/v5/market/kline",
        {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
    )
    raw = data.get("result", {}).get("list", [])
    bars = []
    for row in reversed(raw):  # Bybit возвращает новейшие первыми
        ts, o, h, l, c, vol, vol_q = (row + ["0"] * 7)[:7]
        bars.append(Bar(
            ts=int(ts) // 1000,
            o=float(o), h=float(h), l=float(l), c=float(c),
            vol_quote=float(vol_q),
            confirmed=True,
        ))
    if bars:
        bars[-1].confirmed = False  # последняя свеча ещё не закрыта
    return bars


def _is_night() -> bool:
    hour = time.gmtime().tm_hour
    if NIGHT_START_UTC < NIGHT_END_UTC:
        return NIGHT_START_UTC <= hour < NIGHT_END_UTC
    return hour >= NIGHT_START_UTC or hour < NIGHT_END_UTC


async def scan_one(client: httpx.AsyncClient, ticker: dict) -> list[dict]:
    """Проверить один инструмент, вернуть список карточек (может быть пустым)."""
    symbol = ticker["symbol"]
    base = symbol.replace("USDT", "")
    if base in TOP30_MCAP:
        return []

    try:
        last = float(ticker.get("lastPrice", 0))
        bid = float(ticker.get("bid1Price", 0) or 0)
        ask = float(ticker.get("ask1Price", 0) or 0)
        vol24 = float(ticker.get("turnover24h", 0))

        d1, h4, h1, m5 = await asyncio.gather(
            fetch_klines(client, symbol, "D"),
            fetch_klines(client, symbol, "240"),
            fetch_klines(client, symbol, "60"),
            fetch_klines(client, symbol, "5"),
        )

        result = evaluate(symbol, last, bid, ask, d1, h4, h1, m5, vol24)
        return result.get("cards", [])
    except Exception as e:
        logger.debug("scan_one %s error: %s", symbol, e)
        return []


def apply_filter(card: dict, f: dict) -> bool:
    """Проверить карточку против фильтра пользователя."""
    if card["side"] not in f.get("sides", ["LONG", "SHORT"]):
        return False
    if card["strength"] < f.get("strength_min", 3):
        return False
    if card["dist_atr"] > f.get("dist_atr_max", 0.5):
        return False
    if f.get("bias_filter"):
        bias = card.get("d1_bias", "flat")
        if card["side"] == "LONG" and bias != "up":
            return False
        if card["side"] == "SHORT" and bias != "down":
            return False
    return True


async def run_scan(
    on_signal: Callable[[dict, list[int]], Awaitable[None]],
    subscribers: list[int],
    chat_filters: Callable[[int], dict],
):
    """Один полный скан. on_signal вызывается для каждого прошедшего сигнала."""
    if _is_night():
        logger.info("Ночное время, скан пропущен")
        return

    async with httpx.AsyncClient() as client:
        tickers = await fetch_tickers(client)
        logger.info("Скан: %d инструментов", len(tickers))

        # Пакетами по 20, чтобы не перегружать Bybit
        batch_size = 20
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i: i + batch_size]
            results = await asyncio.gather(
                *[scan_one(client, t) for t in batch], return_exceptions=True
            )
            for cards in results:
                if isinstance(cards, Exception):
                    continue
                for card in cards:
                    ticker = card["ticker"]
                    side = card["side"]
                    level = card["level"]

                    if storage.is_duplicate(ticker, side, level):
                        continue

                    # Определяем, каким подписчикам слать
                    targets = []
                    for chat_id in subscribers:
                        f = chat_filters(chat_id)
                        if apply_filter(card, f):
                            targets.append(chat_id)

                    if targets:
                        storage.mark_sent(ticker, side, level)
                        await on_signal(card, targets)

            await asyncio.sleep(0.3)  # пауза между пакетами
