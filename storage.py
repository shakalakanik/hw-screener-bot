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

        CREATE TABLE IF NOT EXISTS html_templates (
            user_id        INTEGER PRIMARY KEY,
            templates_json TEXT NOT NULL DEFAULT '{}',
            active_name    TEXT
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


def set_custom_filter(chat_id: int, filt: dict):
    """Сохранить произвольный фильтр бота (custom_json). template помечаем miniapp."""
    with _conn() as c:
        c.execute(
            "INSERT INTO user_filters(chat_id, template, custom_json) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET template=excluded.template, "
            "custom_json=excluded.custom_json",
            (chat_id, "miniapp", json.dumps(filt, ensure_ascii=False)),
        )


# ── HTML-шаблоны Mini App ─────────────────────────────────────────────────────

def get_html_templates(user_id: int) -> tuple[dict, str | None]:
    """Вернуть (templates_dict, active_name)."""
    with _conn() as c:
        row = c.execute(
            "SELECT templates_json, active_name FROM html_templates WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return {}, None
    try:
        tpl = json.loads(row["templates_json"] or "{}")
    except json.JSONDecodeError:
        tpl = {}
    if not isinstance(tpl, dict):
        tpl = {}
    return tpl, row["active_name"]


def set_html_templates(user_id: int, templates: dict):
    if not isinstance(templates, dict):
        raise ValueError("templates must be a dict")
    with _conn() as c:
        row = c.execute(
            "SELECT active_name FROM html_templates WHERE user_id=?", (user_id,)
        ).fetchone()
        active = row["active_name"] if row else None
        if active and active not in templates:
            active = None
        c.execute(
            "INSERT INTO html_templates(user_id, templates_json, active_name) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET templates_json=excluded.templates_json, "
            "active_name=excluded.active_name",
            (user_id, json.dumps(templates, ensure_ascii=False), active),
        )


def set_one_html_template(user_id: int, name: str, filt: dict):
    tpl, active = get_html_templates(user_id)
    tpl[name] = filt
    with _conn() as c:
        c.execute(
            "INSERT INTO html_templates(user_id, templates_json, active_name) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET templates_json=excluded.templates_json",
            (user_id, json.dumps(tpl, ensure_ascii=False), active),
        )


def delete_html_template(user_id: int, name: str) -> bool:
    tpl, active = get_html_templates(user_id)
    if name not in tpl:
        return False
    del tpl[name]
    if active == name:
        active = None
    with _conn() as c:
        c.execute(
            "INSERT INTO html_templates(user_id, templates_json, active_name) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET templates_json=excluded.templates_json, "
            "active_name=excluded.active_name",
            (user_id, json.dumps(tpl, ensure_ascii=False), active),
        )
    return True


def set_active_html_template(user_id: int, name: str | None):
    tpl, _ = get_html_templates(user_id)
    if name is not None and name not in tpl:
        raise KeyError(f"template not found: {name}")
    with _conn() as c:
        row = c.execute(
            "SELECT templates_json FROM html_templates WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            c.execute(
                "INSERT INTO html_templates(user_id, templates_json, active_name) VALUES(?,?,?)",
                (user_id, "{}", name),
            )
        else:
            c.execute(
                "UPDATE html_templates SET active_name=? WHERE user_id=?",
                (name, user_id),
            )


def map_html_filter_to_bot(html: dict, name: str = "custom") -> dict:
    """Best-effort HTML filter keys → bot filter for run_scan."""
    html = html or {}

    strength_min = 3
    raw_str = html.get("str")
    if raw_str is not None and str(raw_str).lower() not in ("auto", "", "null", "none"):
        try:
            strength_min = int(float(str(raw_str).replace(",", ".")))
        except (TypeError, ValueError):
            strength_min = 3

    dist_atr_max = 0.5
    raw_dist = html.get("dist")
    if raw_dist is not None and str(raw_dist).lower() not in ("auto", "", "null", "none"):
        try:
            dist_atr_max = float(str(raw_dist).replace(",", "."))
        except (TypeError, ValueError):
            dist_atr_max = 0.5

    sides = ["LONG", "SHORT"]
    raw_side = str(html.get("side", "auto") or "auto").lower()
    if raw_side == "long":
        sides = ["LONG"]
    elif raw_side == "short":
        sides = ["SHORT"]

    raw_bias = str(html.get("bias", "auto") or "auto").lower()
    bias_filter = raw_bias not in ("auto", "", "null", "none", "off", "flat")

    return {
        "desc": f"miniapp:{name}",
        "strength_min": strength_min,
        "dist_atr_max": dist_atr_max,
        "sides": sides,
        "bias_filter": bias_filter,
        "html": html,
    }


def apply_active_template_to_bot_filter(user_id: int, name: str | None = None):
    """Установить active HTML-шаблон и пробросить mapped фильтр в user_filters."""
    tpl, active = get_html_templates(user_id)
    use = name if name is not None else active
    if not use or use not in tpl:
        raise KeyError("no active/named template")
    set_active_html_template(user_id, use)
    bot_filt = map_html_filter_to_bot(tpl[use], use)
    set_custom_filter(user_id, bot_filt)
    return bot_filt
