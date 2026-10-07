"""bot.py — HW Screener Bot с синхронизацией шаблонов из HTML."""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatAction
from aiogram.types import (
    Message, CallbackQuery,
    BotCommand, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup,
    WebAppInfo,
)
from urllib.parse import quote

import storage
import ai_chat
import journal_charts
from screener import run_scan, run_manual_scan, check_signal_outcomes, auto_template_entry

AUTO_NAME = storage.AUTO_TEMPLATE_NAME

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

TOKEN           = os.environ["TG_BOT_TOKEN"]
# Авто-скан раз в час по часам: сразу после закрытия H1-свечи (HH:00 + задержка на
# публикацию закрытой свечи биржей). Все сигналы должны уйти в окне HH:00–HH:05.
# SCAN_INTERVAL_MIN больше не используется (старые 15 мин не могут переопределить расписание).
SCAN_HOUR_DELAY_S = max(5, min(120, int(os.environ.get("SCAN_HOUR_DELAY_S", "40"))))
SCAN_SEND_DEADLINE_S = 300   # к HH:05 всё должно быть отправлено
SCAN_SCHEDULE_TEXT = "раз в час, сразу после закрытия часовой свечи (сигналы в HH:00–HH:05 МСК)"


def next_scan_ts(now: float, delay_s: int = SCAN_HOUR_DELAY_S) -> float:
    """Ближайший момент HH:00:00 + delay_s строго после now (unix-секунды; часы UTC = часы МСК)."""
    t = (int(now) // 3600) * 3600 + delay_s
    while t <= now:
        t += 3600
    return float(t)
PORT            = int(os.environ.get("PORT", "8080"))
PUBLIC_URL      = os.environ.get("PUBLIC_URL", "").rstrip("/")   # https://yourapp.railway.app
MSK = timezone(timedelta(hours=3))

bot = Bot(token=TOKEN)
dp  = Dispatcher()

_subscribers: set[int] = set()
_pending_cards: dict[str, dict] = {}   # card_id → card data (also persisted in SQLite 48h)
_pending_rename: dict[int, str] = {}   # chat_id → old template name
_filter_tab: dict[int, str] = {}       # chat_id → "crypto" | "ru"

# ── Sync-токены: chat_id → token ──────────────────────────────────────────────
_sync_tokens: dict[str, int] = {}   # token → chat_id

# Ручные фоновые сканы из Mini App: job_id → state; один активный на chat
_manual_jobs: dict[str, dict] = {}
_manual_job_by_chat: dict[int, str] = {}

# ── Sync-токен: стабильный per chat (без hourly bucket) ───────────────────────
def _make_token(chat_id: int) -> str:
    """Стабильный токен: не меняется при рестарте/деплое (пока TOKEN тот же)."""
    raw = f"{chat_id}:{TOKEN}:hw-sync-v1"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _register_sync_token(chat_id: int) -> str:
    token = _make_token(chat_id)
    _sync_tokens[token] = chat_id
    storage.save_sync_token(chat_id, token)
    return token


def _resolve_sync_chat(token: str) -> int | None:
    chat_id = _sync_tokens.get(token)
    if chat_id is not None:
        return chat_id
    chat_id = storage.get_chat_id_by_sync_token(token)
    if chat_id is not None:
        _sync_tokens[token] = chat_id
        return chat_id
    return None


def _sync_endpoint(chat_id: int) -> str | None:
    if not PUBLIC_URL:
        return None
    token = _register_sync_token(chat_id)
    return f"{PUBLIC_URL}/sync/{token}"


def _journal_slug(market: str | None) -> str:
    """Path segment. Мосбиржа → moex (query string Telegram может выкинуть)."""
    m = storage.normalize_journal_market(market)
    return "moex" if m == "ru" else "algo" if m == "algo" else "crypto"


def _app_url_with_sync(
    chat_id: int,
    focus: tuple | None = None,
    watch: bool = False,
    journal: tuple | None = None,
) -> str | None:
    """URL Mini App.

    journal=(market, journal_id|None) → путь /app/journal/crypto[/id] или
    /app/journal/moex[/id]. Путь, а не только ?tab=: Telegram иногда отбрасывает
    query у web_app. watch/focus оставлен для старых ссылок на «Отслеживаемые».
    """
    sync = _sync_endpoint(chat_id)
    if not sync:
        return None
    if journal is not None:
        market, journal_id = journal[0], (journal[1] if len(journal) > 1 else None)
        slug = _journal_slug(market)
        path = f"/app/journal/{slug}"
        if journal_id:
            path += f"/{int(journal_id)}"
        fm = "ru" if slug == "moex" else slug
        # tab=journal первым в query — запасной канал, если query всё же доедет
        url = f"{PUBLIC_URL}{path}?tab=journal&fm={fm}"
        if journal_id:
            url += f"&jid={int(journal_id)}"
        url += f"&sync={quote(sync, safe='')}&autosync=1"
        return url
    path = "/app/watch" if (watch or focus) else "/app"
    if watch or focus:
        url = f"{PUBLIC_URL}{path}?tab=watch"
        if focus:
            ticker, ts, mkt = focus
            base = ticker[:-4] if str(ticker).endswith("USDT") else ticker
            url += f"&focus={quote(str(base), safe='')}&ft={int(ts)}&fm={quote(str(mkt), safe='')}"
        url += f"&sync={quote(sync, safe='')}&autosync=1"
    else:
        url = f"{PUBLIC_URL}{path}?sync={quote(sync, safe='')}&autosync=1"
    return url


def _remember_journal(chat_id: int, card: dict) -> int | None:
    """Строка журнала до отправки. Если Telegram не примет сообщение — удалить."""
    try:
        jid, dropped = storage.add_signal_journal(chat_id, card)
    except Exception as e:
        logger.warning("journal save failed chat=%s: %s", chat_id, e)
        return None
    if dropped:
        try:
            journal_charts.unlink_ids(dropped)
        except Exception as e:
            logger.warning("journal prune files: %s", e)
    return jid


def _drop_journal(journal_id: int | None) -> None:
    if not journal_id:
        return
    try:
        storage.delete_signal_journal(journal_id)
    except Exception as e:
        logger.warning("journal delete %s: %s", journal_id, e)
    try:
        journal_charts.unlink_ids([journal_id])
    except Exception:
        pass


async def _fill_journal_charts(journal_id: int) -> None:
    try:
        await journal_charts.ensure(journal_id)
    except Exception as e:
        logger.warning("journal charts %s: %s", journal_id, e)


def _journal_startapp_url(market: str | None, journal_id: int | None) -> str:
    """Прямая ссылка Mini App. startapp доходит даже когда web_app открывает только /app.

    Допустимые символы startapp: A-Z a-z 0-9 _ -
    j_c / j_c_<id> — крипта, j_m / j_m_<id> — Мосбиржа, j_a / j_a_<id> — Крипта (Алго).
    """
    m = storage.normalize_journal_market(market)
    code = "m" if m == "ru" else "a" if m == "algo" else "c"   # j_a / j_a_<id> — Крипта (Алго)
    param = f"j_{code}"
    if journal_id:
        param += f"_{int(journal_id)}"
    return f"https://t.me/HWtradingscreener_bot?startapp={param}"


def _view_signal_button(
    chat_id: int,
    ticker: str,
    signal_ts: int,
    market: str,
    journal_id: int | None = None,
) -> InlineKeyboardButton | None:
    """Открывает Mini App сразу на «Журнал сигналов» нужного рынка."""
    if not journal_id:
        try:
            journal_id = storage.find_signal_journal_id(
                chat_id, ticker, int(signal_ts or 0), market or "crypto",
            )
        except Exception as e:
            logger.warning("journal lookup failed: %s", e)
            journal_id = None
    # web_app-кнопка (только личные чаты) открывает Mini App всегда заново и сразу по адресу
    # /app/journal/<рынок>/<id>; не зависит от Main Mini App в BotFather. Ссылка t.me?startapp
    # при уже открытом/свёрнутом Mini App лишь разворачивает старое окно — она запасной вариант
    # (группы/нет PUBLIC_URL).
    if int(chat_id or 0) > 0:
        app = _app_url_with_sync(chat_id, journal=(market or "crypto", journal_id))
        if app:
            return InlineKeyboardButton(text="🔎 Посмотреть сигнал", web_app=WebAppInfo(url=app))
    return InlineKeyboardButton(
        text="🔎 Посмотреть сигнал",
        url=_journal_startapp_url(market or "crypto", journal_id),
    )


def main_keyboard(chat_id: int | None = None) -> ReplyKeyboardMarkup:
    """Клавиатура. Шаблоны обновляются только синхронизацией («🔗 Синхронизация шаблонов» / /syncurl).
    ИИ всегда включён — отдельной кнопки нет."""
    rows = [
        [KeyboardButton(text="📡 Скан"), KeyboardButton(text="⚙️ Фильтр")],
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="🔗 Синхронизация шаблонов")],
    ]
    rows.append([KeyboardButton(text="▶️ Старт"), KeyboardButton(text="⛔ Стоп")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


# Совместимость: статический alias (без WebApp) — лучше передавать chat_id
MAIN_KEYBOARD = main_keyboard()


# ── Форматирование сигнала ────────────────────────────────────────────────────
_MARKET_LABEL = {"crypto": "🌐 Крипта", "ru": "🇷🇺 Мосбиржа", "algo": "⚡ Крипта (Алго)"}


def _format_card(card: dict) -> str:
    side_emoji = "🟢" if card["side"] == "LONG" else "🔴"
    bias_map   = {"up": "↑ up", "down": "↓ down", "flat": "→ flat"}
    bias = bias_map.get(card.get("d1_bias", "flat"), "flat")

    entry    = card["last"]
    stop     = card["stop"]
    take     = card["take"]
    level    = card["level"]
    risk_pct = abs(entry - stop) / entry * 100
    tp_pct   = abs(take - entry)  / entry * 100
    dist_pct = card.get("dist_atr", 0) * 100   # dist_atr — доля ATR (0.72 = 72% ATR), не сам ATR

    market   = card.get("market", "crypto")
    market_label = _MARKET_LABEL.get(market, market)
    tpl_name = card.get("matched_template") or ""
    tpl_line = f"📋 Шаблон: <b>{tpl_name}</b>\n" if tpl_name else ""

    strategy = card.get("strategy", "brk")
    strategy_label = ("📈 Пробой" if strategy == "brk" else "⚡ Алго" if strategy == "algo"
                      else "🔻 Ложный пробой")
    prob = card.get("prob")
    prob_line = f"  (модель p={prob:.2f})" if strategy == "fbo" and prob is not None else ""

    # Время самого сигнала (час его появления на рынке), не время отправки сообщения. MSK, не UTC.
    signal_ts = card.get("signal_ts")
    if signal_ts:
        ts_msk = datetime.fromtimestamp(signal_ts / 1000, tz=MSK)
        time_str = ts_msk.strftime("%d.%m %H:%M МСК")
    else:
        time_str = datetime.now(MSK).strftime("%d.%m %H:%M МСК") + " (время скана)"

    if strategy == "algo":   # «Крипта (Алго)»: вход на закрытии часа, стоп max(1.5·ATR14 H1, 2%), тейк 3R
        return "\n".join([
            f"{side_emoji} <b>{card['ticker']}</b> — {card['side']}",
            f"{market_label}  ·  {strategy_label}",
            tpl_line.rstrip("\n"),
            f"🧩 Условия: {card.get('algo_combo') or '—'}",
            f"📥 Вход: <code>{entry:.4f}</code>",
            f"⛔ Стоп-лосс: <code>{stop:.4f}</code>  (−{risk_pct:.1f}%)",
            f"💰 Тейк-профит: <code>{take:.4f}</code>  (+{tp_pct:.1f}%)",
            f"🕐 Сигнал: {time_str}",
        ])

    return "\n".join([
        f"{side_emoji} <b>{card['ticker']}</b> — {card['side']}",
        f"{market_label}  ·  {strategy_label}{prob_line}",
        tpl_line.rstrip("\n"),
        f"🎯 Уровень: <code>{level:.4f}</code>  [{card['kind']}]",
        f"📥 Вход: <code>{entry:.4f}</code>  (расст. {dist_pct:.0f}% ATR)",
        f"⛔ Стоп-лосс: <code>{stop:.4f}</code>  (−{risk_pct:.1f}%)",
        f"💰 Тейк-профит: <code>{take:.4f}</code>  (+{tp_pct:.1f}%)",
        f"⭐ Оценка: <b>{card['score']:.1f}</b> / 10" if card.get("score") is not None
        else f"💪 Сила: {'★' * card['strength']}{'☆' * (5 - card['strength'])}",
        f"📊 Тренд D1: {bias}",
        f"🕐 Сигнал: {time_str}",
    ])



def _parse_card_from_message(msg) -> dict | None:
    """Восстановить карточку из уже отправленного Telegram-сообщения (для 👁 за 48ч)."""
    raw = getattr(msg, "html_text", None) or getattr(msg, "text", None) or ""
    if not raw:
        return None
    plain = re.sub(r"<[^>]+>", "", raw)

    m_head = re.search(
        r"[🟢🔴]\s*(\S+)\s*[—\-]\s*(LONG|SHORT)",
        plain,
        re.I,
    )
    if not m_head:
        return None
    ticker = m_head.group(1).strip()
    side = m_head.group(2).upper()

    market = "crypto"
    if "Мосбиржа" in plain or "🇷🇺" in raw:
        market = "ru"
    elif "Крипта (Алго)" in plain:
        market = "algo"
    strategy = "brk"
    if "Ложный пробой" in plain:
        strategy = "fbo"
    elif "⚡ Алго" in plain:
        strategy = "algo"

    def _code_after(label: str) -> float | None:
        mm = re.search(
            re.escape(label) + r"[^<]*<code>\s*([0-9]+(?:\.[0-9]+)?)\s*</code>",
            raw,
            re.I,
        )
        if mm:
            try:
                return float(mm.group(1))
            except ValueError:
                return None
        mm = re.search(
            re.escape(label) + r"[^\d]*([0-9]+(?:\.[0-9]+)?)",
            plain,
            re.I,
        )
        if mm:
            try:
                return float(mm.group(1))
            except ValueError:
                return None
        return None

    level = _code_after("Уровень")
    entry = _code_after("Вход")
    stop = _code_after("Стоп")
    take = _code_after("Тейк")
    if level is None and strategy == "algo":
        level = entry   # у алго нет уровня — уровень = цена входа
    if level is None or entry is None or stop is None or take is None:
        return None

    kind = ""
    mk = re.search(r"Уровень:[^\[]*\[([^\]]+)\]", plain)
    if mk:
        kind = mk.group(1).strip()

    score = None
    msc = re.search(r"Оценка:\s*(\d+(?:\.\d+)?)\s*/\s*10", plain)
    if msc:
        try:
            score = float(msc.group(1))
        except ValueError:
            score = None
    strength = plain.count("★")
    if strength <= 0:
        strength = 3

    prob = None
    mp = re.search(r"p\s*=\s*([0-9]+(?:\.[0-9]+)?)", plain, re.I)
    if mp:
        try:
            prob = float(mp.group(1))
        except ValueError:
            prob = None

    signal_ts = 0
    mt = re.search(r"Сигнал:\s*(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})\s*МСК", plain)
    if mt:
        day, month, hour, minute = map(int, mt.groups())
        now = datetime.now(MSK)
        year = now.year
        try:
            ts = datetime(year, month, day, hour, minute, tzinfo=MSK)
            if ts > now + timedelta(days=1):
                ts = datetime(year - 1, month, day, hour, minute, tzinfo=MSK)
            signal_ts = int(ts.timestamp() * 1000)
        except ValueError:
            signal_ts = 0
    if not signal_ts and getattr(msg, "date", None):
        d = msg.date
        if getattr(d, "tzinfo", None) is None:
            from datetime import timezone as _tz
            d = d.replace(tzinfo=_tz.utc)
        signal_ts = int(d.timestamp() * 1000)

    dist_atr = 0.0
    md = re.search(r"расст\.\s*(\d+(?:\.\d+)?)%\s*ATR", plain, re.I)
    if md:
        try:
            dist_atr = float(md.group(1)) / 100.0
        except ValueError:
            dist_atr = 0.0

    return {
        "ticker": ticker,
        "side": side,
        "market": market,
        "strategy": strategy,
        "level": level,
        "last": entry,
        "stop": stop,
        "take": take,
        "kind": kind,
        "score": score,
        "strength": min(5, max(1, strength)),
        "prob": prob,
        "signal_ts": signal_ts,
        "dist_atr": dist_atr,
        "d1_bias": "flat",
    }


async def send_signal(card: dict, chat_ids: list[int]):
    # Hard cap: never deliver a card older than 12h (defense in depth).
    signal_ts = int(card.get("signal_ts") or 0)
    if not signal_ts or (int(time.time() * 1000) - signal_ts) > 12 * 3600 * 1000:
        logger.info(
            "Drop stale card %s ts=%s (age>12h)",
            card.get("ticker"), signal_ts,
        )
        return
    text = _format_card(card)
    for chat_id in chat_ids:
        journal_id = None
        sent_ok = False
        try:
            card_key = f"{card['ticker']}:{card['side']}:{card.get('strategy','brk')}:{int(card['level']*1e6)}"
            card_id = storage.make_pending_card_id(card_key, chat_id)
            payload = {**card, "chat_id": chat_id}
            _pending_cards[card_id] = payload
            try:
                storage.save_pending_signal_card(
                    card_id, chat_id, payload, signal_ts=signal_ts,
                )
            except Exception as e:
                logger.warning("pending card save failed %s: %s", card_id, e)
            try:
                storage.record_signal_delivery(chat_id, card)
            except Exception as e:
                logger.warning("signal_history record failed %s: %s", card_id, e)
            # Журнал — только если карточка реально ушла в чат (см. except ниже).
            journal_id = _remember_journal(chat_id, card)
            # watch:{16-hex} fits Telegram 64-byte callback_data limit
            row = [InlineKeyboardButton(text="👁 Отслеживать", callback_data=f"watch:{card_id}")]
            view_btn = _view_signal_button(
                chat_id,
                card.get("ticker") or "",
                signal_ts,
                card.get("market") or "crypto",
                journal_id=journal_id,
            )
            rows = [row]
            if view_btn:
                rows.append([view_btn])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
            sent_ok = True
        except Exception as e:
            if journal_id and not sent_ok:
                _drop_journal(journal_id)
            logger.warning("Не удалось отправить %s: %s", chat_id, e)
            continue
        if journal_id:
            asyncio.create_task(_fill_journal_charts(journal_id))


# ── /start ────────────────────────────────────────────────────────────────────
@dp.message(Command("start"))
@dp.message(F.text == "▶️ Старт")
async def cmd_start(msg: Message):
    _subscribers.add(msg.chat.id)
    storage.init()
    await msg.answer(
        "👋 <b>HW Screener Bot</b>\n\n"
        "Команды:\n"
        "/filter — выбрать шаблоны и рынки\n"
        "/syncurl или «🔗 Синхронизация шаблонов» — синхронизировать шаблоны из Mini App\n"
        "/scan — запустить скан сейчас\n"
        "/status — текущие настройки\n"
        "/ai — ИИ-помощник (настройки и сигналы)\n"
        "/stop — остановить сигналы\n\n"
        "🤖 ИИ всегда включён: просто пиши свободным текстом по-русски.",
        parse_mode="HTML",
        reply_markup=main_keyboard(msg.chat.id),
    )


# ── /stop ─────────────────────────────────────────────────────────────────────
@dp.message(Command("stop"))
@dp.message(F.text == "⛔ Стоп")
async def cmd_stop(msg: Message):
    _subscribers.discard(msg.chat.id)
    await msg.answer(
        "⛔ Сигналы остановлены. Нажми «▶️ Старт», чтобы возобновить.",
        reply_markup=main_keyboard(msg.chat.id),
    )


# ── /syncurl — выдать стабильную ссылку для HTML ─────────────────────────────
@dp.message(Command("syncurl"))
@dp.message(Command("refresh_tpl"))   # старая команда → тот же sync
@dp.message(F.text.in_({"🔗 Синхронизация шаблонов", "🔗 Sync", "🔄 Обновить шаблоны"}))   # старые клавиатуры
async def cmd_syncurl(msg: Message):
    chat_id = msg.chat.id
    if not PUBLIC_URL:
        await msg.answer(
            "⚠️ Переменная <code>PUBLIC_URL</code> не задана в Railway.\n\n"
            "Зайди в Railway → Variables → добавь:\n"
            "<code>PUBLIC_URL = https://&lt;твой домен&gt;.railway.app</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(chat_id),
        )
        return
    url = _sync_endpoint(chat_id)
    app = _app_url_with_sync(chat_id)
    kb_extra = None
    if app:
        kb_extra = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="📱 Открыть Mini App → синхронизация",
                web_app=WebAppInfo(url=app),
            )
        ]])
    await msg.answer(
        f"🔗 <b>URL синхронизации</b> (постоянный)\n\n"
        f"<code>{url}</code>\n\n"
        f"Вставь <b>один раз</b> в HTML (📬) — сохранится в браузере.\n"
        f"Дальше просто открывай Mini App кнопкой ниже («🔗 Синхронизация шаблонов») — шаблоны синхронизируются сами.\n\n"
        f"Ссылка не протухает после деплоя (пока не сменится токен бота).",
        parse_mode="HTML",
        reply_markup=kb_extra or main_keyboard(chat_id),
    )
    if kb_extra:
        await msg.answer(
            "Клавиатура обновлена.",
            reply_markup=main_keyboard(chat_id),
        )


