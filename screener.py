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

        # Порог модели для FBO берём минимальным (0.15) на этапе скана: конкретный порог
        # каждого активного шаблона (sc_thr) досчитывается позже в match_filter/_matches_single —
        # так один скан обслуживает все шаблоны с разными порогами, а не только дефолтный.
        result = evaluate(symbol, last, bid, ask, d1, h4, h1, m5, vol24, fbo_threshold=0.15)
        return result.get("cards", [])
    except Exception as e:
        logger.debug("scan_one %s error: %s", symbol, e)
        return []


def _matches_single(card: dict, f: dict) -> bool:
    """
    Проверить карточку против одного набора фильтров.
    Понимает два формата:
      1) старый упрощённый (sides, strength_min, dist_atr_max, bias_filter)
      2) реальные ключи шаблона из HTML (thr, stop, str, cross, dist, age,
         maxrisk, bias, side, kind — без префикса sc_/bt_, как сохраняет HTML)
    Значения отсутствующих ключей — «авто» (фильтр выключен), так же как в HTML.
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

    # ── реальные ключи шаблона HTML (FIDS) ──
    # Порог модели (thr) для FBO уже применён внутри evaluate_fbo при генерации карточки
    # (там используется MODEL.threshold по умолчанию); здесь донасчитываем более строгий
    # порог, если шаблон просит выше дефолтного.
    if "thr" in f and f["thr"] not in (None, "auto"):
        try:
            thr = float(f["thr"])
            if card.get("strategy") == "fbo" and (card.get("prob") or 0) < thr:
                return False
        except (TypeError, ValueError):
            pass

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
            tpl_strategy = tpl.get("_strategy", "both")
            if tpl_strategy != "both" and tpl_strategy != card_strategy:
                continue
            if _matches_single(card, tpl.get("filters", tpl)):
                return name
        return None

    # Старый формат (без шаблонов из HTML)
    return "" if _matches_single(card, f) else None


# Оставлен для обратной совместимости, если где-то ещё вызывается напрямую
def apply_filter(card: dict, f: dict) -> bool:
    return match_filter(card, f) is not None


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
                    card.setdefault("market", "crypto")   # весь Bybit-скан — крипта
                    ticker = card["ticker"]
                    side = card["side"]
                    level = card["level"]
                    strategy = card.get("strategy", "brk")

                    if storage.is_duplicate(ticker, side, level, strategy):
                        continue

                    # Каждому подписчику мог совпасть свой шаблон — группируем по имени шаблона
                    # чтобы отправить сигнал один раз на группу с одинаковым совпадением,
                    # но с точным указанием какого шаблона это касается у каждого.
                    sent_any = False
                    by_template: dict[str, list[int]] = {}
                    for chat_id in subscribers:
                        f = chat_filters(chat_id)
                        tpl_name = match_filter(card, f)
                        if tpl_name is not None:
                            by_template.setdefault(tpl_name, []).append(chat_id)

                    for tpl_name, chat_ids in by_template.items():
                        card_out = {**card, "matched_template": tpl_name}
                        await on_signal(card_out, chat_ids)
                        sent_any = True

                    if sent_any:
                        storage.mark_sent(ticker, side, level, strategy)

            await asyncio.sleep(0.3)  # пауза между пакетами
