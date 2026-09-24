"""storage.py — SQLite-хранилище сигналов, настроек и дедупликации."""
import sqlite3
import json
import time
import hashlib
import os
import shutil
from pathlib import Path


def _resolve_db_path() -> Path:
    """Prefer Railway volume /data; allow override via DB_PATH env."""
    env = (os.environ.get("DB_PATH") or "").strip()
    if env:
        return Path(env)
    data = Path("/data")
    try:
        if data.is_dir() and os.access(data, os.W_OK):
            return data / "hw_bot.db"
    except OSError:
        pass
    return Path("./hw_bot.db")


def _maybe_migrate_legacy_db(dest: Path) -> None:
    """One-time copy from image WORKDIR DB into volume if dest is missing."""
    if dest.exists() and dest.stat().st_size > 0:
        return
    candidates = [Path("/app/hw_bot.db"), Path("./hw_bot.db")]
    try:
        dest_resolved = dest.resolve()
    except OSError:
        dest_resolved = dest
    for src in candidates:
        try:
            if not src.exists() or src.stat().st_size <= 0:
                continue
            if src.resolve() == dest_resolved:
                continue
        except OSError:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"[storage] Migrated legacy DB {src} → {dest}", flush=True)
        return


_DB_PATH_LOGGED = False


def _ensure_db_path() -> Path:
    global _DB_PATH_LOGGED, DB_PATH
    DB_PATH = _resolve_db_path()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _maybe_migrate_legacy_db(DB_PATH)
    if not _DB_PATH_LOGGED:
        print(f"[storage] DB_PATH={DB_PATH}", flush=True)
        _DB_PATH_LOGGED = True
    return DB_PATH


DB_PATH = _resolve_db_path()


def _conn() -> sqlite3.Connection:
    path = _ensure_db_path()
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _column_exists(c: sqlite3.Connection, table: str, column: str) -> bool:
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _migrate(c: sqlite3.Connection):
    """Досоздать колонки, если таблицы остались со старой схемой (не удаляя данные)."""
    # dedup: старая PK была (ticker, side, level) без strategy — пересоздаём таблицу целиком,
    # т.к. SQLite не может добавить колонку в составной PRIMARY KEY через ALTER TABLE.
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]

    if "dedup" in tables and not _column_exists(c, "dedup", "strategy"):
        c.executescript("""
            ALTER TABLE dedup RENAME TO dedup_old;
            CREATE TABLE dedup (
                ticker   TEXT NOT NULL,
                side     TEXT NOT NULL,
                level    REAL NOT NULL,
                strategy TEXT NOT NULL DEFAULT 'brk',
                expires  INTEGER NOT NULL,
                PRIMARY KEY (ticker, side, level, strategy)
            );
            INSERT INTO dedup (ticker, side, level, strategy, expires)
                SELECT ticker, side, level, 'brk', expires FROM dedup_old;
            DROP TABLE dedup_old;
        """)

    if "signals" in tables and not _column_exists(c, "signals", "kind"):
        c.execute("ALTER TABLE signals ADD COLUMN kind TEXT")

    if "watchlist" in tables and not _column_exists(c, "watchlist", "strategy"):
        c.execute("ALTER TABLE watchlist ADD COLUMN strategy TEXT NOT NULL DEFAULT 'brk'")