async def cmd_refresh_tpl(msg: Message):   # не зарегистрирован: «Обновить шаблоны» убрано, всё через /syncurl
    chat_id = msg.chat.id
    if not PUBLIC_URL:
        await msg.answer(
            "⚠️ Нет <code>PUBLIC_URL</code>. Сначала задай переменную в Railway, "
            "затем /syncurl.",
            parse_mode="HTML",
            reply_markup=main_keyboard(chat_id),
        )
        return
    app = _app_url_with_sync(chat_id)
    url = _sync_endpoint(chat_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📱 Открыть скринер → синхронизация",
            web_app=WebAppInfo(url=app),
        )
    ]])
    await msg.answer(
        "🔄 <b>Обновить шаблоны</b>\n\n"
        "1. Открой Mini App кнопкой ниже\n"
        "2. Скринер сам подставит sync URL и отправит шаблоны (📬)\n"
        "3. Потом выбери активные в /filter\n\n"
        f"URL (если открываешь HTML вручную):\n<code>{url}</code>",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ── /status ───────────────────────────────────────────────────────────────────
@dp.message(Command("weekcap"))
async def cmd_weekcap(msg: Message):
    """Недельный лимит в боте отключён: «макс. сделок в неделю» — только фильтр бэктеста HTML."""
    await msg.answer(
        "Недельный лимит в боте отключён: «макс. сделок в неделю» — фильтр бэктеста в HTML. "
        "Количество сигналов бота ограничивают только шаблоны/сценарии."
    )


@dp.message(Command("status"))
@dp.message(F.text == "📊 Статус")
async def cmd_status(msg: Message):
    chat_id   = msg.chat.id
    cfg       = storage.get_active_config(chat_id)
    tpls      = storage.get_html_templates(chat_id)
    subscribed = chat_id in _subscribers

    by_mkt         = cfg.get("names_by_market") or {m: [] for m in storage.MARKETS}
    active_markets = cfg["markets"]

    mkt_str = " + ".join(
        {"crypto": "Крипта", "ru": "Мосбиржа", "algo": "Крипта (Алго)"}.get(m, m)
        for m in active_markets
    ) or "нет (включи шаблоны в /filter)"

    auto = cfg.get("auto") or {}
    if not tpls and not any(auto.values()):
        tpl_str = "Шаблоны не синхронизированы.\nИспользуй /syncurl → кнопку 📬 в HTML."
    elif not any(by_mkt.get(m) or auto.get(m) for m in storage.MARKETS):
        tpl_str = f"Шаблонов загружено: {len(tpls)}\nАктивных: нет — выбери через /filter"
    else:
        overrides = storage.get_template_strategy_overrides(chat_id)
        label = {"fbo": "🔻ЛП", "brk": "📈Проб", "both": "📈🔻Оба"}
        blocks = []
        for m, mlabel in (("crypto", "🌐 Крипта"), ("ru", "🇷🇺 Мосбиржа"), ("algo", "⚡ Крипта (Алго)")):
            names = by_mkt.get(m) or []
            mtpls = storage.get_html_templates(chat_id, m)
            if auto.get(m):
                a_strat = storage.get_auto_strategy(chat_id, m)
                blocks.append(f"{mlabel}:\n  ✅ {label.get(a_strat, '')} {AUTO_NAME} (все фильтры авто)")
                continue
            if not names:
                blocks.append(f"{mlabel}: —")
                continue
            lines = []
            for n in names:
                if m == "algo":
                    lines.append(f"  ✅ ⚡ {n}")
                    continue
                strat = _effective_strategy(
                    chat_id, n, mtpls.get(n, {}).get("filters", {}), overrides
                )
                lines.append(f"  ✅ {label.get(strat, '')} {n}")
            blocks.append(f"{mlabel}:\n" + "\n".join(lines))
        tpl_str = "\n".join(blocks)

    bo = cfg.get("best_only") or storage.get_best_only(chat_id)
    bo_str = ", ".join(
        f"{ {'crypto': 'Крипта', 'ru': 'Мосбиржа', 'algo': 'Алго'}[m] }: "
        f"{'⭐ лучший' if bo.get(m) else 'все'}"
        for m in storage.MARKETS
    )

    await msg.answer(
        f"📋 <b>Статус</b>\n\n"
        f"Подписка: {'✅ активна' if subscribed else '❌ остановлена'}\n"
        f"Рынки: {mkt_str}\n"
        f"Режим: {bo_str}\n\n"
        f"{tpl_str}\n\n"
        f"Авто-скан: {SCAN_SCHEDULE_TEXT}",
        parse_mode="HTML",
        reply_markup=main_keyboard(chat_id),
    )


# ── /filter — per-market шаблоны + стратегия + rename ────────────────────────
@dp.message(Command("filter"))
@dp.message(F.text == "⚙️ Фильтр")
async def cmd_filter(msg: Message):
    chat_id = msg.chat.id
    _pending_rename.pop(chat_id, None)
    tpls    = storage.get_html_templates(chat_id)
    if not tpls:
        await msg.answer(
            "📭 Шаблоны ещё не синхронизированы.\n\n"
            "1. Открой HTML-скринер\n"
            "2. Сохрани шаблоны кнопкой «Сохранить как шаблон»\n"
            "3. /syncurl или «🔗 Синхронизация шаблонов»\n"
            "4. В Mini App нажми 📬 (URL сохранится сам)\n\n"
            f"Пока можно включить встроенный {AUTO_NAME} — все фильтры авто."
        )
    await _send_filter_menu(chat_id)


_STRAT_CYCLE = {"fbo": "brk", "brk": "both", "both": "fbo"}
_STRAT_ICON  = {"fbo": "🔻ЛП", "brk": "📈Проб", "both": "📈🔻Оба"}
_MKT_TAB     = {"crypto": "🌐 Крипта", "ru": "🇷🇺 Мосбиржа", "algo": "⚡ Крипта (Алго)"}
_MARKETS     = storage.MARKETS          # crypto | ru | algo
_AUTO_MARKETS = ("crypto", "ru")        # «🤖 Авто» — только у ЛП/пробоя (в алго нет шаблона «Авто»)


def _effective_strategy(chat_id: int, name: str, tpl_filters: dict, overrides: dict) -> str:
    """Явное переопределение из бота важнее _strat, записанного в HTML."""
    if name in overrides:
        return overrides[name]
    raw = (tpl_filters or {}).get("_strat", "fbo")
    return raw if raw in _STRAT_CYCLE else "fbo"


def _resolve_tpl_name(tpls: dict, key: str) -> str | None:
    """Callback data обрезает имя до 40 символов — восстановить полное."""
    if key in tpls:
        return key
    return next((k for k in tpls if k[:40] == key), None)


async def _send_filter_root(chat_id: int, edit_msg=None):
    """Корень /filter: только выбор рынка."""
    _filter_tab.pop(chat_id, None)
    cfg = storage.get_active_config(chat_id)
    by = cfg.get("names_by_market") or {m: [] for m in storage.MARKETS}
    enabled = set(cfg.get("markets") or [])

    buttons = [[
        InlineKeyboardButton(text="🌐 Крипта", callback_data="filter_mkt:crypto"),
        InlineKeyboardButton(text="🇷🇺 Мосбиржа", callback_data="filter_mkt:ru"),
    ], [
        InlineKeyboardButton(text="⚡ Крипта (Алго)", callback_data="filter_mkt:algo"),
    ]]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    def _mkt_line(mkt: str, label: str) -> str:
        on = mkt in enabled
        n = len(by.get(mkt) or [])
        state = "🟢 вкл" if on else "⚪ выкл"
        if (cfg.get("auto") or {}).get(mkt):
            return f"{label}: {state}, активный: {AUTO_NAME}"
        return f"{label}: {state}, шаблонов: {n}"

    txt = (
        "🎛 <b>Фильтр</b>\n\n"
        "Выбери рынок:\n"
        f"• {_mkt_line('crypto', '🌐 Крипта')}\n"
        f"• {_mkt_line('ru', '🇷🇺 Мосбиржа')}\n"
        f"• {_mkt_line('algo', '⚡ Крипта (Алго)')}\n\n"
        "Шаблоны у каждого рынка свои (как в HTML)."
    )

    if edit_msg:
        try:
            await edit_msg.edit_text(txt, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")
    else:
        await bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")


async def _send_filter_menu(chat_id: int, edit_msg=None, market: str | None = None):
    """Экран управления одним рынком. market=None → корневой выбор рынка."""
    if market not in _MARKETS:
        await _send_filter_root(chat_id, edit_msg=edit_msg)
        return

    _filter_tab[chat_id] = market
    cfg = storage.get_active_config(chat_id)   # (миграция копирует активные шаблоны в свой рынок)
    tpls = storage.get_html_templates(chat_id, market)
    by_mkt = cfg.get("names_by_market") or {m: [] for m in _MARKETS}
    active = set(by_mkt.get(market) or [])
    enabled = market in (cfg.get("markets") or [])
    overrides = storage.get_template_strategy_overrides(chat_id)
    is_algo = market == "algo"
    auto_on = (not is_algo) and bool((cfg.get("auto") or {}).get(market))
    auto_strat = storage.get_auto_strategy(chat_id, market) if not is_algo else "fbo"

    # Шаблоны ТОЛЬКО этого рынка (в новом HTML крипта / РФ / алго хранятся раздельно)
    shared_names = sorted(tpls.keys())

    buttons = []
    # 🤖 Авто — встроенный шаблон HTML «Авто — все фильтры сброшены», отдельной строкой сверху
    if not is_algo:
      buttons.append([
        InlineKeyboardButton(
            text=("✅ 🤖 АВТО · все фильтры авто ✅" if auto_on else "🤖 АВТО · все фильтры авто"),
            callback_data=f"auto_on:{market}",
        ),
        InlineKeyboardButton(text=_STRAT_ICON.get(auto_strat, "🔻ЛП"),
                             callback_data=f"auto_strat:{market}"),
      ])
    on_off = "🟢 Рынок ВКЛ" if enabled else "⚪ Рынок ВЫКЛ"
    buttons.append([InlineKeyboardButton(
        text=on_off,
        callback_data=f"mkt_on:{market}",
    )])

    bo = (cfg.get("best_only") or storage.get_best_only(chat_id)).get(market, False)
    bo_label = "⭐ Только лучший ✓" if bo else "⭐ Только лучший"
    buttons.append([InlineKeyboardButton(
        text=bo_label,
        callback_data=f"best_only:{market}",
    )])

    if not shared_names:
        buttons.append([InlineKeyboardButton(
            text="(нет шаблонов — синхронизируй из HTML)",
            callback_data="noop",
        )])
    else:
        buttons.append([InlineKeyboardButton(
            text="── 📋 Мои шаблоны ──" + (" (выкл, пока Авто)" if auto_on else ""),
            callback_data="noop",
        )])
        for name in shared_names:
            check = "✅" if (name in active and not auto_on) else "⬜"
            key = name[:40]
            if is_algo:   # у алго нет стратегии ЛП/пробой — только набор индикаторов
                buttons.append([InlineKeyboardButton(text=f"{check} ⚡ {name}", callback_data=f"tpl:{key}")])
                continue
            strat = _effective_strategy(
                chat_id, name, (tpls[name].get("filters") or {}), overrides
            )
            strat_tag = _STRAT_ICON.get(strat, "🔻ЛП")
            buttons.append([
                InlineKeyboardButton(text=f"{check} {name}", callback_data=f"tpl:{key}"),
                InlineKeyboardButton(text=strat_tag, callback_data=f"strat:{key}"),
            ])

    buttons.append([
        InlineKeyboardButton(text="✅ Все", callback_data="tpl_all:1"),
        InlineKeyboardButton(text="⬜ Сброс", callback_data="tpl_all:0"),
    ])
    buttons.append([InlineKeyboardButton(
        text="◀️ Назад", callback_data="filter_back"
    )])
    buttons.append([InlineKeyboardButton(
        text="💾 Сохранить и закрыть", callback_data="filter_done"
    )])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    n_on = len(active)
    bo_state = "вкл" if bo else "выкл"
    if auto_on:
        act_line = f"активный: <b>{AUTO_NAME}</b> ({_STRAT_ICON.get(auto_strat, '')})"
    else:
        act_line = f"активных шаблонов: <b>{n_on}</b>"
    if is_algo:
        txt = (
            f"🎛 <b>{_MKT_TAB[market]}</b>\n\n"
            f"Рынок: <b>{'вкл' if enabled else 'выкл'}</b> · {act_line}\n"
            f"Только лучший: <b>{bo_state}</b>\n\n"
            "• Скан по индикаторам (как «Крипта (Алго)» в HTML): каждый закрытый час, где выполнены "
            "условия шаблона → сигнал; на монете новый — только после закрытия предыдущего\n"
            "• Шаблоны алго сохраняются в HTML на рынке «Крипта (Алго)» (кнопка «В шаблон»)\n"
            "• Вкл/выкл рынок — получать сигналы по нему"
        )
    else:
      txt = (
        f"🎛 <b>{_MKT_TAB[market]}</b>\n\n"
        f"Рынок: <b>{'вкл' if enabled else 'выкл'}</b> · {act_line}\n"
        f"Только лучший: <b>{bo_state}</b>\n\n"
        f"• {AUTO_NAME} — как шаблон «Авто» в HTML: все фильтры сброшены в авто; "
        "выбор своего шаблона выключает Авто\n"
        "• Вкл/выкл рынок — получать сигналы по нему\n"
        "• ⭐ Только лучший — если в одном скане несколько сигналов "
        "по этому рынку, отправить только лучший\n"
        "• Слева — шаблоны этого рынка (у каждого рынка свои)\n"
        "• Стратегия: 🔻ЛП → 📈Проб → 📈🔻Оба\n"
        "• Переименовать шаблон — в HTML (✏️ Переименовать)"
    )

    if edit_msg:
        try:
            await edit_msg.edit_text(txt, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")
    else:
        await bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@dp.callback_query(F.data == "filter_back")
async def cb_filter_back(call: CallbackQuery):
    chat_id = call.message.chat.id
    await _send_filter_root(chat_id, edit_msg=call.message)
    await call.answer()


@dp.callback_query(F.data.startswith("filter_mkt:"))
async def cb_filter_mkt(call: CallbackQuery):
    chat_id = call.message.chat.id
    mkt = call.data.split(":", 1)[1]
    if mkt not in _MARKETS:
        await call.answer()
        return
    await _send_filter_menu(chat_id, edit_msg=call.message, market=mkt)
    await call.answer(_MKT_TAB[mkt])


# Совместимость со старыми callback
@dp.callback_query(F.data.startswith("fmkt:"))
async def cb_filter_tab(call: CallbackQuery):
    chat_id = call.message.chat.id
    mkt = call.data.split(":", 1)[1]
    if mkt not in _MARKETS:
        await call.answer()
        return
    await _send_filter_menu(chat_id, edit_msg=call.message, market=mkt)
    await call.answer(_MKT_TAB[mkt])


@dp.callback_query(F.data.startswith("mkt:"))
async def cb_market(call: CallbackQuery):
    chat_id = call.message.chat.id
    mkt = call.data.split(":", 1)[1]
    if mkt not in _MARKETS:
        await call.answer()
        return
    await _send_filter_menu(chat_id, edit_msg=call.message, market=mkt)
    await call.answer(_MKT_TAB[mkt])


@dp.callback_query(F.data.startswith("mkt_on:"))
async def cb_mkt_on(call: CallbackQuery):
    chat_id = call.message.chat.id
    mkt = call.data.split(":", 1)[1]
    if mkt not in _MARKETS:
        await call.answer()
        return
    cfg = storage.get_active_config(chat_id)
    enabled = set(cfg.get("markets") or [])
    turn_on = mkt not in enabled
    storage.set_market_enabled(chat_id, mkt, turn_on)
    await _send_filter_menu(chat_id, edit_msg=call.message, market=mkt)
    await call.answer("вкл" if turn_on else "выкл")


@dp.callback_query(F.data.startswith("best_only:"))
async def cb_best_only(call: CallbackQuery):
    chat_id = call.message.chat.id
    mkt = call.data.split(":", 1)[1]
    if mkt not in _MARKETS:
        await call.answer()
        return
    cur = storage.get_best_only(chat_id).get(mkt, False)
    storage.set_best_only(chat_id, mkt, not cur)
    await _send_filter_menu(chat_id, edit_msg=call.message, market=mkt)
    await call.answer("только лучший: вкл" if not cur else "только лучший: выкл")


@dp.callback_query(F.data.startswith("auto_on:"))
async def cb_auto_on(call: CallbackQuery):
    """Вкл/выкл встроенный «🤖 Авто» для рынка. Шаблоны пользователя не трогаются."""
    chat_id = call.message.chat.id
    mkt = call.data.split(":", 1)[1]
    if mkt not in _AUTO_MARKETS:
        await call.answer()
        return
    turn_on = not storage.get_auto_mode(chat_id).get(mkt, False)
    storage.set_auto_mode(chat_id, mkt, turn_on)
    enabled = mkt in (storage.get_active_config(chat_id).get("markets") or [])
    await _send_filter_menu(chat_id, edit_msg=call.message, market=mkt)
    if turn_on:
        await call.answer(f"{AUTO_NAME}: вкл" + ("" if enabled else " (рынок выкл — включи 🟢)"))
    else:
        await call.answer(f"{AUTO_NAME}: выкл → мои шаблоны")


@dp.callback_query(F.data.startswith("auto_strat:"))
async def cb_auto_strat(call: CallbackQuery):
    chat_id = call.message.chat.id
    mkt = call.data.split(":", 1)[1]
    if mkt not in _AUTO_MARKETS:
        await call.answer()
        return
    new_strat = _STRAT_CYCLE.get(storage.get_auto_strategy(chat_id, mkt), "fbo")
    storage.set_auto_strategy(chat_id, mkt, new_strat)
    await _send_filter_menu(chat_id, edit_msg=call.message, market=mkt)
    label = {"fbo": "Ложный пробой", "brk": "Пробой", "both": "Оба"}[new_strat]
    await call.answer(f"{AUTO_NAME}: {label}")


@dp.callback_query(F.data.startswith("tpl:"))
async def cb_tpl(call: CallbackQuery):
    chat_id = call.message.chat.id
    key = call.data.split(":", 1)[1]
    market = _filter_tab.get(chat_id)
    if market not in _MARKETS:
        market = "crypto"
    tpls = storage.get_html_templates(chat_id, market)
    full_name = _resolve_tpl_name(tpls, key)
    if not full_name:
        await call.answer("Шаблон не найден")
        return
    cfg = storage.get_active_config(chat_id)
    active = list((cfg.get("names_by_market") or {}).get(market) or [])
    if (cfg.get("auto") or {}).get(market):
        # Выбор своего шаблона выключает Авто; выбранный шаблон — точно активен
        storage.set_auto_mode(chat_id, market, False)
        if full_name not in active:
            active.append(full_name)
    elif full_name in active:
        active = [n for n in active if n != full_name]
    else:
        active.append(full_name)
    storage.set_active_templates_for_market(chat_id, market, active)
    await _send_filter_menu(chat_id, edit_msg=call.message, market=market)
    await call.answer()


@dp.callback_query(F.data.startswith("strat:"))
async def cb_strat(call: CallbackQuery):
    chat_id = call.message.chat.id
    key = call.data.split(":", 1)[1]
    market = _filter_tab.get(chat_id)
    if market not in _MARKETS:
        market = "crypto"
    tpls = storage.get_html_templates(chat_id, market)
    full_name = _resolve_tpl_name(tpls, key)
    if not full_name:
        await call.answer("Шаблон не найден")
        return
    overrides = storage.get_template_strategy_overrides(chat_id)
    current = _effective_strategy(chat_id, full_name, tpls[full_name]["filters"], overrides)
    new_strat = _STRAT_CYCLE.get(current, "fbo")
    storage.set_template_strategy(chat_id, full_name, new_strat)
    await _send_filter_menu(chat_id, edit_msg=call.message, market=market)
    label = {"fbo": "Ложный пробой", "brk": "Пробой", "both": "Оба"}[new_strat]
    await call.answer(f"{full_name}: {label}")


@dp.callback_query(F.data.startswith("ren:"))
async def cb_rename(call: CallbackQuery):
    """Оставлен для старых сообщений; основной rename — в HTML."""
    chat_id = call.message.chat.id
    key = call.data.split(":", 1)[1]
    tpls = storage.get_html_templates(chat_id)
    full_name = _resolve_tpl_name(tpls, key)
    if not full_name:
        await call.answer("Шаблон не найден")
        return
    market = _filter_tab.get(chat_id) or tpls[full_name].get("market", "crypto")
    _filter_tab[chat_id] = market if market in _MARKETS else "crypto"
    _pending_rename[chat_id] = full_name
    await call.answer()
    await bot.send_message(
        chat_id,
        f"✏️ Пришли новое имя для «<b>{full_name}</b>»\n"
        f"(или /cancel). Предпочтительнее rename в HTML.",
        parse_mode="HTML",
    )


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message):
    chat_id = msg.chat.id
    cleared = False
    if chat_id in _pending_rename:
        _pending_rename.pop(chat_id, None)
        cleared = True
        await msg.answer("Отменено.")
        await _send_filter_menu(chat_id, market=_filter_tab.get(chat_id))
    if storage.get_ai_pending(chat_id):
        storage.clear_ai_pending(chat_id)
        cleared = True
        await msg.answer("Предложение ИИ отменено.")
    if not cleared:
        await msg.answer("Нечего отменять.")



# ── ИИ (Gemini) ───────────────────────────────────────────────────────────────

_AI_YES = {"да", "да.", "yes", "y", "ок", "ok", "✅", "применить"}
_AI_NO = {"нет", "нет.", "no", "n", "отмена", "cancel", "❌"}


def _ai_user_context(chat_id: int) -> dict:
    cfg = storage.get_active_config(chat_id)
    return {
        "subscribed": chat_id in _subscribers,
        "ai_enabled": storage.get_ai_enabled(chat_id),
        "scan_schedule": SCAN_SCHEDULE_TEXT,
        "markets": list(cfg.get("markets") or []),
        "names_by_market": cfg.get("names_by_market") or {m: [] for m in storage.MARKETS},
        "templates": storage.get_html_templates(chat_id),
        "templates_by_market": {m: sorted(storage.get_html_templates(chat_id, m)) for m in storage.MARKETS},
        "strategy_overrides": storage.get_template_strategy_overrides(chat_id),
        "auto": storage.get_auto_mode(chat_id),
        "auto_strategy": {m: storage.get_auto_strategy(chat_id, m) for m in _AUTO_MARKETS},
        "best_only": storage.get_best_only(chat_id),
        "template_strategies": _template_strategies_by_market(chat_id),
    }


def _template_strategies_by_market(chat_id: int) -> dict:
    ov = storage.get_template_strategy_overrides(chat_id)
    out = {}
    for m in storage.MARKETS:
        tpls = storage.get_html_templates(chat_id, m)
        out[m] = {n: (_effective_strategy(chat_id, n, (e or {}).get("filters") or {}, ov) if m != "algo" else "algo")
                  for n, e in tpls.items()}
    return out


def _fuzzy_tpl(tpls: dict, name: str) -> str | None:
    """Имя шаблона рынка: точное → без регистра → префикс/вхождение → похожее (опечатки)."""
    if not name:
        return None
    if name in tpls:
        return name
    low = {k.lower().strip(): k for k in tpls}
    n = name.lower().strip()
    if n in low:
        return low[n]
    hits = [k for lk, k in low.items() if lk.startswith(n) or n.startswith(lk[:40]) or n in lk]
    if len(hits) == 1:
        return hits[0]
    import difflib
    close = difflib.get_close_matches(n, list(low), n=2, cutoff=0.75)
    if len(close) == 1 or (close and difflib.SequenceMatcher(None, n, close[0]).ratio() >= 0.9):
        return low[close[0]]
    return None


def _ai_resolve_names(chat_id: int, proposal: dict) -> dict:
    """Перед карточкой подтверждения: имена шаблонов → реальные имена рынка; ненайденные → заметка."""
    items = proposal.get("actions") if proposal.get("action") == "multi" else [proposal]
    notes = list(proposal.get("notes") or [])
    keep = []
    for it in items or []:
        p = it.get("params") or {}
        a = it.get("action")
        if a in ("set_templates", "add_templates", "remove_templates"):
            tpls = storage.get_html_templates(chat_id, p.get("market"))
            got, miss = [], []
            for n in p.get("names") or []:
                r = _fuzzy_tpl(tpls, n)
                if r:
                    if r not in got:
                        got.append(r)
                else:
                    miss.append(n)
            if miss:
                notes.append(f"Шаблон(ы) {', '.join('«'+x+'»' for x in miss)} не найдены на рынке "
                             f"{_AI_MKT_RU.get(p.get('market'))} — пропускаю.")
            if not got and a != "set_templates":
                continue
            it = {**it, "params": {**p, "names": got}}
        elif a == "set_strategy":
            mk = [p["market"]] if p.get("market") in storage.MARKETS else list(storage.MARKETS)
            found = None
            for m in mk:
                r = _fuzzy_tpl(storage.get_html_templates(chat_id, m), p.get("template") or "")
                if r:
                    found = (m, r)
                    break
            if not found:
                notes.append(f"Шаблон «{p.get('template')}» не найден — пропускаю.")
                continue
            it = {**it, "params": {**p, "market": found[0], "template": found[1]}}
        keep.append(it)
    if proposal.get("action") != "multi" and len(keep) == 1 and not notes:
        return keep[0]
    return {"action": "multi", "actions": keep, "notes": notes, "summary": f"{len(keep)} изменений"}


_AI_MKT_RU = {"crypto": "крипта", "ru": "мосбиржа", "algo": "крипта (алго)"}


_AI_STRAT_RU = {"fbo": "ложный пробой", "brk": "пробой", "both": "оба (ЛП + пробой)"}


def _format_proposal(proposal: dict) -> str:
    if proposal.get("action") == "multi":
        lines = [f"{i}. {_format_proposal(a)}" for i, a in enumerate(proposal.get("actions") or [], 1)]
        lines += [f"ℹ️ {n}" for n in proposal.get("notes") or []]
        return "\n".join(lines) or "—"
    action = proposal.get("action")
    p = proposal.get("params") or {}
    summary = proposal.get("summary") or action
    if action == "set_market":
        m = _AI_MKT_RU.get(p.get("market"), "крипта")
        st = "включить" if p.get("enabled") else "выключить"
        return f"{st} рынок <b>{m}</b>"
    if action == "set_auto":
        m = _AI_MKT_RU.get(p.get("market"), "крипта")
        if not p.get("enabled"):
            return f"{m}: выключить 🤖 Авто (останутся мои шаблоны)"
        st = f" на <b>{_AI_STRAT_RU[p['strategy']]}</b>" if p.get("strategy") else ""
        return f"{m}: включить 🤖 Авто{st}"
    if action == "set_auto_strategy":
        m = _AI_MKT_RU.get(p.get("market"), "крипта")
        return f"{m}: стратегия 🤖 Авто → <b>{_AI_STRAT_RU.get(p.get('strategy'))}</b>"
    if action == "set_best_only":
        m = _AI_MKT_RU.get(p.get("market"), "крипта")
        return f"{m}: «только лучший сигнал» — <b>{'вкл' if p.get('enabled') else 'выкл'}</b>"
    if action == "set_strategy":
        return (f"стратегия шаблона <b>{p.get('template')}</b> → "
                f"<b>{_AI_STRAT_RU.get(p.get('strategy'), p.get('strategy'))}</b>")
    if action == "set_templates":
        names = ", ".join(p.get("names") or []) or "—"
        m = _AI_MKT_RU.get(p.get("market"), "крипта")
        return f"активные шаблоны ({m}): <b>{names}</b>"
    if action == "add_templates":
        names = ", ".join(p.get("names") or []) or "—"
        m = _AI_MKT_RU.get(p.get("market"), "крипта")
        return f"добавить шаблоны ({m}): <b>{names}</b>"
    if action == "remove_templates":
        names = ", ".join(p.get("names") or []) or "—"
        m = _AI_MKT_RU.get(p.get("market"), "крипта")
        return f"убрать шаблоны ({m}): <b>{names}</b>"
    if action == "subscribe":
        return "включить подписку на сигналы (/start)"
    if action == "unsubscribe":
        return "остановить сигналы (/stop)"
    return summary


def _apply_ai_proposal(chat_id: int, proposal: dict) -> str:
    """Применить подтверждённое предложение (одно или multi — по порядку). Те же функции
    storage, что и кнопки /filter. Возвращает текст результата (по пункту на действие)."""
    if proposal.get("action") == "multi":
        out = []
        for i, a in enumerate(proposal.get("actions") or [], 1):
            try:
                r = _apply_ai_one(chat_id, a)
                ok = not any(x in r for x in ("не найден", "Неизвестн", "ничего не менял"))
            except Exception as e:
                logger.exception("AI apply %s", a)
                r, ok = f"ошибка: {e}", False
            out.append(f"{'✅' if ok else '⚠️'} {i}. {_format_proposal(a)} — {r}")
        return "\n".join(out) or "Нечего применять."
    return _apply_ai_one(chat_id, proposal)


def _apply_ai_one(chat_id: int, proposal: dict) -> str:
    action = proposal.get("action")
    p = proposal.get("params") or {}
    if action == "set_market":
        if p.get("market") not in storage.MARKETS:
            return "Неизвестный рынок — ничего не менял."
        storage.set_market_enabled(chat_id, p["market"], bool(p.get("enabled")))
        if p["market"] == "algo" and p.get("enabled") and not (
                storage.get_active_config(chat_id).get("names_by_market") or {}).get("algo"):
            return ("Рынок «Крипта (Алго)» включён, но алго-шаблон не выбран — сканов и сигналов "
                    "по алго не будет, пока не выберешь шаблон в /filter.")
        return "готово."
    if action == "set_auto":
        m = p.get("market")
        if m not in _AUTO_MARKETS:
            return "у этого рынка нет 🤖 Авто — ничего не менял."
        storage.set_auto_mode(chat_id, m, bool(p.get("enabled")))      # = кнопка auto_on
        if p.get("enabled") and p.get("strategy") in _STRAT_CYCLE:
            storage.set_auto_strategy(chat_id, m, p["strategy"])       # = кнопка auto_strat
        return "готово."
    if action == "set_auto_strategy":
        m = p.get("market")
        if m not in _AUTO_MARKETS or p.get("strategy") not in _STRAT_CYCLE:
            return "у этого рынка нет 🤖 Авто — ничего не менял."
        storage.set_auto_strategy(chat_id, m, p["strategy"])
        return "готово."
    if action == "set_best_only":
        if p.get("market") not in storage.MARKETS:
            return "Неизвестный рынок — ничего не менял."
        storage.set_best_only(chat_id, p["market"], bool(p.get("enabled")))   # = кнопка best_only
        return "готово."
    if action == "set_strategy":
        name = p.get("template") or ""
        tpls = (storage.get_html_templates(chat_id, p["market"]) if p.get("market") in storage.MARKETS
                else storage.get_html_templates(chat_id))
        if name not in tpls:
            # prefix match like filter callbacks
            full = next((k for k in tpls if k == name or k.startswith(name) or name.startswith(k[:40])), None)
            if not full:
                return f"Шаблон «{name}» не найден — ничего не менял."
            name = full
        storage.set_template_strategy(chat_id, name, p["strategy"])
        return f"Стратегия «{name}» → {p['strategy']}."
    if action in ("set_templates", "add_templates", "remove_templates"):
        market = p.get("market")
        if market not in storage.MARKETS:
            return "Неизвестный рынок — ничего не менял."
        names = list(p.get("names") or [])
        tpls = storage.get_html_templates(chat_id, market)   # шаблоны этого рынка
        resolved = []
        for n in names:
            full = _fuzzy_tpl(tpls, n)
            if full and full not in resolved:
                resolved.append(full)
        cfg = storage.get_active_config(chat_id)
        current = list((cfg.get("names_by_market") or {}).get(market) or [])
        if action == "set_templates":
            new_names = resolved
        elif action == "add_templates":
            new_names = list(current)
            for n in resolved:
                if n not in new_names:
                    new_names.append(n)
        else:
            drop = set(resolved)
            new_names = [n for n in current if n not in drop]
        storage.set_active_templates_for_market(chat_id, market, new_names)
        if market in ("crypto", "ru"):
            storage.set_auto_mode(chat_id, market, False)
        return f"активных шаблонов: {len(new_names)}."
    if action == "subscribe":
        _subscribers.add(chat_id)
        return "Подписка на сигналы включена."
    if action == "unsubscribe":
        _subscribers.discard(chat_id)
        return "Сигналы остановлены."
    return "Неизвестное действие — ничего не менял."


async def _send_ai_proposal_confirm(chat_id: int, proposal: dict, base_text: str):
    proposal = _ai_resolve_names(chat_id, proposal)
    if proposal.get("action") == "multi" and not proposal.get("actions"):
        notes = "\n".join(f"ℹ️ {n}" for n in proposal.get("notes") or [])
        txt = f"{base_text}\n\n{notes}".strip() or "Нечего менять."
        try:
            await bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=main_keyboard(chat_id))
        except Exception:
            await bot.send_message(chat_id, ai_chat.strip_markup(txt), reply_markup=main_keyboard(chat_id))
        return
    storage.set_ai_pending(chat_id, proposal, proposal.get("summary") or "")
    detail = _format_proposal(proposal)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, применить", callback_data="ai_confirm:yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="ai_confirm:no"),
    ]])
    body = (
        f"{base_text}\n\n"
        f"⚠️ <b>Предложение изменить настройки</b>\n"
        f"{detail}\n\n"
        f"Чтобы применить — ответь <b>да</b> или нажми кнопку. "
        f"Иначе «нет» / /cancel."
    )
    try:
        await bot.send_message(chat_id, body, parse_mode="HTML", reply_markup=kb)
    except Exception:   # Telegram не разобрал HTML → plain без маркеров
        await bot.send_message(chat_id, ai_chat.strip_markup(body), reply_markup=kb)


