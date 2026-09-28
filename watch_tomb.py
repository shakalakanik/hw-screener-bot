"""watch_tomb.py — tombstones for Mini App «Отслеживаемые» (watch list).

Why: watch is synced by union-merge (server ∪ device localStorage ∪ bot watchlist)
so nothing is ever lost — but that also resurrected rows the user deleted/cleared
(stale copy on another device / WebView re-pushed them). Now every delete writes a
tombstone (key "base|t|mkt" → deletedAt ms) and «Очистить всё» writes cleared_at ms.
A row survives only if its `added` (fallback `t`) is newer than both, so a fresh
add after a clear/delete still appears.

Tables are additive (CREATE IF NOT EXISTS) in the same DB (DB_PATH). Templates are
not touched. A trigger on bot `watchlist` DELETE tombstones rows removed with the
bot's «🗑 Удалить из отслеживаемого» button, so bot and Mini App stay in sync.
"""
from __future__ import annotations

import json
import sqlite3
import time

import storage

TOMB_TTL_MS = 90 * 86400 * 1000
TOMB_MAX = 3000

_BASE_SQL = (
    "CASE WHEN {r}.market='ru' THEN {r}.ticker "
    "WHEN {r}.ticker LIKE '%USDT' THEN substr({r}.ticker, 1, length({r}.ticker)-4) "
    "ELSE {r}.ticker END"
)


_READY = False


