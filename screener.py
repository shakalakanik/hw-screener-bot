"""screener.py — тянет данные с Bybit/OKX (auto) и MOEX ISS (ru), прогоняет hwv1.evaluate."""
import asyncio
import logging
import os
import time
from typing import Callable, Awaitable

import httpx

from hwv1 import Bar, evaluate
import storage
from moex import (
    fetch_tickers_moex,
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


async def _get_bybit(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    r = await client.get(f"{BYBIT_BASE}{path}", params=params, timeout=15)
    body = r.text or ""
    if r.status_code == 403 or (
        "CloudFront" in body and "block" in body.lower()
    ):
        raise BybitBlocked(f"Bybit HTTP {r.status_code}: geo/CloudFront block")
    r.raise_for_status()
    return r.json()


async def _get_okx(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    r = await client.get(f"{OKX_BASE}{path}", params=params, timeout=15)
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


async def fetch_tickers_bybit(client: httpx.AsyncClient) -> list[dict]:
    """Топ-N USDT-perp Bybit по обороту за сутки."""
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
        and float(t.get("turnover24h", 0) or 0) >= MIN_VOL_USD_24H
    ]
    usdt.sort(key=lambda t: t["turnover24h"], reverse=True)
    return usdt[:TOP_N]


async def fetch_tickers_okx(client: httpx.AsyncClient) -> list[dict]:
    """Топ-N USDT-SWAP OKX. Оборот: volCcy24h * last (как HTML OKX.tickers)."""
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
        if turn < MIN_VOL_USD_24H:
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
    return out[:TOP_N]


async def fetch_tickers(client: httpx.AsyncClient) -> list[dict]:
    """Выбрать источник по DATA_SOURCE; в auto — Bybit, при 403/CloudFront → OKX."""
    global _active_exchange
    pref = DATA_SOURCE

    if pref == "okx":
        _active_exchange = "okx"
        return await fetch_tickers_okx(client)

    if pref == "bybit":
        _active_exchange = "bybit"
        return await fetch_tickers_bybit(client)

    # auto: Bybit → OKX (HTML pickSource)
    try:
        tickers = await fetch_tickers_bybit(client)
        if tickers:
            _active_exchange = "bybit"
            return tickers
    except BybitBlocked as e:
        logger.warning("Bybit недоступен (%s) — переключаюсь на OKX", e)
    except Exception as e:
        # В auto любая ошибка тикеров Bybit → пробуем OKX (как HTML catch)
        logger.warning("Bybit tickers error (%s) — пробую OKX", e)

    _active_exchange = "okx"
    return await fetch_tickers_okx(client)


async def fetch_klines_bybit(
    client: httpx.AsyncClient, symbol: str, interval: str, limit: int = LIMIT
) -> list[Bar]:
    data = await _get_bybit(
        client,
        "/v5/market/kline",
        {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
    )
    raw = data.get("result", {}).get("list", [])
    bars = []
    for row in reversed(raw):  # Bybit: новейшие первыми
        ts, o, h, l, c, vol, vol_q = (row + ["0"] * 7)[:7]
        bars.append(Bar(
            ts=int(ts),  # ms
            o=float(o), h=float(h), l=float(l), c=float(c),
            vol_quote=float(vol_q),
            confirmed=True,
        ))
    if bars:
        bars[-1].confirmed = False
    return bars


async def fetch_klines_okx(
    client: httpx.AsyncClient, inst_id: str, interval: str, limit: int = LIMIT
) -> list[Bar]:
    """OKX candles; ts в ms. bar: 1Dutc|4H|1H|5m. vol_quote = k[7] ?? k[6] (HTML)."""
    bar = OKX_BAR_MAP.get(interval, interval)
    # OKX отдаёт до 300 за запрос, новейшие первыми
    data = await _get_okx(
        client,
        "/api/v5/market/candles",
        {"instId": inst_id, "bar": bar, "limit": min(limit, 300)},
    )
    raw = data.get("data") or []
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
    if bars:
        bars[-1].confirmed = False
    return bars


async def fetch_klines(
    client: httpx.AsyncClient, symbol: str, interval: str, limit: int = LIMIT,
    inst_id: str | None = None,
) -> list[Bar]:
    if _active_exchange == "okx":
        return await fetch_klines_okx(client, inst_id or symbol, interval, limit)
    return await fetch_klines_bybit(client, symbol, interval, limit)


async def scan_one(client: httpx.AsyncClient, ticker: dict) -> list[dict]:
    """Проверить один инструмент, вернуть список карточек (может быть пустым)."""
    symbol = ticker["symbol"]
    inst_id = ticker.get("inst_id") or symbol

    try:
        last = float(ticker.get("lastPrice", 0))
        bid = float(ticker.get("bid1Price", 0) or 0)
        ask = float(ticker.get("ask1Price", 0) or 0)
        vol24 = float(ticker.get("turnover24h", 0))

        d1, h4, h1, m5 = await asyncio.gather(
            fetch_klines(client, symbol, "D", inst_id=inst_id),
            fetch_klines(client, symbol, "240", inst_id=inst_id),
            fetch_klines(client, symbol, "60", inst_id=inst_id),
            fetch_klines(client, symbol, "5", inst_id=inst_id),
        )

        # Порог модели для FBO — пол HTML DEF.thr=0.20 на этапе скана; более строгий thr
        # шаблона досчитывается в match_filter/_matches_single.
        # no_night=True — соответствует чекбоксу «без ночи» в HTML, включённому по умолчанию.
        result = evaluate(
            symbol, last, bid, ask, d1, h4, h1, m5, vol24,
            fbo_threshold=0.20, no_night=True, lookback_hours=SCAN_LOOKBACK_H,
        )
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
        p_prob = prev["card"].get("prob") or 0
        c_prob = card.get("prob") or 0
        # первый по времени; при равном ts — выше prob, затем base
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
                -(it["card"].get("prob") or 0),
                _card_base(it["card"]),
            )
        )
        keep.extend(group[:cap])
    # сохранить относительный порядок исходного списка
    keep_ids = set(id(x) for x in keep)
    return [p for p in pending if id(p) in keep_ids]



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
            tpl_mkts = {t.get("_market", "crypto") for t in multi}
            for m in markets:
                if m in tpl_mkts:
                    needed.add(m)
        else:
            needed.update(markets)
    return needed


async def run_scan(
    on_signal: Callable[[dict, list[int]], Awaitable[None]],
    subscribers: list[int],
    chat_filters: Callable[[int], dict],
    incremental: bool = True,
) -> int:
    """Один полный скан. on_signal вызывается для каждого прошедшего сигнала.

    Ночной фильтр (23:00–09:00 МСК) применяется ВНУТРИ hwv1.evaluate() к каждому
    часу-кандидату отдельно, как в HTML (opt.noNight) — не блокирует скан целиком.

    incremental=True (autoscan и /scan): только signal_ts новее watermark + age≤12h.
    Никогда не отправляем карточки старше MAX_SIGNAL_AGE_H часов.
    Возвращает число отправленных карточек (вызовов on_signal).
    """
    now_ms = int(time.time() * 1000)
    min_ts = 0
    if incremental:
        wm = storage.get_send_watermark_ms()
        if wm is None:
            # Cold start: не дампить 12ч истории — только самый свежий час.
            min_ts = now_ms - COLD_WATERMARK_LOOKBACK_MS
            storage.set_send_watermark_ms(min_ts)
            logger.info(
                "Watermark cold-start → %d (только сигналы новее ~1ч)",
                min_ts,
            )
        else:
            min_ts = int(wm)
        # Дополнительно: watermark не может быть древнее окна age (защита от битых значений)
        floor = now_ms - MAX_SIGNAL_AGE_MS
        if min_ts < floor:
            min_ts = floor

    needed = _needed_markets(subscribers, chat_filters)
    want_crypto = "crypto" in needed
    want_ru = "ru" in needed
    if not want_crypto and not want_ru:
        logger.info("Скан пропущен: нет рынков с активными шаблонами")
        return 0

    pending: list[dict] = []
    skipped_age = 0
    skipped_wm = 0
    skipped_dup = 0

    async def _ingest(cards_list):
        nonlocal skipped_age, skipped_wm, skipped_dup
        for cards in cards_list:
            if isinstance(cards, Exception):
                continue
            for card in cards:
                card.setdefault("market", "crypto")
                ticker = card["ticker"]
                side = card["side"]
                level = card["level"]
                strategy = card.get("strategy", "brk")
                signal_ts = int(card.get("signal_ts") or 0)

                if not signal_ts or (now_ms - signal_ts) > MAX_SIGNAL_AGE_MS:
                    skipped_age += 1
                    continue
                if incremental and signal_ts <= min_ts:
                    skipped_wm += 1
                    continue
                if storage.is_duplicate(ticker, side, level, strategy):
                    skipped_dup += 1
                    continue

                by_template: dict[str, list[int]] = {}
                for chat_id in subscribers:
                    f = chat_filters(chat_id)
                    tpl_name = match_filter(card, f)
                    if tpl_name is not None:
                        by_template.setdefault(tpl_name, []).append(chat_id)
                if by_template:
                    pending.append({"card": card, "by_template": by_template})

    if want_crypto:
        async with httpx.AsyncClient() as client:
            tickers = await fetch_tickers(client)
            ex_label = "OKX" if _active_exchange == "okx" else "Bybit"
            logger.info(
                "Скан crypto: %d через %s (age≤%dh, incremental=%s, min_ts=%s)",
                len(tickers), ex_label, MAX_SIGNAL_AGE_H, incremental, min_ts or "-",
            )
            batch_size = 20
            for i in range(0, len(tickers), batch_size):
                batch = tickers[i: i + batch_size]
                results = await asyncio.gather(
                    *[scan_one(client, t) for t in batch], return_exceptions=True
                )
                await _ingest(results)
                await asyncio.sleep(0.3)

    if want_ru:
        async with moex_client() as client:
            try:
                tickers_ru = await fetch_tickers_moex(client)
            except Exception as e:
                logger.warning("MOEX tickers failed: %s", e)
                tickers_ru = []
            logger.info(
                "Скан MOEX: %d бумаг (min_turn≥%.0f₽, age≤%dh, incremental=%s)",
                len(tickers_ru), MOEX_MIN_TURN, MAX_SIGNAL_AGE_H, incremental,
            )
            # ISS rate-limit: меньше параллелизма, чем крипта
            batch_size = 6
            for i in range(0, len(tickers_ru), batch_size):
                batch = tickers_ru[i: i + batch_size]
                results = await asyncio.gather(
                    *[scan_one_moex(client, t, lookback_hours=SCAN_LOOKBACK_H) for t in batch],
                    return_exceptions=True,
                )
                await _ingest(results)
                await asyncio.sleep(0.5)

    # HTML-like post-filters перед отправкой (в рамках этого скана)
    before = len(pending)
    pending = _apply_coin_limit(pending)
    pending = _cap_cluster(pending)
    if len(pending) != before:
        logger.info(
            "Post-filters: %d → %d (coin≤1/%dh, cap≤%d/(hour,side))",
            before, len(pending), COIN_WINDOW_H, CAP_CLUSTER,
        )

    logger.info(
        "Скан фильтры: age=%d wm=%d dup=%d → pending=%d",
        skipped_age, skipped_wm, skipped_dup, len(pending),
    )

    sent_count = 0
    max_sent_ts = 0
    for item in pending:
        card = item["card"]
        by_template = item["by_template"]
        ticker = card["ticker"]
        side = card["side"]
        level = card["level"]
        strategy = card.get("strategy", "brk")
        signal_ts = int(card.get("signal_ts") or 0)
        # Повторная проверка age непосредственно перед отправкой
        if not signal_ts or (now_ms - signal_ts) > MAX_SIGNAL_AGE_MS:
            continue
        sent_any = False
        for tpl_name, chat_ids in by_template.items():
            card_out = {**card, "matched_template": tpl_name}
            await on_signal(card_out, chat_ids)
            sent_any = True
            sent_count += 1
        if sent_any:
            storage.mark_sent(ticker, side, level, strategy)
            if signal_ts > max_sent_ts:
                max_sent_ts = signal_ts

    if incremental:
        # Поднимаем watermark по факту отправки и/или сдвигаем пол (now−1ч),
        # чтобы пустые сканы не копили растущий бэклог «ещё не отправленных».
        advance_to = max(max_sent_ts, now_ms - COLD_WATERMARK_LOOKBACK_MS)
        new_wm = storage.bump_send_watermark_ms(advance_to)
        logger.info("Watermark → %d (sent=%d)", new_wm, sent_count)

    return sent_count