@dp.message(Command("ai_on"))
async def cmd_ai_on(msg: Message):
    hint = (
        "🤖 ИИ всегда включён. Пиши свободно по-русски "
        "(«включи мосбиржу», вопросы про сигналы/шаблоны).\n"
        "Команды с / по-прежнему работают."
    )
    if not ai_chat.is_configured():
        hint += (
            "\n\n🔑 Сейчас <code>GEMINI_API_KEY</code> не задан — "
            "добавь в Railway Variables."
        )
    await msg.answer(hint, parse_mode="HTML", reply_markup=main_keyboard(msg.chat.id))


@dp.message(Command("ai_off"))
async def cmd_ai_off(msg: Message):
    await msg.answer(
        "🤖 ИИ всегда включён — выключать не нужно. Просто пиши текстом или /ai.",
        reply_markup=main_keyboard(msg.chat.id),
    )


@dp.message(Command("ai"))
@dp.message(F.text.in_({"🤖 ИИ", "🤖 ИИ ✓"}))   # старые клавиатуры → справка
async def cmd_ai(msg: Message):
    chat_id = msg.chat.id
    raw = (msg.text or "").strip()
    if raw in ("🤖 ИИ", "🤖 ИИ ✓") or raw.split("@")[0] == "/ai":
        await msg.answer(
            "🤖 <b>ИИ-помощник HW Screener</b>\n\n"
            "Свободный текст на русском <b>поддерживается</b> — пиши как удобно.\n"
            "Примеры:\n"
            "• включи мосбиржу / выключи крипту\n"
            "• включи крипту авто на ложный пробой, а мосбиржу авто на пробой\n"
            "• всё выключи кроме крипты / на крипте только лучший сигнал\n"
            "• подпишись / стоп сигналы\n"
            "• что значит сила и ATR на карточке\n"
            "• чем fbo отличается от brk\n"
            "• поставь шаблону X стратегию both\n\n"
            "Любое изменение настроек сначала покажу и применю только после «да».\n"
            "ИИ всегда включён — отдельно включать не нужно.",
            parse_mode="HTML",
            reply_markup=main_keyboard(chat_id),
        )
        return

    # /ai <вопрос>
    question = raw.split(maxsplit=1)[1] if raw.startswith("/ai") and " " in raw else raw
    storage.set_ai_enabled(chat_id, True)
    await _handle_ai_question(msg, question)


