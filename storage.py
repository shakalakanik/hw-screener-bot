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
        """)
        _migrate(c)


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


def save_html_templates(chat_id: int, templates: dict, market: str = "crypto"):
    """Сохранить шаблоны из HTML-скринера (merge по имени, per-market replace).

    Рынок шаблона: filters[_market] если есть, иначе аргумент market.
    Шаблоны других рынков не трогаем. Strategy overrides по имени сохраняются.
    Если в HTML переименовали (1 ушёл / 1 пришёл) — обновляем active + strategy.
    """
    with _conn() as c:
        _ensure_html_templates(c)
        _ensure_template_strategy(c)
        _ensure_active_templates(c)

        items: list[tuple[str, dict, str]] = []
        for name, filters in templates.items():
            flt = filters if isinstance(filters, dict) else {}
            m = flt.get("_market") if isinstance(flt, dict) else None
            if m not in ("crypto", "ru"):
                m = market if market in ("crypto", "ru") else "crypto"
            items.append((name, flt if isinstance(filters, dict) else filters, m))

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
            markets TEXT NOT NULL DEFAULT '["crypto"]'
        )
    """)


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
        _ensure_active_templates(c)
        c.execute(
            "INSERT OR REPLACE INTO active_templates(chat_id, names, markets) VALUES(?,?,?)",
            (
                chat_id,
                json.dumps(by, ensure_ascii=False),
                json.dumps(markets, ensure_ascii=False),
            ),
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
        _ensure_active_templates(c)
        c.execute(
            "INSERT OR REPLACE INTO active_templates(chat_id, names, markets) VALUES(?,?,?)",
            (
                chat_id,
                json.dumps(by, ensure_ascii=False),
                json.dumps(markets, ensure_ascii=False),
            ),
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
        _ensure_active_templates(c)
        c.execute(
            "INSERT OR REPLACE INTO active_templates(chat_id, names, markets) VALUES(?,?,?)",
            (
                chat_id,
                json.dumps(by, ensure_ascii=False),
                json.dumps(wanted, ensure_ascii=False),
            ),
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
            "SELECT names, markets FROM active_templates WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if not row:
        return {
            "names_by_market": {"crypto": [], "ru": []},
            "names": [],
            "markets": [],
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
    }


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
