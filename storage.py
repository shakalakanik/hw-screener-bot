"""storage.py — SQLite-хранилище сигналов, настроек и дедупликации."""
import sqlite3
import json
import time
from pathlib import Path

DB_PATH = Path("hw_bot.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
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
        """)


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


def mark_sent(ticker: str, side: str, level: float, strategy: str = "brk", ttl_hours: int = 12):
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
    """Получить watchlist пользователя в формате совместимом с HTML."""
    with _conn() as c:
        _ensure_watchlist(c)
        rows = c.execute(
            "SELECT * FROM watchlist WHERE chat_id=? AND market=? ORDER BY added_ts DESC LIMIT 200",
            (chat_id, market),
        ).fetchall()
    result = []
    for r in rows:
        result.append({
            "type": r["strategy"],   # 'brk' | 'fbo' — реальная стратегия сигнала, как в HTML
            "base": r["ticker"],
            "sym": r["ticker"] + "USDT",
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


def save_html_templates(chat_id: int, templates: dict, market: str = "crypto"):
    """Сохранить шаблоны из HTML-скринера."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS html_templates (
                chat_id  INTEGER NOT NULL,
                name     TEXT NOT NULL,
                filters  TEXT NOT NULL,
                market   TEXT NOT NULL DEFAULT 'crypto',
                PRIMARY KEY (chat_id, name)
            )
        """)
        # Удаляем старые шаблоны этого пользователя и вставляем новые
        c.execute("DELETE FROM html_templates WHERE chat_id=?", (chat_id,))
        for name, filters in templates.items():
            c.execute(
                "INSERT INTO html_templates(chat_id, name, filters, market) VALUES(?,?,?,?)",
                (chat_id, name, json.dumps(filters, ensure_ascii=False), market),
            )


def get_html_templates(chat_id: int) -> dict:
    """Получить все шаблоны пользователя из HTML."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS html_templates (
                chat_id  INTEGER NOT NULL,
                name     TEXT NOT NULL,
                filters  TEXT NOT NULL,
                market   TEXT NOT NULL DEFAULT 'crypto',
                PRIMARY KEY (chat_id, name)
            )
        """)
        rows = c.execute(
            "SELECT name, filters, market FROM html_templates WHERE chat_id=?",
            (chat_id,),
        ).fetchall()
    return {r["name"]: {"filters": json.loads(r["filters"]), "market": r["market"]} for r in rows}


# ── Активные подписки на шаблоны ─────────────────────────────────────────────

def set_active_templates(chat_id: int, names: list[str]):
    """Установить список активных шаблонов для пользователя."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS active_templates (
                chat_id INTEGER PRIMARY KEY,
                names   TEXT NOT NULL DEFAULT '[]',
                markets TEXT NOT NULL DEFAULT '["crypto"]'
            )
        """)
        c.execute(
            "INSERT OR REPLACE INTO active_templates(chat_id, names, markets) "
            "SELECT ?, ?, COALESCE((SELECT markets FROM active_templates WHERE chat_id=?), '[\"crypto\"]')",
            (chat_id, json.dumps(names, ensure_ascii=False), chat_id),
        )


def set_active_markets(chat_id: int, markets: list[str]):
    """Установить список активных рынков."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS active_templates (
                chat_id INTEGER PRIMARY KEY,
                names   TEXT NOT NULL DEFAULT '[]',
                markets TEXT NOT NULL DEFAULT '["crypto"]'
            )
        """)
        c.execute(
            "INSERT OR REPLACE INTO active_templates(chat_id, names, markets) "
            "SELECT ?, COALESCE((SELECT names FROM active_templates WHERE chat_id=?), '[]'), ?",
            (chat_id, chat_id, json.dumps(markets, ensure_ascii=False)),
        )


def get_active_config(chat_id: int) -> dict:
    """Получить активные шаблоны и рынки пользователя."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS active_templates (
                chat_id INTEGER PRIMARY KEY,
                names   TEXT NOT NULL DEFAULT '[]',
                markets TEXT NOT NULL DEFAULT '["crypto"]'
            )
        """)
        row = c.execute(
            "SELECT names, markets FROM active_templates WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if not row:
        return {"names": [], "markets": ["crypto"]}
    return {
        "names": json.loads(row["names"]),
        "markets": json.loads(row["markets"]),
    }