async def _handle_ai_question(msg: Message, question: str):
    chat_id = msg.chat.id
    question = (question or "").strip()
    if not question:
        await msg.answer("Напиши вопрос после /ai или просто текстом в ИИ-режиме.")
        return
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    history = storage.get_ai_history(chat_id)
    # history for model should not include the current turn
    reply = await ai_chat.chat(question, history=history, context=_ai_user_context(chat_id))
    out = (reply.text or "").strip() or "Пустой ответ."
    # User-facing text must stay short — never dump raw 503/JSON bodies
    if reply.error and (
        len(out) > 400
        or out.lstrip().startswith("{")
        or ("503" in out and ("error" in out.lower() or "UNAVAILABLE" in out))
    ):
        out = (
            "⏳ Google перегружен или временно недоступен. "
            "Попробуй через минуту — свободный текст поддерживается."
        )
    storage.append_ai_message(chat_id, "user", question)
    storage.append_ai_message(chat_id, "model", out)
    if reply.proposal:
        await _send_ai_proposal_confirm(chat_id, reply.proposal, out)
    else:
        try:
            await msg.answer(out, parse_mode="HTML", reply_markup=main_keyboard(chat_id))
        except Exception:   # can't parse entities → plain без маркеров
            await msg.answer(ai_chat.strip_markup(out), reply_markup=main_keyboard(chat_id))


