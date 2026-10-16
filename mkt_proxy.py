"""Прокси рыночных данных для Mini App: телефон ходит только на наш сервер, сервер — к биржам.

Только белый список: api.bybit.com /v5/market/{kline,tickers}, www.okx.com /api/v5/market/{candles,history-candles,tickers},
iss.moex.com /iss/{engines,history/engines}/stock/markets/{shares,index}/…json. Только GET, только авторизованный
пользователь Mini App. Таймаут, небольшой кэш (TTL 15–300 с, ≤40 МБ), лимит одновременных запросов на биржу.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict

import httpx
from aiohttp import web

logger = logging.getLogger(__name__)

UPSTREAMS = {
    "bybit": ("https://api.bybit.com", re.compile(r"^/v5/market/(kline|tickers)$"), 8),
    "okx": ("https://www.okx.com", re.compile(r"^/api/v5/market/(candles|history-candles|tickers)$"), 4),
    "iss": ("https://iss.moex.com", re.compile(r"^/iss/(engines|history/engines)/stock/markets/(shares|index)"
                                               r"(/[A-Za-z0-9_.\-]+){1,8}\.json$"), 6),
}
TIMEOUT_S = 12.0
MAX_QUERY = 600
CACHE_MAX_BYTES = 40 * 1024 * 1024

_sems = {k: asyncio.Semaphore(v[2]) for k, v in UPSTREAMS.items()}
_client: httpx.AsyncClient | None = None
_cache: "OrderedDict[str, tuple[float, int, bytes, str]]" = OrderedDict()
_cache_bytes = 0
_inflight: dict[str, asyncio.Future] = {}
stats = {"req": 0, "hit": 0, "up": 0, "bytes_out": 0, "err": 0}


def _ttl(up: str, path: str, query: str) -> float:
    if "tickers" in path or "marketdata" in query:
        return 15.0
    if path.startswith("/iss/history/"):
        return 300.0
    return 25.0


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT_S, headers={"User-Agent": "hw-miniapp-proxy/1"},
                                    limits=httpx.Limits(max_connections=24, max_keepalive_connections=12))
    return _client


def _cache_put(key: str, val: tuple) -> None:
    global _cache_bytes
    old = _cache.pop(key, None)
    if old:
        _cache_bytes -= len(old[2])
    _cache[key] = val
    _cache_bytes += len(val[2])
    while _cache_bytes > CACHE_MAX_BYTES and _cache:
        _, v = _cache.popitem(last=False)
        _cache_bytes -= len(v[2])


async def _fetch(up: str, url: str) -> tuple[int, bytes, str]:
    async with _sems[up]:
        stats["up"] += 1
        r = await _client_get().get(url)
        return r.status_code, r.content, r.headers.get("content-type", "application/json")


async def handle(request: web.Request) -> web.Response:
    from webapp_server import require_user   # только пользователи Mini App — не открытый прокси
    require_user(request)
    up = request.match_info.get("up", "")
    path = "/" + request.match_info.get("path", "")
    if up not in UPSTREAMS:
        raise web.HTTPNotFound(text="unknown upstream")
    base, rx, _ = UPSTREAMS[up]
    if not rx.match(path) or ".." in path:
        raise web.HTTPForbidden(text="path not allowed")
    # query без авторизационных параметров Mini App
    q = [(k, v) for k, v in request.rel_url.query.items() if k not in ("initData", "token", "debug_user_id")]
    query = str(httpx.QueryParams(q))
    if len(query) > MAX_QUERY:
        raise web.HTTPBadRequest(text="query too long")
    url = base + path + ("?" + query if query else "")
    stats["req"] += 1
    now = time.time()
    hit = _cache.get(url)
    if hit and hit[0] > now:
        stats["hit"] += 1
        _cache.move_to_end(url)
        status, body, ct = hit[1], hit[2], hit[3]
    else:
        fut = _inflight.get(url)
        if fut is None:   # одинаковые одновременные запросы — один поход к бирже
            fut = asyncio.get_running_loop().create_future()
            _inflight[url] = fut
            try:
                res = await _fetch(up, url)
                fut.set_result(res)
            except Exception as e:
                stats["err"] += 1
                logger.info("mkt proxy %s: %s", url[:160], e)
                fut.set_result((502, b'{"error":"upstream"}', "application/json"))
            finally:
                _inflight.pop(url, None)
        status, body, ct = await fut
        if status == 200:
            _cache_put(url, (now + _ttl(up, path, query), status, body, ct))
    stats["bytes_out"] += len(body)
    resp = web.Response(status=status, body=body, content_type=ct.split(";")[0],
                        headers={"Cache-Control": "no-store"})
    resp.enable_compression()   # gzip к телефону — экономит трафик Railway
    return resp


def setup(app: web.Application) -> None:
    app.router.add_get("/api/mkt/{up}/{path:.*}", handle)

    async def _close(_app):
        global _client
        if _client is not None:
            await _client.aclose()
            _client = None
    app.on_cleanup.append(_close)