def init():
    _ensure_db_path()
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker  TEXT NOT NULL,
            side    TEXT NOT NULL,
            level   REAL NOT NULL,
            kind    TEXT,
            ts      INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_filters (
            chat_id     INTEGER PRIMARY KEY,
            template    TEXT NOT NULL DEFAULT 'default',
            custom_json TEXT
        );

        CREATE TABLE IF NOT EXISTS dedup (
            ticker   TEXT NOT NULL,
            side     TEXT NOT NULL,
            level    REAL NOT NULL,
            strategy TEXT NOT NULL DEFAULT 'brk',
            expires  INTEGER NOT NULL,
            PRIMARY KEY (ticker, side, level, strategy)
        );

        CREATE TABLE IF NOT EXISTS bot_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_signal_cards (
            card_id    TEXT PRIMARY KEY,
            chat_id    INTEGER NOT NULL,
            card_json  TEXT NOT NULL,
            signal_ts  INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pending_signal_cards_created
            ON pending_signal_cards(created_at);
        """)
        _migrate(c)
        cleanup_pending_signal_cards()


# ── Watermark: max signal_ts already sent (incremental autoscan) ──────────────

_WM_KEY = "send_watermark_ms"


def get_send_watermark_ms() -> int | None:
    """Последний отправленный signal_ts (ms), либо None если ещё не было."""
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = c.execute(
            "SELECT value FROM bot_meta WHERE key=?", (_WM_KEY,)
        ).fetchone()
    if not row:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def set_send_watermark_ms(ts_ms: int) -> None:
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        c.execute(
            "INSERT OR REPLACE INTO bot_meta(key, value) VALUES(?, ?)",
            (_WM_KEY, str(int(ts_ms))),
        )


def bump_send_watermark_ms(ts_ms: int) -> int:
    """Поднять watermark до ts_ms, если он больше текущего. Вернуть новое значение."""
    cur = get_send_watermark_ms()
    if cur is None or ts_ms > cur:
        set_send_watermark_ms(ts_ms)
        return int(ts_ms)
    return cur


# ── Дедупликация (правило: один сигнал на монету+уровень+стратегию раз в 12 ч) ─

def is_duplicate(ticker: str, side: str, level: float, strategy: str = "brk") -> bool:
    with _conn() as c:
        now = int(time.time())
        row = c.execute(
            "SELECT expires FROM dedup WHERE ticker=? AND side=? AND strategy=? AND ABS(level-?)<?",
            (ticker, side, strategy, level, level * 0.005),
        ).fetchone()
        if row and row["expires"] > now:
            return True
        return False


def mark_sent(ticker: str, side: str, level: float, strategy: str = "brk", ttl_hours: int = 36):
    expires = int(time.time()) + ttl_hours * 3600
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO dedup(ticker,side,level,strategy,expires) VALUES(?,?,?,?,?)",
            (ticker, side, level, strategy, expires),
        )
        c.execute(
            "INSERT INTO signals(ticker,side,level,kind,ts) VALUES(?,?,?,?,?)",
            (ticker, side, level, strategy, int(time.time())),
        )


# ── Фильтры пользователя ──────────────────────────────────────────────────────

TEMPLATES = {
    "default": {
        "desc": "Стандарт — сила ≥3, дистанция ≤0.5 ATR",
        "strength_min": 3,
        "dist_atr_max": 0.50,
        "sides": ["LONG", "SHORT"],
        "bias_filter": False,
    },
    "strong": {
        "desc": "Только сильные уровни — сила ≥4, дистанция ≤0.35 ATR",
        "strength_min": 4,
        "dist_atr_max": 0.35,
        "sides": ["LONG", "SHORT"],
        "bias_filter": False,
    },
    "long_only": {
        "desc": "Только лонги",
        "strength_min": 3,
        "dist_atr_max": 0.50,
        "sides": ["LONG"],
        "bias_filter": False,
    },
    "short_only": {
        "desc": "Только шорты",
        "strength_min": 3,
        "dist_atr_max": 0.50,
        "sides": ["SHORT"],
        "bias_filter": False,
    },
    "trend": {
        "desc": "По тренду D1 — только лонги в up, шорты в down",
        "strength_min": 3,
        "dist_atr_max": 0.50,
        "sides": ["LONG", "SHORT"],
        "bias_filter": True,  # side должен совпадать с bias
    },
    "scalp": {
        "desc": "Скальп — дистанция ≤0.2 ATR, сила любая",
        "strength_min": 2,
        "dist_atr_max": 0.20,
        "sides": ["LONG", "SHORT"],
        "bias_filter": False,
    },
}


def get_filter(chat_id: int) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT template, custom_json FROM user_filters WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
    if not row:
        return TEMPLATES["default"]
    if row["custom_json"]:
        return json.loads(row["custom_json"])
    return TEMPLATES.get(row["template"], TEMPLATES["default"])


def set_template(chat_id: int, template: str):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO user_filters(chat_id, template, custom_json) VALUES(?,?,NULL)",
            (chat_id, template),
        )


def get_template_name(chat_id: int) -> str:
    with _conn() as c:
        row = c.execute(
            "SELECT template FROM user_filters WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row["template"] if row else "default"



# ── Watchlist — отслеживаемые сигналы ─────────────────────────────────────────

def _ensure_watchlist(c: sqlite3.Connection):
    c.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id  INTEGER NOT NULL,
            market   TEXT NOT NULL DEFAULT 'crypto',
            strategy TEXT NOT NULL DEFAULT 'brk',
            ticker   TEXT NOT NULL,
            side     INTEGER NOT NULL,
            level    REAL NOT NULL,
            entry    REAL NOT NULL,
            stop     REAL NOT NULL,
            take     REAL NOT NULL,
            kind     TEXT,
            prob     REAL,
            risk_pct REAL,
            signal_ts INTEGER NOT NULL,
            added_ts  INTEGER NOT NULL,
            raw_json  TEXT
        )
    """)
    _migrate(c)


def add_to_watchlist(chat_id: int, item: dict) -> int:
    """Добавить сигнал в watchlist. Возвращает id записи."""
    with _conn() as c:
        _ensure_watchlist(c)
        cur = c.execute(
            """INSERT INTO watchlist
               (chat_id, market, strategy, ticker, side, level, entry, stop, take,
                kind, prob, risk_pct, signal_ts, added_ts, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                chat_id,
                item.get("market", "crypto"),
                item.get("strategy", "brk"),
                item["ticker"],
                1 if item.get("side") == "LONG" else 0,
                item.get("level", 0),
                item.get("entry", 0),
                item.get("stop", 0),
                item.get("take", 0),
                item.get("kind", ""),
                item.get("prob", 0),
                item.get("risk_pct", 0),
                item.get("signal_ts", int(time.time() * 1000)),
                int(time.time() * 1000),
                json.dumps(item, ensure_ascii=False),
            ),
        )
        return cur.lastrowid


def get_watchlist(chat_id: int, market: str = "crypto") -> list[dict]:
    """Получить watchlist пользователя в формате совместимом с HTML.

    market: 'crypto' | 'ru' | 'all' (оба рынка). Каждая запись содержит mkt/market.
    """
    with _conn() as c:
        _ensure_watchlist(c)
        if market in ("all", "*", ""):
            rows = c.execute(
                "SELECT * FROM watchlist WHERE chat_id=? ORDER BY added_ts DESC LIMIT 400",
                (chat_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM watchlist WHERE chat_id=? AND market=? ORDER BY added_ts DESC LIMIT 200",
                (chat_id, market),
            ).fetchall()
    result = []
    for r in rows:
        mkt = r["market"] or "crypto"
        ticker = r["ticker"]
        # crypto: BTCUSDT; ru: тикер бумаги как есть
        if mkt == "ru":
            base = ticker
            sym = ticker
        else:
            base = ticker[:-4] if ticker.endswith("USDT") else ticker
            sym = ticker if ticker.endswith("USDT") else (ticker + "USDT")
        result.append({
            "type": r["strategy"],   # 'brk' | 'fbo' — реальная стратегия сигнала, как в HTML
            "mkt": mkt,
            "market": mkt,
            "base": base,
            "sym": sym,
            "t": r["signal_ts"],
            "d": r["side"],
            "side": "LONG" if r["side"] else "SHORT",
            "lv": r["level"],
            "k": r["kind"],
            "p": r["prob"],
            "e": r["entry"],
            "st": r["stop"],
            "tk": r["take"],
            "rp": r["risk_pct"],
            "strength": 3,
            "crosses": 0,
            "vol_mult": 1.0,
            "added": r["added_ts"],
        })
    return result


def remove_from_watchlist(chat_id: int, watch_id: int):
    with _conn() as c:
        _ensure_watchlist(c)
        c.execute("DELETE FROM watchlist WHERE id=? AND chat_id=?", (watch_id, chat_id))


# ── Pending signal cards (👁 button survives restarts, TTL 48h) ───────────────

PENDING_CARD_TTL_SEC = 48 * 3600


def _ensure_pending_signal_cards(c: sqlite3.Connection):
    c.execute("""
        CREATE TABLE IF NOT EXISTS pending_signal_cards (
            card_id    TEXT PRIMARY KEY,
            chat_id    INTEGER NOT NULL,
            card_json  TEXT NOT NULL,
            signal_ts  INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_signal_cards_created "
        "ON pending_signal_cards(created_at)"
    )


def make_pending_card_id(card_key: str, chat_id: int) -> str:
    """Short stable id for Telegram callback_data (fits in 64 bytes with prefix)."""
    raw = f"{card_key}:{int(chat_id)}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def save_pending_signal_card(
    card_id: str,
    chat_id: int,
    card: dict,
    signal_ts: int | None = None,
) -> None:
    now = int(time.time())
    if signal_ts is None:
        signal_ts = int(card.get("signal_ts") or 0)
    with _conn() as c:
        _ensure_pending_signal_cards(c)
        c.execute(
            """INSERT OR REPLACE INTO pending_signal_cards
               (card_id, chat_id, card_json, signal_ts, created_at)
               VALUES (?,?,?,?,?)""",
            (
                card_id,
                int(chat_id),
                json.dumps(card, ensure_ascii=False),
                int(signal_ts or 0),
                now,
            ),
        )
        cutoff = now - PENDING_CARD_TTL_SEC
        c.execute(
            "DELETE FROM pending_signal_cards WHERE created_at < ?",
            (cutoff,),
        )