@dp.callback_query(F.data.startswith("ai_confirm:"))
async def cb_ai_confirm(call: CallbackQuery):
    chat_id = call.message.chat.id
    decision = call.data.split(":", 1)[1]
    pending = storage.get_ai_pending(chat_id)
    if not pending:
        await call.answer("Нечего подтверждать", show_alert=True)
        return
    storage.clear_ai_pending(chat_id)
    if decision == "yes":
        result = _apply_ai_proposal(chat_id, pending["proposal"])
        await call.message.edit_text(
            f"✅ Применено.\n{result}",
            parse_mode="HTML",
        )
        await call.answer("Готово")
    else:
        await call.message.edit_text("❌ Изменение отклонено.")
        await call.answer("Отменено")


@dp.message(F.text & ~F.text.startswith("/"))
async def on_free_text(msg: Message):
    """Rename после ✏️, подтверждение ИИ («да»), либо чат в ИИ-режиме."""
    chat_id = msg.chat.id
    text_raw = (msg.text or "").strip()
    menu_labels = {
        "📡 Скан", "⚙️ Фильтр", "📊 Статус", "🔗 Sync", "🔗 Синхронизация шаблонов",
        "🔄 Обновить шаблоны", "▶️ Старт", "⛔ Стоп",
        "🤖 ИИ", "🤖 ИИ ✓",
    }

    # 1) Rename template
    old = _pending_rename.get(chat_id)
    if old:
        if text_raw in menu_labels:
            _pending_rename.pop(chat_id, None)
            raise SkipHandler
        _pending_rename.pop(chat_id, None)
        ok, err = storage.rename_html_template(chat_id, old, text_raw, _filter_tab.get(chat_id))
        if not ok:
            await msg.answer(f"❌ {err}\nПопробуй ещё раз или /cancel")
            _pending_rename[chat_id] = old
            return
        await msg.answer(f"✅ «{old}» → «{text_raw}»")
        await _send_filter_menu(chat_id, market=_filter_tab.get(chat_id))
        return

    # 2) Pending AI setting confirmation
    pending = storage.get_ai_pending(chat_id)
    if pending:
        low = text_raw.lower()
        if low in _AI_YES:
            storage.clear_ai_pending(chat_id)
            result = _apply_ai_proposal(chat_id, pending["proposal"])
            await msg.answer(
                f"✅ Применено.\n{result}",
                parse_mode="HTML",
                reply_markup=main_keyboard(chat_id),
            )
            return
        if low in _AI_NO:
            storage.clear_ai_pending(chat_id)
            await msg.answer(
                "❌ Изменение отклонено.",
                reply_markup=main_keyboard(chat_id),
            )
            return
        # Не да/нет — напомнить, но если ИИ-режим — можно ответить на вопрос после напоминания
        await msg.answer(
            "Сейчас ждёт подтверждение изменения настроек от ИИ.\n"
            "Ответь <b>да</b> / <b>нет</b> или /cancel.\n"
            f"Предложение: {_format_proposal(pending['proposal'])}",
            parse_mode="HTML",
        )
        return

    # 3) AI mode free-text chat
    if text_raw in menu_labels:
        raise SkipHandler
    if not storage.get_ai_enabled(chat_id):
        raise SkipHandler
    await _handle_ai_question(msg, text_raw)


