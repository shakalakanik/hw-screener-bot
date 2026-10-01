"""journal_charts.py — PNG D1/H1/5m for signals that were sent to Telegram.

Files live next to the SQLite DB (production: /data/journal_charts) so they
survive restarts on the Railway volume. Names are `{journal_id}_{tf}.png`.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import zlib
from pathlib import Path

import httpx

import storage
from hwv1 import Bar
from moex import (
    fetch_klines_moex_d1,
    fetch_klines_moex_h1,
    fetch_klines_moex_m5,
    moex_client,
)
from screener import fetch_klines_bybit, fetch_klines_okx

logger = logging.getLogger(__name__)

_TFS = ("d1", "h1", "m5")
_TF_MS = {"d1": 86_400_000, "h1": 3_600_000, "m5": 300_000}
# same window as screener.html winAround()
_WIN = {"d1": (45, 12), "h1": (70, 30), "m5": (80, 40)}

_BG = (18, 19, 26)
_GREEN = (38, 166, 154)
_RED = (239, 83, 80)
_LEVEL = (255, 213, 79)
_ENTRY = (176, 182, 200)
_ACCENT = (122, 162, 247)
_FG = (230, 232, 239)

# 5×7 glyphs, high bit = leftmost pixel
_FONT = {
    "0": (0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110),
    "1": (0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "2": (0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b01000, 0b11111),
    "3": (0b11110, 0b00001, 0b00001, 0b01110, 0b00001, 0b00001, 0b11110),
    "4": (0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010),
    "5": (0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110),
    "6": (0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110),
    "7": (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000),
    "8": (0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110),
    "9": (0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100),
    ".": (0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b01100, 0b01100),
    "-": (0b00000, 0b00000, 0b00000, 0b11111, 0b00000, 0b00000, 0b00000),
}

_locks: dict[int, asyncio.Lock] = {}


def chart_filenames(row_id: int) -> list[str]:
    return [f"{int(row_id)}_{tf}.png" for tf in _TFS]


def unlink_ids(row_ids) -> None:
    root = storage.journal_charts_dir()
    for rid in row_ids or []:
        for name in chart_filenames(int(rid)):
            try:
                (root / name).unlink(missing_ok=True)
            except OSError:
                pass


def path_for(row_id: int, tf: str) -> Path | None:
    if tf not in _TFS:
        return None
    root = storage.journal_charts_dir().resolve()
    p = (root / f"{int(row_id)}_{tf}.png").resolve()
    if not str(p).startswith(str(root)) or not p.is_file() or p.stat().st_size < 32:
        return None
    return p


def all_present(row_id: int) -> bool:
    return all(path_for(row_id, tf) for tf in _TFS)


def _lock(row_id: int) -> asyncio.Lock:
    lk = _locks.get(row_id)
    if lk is None:
        lk = asyncio.Lock()
        _locks[row_id] = lk
    return lk


def _fmt_px(v: float) -> str:
    a = abs(float(v))
    if a >= 1000:
        return f"{v:.1f}"
    if a >= 1:
        return f"{v:.3f}"
    if a >= 0.01:
        return f"{v:.5f}"
    return f"{v:.4g}"


def _png(width: int, height: int, rgb: bytearray) -> bytes:
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")


def _put(buf: bytearray, w: int, h: int, x: int, y: int, color: tuple[int, int, int]) -> None:
    if 0 <= x < w and 0 <= y < h:
        i = (y * w + x) * 3
        buf[i] = color[0]
        buf[i + 1] = color[1]
        buf[i + 2] = color[2]


def _hline(buf, w, h, y, x0, x1, color, dash: int = 0) -> None:
    y = int(round(y))
    for x in range(int(x0), int(x1)):
        if dash and (x // dash) % 2 == 1:
            continue
        _put(buf, w, h, x, y, color)


def _vline(buf, w, h, x, y0, y1, color, dash: int = 0) -> None:
    x = int(round(x))
    for y in range(int(y0), int(y1)):
        if dash and (y // dash) % 2 == 1:
            continue
        _put(buf, w, h, x, y, color)


def _text(buf, w, h, x, y, text: str, color, scale: int = 2) -> None:
    cx = int(x)
    for ch in text:
        rows = _FONT.get(ch)
        if not rows:
            cx += 3 * scale
            continue
        for ry, bits in enumerate(rows):
            for rx in range(5):
                if bits & (1 << (4 - rx)):
                    for dy in range(scale):
                        for dx in range(scale):
                            _put(buf, w, h, cx + rx * scale + dx, y + ry * scale + dy, color)
        cx += 6 * scale


def _win_around(bars: list[Bar], ts: int, before: int, after: int) -> list[Bar]:
    idx = next((i for i, b in enumerate(bars) if b.ts >= ts), -1)
    if idx < 0:
        return list(bars[-before:]) if bars else []
    return list(bars[max(0, idx - before): idx + after])


def render_chart_png(
    bars: list[Bar],
    *,
    level: float,
    entry: float,
    stop: float,
    take: float,
    signal_ts: int,
    tf_ms: int,
    width: int = 720,
    height: int = 240,
) -> bytes:
    """Candles + level / entry / stop / take, same marks as the Mini App canvas."""
    w, h = width, height
    buf = bytearray(_BG * (w * h))
    pad_l, pad_r, pad_t, pad_b = 8, 86, 10, 16
    vals = [float(level), float(entry), float(stop), float(take)]
    for b in bars:
        vals.append(float(b.l))
        vals.append(float(b.h))
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    plot_h = h - pad_t - pad_b

    def y_of(v: float) -> float:
        return pad_t + (hi - float(v)) / rng * plot_h

    marks = (
        (float(level), _LEVEL, 0),
        (float(entry), _ENTRY, 4),
        (float(stop), _RED, 4),
        (float(take), _GREEN, 4),
    )
    x_right = w - pad_r
    for price, col, dash in marks:
        _hline(buf, w, h, y_of(price), pad_l, x_right, col, dash)
        _text(buf, w, h, x_right + 4, max(2, int(y_of(price)) - 7), _fmt_px(price), col, 2)

    n = len(bars)
    if n:
        cw = (x_right - pad_l) / n
        for i, b in enumerate(bars):
            cx = pad_l + i * cw + cw / 2
            up = b.c >= b.o
            col = _GREEN if up else _RED
            _vline(buf, w, h, cx, y_of(b.h), y_of(b.l) + 1, col, 0)
            bw = max(1, int(cw * 0.65))
            y1, y2 = y_of(b.o), y_of(b.c)
            top, bot = (y1, y2) if y1 <= y2 else (y2, y1)
            if bot - top < 1:
                bot = top + 1
            x0 = int(cx - bw / 2)
            for yy in range(int(top), int(bot) + 1):
                for xx in range(x0, x0 + bw):
                    _put(buf, w, h, xx, yy, col)
            if signal_ts and abs(b.ts - int(signal_ts)) < int(tf_ms):
                _vline(buf, w, h, cx, pad_t, h - pad_b, _ACCENT, 3)
    return _png(w, h, buf)


def _crypto_symbol(ticker: str) -> str:
    t = (ticker or "").upper().strip()
    if t.endswith("-USDT-SWAP"):
        base = t.split("-")[0]
        return base + "USDT"
    if t.endswith("USDT"):
        return t
    return t + "USDT"


def _okx_inst(symbol: str) -> str:
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return f"{base}-USDT-SWAP"


async def _fetch_crypto_tf(client: httpx.AsyncClient, which: str, symbol: str, inst: str, tf: str) -> list[Bar]:
    try:
        if which == "bybit":
            if tf == "d1":
                return list(await fetch_klines_bybit(client, symbol, "D", 90))
            if tf == "h1":
                return list(await fetch_klines_bybit(client, symbol, "60", 160))
            return list(await fetch_klines_bybit(client, symbol, "5", 1000))
        if tf == "d1":
            return list(await fetch_klines_okx(client, inst, "D", 90))
        if tf == "h1":
            return list(await fetch_klines_okx(client, inst, "60", 160))
        return list(await fetch_klines_okx(client, inst, "5", 500))
    except Exception as e:
        logger.warning("journal %s %s %s: %s", which, symbol, tf, e)
        return []


async def _fetch_three(card: dict) -> dict[str, list[Bar]]:
    market = storage.normalize_journal_market(card.get("market"))
    ticker = str(card.get("ticker") or "")
    signal_ts = int(card.get("signal_ts") or 0)
    out: dict[str, list[Bar]] = {tf: [] for tf in _TFS}
    if market == "ru":
        async with moex_client() as client:
            jobs = {
                "d1": fetch_klines_moex_d1(client, ticker, 80),
                "h1": fetch_klines_moex_h1(client, ticker, 140),
                "m5": fetch_klines_moex_m5(
                    client, ticker, 160, signal_ts + 40 * 300_000 if signal_ts else None,
                ),
            }
            for tf, coro in jobs.items():
                try:
                    out[tf] = list(await coro)
                except Exception as e:
                    logger.warning("journal moex %s %s: %s", ticker, tf, e)
        return out

    symbol = _crypto_symbol(ticker)
    inst = _okx_inst(symbol)
    async with httpx.AsyncClient(timeout=20.0) as client:
        for tf in _TFS:
            bars = await _fetch_crypto_tf(client, "bybit", symbol, inst, tf)
            if not bars:
                bars = await _fetch_crypto_tf(client, "okx", symbol, inst, tf)
            out[tf] = bars
    return out


def _prices(card: dict) -> tuple[float, float, float, float] | None:
    try:
        level = float(card.get("level"))
        entry = float(card.get("last") if card.get("last") is not None else card.get("entry"))
        stop = float(card.get("stop"))
        take = float(card.get("take"))
    except (TypeError, ValueError):
        return None
    return level, entry, stop, take


async def save_charts(row_id: int, card: dict) -> list[str]:
    """Write up to three PNGs. Returns basenames that were written."""
    prices = _prices(card)
    if not prices:
        return []
    level, entry, stop, take = prices
    signal_ts = int(card.get("signal_ts") or 0)
    bars = await _fetch_three(card)
    root = storage.journal_charts_dir()
    written: list[str] = []
    for tf in _TFS:
        before, after = _WIN[tf]
        window = _win_around(bars.get(tf) or [], signal_ts, before, after)
        try:
            png = render_chart_png(
                window,
                level=level, entry=entry, stop=stop, take=take,
                signal_ts=signal_ts, tf_ms=_TF_MS[tf],
            )
        except Exception as e:
            logger.warning("journal render %s %s: %s", row_id, tf, e)
            continue
        name = f"{int(row_id)}_{tf}.png"
        tmp = root / (name + ".tmp")
        dest = root / name
        tmp.write_bytes(png)
        tmp.replace(dest)
        written.append(name)
    return written


async def ensure(row_id: int) -> None:
    """Create any missing chart files for a journal row. Safe to call twice."""
    row_id = int(row_id)
    if all_present(row_id):
        return
    async with _lock(row_id):
        if all_present(row_id):
            return
        row = storage.get_signal_journal(row_id)
        if not row:
            return
        await save_charts(row_id, row.get("card") or {})