def get_pending_signal_card(
    card_id: str,
    chat_id: int | None = None,
) -> tuple[dict, int, int] | None:
    """Return (card, signal_ts, created_at) or None."""
    with _conn() as c:
        _ensure_pending_signal_cards(c)
        if chat_id is not None:
            row = c.execute(
                "SELECT card_json, signal_ts, created_at FROM pending_signal_cards "
                "WHERE card_id=? AND chat_id=?",
                (card_id, int(chat_id)),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT card_json, signal_ts, created_at FROM pending_signal_cards "
                "WHERE card_id=?",
                (card_id,),
            ).fetchone()
    if not row:
        return None
    try:
        card = json.loads(row["card_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(card, dict):
        return None
    return card, int(row["signal_ts"] or 0), int(row["created_at"] or 0)


def pending_card_is_fresh(
    signal_ts: int,
    created_at: int,
    max_age_sec: int = PENDING_CARD_TTL_SEC,
) -> bool:
    """Age ≤ max_age_sec; prefer signal_ts (ms or sec), else created_at (sec)."""
    now = time.time()
    ts = int(signal_ts or 0)
    if ts > 0:
        # Heuristic: values > 1e12 are milliseconds
        age_base = ts / 1000.0 if ts > 1_000_000_000_000 else float(ts)
        return (now - age_base) <= max_age_sec
    ca = int(created_at or 0)
    if ca > 0:
        return (now - ca) <= max_age_sec
    return False


def cleanup_pending_signal_cards(max_age_sec: int = PENDING_CARD_TTL_SEC) -> int:
    cutoff = int(time.time()) - int(max_age_sec)
    with _conn() as c:
        _ensure_pending_signal_cards(c)
        cur = c.execute(
            "DELETE FROM pending_signal_cards WHERE created_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0


def watchlist_item_html_shape(item: dict) -> dict:
    """Map bot watchlist item to HTML / Mini App watch row shape."""
    mkt = item.get("market", "crypto") or "crypto"
    ticker = item["ticker"]
    if mkt == "ru":
        base = ticker
        sym = ticker
    else:
        base = ticker[:-4] if ticker.endswith("USDT") else ticker
        sym = ticker if ticker.endswith("USDT") else (ticker + "USDT")
    side = item.get("side", "LONG")
    side_int = 1 if side == "LONG" or side is True or side == 1 else 0
    return {
        "type": item.get("strategy", "brk"),
        "mkt": mkt,
        "market": mkt,
        "base": base,
        "sym": sym,
        "t": int(item.get("signal_ts") or int(time.time() * 1000)),
        "d": side_int,
        "side": "LONG" if side_int else "SHORT",
        "lv": item.get("level", 0),
        "k": item.get("kind", ""),
        "p": item.get("prob", 0) or 0,
        "e": item.get("entry", 0),
        "st": item.get("stop", 0),
        "tk": item.get("take", 0),
        "rp": item.get("risk_pct", 0),
        "strength": 3,
        "crosses": 0,
        "vol_mult": 1.0,
        "added": int(time.time() * 1000),
    }


def append_miniapp_watch_item(user_id: int, item_html: dict) -> None:
    """Append one HTML-shaped row into miniapp_watch (dedupe base+t+mkt)."""
    if not isinstance(item_html, dict):
        return
    m = item_html.get("mkt") or item_html.get("market") or "crypto"
    base = item_html.get("base")
    t = item_html.get("t")
    with _conn() as c:
        _ensure_miniapp_state(c)
        row = c.execute(
            "SELECT data_json FROM miniapp_watch WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
        watch = _json_loads(row["data_json"] if row else None, [])
        if not isinstance(watch, list):
            watch = []
        for x in watch:
            if not isinstance(x, dict):
                continue
            xm = x.get("mkt") or x.get("market") or "crypto"
            if x.get("base") == base and x.get("t") == t and xm == m:
                return
        watch.append(item_html)
        if len(watch) > 500:
            watch = watch[-500:]
        c.execute(
            "INSERT OR REPLACE INTO miniapp_watch"
            "(user_id, data_json, updated_at, cleared) VALUES(?,?,?,?)",
            (
                int(user_id),
                json.dumps(watch, ensure_ascii=False),
                int(time.time()),
                0,
            ),
        )


def _ensure_html_templates(c: sqlite3.Connection):
    c.execute("""
        CREATE TABLE IF NOT EXISTS html_templates (
            chat_id  INTEGER NOT NULL,
            name     TEXT NOT NULL,
            filters  TEXT NOT NULL,
            market   TEXT NOT NULL DEFAULT 'crypto',
            PRIMARY KEY (chat_id, name)
        )
    """)


def _normalize_incoming_template(filters, default_market: str) -> tuple[dict, str]:
    """Accept flat HTML filters or nested {filters, market} from get_html_templates."""
    if not isinstance(filters, dict):
        m = default_market if default_market in ("crypto", "ru") else "crypto"
        return {}, m
    # Nested API/DB shape → flatten for storage + Mini App
    if "filters" in filters and isinstance(filters.get("filters"), dict):
        flt = dict(filters["filters"])
        m = filters.get("market") or flt.get("_market")
    else:
        flt = dict(filters)
        m = flt.get("_market")
    if m not in ("crypto", "ru"):
        m = default_market if default_market in ("crypto", "ru") else "crypto"
    flt["_market"] = m
    return flt, m


def save_html_templates(
    chat_id: int,
    templates: dict,
    market: str = "crypto",
    *,
    replace: bool = False,
    remove_missing: bool = False,
):
    """Сохранить шаблоны из HTML-скринера (merge-only upsert by default).

    Рынок шаблона: filters[_market] / nested market, иначе аргумент market.
    По умолчанию НЕ удаляет шаблоны, которых нет во входящем dict
    (защита от partial/empty sync wipe). Явное удаление отсутствующих —
    только при replace=True или remove_missing=True.
    Пустой templates → no-op.
    """
    if not templates:
        return

    do_remove = bool(replace or remove_missing)

    with _conn() as c:
        _ensure_html_templates(c)
        _ensure_template_strategy(c)
        _ensure_active_templates(c)

        items: list[tuple[str, dict, str]] = []
        for name, filters in templates.items():
            flt, m = _normalize_incoming_template(filters, market)
            items.append((name, flt, m))

        markets_touched = {m for _, _, m in items} or {
            market if market in ("crypto", "ru") else "crypto"
        }
        incoming_names = {name for name, _, _ in items}

        before_names = set()
        for m in markets_touched:
            for r in c.execute(
                "SELECT name FROM html_templates WHERE chat_id=? AND market=?",
                (chat_id, m),
            ).fetchall():
                before_names.add(r["name"])

        for name, flt, m in items:
            c.execute(
                "INSERT OR REPLACE INTO html_templates(chat_id, name, filters, market) "
                "VALUES(?,?,?,?)",
                (chat_id, name, json.dumps(flt, ensure_ascii=False), m),
            )

        if not do_remove:
            return

        removed = set()
        for m in markets_touched:
            rows = c.execute(
                "SELECT name FROM html_templates WHERE chat_id=? AND market=?",
                (chat_id, m),
            ).fetchall()
            for r in rows:
                if r["name"] not in incoming_names:
                    removed.add(r["name"])
                    c.execute(
                        "DELETE FROM html_templates WHERE chat_id=? AND name=?",
                        (chat_id, r["name"]),
                    )

        added = incoming_names - before_names
        # Типичный rename в HTML: ровно одно имя ушло и одно пришло
        if len(removed) == 1 and len(added) == 1:
            old = next(iter(removed))
            new = next(iter(added))
            strat = c.execute(
                "SELECT strategy FROM template_strategy WHERE chat_id=? AND name=?",
                (chat_id, old),
            ).fetchone()
            if strat:
                c.execute(
                    "DELETE FROM template_strategy WHERE chat_id=? AND name=?",
                    (chat_id, old),
                )
                c.execute(
                    "INSERT OR REPLACE INTO template_strategy(chat_id, name, strategy) "
                    "VALUES(?,?,?)",
                    (chat_id, new, strat["strategy"]),
                )
            active_row = c.execute(
                "SELECT names FROM active_templates WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            if active_row:
                by = _parse_names_by_market(active_row["names"], None)
                changed = False
                for mkt, names in by.items():
                    if old in names:
                        by[mkt] = [new if n == old else n for n in names]
                        changed = True
                if changed:
                    c.execute(
                        "UPDATE active_templates SET names=? WHERE chat_id=?",
                        (json.dumps(by, ensure_ascii=False), chat_id),
                    )
        elif removed:
            # Просто вычистить исчезнувшие имена из active
            active_row = c.execute(
                "SELECT names FROM active_templates WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            if active_row:
                by = _parse_names_by_market(active_row["names"], None)
                changed = False
                for mkt, names in list(by.items()):
                    cleaned = [n for n in names if n not in removed]
                    if cleaned != names:
                        by[mkt] = cleaned
                        changed = True
                if changed:
                    c.execute(
                        "UPDATE active_templates SET names=? WHERE chat_id=?",
                        (json.dumps(by, ensure_ascii=False), chat_id),
                    )


def get_html_templates(chat_id: int) -> dict:
    """Получить все шаблоны пользователя из HTML."""
    with _conn() as c:
        _ensure_html_templates(c)
        rows = c.execute(
            "SELECT name, filters, market FROM html_templates WHERE chat_id=?",
            (chat_id,),
        ).fetchall()
    return {
        r["name"]: {"filters": json.loads(r["filters"]), "market": r["market"]}
        for r in rows
    }


def rename_html_template(chat_id: int, old: str, new: str) -> tuple[bool, str]:
    """Переименовать шаблон: html_templates, template_strategy, active lists.

    Возвращает (ok, error_message). error_message пустой при успехе.
    """
    new = (new or "").strip()
    if not new:
        return False, "Имя не может быть пустым"
    if len(new) > 64:
        return False, "Слишком длинное имя (макс. 64)"
    if new == old:
        return False, "Имя не изменилось"

    with _conn() as c:
        _ensure_html_templates(c)
        _ensure_template_strategy(c)
        _ensure_active_templates(c)

        row = c.execute(
            "SELECT filters, market FROM html_templates WHERE chat_id=? AND name=?",
            (chat_id, old),
        ).fetchone()
        if not row:
            return False, "Шаблон не найден"
        clash = c.execute(
            "SELECT 1 FROM html_templates WHERE chat_id=? AND name=?",
            (chat_id, new),
        ).fetchone()
        if clash:
            return False, f"Шаблон «{new}» уже существует"

        c.execute(
            "INSERT INTO html_templates(chat_id, name, filters, market) VALUES(?,?,?,?)",
            (chat_id, new, row["filters"], row["market"]),
        )
        c.execute(
            "DELETE FROM html_templates WHERE chat_id=? AND name=?",
            (chat_id, old),
        )

        strat = c.execute(
            "SELECT strategy FROM template_strategy WHERE chat_id=? AND name=?",
            (chat_id, old),
        ).fetchone()
        if strat:
            c.execute(
                "DELETE FROM template_strategy WHERE chat_id=? AND name=?",
                (chat_id, old),
            )
            c.execute(
                "INSERT OR REPLACE INTO template_strategy(chat_id, name, strategy) "
                "VALUES(?,?,?)",
                (chat_id, new, strat["strategy"]),
            )

        active_row = c.execute(
            "SELECT names, markets FROM active_templates WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        if active_row:
            by = _parse_names_by_market(active_row["names"], None)
            changed = False
            for mkt, names in by.items():
                if old in names:
                    by[mkt] = [new if n == old else n for n in names]
                    changed = True
            if changed:
                c.execute(
                    "UPDATE active_templates SET names=? WHERE chat_id=?",
                    (json.dumps(by, ensure_ascii=False), chat_id),
                )

    return True, ""


# ── Активные подписки на шаблоны (per-market) ────────────────────────────────

def _ensure_active_templates(c: sqlite3.Connection):
    c.execute("""
        CREATE TABLE IF NOT EXISTS active_templates (
            chat_id INTEGER PRIMARY KEY,
            names   TEXT NOT NULL DEFAULT '[]',
            markets TEXT NOT NULL DEFAULT '["crypto"]',
            best_only TEXT NOT NULL DEFAULT '{}'
        )
    """)
    if not _column_exists(c, "active_templates", "best_only"):
        c.execute(
            "ALTER TABLE active_templates ADD COLUMN best_only TEXT NOT NULL DEFAULT '{}'"
        )


def _parse_best_only(raw) -> dict:
    """Нормализовать best_only JSON → {"crypto": bool, "ru": bool}."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return {
        "crypto": bool(parsed.get("crypto")),
        "ru": bool(parsed.get("ru")),
    }


def _upsert_active_templates(
    c: sqlite3.Connection,
    chat_id: int,
    names_json: str,
    markets_json: str,
    best_only: dict | None = None,
):
    """INSERT OR REPLACE с сохранением best_only, если не передан явно."""
    _ensure_active_templates(c)
    if best_only is None:
        row = c.execute(
            "SELECT best_only FROM active_templates WHERE chat_id=?", (chat_id,)
        ).fetchone()
        best_only = _parse_best_only(row["best_only"] if row else None)
    else:
        best_only = _parse_best_only(best_only)
    c.execute(
        "INSERT OR REPLACE INTO active_templates(chat_id, names, markets, best_only) "
        "VALUES(?,?,?,?)",
        (
            chat_id,
            names_json,
            markets_json,
            json.dumps(best_only, ensure_ascii=False),
        ),
    )


def _parse_names_by_market(names_raw, tpls: dict | None) -> dict:
    """Нормализовать names → {"crypto": [...], "ru": [...]}.

    Старый формат — плоский список: раскладываем по market шаблона,
    либо кладём в оба рынка, если шаблонов нет.
    """
    try:
        parsed = json.loads(names_raw) if isinstance(names_raw, str) else names_raw
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = []

    if isinstance(parsed, dict):
        return {
            "crypto": [n for n in (parsed.get("crypto") or []) if n],
            "ru": [n for n in (parsed.get("ru") or []) if n],
        }

    flat = [n for n in (parsed or []) if n]
    if tpls:
        by = {"crypto": [], "ru": []}
        for n in flat:
            m = (tpls.get(n) or {}).get("market", "crypto")
            if m not in by:
                by[m] = []
            if n not in by[m]:
                by[m].append(n)
        return by
    # Без метаданных шаблонов — дублируем в оба рынка (безопасная миграция)
    return {"crypto": list(flat), "ru": list(flat)}


def _markets_from_names(by: dict) -> list[str]:
    return [m for m in ("crypto", "ru") if by.get(m)]


def set_active_templates(chat_id: int, names):
    """Установить активные шаблоны.

    names: list[str] (legacy flat → оба рынка) или dict per-market.
    markets = рынки с ≥1 шаблоном (вкл автоматически).
    """
    if isinstance(names, dict):
        by = {
            "crypto": list(names.get("crypto") or []),
            "ru": list(names.get("ru") or []),
        }
    else:
        flat = list(names or [])
        by = {"crypto": list(flat), "ru": list(flat)}
    markets = _markets_from_names(by)
    with _conn() as c:
        _upsert_active_templates(
            c,
            chat_id,
            json.dumps(by, ensure_ascii=False),
            json.dumps(markets, ensure_ascii=False),
        )


def set_active_templates_for_market(chat_id: int, market: str, names: list[str]):
    """Установить активные шаблоны только для одного рынка.

    Непустое names → рынок включается. Пустое names → рынок не трогаем
    (явный mkt_on отвечает за выкл).
    """
    if market not in ("crypto", "ru"):
        market = "crypto"
    cfg = get_active_config(chat_id)
    by = {
        "crypto": list(cfg["names_by_market"].get("crypto") or []),
        "ru": list(cfg["names_by_market"].get("ru") or []),
    }
    by[market] = list(names or [])
    markets = [m for m in ("crypto", "ru") if m in set(cfg.get("markets") or [])]
    if by[market] and market not in markets:
        markets.append(market)
    with _conn() as c:
        _upsert_active_templates(
            c,
            chat_id,
            json.dumps(by, ensure_ascii=False),
            json.dumps(markets, ensure_ascii=False),
        )


def set_active_markets(chat_id: int, markets: list[str]):
    """Явно задать включённые рынки (не чистит выбранные шаблоны)."""
    cfg = get_active_config(chat_id)
    by = {
        "crypto": list(cfg["names_by_market"].get("crypto") or []),
        "ru": list(cfg["names_by_market"].get("ru") or []),
    }
    wanted = [m for m in ("crypto", "ru") if m in set(markets or [])]
    with _conn() as c:
        _upsert_active_templates(
            c,
            chat_id,
            json.dumps(by, ensure_ascii=False),
            json.dumps(wanted, ensure_ascii=False),
        )


def set_market_enabled(chat_id: int, market: str, enabled: bool):
    """Вкл/выкл один рынок для сигналов, не трогая names_by_market."""
    if market not in ("crypto", "ru"):
        return
    cfg = get_active_config(chat_id)
    markets = [m for m in ("crypto", "ru") if m in set(cfg.get("markets") or [])]
    if enabled and market not in markets:
        markets.append(market)
    if not enabled and market in markets:
        markets = [m for m in markets if m != market]
    set_active_markets(chat_id, markets)


def get_active_config(chat_id: int) -> dict:
    """Активные шаблоны per-market + рынки с ≥1 активным шаблоном.

    Возвращает:
      names_by_market: {"crypto": [...], "ru": [...]}
      names: плоский union (для статуса / совместимости)
      markets: рынки, у которых есть активные шаблоны
    """
    with _conn() as c:
        _ensure_active_templates(c)
        row = c.execute(
            "SELECT names, markets, best_only FROM active_templates WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
    if not row:
        return {
            "names_by_market": {"crypto": [], "ru": []},
            "names": [],
            "markets": [],
            "best_only": {"crypto": False, "ru": False},
        }

    # Подтянуть market шаблонов для миграции плоского списка
    tpls = get_html_templates(chat_id)
    by = _parse_names_by_market(row["names"], tpls)

    # Если прочитали старый плоский формат — перезаписать в новом
    try:
        raw = json.loads(row["names"])
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = None
    if not isinstance(raw, dict):
        with _conn() as c:
            _ensure_active_templates(c)
            c.execute(
                "UPDATE active_templates SET names=?, markets=? WHERE chat_id=?",
                (
                    json.dumps(by, ensure_ascii=False),
                    json.dumps(_markets_from_names(by), ensure_ascii=False),
                    chat_id,
                ),
            )

    # markets column = explicit enable; migrate from names if empty/malformed
    try:
        stored_markets = json.loads(row["markets"]) if row["markets"] else []
    except (TypeError, ValueError, json.JSONDecodeError):
        stored_markets = []
    if not isinstance(stored_markets, list):
        stored_markets = []
    markets = [m for m in ("crypto", "ru") if m in stored_markets]
    if not markets:
        # Миграция: раньше markets выводились из names
        markets = _markets_from_names(by)

    flat = list(by.get("crypto") or []) + list(by.get("ru") or [])
    seen = set()
    names = []
    for n in flat:
        if n not in seen:
            seen.add(n)
            names.append(n)
    return {
        "names_by_market": by,
        "names": names,
        "markets": markets,
        "best_only": _parse_best_only(row["best_only"] if row is not None else None),
    }


def get_best_only(chat_id: int) -> dict:
    """Режим «только лучший» per-market: {"crypto": bool, "ru": bool}."""
    return dict(get_active_config(chat_id).get("best_only") or {"crypto": False, "ru": False})


def set_best_only(chat_id: int, market: str, enabled: bool):
    """Вкл/выкл «только лучший» для одного рынка (crypto|ru)."""
    if market not in ("crypto", "ru"):
        return
    with _conn() as c:
        _ensure_active_templates(c)
        row = c.execute(
            "SELECT names, markets, best_only FROM active_templates WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        bo = _parse_best_only(row["best_only"] if row else None)
        bo[market] = bool(enabled)
        if not row:
            _upsert_active_templates(
                c,
                chat_id,
                json.dumps({"crypto": [], "ru": []}, ensure_ascii=False),
                json.dumps([], ensure_ascii=False),
                best_only=bo,
            )
        else:
            c.execute(
                "UPDATE active_templates SET best_only=? WHERE chat_id=?",
                (json.dumps(bo, ensure_ascii=False), chat_id),
            )


# ── Переопределение стратегии для конкретного шаблона (per chat) ─────────────
# По умолчанию стратегия берётся из _strat, записанного в HTML при сохранении
# шаблона. Здесь хранится ТОЛЬКО явное переопределение, сделанное кнопкой в
# /filter — если записи нет, действует значение из HTML.

def _ensure_template_strategy(c: sqlite3.Connection):
    c.execute("""
        CREATE TABLE IF NOT EXISTS template_strategy (
            chat_id  INTEGER NOT NULL,
            name     TEXT NOT NULL,
            strategy TEXT NOT NULL,
            PRIMARY KEY (chat_id, name)
        )
    """)


def set_template_strategy(chat_id: int, name: str, strategy: str):
    """strategy: 'fbo' | 'brk' | 'both'."""
    with _conn() as c:
        _ensure_template_strategy(c)
        c.execute(
            "INSERT OR REPLACE INTO template_strategy(chat_id, name, strategy) VALUES(?,?,?)",
            (chat_id, name, strategy),
        )


def get_template_strategy_overrides(chat_id: int) -> dict:
    """Вернуть {name: strategy} только для шаблонов с явным переопределением."""
    with _conn() as c:
        _ensure_template_strategy(c)
        rows = c.execute(
            "SELECT name, strategy FROM template_strategy WHERE chat_id=?", (chat_id,)
        ).fetchall()
    return {r["name"]: r["strategy"] for r in rows}


# ── Sync-токены (стабильные, переживают рестарт) ─────────────────────────────

def _ensure_sync_tokens(c: sqlite3.Connection):
    c.execute("""
        CREATE TABLE IF NOT EXISTS sync_tokens (
            chat_id INTEGER PRIMARY KEY,
            token   TEXT NOT NULL UNIQUE
        )
    """)


def save_sync_token(chat_id: int, token: str) -> None:
    with _conn() as c:
        _ensure_sync_tokens(c)
        # один токен на чат; старый token-row с тем же token у другого чата — убрать
        c.execute("DELETE FROM sync_tokens WHERE token=? AND chat_id!=?", (token, chat_id))
        c.execute(
            "INSERT OR REPLACE INTO sync_tokens(chat_id, token) VALUES(?,?)",
            (chat_id, token),
        )


def get_chat_id_by_sync_token(token: str) -> int | None:
    with _conn() as c:
        _ensure_sync_tokens(c)
        row = c.execute(
            "SELECT chat_id FROM sync_tokens WHERE token=?", (token,)
        ).fetchone()
    return int(row["chat_id"]) if row else None


def get_sync_token_for_chat(chat_id: int) -> str | None:
    with _conn() as c:
        _ensure_sync_tokens(c)
        row = c.execute(
            "SELECT token FROM sync_tokens WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row["token"] if row else None


def load_all_sync_tokens() -> dict[str, int]:
    """token → chat_id для прогрева in-memory кэша при старте."""
    with _conn() as c:
        _ensure_sync_tokens(c)
        rows = c.execute("SELECT token, chat_id FROM sync_tokens").fetchall()
    return {r["token"]: int(r["chat_id"]) for r in rows}


# ── AI chat: режим, история, pending-подтверждения ───────────────────────────

_AI_HISTORY_TTL_SEC = 6 * 3600
_AI_PENDING_TTL_SEC = 15 * 60


def _ensure_ai_tables(c: sqlite3.Connection):
    c.executescript("""
        CREATE TABLE IF NOT EXISTS ai_state (
            chat_id    INTEGER PRIMARY KEY,
            enabled    INTEGER NOT NULL DEFAULT 0,
            updated_ts INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_messages (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role    TEXT NOT NULL,
            content TEXT NOT NULL,
            ts      INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ai_messages_chat_ts
            ON ai_messages(chat_id, ts);
        CREATE TABLE IF NOT EXISTS ai_pending (
            chat_id       INTEGER PRIMARY KEY,
            proposal_json TEXT NOT NULL,
            summary       TEXT NOT NULL,
            expires       INTEGER NOT NULL
        );
    """)


def get_ai_enabled(chat_id: int) -> bool:
    with _conn() as c:
        _ensure_ai_tables(c)
        row = c.execute(
            "SELECT enabled FROM ai_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return bool(row["enabled"]) if row else False


def set_ai_enabled(chat_id: int, enabled: bool) -> None:
    with _conn() as c:
        _ensure_ai_tables(c)
        c.execute(
            "INSERT OR REPLACE INTO ai_state(chat_id, enabled, updated_ts) VALUES(?,?,?)",
            (chat_id, 1 if enabled else 0, int(time.time())),
        )


def append_ai_message(chat_id: int, role: str, content: str, keep: int = 16) -> None:
    """role: 'user' | 'model'. Хранит последние keep сообщений, чистит старше TTL."""
    now = int(time.time())
    with _conn() as c:
        _ensure_ai_tables(c)
        c.execute(
            "INSERT INTO ai_messages(chat_id, role, content, ts) VALUES(?,?,?,?)",
            (chat_id, role, content, now),
        )
        cutoff = now - _AI_HISTORY_TTL_SEC
        c.execute("DELETE FROM ai_messages WHERE chat_id=? AND ts<?", (chat_id, cutoff))
        rows = c.execute(
            "SELECT id FROM ai_messages WHERE chat_id=? ORDER BY id DESC",
            (chat_id,),
        ).fetchall()
        if len(rows) > keep:
            drop_ids = [r["id"] for r in rows[keep:]]
            c.executemany("DELETE FROM ai_messages WHERE id=?", [(i,) for i in drop_ids])


def get_ai_history(chat_id: int, limit: int = 16) -> list[dict]:
    now = int(time.time())
    with _conn() as c:
        _ensure_ai_tables(c)
        c.execute(
            "DELETE FROM ai_messages WHERE chat_id=? AND ts<?",
            (chat_id, now - _AI_HISTORY_TTL_SEC),
        )
        rows = c.execute(
            "SELECT role, content FROM ai_messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    out = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    return out


def clear_ai_history(chat_id: int) -> None:
    with _conn() as c:
        _ensure_ai_tables(c)
        c.execute("DELETE FROM ai_messages WHERE chat_id=?", (chat_id,))


def set_ai_pending(chat_id: int, proposal: dict, summary: str) -> None:
    with _conn() as c:
        _ensure_ai_tables(c)
        c.execute(
            "INSERT OR REPLACE INTO ai_pending(chat_id, proposal_json, summary, expires) "
            "VALUES(?,?,?,?)",
            (
                chat_id,
                json.dumps(proposal, ensure_ascii=False),
                summary,
                int(time.time()) + _AI_PENDING_TTL_SEC,
            ),
        )


def get_ai_pending(chat_id: int) -> dict | None:
    now = int(time.time())
    with _conn() as c:
        _ensure_ai_tables(c)
        row = c.execute(
            "SELECT proposal_json, summary, expires FROM ai_pending WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        if not row:
            return None
        if int(row["expires"]) < now:
            c.execute("DELETE FROM ai_pending WHERE chat_id=?", (chat_id,))
            return None
        try:
            proposal = json.loads(row["proposal_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            c.execute("DELETE FROM ai_pending WHERE chat_id=?", (chat_id,))
            return None
    return {"proposal": proposal, "summary": row["summary"]}


def clear_ai_pending(chat_id: int) -> None:
    with _conn() as c:
        _ensure_ai_tables(c)
        c.execute("DELETE FROM ai_pending WHERE chat_id=?", (chat_id,))


# ── Mini App UI state (watch / backtest / signals) — keyed by Telegram user_id ─
# Separate from html_templates / watchlist bot tables. Never migrates templates.

_MINIAPP_MAX_ITEMS = 2500  # per-market cap for backtest/signals blobs


def _ensure_miniapp_state(c: sqlite3.Connection):
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS miniapp_watch (
            user_id    INTEGER PRIMARY KEY,
            data_json  TEXT NOT NULL DEFAULT '[]',
            updated_at INTEGER NOT NULL,
            cleared    INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS miniapp_backtest (
            user_id    INTEGER PRIMARY KEY,
            data_json  TEXT NOT NULL DEFAULT '{}',
            updated_at INTEGER NOT NULL,
            cleared    INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS miniapp_signals (
            user_id    INTEGER PRIMARY KEY,
            data_json  TEXT NOT NULL DEFAULT '{}',
            updated_at INTEGER NOT NULL,
            cleared    INTEGER NOT NULL DEFAULT 0
        );
        """
    )


def _json_loads(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _cap_market_lists(obj: dict, cap: int = _MINIAPP_MAX_ITEMS) -> dict:
    """Keep only crypto/ru list keys; trim each list to last `cap` items."""
    out: dict = {}
    if not isinstance(obj, dict):
        return {"crypto": [], "ru": []}
    for m in ("crypto", "ru"):
        v = obj.get(m, [])
        if not isinstance(v, list):
            v = []
        if len(v) > cap:
            v = v[-cap:]
        out[m] = v
    return out


def get_miniapp_state(user_id: int) -> dict:
    """Return watch + backtest + signals for Mini App hydrate."""
    with _conn() as c:
        _ensure_miniapp_state(c)
        w = c.execute(
            "SELECT data_json, updated_at, cleared FROM miniapp_watch WHERE user_id=?",
            (user_id,),
        ).fetchone()
        b = c.execute(
            "SELECT data_json, updated_at, cleared FROM miniapp_backtest WHERE user_id=?",
            (user_id,),
        ).fetchone()
        s = c.execute(
            "SELECT data_json, updated_at, cleared FROM miniapp_signals WHERE user_id=?",
            (user_id,),
        ).fetchone()

    watch = _json_loads(w["data_json"] if w else None, [])
    if not isinstance(watch, list):
        watch = []
    backtest = _cap_market_lists(_json_loads(b["data_json"] if b else None, {}))
    signals = _cap_market_lists(_json_loads(s["data_json"] if s else None, {}))

    return {
        "watch": watch,
        "backtest": backtest,
        "signals": signals,
        "updated_at": {
            "watch": int(w["updated_at"]) if w else 0,
            "backtest": int(b["updated_at"]) if b else 0,
            "signals": int(s["updated_at"]) if s else 0,
        },
        "cleared": {
            "watch": bool(w["cleared"]) if w else False,
            "backtest": bool(b["cleared"]) if b else False,
            "signals": bool(s["cleared"]) if s else False,
        },
    }


def _empty_overwrite_blocked(
    *,
    incoming_empty: bool,
    server_nonempty: bool,
    clear: bool,
    client_updated_at,
    server_updated_at: int,
) -> bool:
    """Protect against accidental empty wipe (boot glitch / wiped WebView).

    Allow empty write only when:
      - clear=True (explicit reset from UI confirm), OR
      - client_updated_at matches server (client was in sync and emptied intentionally).
    """
    if not incoming_empty or not server_nonempty:
        return False
    if clear:
        return False
    try:
        cu = int(client_updated_at) if client_updated_at is not None else None
    except (TypeError, ValueError):
        cu = None
    if cu is not None and server_updated_at and cu == int(server_updated_at):
        return False
    return True


def put_miniapp_state(
    user_id: int,
    *,
    watch=None,
    backtest=None,
    signals=None,
    clear_watch: bool = False,
    clear_backtest: bool = False,
    clear_signals: bool = False,
    client_updated_at: dict | None = None,
) -> dict:
    """Upsert Mini App state. Skips keys that are None (partial PUT).

    Empty-overwrite guard (per key): if client sends empty while server has data,
    require clear_*=True OR matching client_updated_at[key] == server updated_at.
    Rejected keys are left unchanged and listed in result['rejected'].

    Rule documented for operators: never wipe server watch/backtest/signals on a
    blank first-load PUT; only explicit clear from UI confirm (or in-sync empty).
    """
    if client_updated_at is None:
        client_updated_at = {}
    if not isinstance(client_updated_at, dict):
        client_updated_at = {}

    now = int(time.time())
    rejected: list[str] = []
    current = get_miniapp_state(user_id)

    with _conn() as c:
        _ensure_miniapp_state(c)

        if watch is not None:
            if not isinstance(watch, list):
                watch = []
            server_w = current["watch"]
            if _empty_overwrite_blocked(
                incoming_empty=len(watch) == 0,
                server_nonempty=bool(server_w),
                clear=clear_watch,
                client_updated_at=client_updated_at.get("watch"),
                server_updated_at=current["updated_at"]["watch"],
            ):
                rejected.append("watch")
            else:
                c.execute(
                    "INSERT OR REPLACE INTO miniapp_watch"
                    "(user_id, data_json, updated_at, cleared) VALUES(?,?,?,?)",
                    (
                        user_id,
                        json.dumps(watch, ensure_ascii=False),
                        now,
                        1 if (clear_watch or len(watch) == 0) else 0,
                    ),
                )

        if backtest is not None:
            bt = _cap_market_lists(backtest if isinstance(backtest, dict) else {})
            server_b = current["backtest"]
            incoming_empty = not bt.get("crypto") and not bt.get("ru")
            server_nonempty = bool(server_b.get("crypto") or server_b.get("ru"))
            if _empty_overwrite_blocked(
                incoming_empty=incoming_empty,
                server_nonempty=server_nonempty,
                clear=clear_backtest,
                client_updated_at=client_updated_at.get("backtest"),
                server_updated_at=current["updated_at"]["backtest"],
            ):
                rejected.append("backtest")
            else:
                c.execute(
                    "INSERT OR REPLACE INTO miniapp_backtest"
                    "(user_id, data_json, updated_at, cleared) VALUES(?,?,?,?)",
                    (
                        user_id,
                        json.dumps(bt, ensure_ascii=False),
                        now,
                        1 if (clear_backtest or incoming_empty) else 0,
                    ),
                )

        if signals is not None:
            sig = _cap_market_lists(signals if isinstance(signals, dict) else {})
            server_s = current["signals"]
            incoming_empty = not sig.get("crypto") and not sig.get("ru")
            server_nonempty = bool(server_s.get("crypto") or server_s.get("ru"))
            if _empty_overwrite_blocked(
                incoming_empty=incoming_empty,
                server_nonempty=server_nonempty,
                clear=clear_signals,
                client_updated_at=client_updated_at.get("signals"),
                server_updated_at=current["updated_at"]["signals"],
            ):
                rejected.append("signals")
            else:
                c.execute(
                    "INSERT OR REPLACE INTO miniapp_signals"
                    "(user_id, data_json, updated_at, cleared) VALUES(?,?,?,?)",
                    (
                        user_id,
                        json.dumps(sig, ensure_ascii=False),
                        now,
                        1 if (clear_signals or incoming_empty) else 0,
                    ),
                )

    out = get_miniapp_state(user_id)
    out["ok"] = True
    out["rejected"] = rejected
    return out