@dp.callback_query(F.data.startswith("tpl_all:"))
async def cb_tpl_all(call: CallbackQuery):
    chat_id = call.message.chat.id
    enable = call.data.endswith(":1")
    market = _filter_tab.get(chat_id, "crypto")
    if market not in _MARKETS:
        market = "crypto"
    tpls = storage.get_html_templates(chat_id, market)
    # Шаблоны только этого рынка
    all_names = list(tpls.keys())
    storage.set_active_templates_for_market(
        chat_id, market, all_names if enable else []
    )
    if market in _AUTO_MARKETS:
        storage.set_auto_mode(chat_id, market, False)
    await _send_filter_menu(chat_id, edit_msg=call.message, market=market)
    await call.answer(
        f"{_MKT_TAB[market]}: все включены" if enable else f"{_MKT_TAB[market]}: сброс"
    )


@dp.callback_query(F.data == "refresh_tpl")
async def cb_refresh_tpl(call: CallbackQuery):
    chat_id = call.message.chat.id
    await call.answer()
    if not PUBLIC_URL:
        await bot.send_message(
            chat_id,
            "⚠️ Нет PUBLIC_URL — сначала настрой Railway, затем /syncurl.",
            reply_markup=main_keyboard(chat_id),
        )
        return
    app = _app_url_with_sync(chat_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📱 Открыть скринер → sync",
            web_app=WebAppInfo(url=app),
        )
    ]])
    await bot.send_message(
        chat_id,
        "🔄 Открой Mini App — sync URL подставится сам, шаблоны уйдут в бота.",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "filter_done")
async def cb_filter_done(call: CallbackQuery):
    chat_id = call.message.chat.id
    _pending_rename.pop(chat_id, None)
    cfg = storage.get_active_config(chat_id)
    by = cfg.get("names_by_market") or {}
    enabled = set(cfg.get("markets") or [])
    parts = []
    for m, label in (("crypto", "крипта"), ("ru", "мосбиржа"), ("algo", "алго")):
        if m not in enabled:
            continue
        if (cfg.get("auto") or {}).get(m):
            parts.append(f"{label}: {AUTO_NAME}")
            continue
        n = len(by.get(m) or [])
        parts.append(f"{label}: {n}")
    mkts = ", ".join(parts) or "нет активных"
    _subscribers.add(chat_id)
    await call.message.edit_text(
        f"✅ Настройки сохранены.\n\n"
        f"Активно: <b>{mkts}</b>\n\n"
        f"Сигналы {SCAN_SCHEDULE_TEXT} "
        f"по включённым рынкам.",
        parse_mode="HTML",
    )
    await call.answer()


