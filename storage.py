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
            ticker  TEXT NOT NULL,
            side    TEXT NOT NULL,
            level   REAL NOT NULL,
            expires INTEGER NOT NULL,
            PRIMARY KEY (ticker, side, level)
        );
        """)


# ── Дедупликация (правило: один сигнал на монету раз в 12 ч) ─────────────────

def is_duplicate(ticker: str, side: str, level: float) -> bool:
    with _conn() as c:
        now = int(time.time())
        row = c.execute(
            "SELECT expires FROM dedup WHERE ticker=? AND side=? AND ABS(level-?)<?",
            (ticker, side, level, level * 0.005),
        ).fetchone()
        if row and row["expires"] > now:
            return True
        return False


def mark_sent(ticker: str, side: str, level: float, ttl_hours: int = 12):
    expires = int(time.time()) + ttl_hours * 3600
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO dedup(ticker,side,level,expires) VALUES(?,?,?,?)",
            (ticker, side, level, expires),
        )
        c.execute(
            "INSERT INTO signals(ticker,side,level,kind,ts) VALUES(?,?,?,?,?)",
            (ticker, side, level, "", int(time.time())),
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