def _ensure(c: sqlite3.Connection) -> None:
    global _READY
    if _READY:
        return
    storage._ensure_watchlist(c)
    c.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS miniapp_watch_tombs (
            user_id    INTEGER NOT NULL,
            key        TEXT NOT NULL,
            deleted_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, key)
        );
        CREATE TABLE IF NOT EXISTS miniapp_watch_clear (
            user_id    INTEGER PRIMARY KEY,
            cleared_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TRIGGER IF NOT EXISTS trg_watchlist_tomb AFTER DELETE ON watchlist
        BEGIN
            INSERT INTO miniapp_watch_tombs(user_id, key, deleted_at)
            VALUES (
                OLD.chat_id,
                ({_BASE_SQL.format(r='OLD')}) || '|' || OLD.signal_ts || '|' || COALESCE(OLD.market, 'crypto'),
                CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)
            )
            ON CONFLICT(user_id, key) DO UPDATE SET deleted_at=max(deleted_at, excluded.deleted_at);
        END;
        """
    )
    _READY = True


def install() -> None:
    """Create tables + trigger at startup (idempotent)."""
    with storage._conn() as c:
        _ensure(c)


def row_key(x: dict) -> str:
    if not isinstance(x, dict):
        return ""
    m = x.get("mkt") or x.get("market") or "crypto"
    return f"{x.get('base') or ''}|{x.get('t') or ''}|{m}"


def _row_time(x: dict) -> int:
    for k in ("added", "t"):
        try:
            v = int(x.get(k) or 0)
        except (TypeError, ValueError):
            v = 0
        if v:
            return v
    return 0


def alive(x: dict, tombs: dict, cleared_at: int) -> bool:
    rt = _row_time(x)
    if cleared_at and rt <= cleared_at:
        return False
    td = tombs.get(row_key(x))
    return not (td and rt <= int(td))


def read(c: sqlite3.Connection, uid: int) -> tuple[dict, int]:
    _ensure(c)
    r = c.execute("SELECT cleared_at FROM miniapp_watch_clear WHERE user_id=?", (int(uid),)).fetchone()
    cleared_at = int(r["cleared_at"]) if r else 0
    tombs = {
        row["key"]: int(row["deleted_at"])
        for row in c.execute(
            "SELECT key, deleted_at FROM miniapp_watch_tombs WHERE user_id=?", (int(uid),)
        ).fetchall()
    }
    return tombs, cleared_at


def _apply_incoming(c: sqlite3.Connection, uid: int, deleted, cleared) -> bool:
    """Merge client tombstones (max wins). Returns True if anything changed."""
    tombs, cleared_at = read(c, uid)
    changed = False
    try:
        wca = int(cleared or 0)
    except (TypeError, ValueError):
        wca = 0
    if wca > cleared_at:
        cleared_at = wca
        c.execute(
            "INSERT OR REPLACE INTO miniapp_watch_clear(user_id, cleared_at) VALUES(?,?)",
            (int(uid), cleared_at),
        )
        changed = True
    new_tombs = {}
    if isinstance(deleted, dict):
        for k, v in deleted.items():
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if k and v > cleared_at and v > int(tombs.get(k) or 0):
                new_tombs[str(k)[:200]] = v
    for k, v in new_tombs.items():
        c.execute(
            "INSERT OR REPLACE INTO miniapp_watch_tombs(user_id, key, deleted_at) VALUES(?,?,?)",
            (int(uid), k, v),
        )
    changed = changed or bool(new_tombs)
    if changed:
        _purge_bot_watchlist(c, uid, new_tombs, wca if wca == cleared_at else 0)
    # prune: redundant (≤ cleared_at), expired, over cap
    now_ms = int(time.time() * 1000)
    c.execute(
        "DELETE FROM miniapp_watch_tombs WHERE user_id=? AND (deleted_at<=? OR deleted_at<?)",
        (int(uid), cleared_at, now_ms - TOMB_TTL_MS),
    )
    c.execute(
        "DELETE FROM miniapp_watch_tombs WHERE user_id=? AND key NOT IN ("
        "SELECT key FROM miniapp_watch_tombs WHERE user_id=? ORDER BY deleted_at DESC LIMIT ?)",
        (int(uid), int(uid), TOMB_MAX),
    )
    return changed


def _purge_bot_watchlist(c: sqlite3.Connection, uid: int, tombs: dict, cleared_at: int) -> None:
    """Mirror Mini App deletes/clear into bot `watchlist` (feeds /watchlist/<token>)."""
    if cleared_at:
        c.execute("DELETE FROM watchlist WHERE chat_id=? AND added_ts<=?", (int(uid), int(cleared_at)))
    for k, v in (tombs or {}).items():
        parts = str(k).split("|")
        if len(parts) != 3:
            continue
        base, t, m = parts
        try:
            t = int(t)
        except (TypeError, ValueError):
            continue
        tickers = {base} if m == "ru" else {base, base if base.endswith("USDT") else base + "USDT"}
        for tk in tickers:
            c.execute(
                "DELETE FROM watchlist WHERE chat_id=? AND market=? AND ticker=? AND signal_ts=? AND added_ts<=?",
                (int(uid), m, tk, t, int(v)),
            )


def get_state(uid: int) -> dict:
    """storage.get_miniapp_state + tombstone filter + tombstones for the client."""
    st = storage.get_miniapp_state(uid)
    with storage._conn() as c:
        tombs, cleared_at = read(c, uid)
    st["watch"] = [x for x in st.get("watch") or [] if alive(x, tombs, cleared_at)]
    st["watch_tombstones"] = tombs
    st["watch_cleared_at"] = cleared_at
    return st


def put_state(uid: int, *, watch_deleted=None, watch_cleared_at=None,
              watch_merge: bool = False, **kwargs) -> dict:
    """Apply tombstones, then storage.put_miniapp_state.

    watch_merge=True (bridge.js ≥ 20260928c): stored watch = union(server, incoming)
    minus tombstoned rows — bot-added rows are never lost, deleted rows never return.
    Legacy clients (no watch_merge) keep old replace semantics, still tombstone-filtered.
    """
    with storage._conn() as c:
        changed = _apply_incoming(c, uid, watch_deleted, watch_cleared_at)
        tombs, cleared_at = read(c, uid)

    server_alive = [x for x in storage.get_miniapp_state(uid).get("watch") or []
                    if alive(x, tombs, cleared_at)]
    watch = kwargs.get("watch")
    if watch is None and changed:
        watch = server_alive  # tombstones only → drop dead rows from server copy
        kwargs["watch"] = watch
    if watch is not None:
        if not isinstance(watch, list):
            watch = []
        watch = [x for x in watch if isinstance(x, dict) and alive(x, tombs, cleared_at)]
        if watch_merge:
            by_key: dict = {}
            order: list = []
            for x in server_alive + watch:  # incoming overrides same key (fresher _now/_res)
                k = row_key(x)
                if k not in by_key:
                    order.append(k)
                    by_key[k] = x
                else:
                    by_key[k] = {**by_key[k], **x}
            watch = [by_key[k] for k in order][-500:]
        kwargs["watch"] = watch
        if not server_alive:
            # everything on server is tombstoned → an empty result is intended, not a boot glitch
            kwargs["clear_watch"] = True

    if not any(kwargs.get(k) is not None for k in ("watch", "backtest", "signals")):
        out = get_state(uid)
        out.update(ok=True, rejected=[])
        return out
    out = storage.put_miniapp_state(uid, **kwargs)
    out["watch"] = [x for x in out.get("watch") or [] if alive(x, tombs, cleared_at)]
    out["watch_tombstones"] = tombs
    out["watch_cleared_at"] = cleared_at
    return out