# ── /scan ─────────────────────────────────────────────────────────────────────
@dp.message(Command("scan"))
@dp.message(F.text == "📡 Скан")
async def cmd_scan(msg: Message):
    await msg.answer(
        "🔍 Запускаю скан... 1–3 минуты.\nТолько новые сигналы ≤12ч (без дампа истории).",
        reply_markup=main_keyboard(msg.chat.id),
    )
    try:
        stats: dict = {}
        n = await run_scan(
            on_signal=send_signal,
            subscribers=[msg.chat.id],
            chat_filters=_build_filter_for_chat,
            incremental=True,
            chat_best_only=_best_only_for_chat,
            stats=stats,
        )
        await msg.answer(
            f"✅ Скан завершён. Новых карточек: <b>{n}</b>.\n{_scan_summary(msg.chat.id, stats.get(msg.chat.id))}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("Ошибка скана")
        await msg.answer(f"❌ Ошибка: {e}")


def _scan_summary(chat_id: int, st: dict | None) -> str:
    """Итог ручного скана: сколько нашёл бы HTML за то же окно и куда делась разница."""
    if not st:
        return "Найдено за 12ч: 0 (нет активных шаблонов/рынков)."
    parts = [f"Найдено за 12ч: <b>{st['found']}</b>", f"отправлено: <b>{st['sent']}</b>"]
    for key, label in (("already", "уже были"), ("level_dup", "повтор уровня 36ч"),
                       ("conc", "лимит «одновременно»"), ("best_only", "«только лучший»"),
                       ("too_old", "старше 12ч")):
        if st.get(key):
            parts.append(f"{label}: {st[key]}")
    cfg = storage.get_active_config(chat_id)
    auto = [m for m, on in (cfg.get("auto") or {}).items() if on]
    tail = ""
    if auto:
        names = {"fbo": "FBO", "brk": "Пробой", "both": "Оба"}
        tail = "\n🤖 Авто, стратегия: " + ", ".join(
            f"{'крипта' if m == 'crypto' else 'MOEX'} — {names.get(storage.get_auto_strategy(chat_id, m), '?')}"
            for m in auto)
    if st.get("exchange") == "OKX" and ("crypto" in (cfg.get("markets") or [])):
        tail += "\n⚠️ Источник данных бота: OKX (Bybit недоступен или выключен), а HTML на Bybit — список монет и свечи отличаются."
    return " · ".join(parts) + tail


# ── best_only per chat (для screener._apply_best_only) ───────────────────────
def _best_only_for_chat(chat_id: int) -> dict:
    return storage.get_best_only(chat_id)


# ── Построить фильтр для чата из активных шаблонов ───────────────────────────
def _build_filter_for_chat(chat_id: int) -> dict:
    """Фильтр чата: активные шаблоны per-market + явный вкл рынка.

    Шаблоны раздельные по рынкам (crypto / ru / algo); активный набор — names_by_market.
    Рынок сканируется только если он включён и есть ≥1 активный шаблон.
    """
    cfg  = storage.get_active_config(chat_id)
    by   = cfg.get("names_by_market") or {m: [] for m in storage.MARKETS}
    enabled = set(cfg.get("markets") or [])

    overrides = storage.get_template_strategy_overrides(chat_id)
    multi = []
    markets = []

    for mkt in storage.MARKETS:
        if mkt not in enabled:
            continue
        tpls = storage.get_html_templates(chat_id, mkt)   # шаблоны раздельно по рынкам
        if mkt in _AUTO_MARKETS and (cfg.get("auto") or {}).get(mkt):
            # 🤖 Авто: только встроенный шаблон HTML «Авто» (все фильтры = DEF)
            multi.append(auto_template_entry(mkt, storage.get_auto_strategy(chat_id, mkt)))
            markets.append(mkt)
            continue
        names = by.get(mkt) or []
        if not names:
            continue
        any_ok = False
        for n in names:
            if n not in tpls:
                continue
            entry = tpls[n]
            tpl_filters = entry.get("filters") or {}
            strat = "algo" if mkt == "algo" else _effective_strategy(chat_id, n, tpl_filters, overrides)
            multi.append({
                "_name": n,
                "_market": mkt,
                "_strategy": strat,
                "filters": tpl_filters,
            })
            any_ok = True
        if any_ok:
            markets.append(mkt)

    if not multi:
        return {"markets": [], "_multi": []}

    return {"_multi": multi, "markets": markets}



# ── 👁 Callback: отслеживать сигнал ──────────────────────────────────────────
@dp.callback_query(F.data.startswith("watch:"))
async def cb_watch(call: CallbackQuery):
    chat_id = call.message.chat.id
    card_id = call.data[6:]
    if not card_id or card_id == "expired":
        await call.answer(
            "⏰ Сигнал устарел (доступно 48 ч после сигнала)",
            show_alert=True,
        )
        return

    card = _pending_cards.get(card_id)
    signal_ts = int(card.get("signal_ts") or 0) if card else 0
    created_at = int(time.time()) if card else 0

    if not card:
        row = storage.get_pending_signal_card(card_id, chat_id)
        if row:
            card, signal_ts, created_at = row
            _pending_cards[card_id] = card  # warm memory after restart
        else:
            # Fallback: id may belong to this chat under older sends without chat filter
            row = storage.get_pending_signal_card(card_id)
            if row:
                card, signal_ts, created_at = row
                if int(card.get("chat_id") or 0) not in (0, chat_id):
                    card = None

    # Уже пришедшие карточки (до SQLite / после рестарта): из текста сообщения
    if not card and call.message:
        card = _parse_card_from_message(call.message)
        if card:
            signal_ts = int(card.get("signal_ts") or 0)
            created_at = (
                int(call.message.date.timestamp())
                if getattr(call.message, "date", None)
                else int(time.time())
            )
            try:
                save_id = card_id
                if not save_id or save_id == "expired" or save_id == "msg":
                    save_id = storage.make_pending_card_id(
                        f"{card['ticker']}:{card['side']}:{card.get('strategy', 'brk')}:"
                        f"{int(card['level'] * 1e6)}",
                        chat_id,
                    )
                storage.save_pending_signal_card(
                    save_id,
                    chat_id,
                    {**card, "chat_id": chat_id},
                    signal_ts=signal_ts,
                )
                _pending_cards[save_id] = {**card, "chat_id": chat_id}
            except Exception as e:
                logger.warning("re-save parsed pending card: %s", e)

    if not card or not storage.pending_card_is_fresh(
        signal_ts or int(card.get("signal_ts") or 0),
        created_at,
    ):
        await call.answer(
            "⏰ Сигнал устарел (доступно 48 ч после сигнала)",
            show_alert=True,
        )
        return

    entry    = card.get("last", 0)
    stop     = card.get("stop", 0)
    risk_pct = abs(entry - stop) / entry if entry else 0

    item = {
        "market":    card.get("market", "crypto"),
        "strategy":  card.get("strategy", "brk"),
        "ticker":    card["ticker"],
        "side":      card["side"],
        "level":     card.get("level", 0),
        "entry":     entry,
        "stop":      stop,
        "take":      card.get("take", 0),
        "kind":      card.get("kind", ""),
        "prob":      card.get("prob", 0) or 0,
        "score":     card.get("score"),
        "strength":  card.get("strength"),
        "risk_pct":  risk_pct,
        "signal_ts": int(card.get("signal_ts") or time.time() * 1000),
    }
    watch_id = storage.add_to_watchlist(chat_id, item)

    # Mini App «Отслеживаемые»: also mirror into miniapp_watch (private chat_id == user_id)
    try:
        storage.append_miniapp_watch_item(
            chat_id, storage.watchlist_item_html_shape(item),
        )
    except Exception as e:
        logger.warning("miniapp_watch append failed: %s", e)

    # Обновляем кнопку → ✅ Отслеживается (с id для удаления)
    row = [InlineKeyboardButton(text="🗑 Удалить из отслеживаемого", callback_data=f"unwatch:{watch_id}")]
    view_btn = _view_signal_button(
        chat_id, item["ticker"], int(item["signal_ts"]), item.get("market") or "crypto",
    )
    rows = [row]
    if view_btn:
        rows.append([view_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    await call.answer("👁 Добавлено в отслеживаемые!")


@dp.callback_query(F.data.startswith("unwatch:"))
async def cb_unwatch(call: CallbackQuery):
    chat_id  = call.message.chat.id
    watch_id = int(call.data.split(":", 1)[1])
    try:
        wr = storage.get_watchlist_row(chat_id, watch_id)
        if wr:
            tk = wr["ticker"]
            mk = wr.get("market") or "crypto"
            base = tk if mk == "ru" else (tk[:-4] if tk.endswith("USDT") else tk)
            storage.remove_miniapp_watch_item(chat_id, base, int(wr["signal_ts"]), mk)
    except Exception as e:
        logger.warning("miniapp_watch remove failed: %s", e)
    storage.remove_from_watchlist(chat_id, watch_id)

    # Восстанавливаем 👁 — cb_watch снова разберёт текст сообщения при необходимости
    re_id = "msg"
    parsed = None
    try:
        parsed = _parse_card_from_message(call.message)
        if parsed:
            re_key = (
                f"{parsed['ticker']}:{parsed['side']}:{parsed.get('strategy', 'brk')}:"
                f"{int(parsed['level'] * 1e6)}"
            )
            re_id = storage.make_pending_card_id(re_key, chat_id)
            storage.save_pending_signal_card(
                re_id,
                chat_id,
                {**parsed, "chat_id": chat_id},
                signal_ts=int(parsed.get("signal_ts") or 0),
            )
            _pending_cards[re_id] = {**parsed, "chat_id": chat_id}
    except Exception as e:
        logger.warning("unwatch re-bind failed: %s", e)
    row = [InlineKeyboardButton(text="👁 Отслеживать", callback_data=f"watch:{re_id}")]
    rows = [row]
    if parsed:
        view_btn = _view_signal_button(
            chat_id,
            parsed.get("ticker") or "",
            int(parsed.get("signal_ts") or 0),
            parsed.get("market") or "crypto",
        )
        if view_btn:
            rows.append([view_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    await call.answer("Убрано из отслеживаемых")


# ── HTTP: отдать watchlist в HTML ─────────────────────────────────────────────
async def handle_watchlist(request: web.Request) -> web.Response:
    token   = request.match_info.get("token", "")
    chat_id = _resolve_sync_chat(token)
    if not chat_id:
        return web.json_response({"error": "invalid or expired token"}, status=401)
    # Mini App split-watch: market=all → оба раздела; иначе фильтр
    market  = request.rel_url.query.get("market", "all")
    items   = storage.get_watchlist(chat_id, market)
    return web.json_response({"ok": True, "watchlist": items})


# ── HTTP webhook — принимает шаблоны из HTML ─────────────────────────────────
async def handle_sync(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    chat_id = _resolve_sync_chat(token)
    if not chat_id:
        return web.json_response({"error": "invalid or expired token"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    templates = data.get("templates", {})
    market    = data.get("market", "crypto")

    if not isinstance(templates, dict) or not templates:
        return web.json_response({"error": "no templates"}, status=400)

    # Merge-only: never delete templates missing from this body
    storage.save_html_templates(chat_id, templates, market, remove_missing=False)

    # Уведомить пользователя в Telegram
    names = list(templates.keys())
    try:
        await bot.send_message(
            chat_id,
            f"✅ <b>Шаблоны синхронизированы!</b>\n\n"
            f"Загружено шаблонов: <b>{len(names)}</b>\n"
            + "\n".join(f"  • {n}" for n in names[:10])
            + ("\n  ..." if len(names) > 10 else "")
            + f"\n\nТеперь выбери активные через /filter",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить %s: %s", chat_id, e)

    return web.json_response({"ok": True, "count": len(names)})




# ── Manual scan API (Mini App «Сканировать» → сервер) ─────────────────────────
def _extract_sync_token(request: web.Request, body: dict | None = None) -> str:
    """Токен: X-Sync-Token | ?token= | body.token | Authorization Bearer."""
    h = request.headers.get("X-Sync-Token") or request.headers.get("x-sync-token") or ""
    if h.strip():
        return h.strip()
    q = request.rel_url.query.get("token") or ""
    if q.strip():
        return q.strip()
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if body and isinstance(body.get("token"), str):
        return body["token"].strip()
    return ""



def _resolve_manual_scan_chat(request: web.Request, body: dict | None = None) -> int | None:
    """chat_id для /api/manual-scan: sync-токен ИЛИ Telegram initData.

    После DB migrate / stale localStorage sync-токен часто не резолвится,
    но Mini App уже имеет валидный initData — принимаем оба пути.
    При успехе через initData регистрируем sync-токен для будущих запросов.
    """
    token = _extract_sync_token(request, body)
    if token:
        chat_id = _resolve_sync_chat(token)
        if chat_id is not None:
            return chat_id

    init_data = (
        request.headers.get("X-Telegram-Init-Data")
        or request.headers.get("x-telegram-init-data")
        or (request.rel_url.query.get("initData") or "")
    )
    if not init_data and body and isinstance(body.get("initData"), str):
        init_data = body["initData"].strip()
    else:
        init_data = (init_data or "").strip()
    if not init_data:
        return None

    try:
        from webapp_server import validate_init_data
    except ImportError:
        logger.warning("manual-scan: webapp_server.validate_init_data unavailable")
        return None

    parsed = validate_init_data(init_data)
    if not parsed or not parsed.get("user"):
        return None
    uid = parsed["user"].get("id")
    if uid is None:
        return None
    try:
        chat_id = int(uid)
    except (TypeError, ValueError):
        return None
    # Зарегистрировать sync-токен, чтобы последующие sync/scan работали без initData
    _register_sync_token(chat_id)
    return chat_id


def _manual_job_snapshot(job: dict) -> dict:
    out = {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": job.get("progress", 0),
        "message": job.get("message", ""),
        "n_sent": job.get("n_sent", 0),
    }
    if job.get("error"):
        out["error"] = job["error"]
    return out


async def handle_manual_scan_post(request: web.Request) -> web.Response:
    """POST /api/manual-scan — старт фонового скана с params HTML + filters."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)

    chat_id = _resolve_manual_scan_chat(request, body)
    if not chat_id:
        return web.json_response(
            {
                "error": "unauthorized",
                "hint": "открой Mini App кнопкой из бота или /syncurl",
            },
            status=401,
        )

    # Один активный manual job на чат
    existing_id = _manual_job_by_chat.get(chat_id)
    if existing_id:
        existing = _manual_jobs.get(existing_id)
        if existing and existing.get("status") in ("queued", "running"):
            return web.json_response(
                {
                    "error": "busy",
                    "message": "уже идёт ручной скан",
                    "job_id": existing_id,
                    **{k: existing.get(k) for k in ("status", "progress", "message", "n_sent")},
                },
                status=409,
            )

    market = body.get("market") or "crypto"
    if market not in ("crypto", "ru"):
        market = "crypto"
    strategy = body.get("strategy") or body.get("sc_strat") or "fbo"
    if strategy not in ("fbo", "brk", "both"):
        strategy = "fbo"
    source = (body.get("source") or "auto").strip().lower()
    if source not in ("auto", "bybit", "okx"):
        source = "auto"

    def _num(key, default):
        v = body.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(default)

    # HTML: minVol в млн ($ или ₽) → абсолютные единицы * 1e6
    min_vol_raw = body.get("minVol", body.get("min_vol", 1))
    try:
        min_vol_m = float(min_vol_raw)
    except (TypeError, ValueError):
        min_vol_m = 1.0
    # Если уже передали абсолют (>1e5) — не умножаем
    min_vol = min_vol_m if min_vol_m >= 100_000 else min_vol_m * 1_000_000.0

    n_inst = int(_num("nInst", body.get("n_inst", 300)) or 300)
    win_h = int(_num("winH", body.get("win_h", body.get("windowHours", 12))) or 12)
    no_night = bool(body.get("noNight", body.get("no_night", True)))
    no_stocks = bool(body.get("noStocks", body.get("no_stocks", True)))
    template_name = str(body.get("templateName") or body.get("template_name") or "").strip()
    filters = body.get("filters")
    if filters is not None and not isinstance(filters, dict):
        return web.json_response({"error": "filters must be object"}, status=400)
    filters = filters or {}

    job_id = uuid.uuid4().hex[:16]
    job = {
        "job_id": job_id,
        "chat_id": chat_id,
        "status": "queued",
        "progress": 0,
        "message": "в очереди",
        "n_sent": 0,
        "error": None,
        "created_at": time.time(),
    }
    _manual_jobs[job_id] = job
    _manual_job_by_chat[chat_id] = job_id

    async def _on_progress(info: dict):
        job["progress"] = float(info.get("progress") or job["progress"])
        job["message"] = str(info.get("message") or job["message"])
        if "n_sent" in info:
            job["n_sent"] = int(info["n_sent"])

    async def _runner():
        job["status"] = "running"
        job["message"] = "сканирование…"
        try:
            # Уведомить в Telegram о старте
            try:
                await bot.send_message(
                    chat_id,
                    (
                        "🔍 Фоновый скан Mini App…\n"
                        f"Рынок: <b>{'🇷🇺 MOEX' if market == 'ru' else '🌐 crypto'}</b>, "
                        f"стратегия: <b>{strategy}</b>, окно: <b>{win_h}ч</b> "
                        "(карточки ≤12ч).\n"
                        f"Шаблон: <b>{template_name or 'текущие фильтры'}</b>"
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("manual-scan notify start %s: %s", chat_id, e)

            n = await run_manual_scan(
                on_signal=send_signal,
                chat_id=chat_id,
                market=market,
                strategy=strategy,
                source=source,
                min_vol=min_vol,
                n_inst=n_inst,
                win_h=win_h,
                no_night=no_night,
                no_stocks=no_stocks,
                template_name=template_name,
                filters=filters,
                on_progress=_on_progress,
                chat_best_only=_best_only_for_chat,
            )
            job["n_sent"] = n
            job["status"] = "done"
            job["progress"] = 100
            job["message"] = f"готово, отправлено {n}"
            try:
                await bot.send_message(
                    chat_id,
                    f"✅ Фоновый скан завершён. Карточек: <b>{n}</b>.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("manual-scan notify done %s: %s", chat_id, e)
        except Exception as e:
            logger.exception("manual-scan failed chat=%s", chat_id)
            job["status"] = "error"
            job["error"] = str(e)
            job["message"] = f"ошибка: {e}"
            try:
                await bot.send_message(chat_id, f"❌ Фоновый скан: {e}")
            except Exception:
                pass
        finally:
            # освободить слот чата, если это всё ещё наш job
            if _manual_job_by_chat.get(chat_id) == job_id:
                _manual_job_by_chat.pop(chat_id, None)

    asyncio.create_task(_runner())
    return web.json_response({"ok": True, **_manual_job_snapshot(job)}, status=202)


async def handle_manual_scan_get(request: web.Request) -> web.Response:
    """GET /api/manual-scan/{job_id} — статус джоба."""
    job_id = request.match_info.get("job_id", "")
    job = _manual_jobs.get(job_id)
    if not job:
        return web.json_response({"error": "not found"}, status=404)
    # Опционально проверить auth (token / initData) — чтобы чужой job_id не светить
    chat_id = _resolve_manual_scan_chat(request)
    if chat_id is not None and chat_id != job.get("chat_id"):
        return web.json_response({"error": "forbidden"}, status=403)
    return web.json_response(_manual_job_snapshot(job))


async def handle_index(request: web.Request) -> web.Response:
    """Отдать HTML-скринер с inject bridge.js (серверное хранение watch/BT)."""
    import webapp_server as wa
    # Предпочитаем webapp/screener.html; иначе корневой HTML + тот же inject
    if wa.SCREENER_HTML.is_file():
        return await wa.handle_app(request)
    base = Path(__file__).resolve().parent
    for name in ("HW_FBO_scanner_6.html",):
        path = base / name
        if path.is_file():
            html = wa._inject_html(path.read_text(encoding="utf-8"))
            return web.Response(text=html, content_type="text/html", charset="utf-8")
    return web.Response(text="screener html missing", status=404)


def _mount_miniapp_routes(app: web.Application) -> None:
    """Маршруты Mini App из webapp_server (иначе /bridge.js и /api/miniapp/* 404)."""
    import webapp_server as wa
    app.middlewares.append(wa.cors_middleware)
    app.router.add_get("/bridge.js", wa.handle_bridge_js)
    app.router.add_get("/lean.js", wa.handle_lean_js)
    app.router.add_get("/mobile.css", wa.handle_mobile_css)
    app.router.add_get("/api/me", wa.api_me)
    app.router.add_get("/api/templates", wa.api_templates_get)
    app.router.add_put("/api/templates", wa.api_templates_put)
    app.router.add_put("/api/templates/{name}", wa.api_template_one_put)
    app.router.add_delete("/api/templates/{name}", wa.api_template_one_delete)
    app.router.add_put("/api/active-template", wa.api_active_template_put)
    app.router.add_get("/api/signal-filter", wa.api_signal_filter_get)
    app.router.add_put("/api/signal-filter", wa.api_signal_filter_put)
    app.router.add_get("/api/miniapp/state", wa.api_miniapp_state_get)
    app.router.add_put("/api/miniapp/state", wa.api_miniapp_state_put)
    app.router.add_get("/api/journal", wa.api_journal_get)
    app.router.add_get("/api/journal/chart/{row_id}/{tf}", wa.api_journal_chart)

    async def _options(request: web.Request) -> web.Response:
        return web.Response(status=204, headers=wa._cors_headers(request))

    app.router.add_route("OPTIONS", "/api/{tail:.*}", _options)


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# ── Фоновый скан ─────────────────────────────────────────────────────────────
async def scan_loop():
    # Без «догоняющего» скана после рестарта: всегда ждём ближайший HH:00:40.
    while True:
        nxt = next_scan_ts(time.time())
        logger.info("Следующий авто-скан: %s UTC",
                    datetime.fromtimestamp(nxt, timezone.utc).strftime("%H:%M:%S"))
        await asyncio.sleep(max(0.0, nxt - time.time()))
        hour_start = (int(time.time()) // 3600) * 3600
        t0 = time.time()
        if _subscribers:
            logger.info("Авто-скан для %d подписчиков", len(_subscribers))
            try:
                n = await run_scan(
                    on_signal=send_signal,
                    subscribers=list(_subscribers),
                    chat_filters=_build_filter_for_chat,
                    incremental=True,
                    chat_best_only=_best_only_for_chat,
                )
                dur = time.time() - t0
                late = time.time() - hour_start > SCAN_SEND_DEADLINE_S
                (logger.warning if late else logger.info)(
                    "Авто-скан: отправлено новых карточек=%d, длительность %.1f с, завершён в HH:%s%s",
                    n, dur, time.strftime("%M:%S", time.gmtime(time.time())),
                    " — ПОЗЖЕ HH:05!" if late else "",
                )
            except Exception:
                logger.exception("Ошибка авто-скана")
            try:
                res = await check_signal_outcomes()
                if res.get("resolved"):
                    logger.info(
                        "Исходы сигналов: проверено=%d решено=%d",
                        res.get("checked", 0), res.get("resolved", 0),
                    )
            except Exception:
                logger.exception("Ошибка проверки исходов сигналов")


# ── Запуск ────────────────────────────────────────────────────────────────────
async def main():
    storage.init()
    try:
        n = storage.cleanup_pending_signal_cards()
        if n:
            logger.info("Очищено устаревших pending cards: %d", n)
    except Exception:
        logger.exception("cleanup_pending_signal_cards failed")
    _sync_tokens.update(storage.load_all_sync_tokens())
    logger.info("Загружено sync-токенов: %d", len(_sync_tokens))

    await bot.set_my_commands([
        BotCommand(command="scan", description="Запустить скан сейчас"),
        BotCommand(command="filter", description="Шаблоны и рынки"),
        BotCommand(command="status", description="Текущие настройки"),
        BotCommand(command="ai", description="ИИ-помощник (Gemini)"),
        BotCommand(command="syncurl", description="Синхронизация шаблонов (Mini App)"),
        BotCommand(command="start", description="Подписаться на сигналы"),
        BotCommand(command="stop", description="Остановить сигналы"),
    ])

    # HTTP-сервер для webhook
    app = web.Application()
    app.router.add_post("/sync/{token}", handle_sync)
    app.router.add_get("/watchlist/{token}", handle_watchlist)
    app.router.add_post("/api/manual-scan", handle_manual_scan_post)
    app.router.add_get("/api/manual-scan/{job_id}", handle_manual_scan_get)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_index)
    app.router.add_get("/app", handle_index)
    app.router.add_get("/app/watch", handle_index)
    # Журнал: путь, не query — Telegram иногда отбрасывает query у web_app.
    app.router.add_get("/app/journal/{market}", handle_index)
    app.router.add_get("/app/journal/{market}/{sig_id}", handle_index)
    _mount_miniapp_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("HTTP сервер запущен на порту %d", PORT)

    asyncio.create_task(scan_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
