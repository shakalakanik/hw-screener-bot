"""bot.py — HW Screener Bot с синхронизацией шаблонов из HTML."""
import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import (
    Message, CallbackQuery,
    BotCommand, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup,
    WebAppInfo,
)
from urllib.parse import quote

import storage
from screener import run_scan

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

TOKEN           = os.environ["TG_BOT_TOKEN"]
SCAN_INTERVAL   = int(os.environ.get("SCAN_INTERVAL_MIN", "15")) * 60
PORT            = int(os.environ.get("PORT", "8080"))
PUBLIC_URL      = os.environ.get("PUBLIC_URL", "").rstrip("/")   # https://yourapp.railway.app
MSK = timezone(timedelta(hours=3))

bot = Bot(token=TOKEN)
dp  = Dispatcher()

_subscribers: set[int] = set()
_pending_cards: dict[str, dict] = {}   # card_key → card data
_pending_rename: dict[int, str] = {}   # chat_id → old template name
_filter_tab: dict[int, str] = {}       # chat_id → "crypto" | "ru"

# ── Sync-токены: chat_id → token ──────────────────────────────────────────────
_sync_tokens: dict[str, int] = {}   # token → chat_id

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


def _app_url_with_sync(chat_id: int) -> str | None:
    sync = _sync_endpoint(chat_id)
    if not sync:
        return None
    return f"{PUBLIC_URL}/app?sync={quote(sync, safe='')}&autosync=1"


def main_keyboard(chat_id: int | None = None) -> ReplyKeyboardMarkup:
    """Клавиатура; «Обновить шаблоны» — WebApp с sync URL, если PUBLIC_URL задан."""
    rows = [
        [KeyboardButton(text="📡 Скан"), KeyboardButton(text="⚙️ Фильтр")],
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="🔗 Sync")],
    ]
    app_url = _app_url_with_sync(chat_id) if chat_id else None
    if app_url:
        rows.append([KeyboardButton(
            text="🔄 Обновить шаблоны",
            web_app=WebAppInfo(url=app_url),
        )])
    else:
        rows.append([KeyboardButton(text="🔄 Обновить шаблоны")])
    rows.append([KeyboardButton(text="▶️ Старт"), KeyboardButton(text="⛔ Стоп")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


# Совместимость: статический alias (без WebApp) — лучше передавать chat_id
MAIN_KEYBOARD = main_keyboard()


# ── Форматирование сигнала ────────────────────────────────────────────────────
_MARKET_LABEL = {"crypto": "🌐 Крипта", "ru": "🇷🇺 Мосбиржа"}


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
    strategy_label = "📈 Пробой" if strategy == "brk" else "🔻 Ложный пробой"
    prob = card.get("prob")
    prob_line = f"  (модель p={prob:.2f})" if strategy == "fbo" and prob is not None else ""

    # Время самого сигнала (час его появления на рынке), не время отправки сообщения. MSK, не UTC.
    signal_ts = card.get("signal_ts")
    if signal_ts:
        ts_msk = datetime.fromtimestamp(signal_ts / 1000, tz=MSK)
        time_str = ts_msk.strftime("%d.%m %H:%M МСК")
    else:
        time_str = datetime.now(MSK).strftime("%d.%m %H:%M МСК") + " (время скана)"

    return "\n".join([
        f"{side_emoji} <b>{card['ticker']}</b> — {card['side']}",
        f"{market_label}  ·  {strategy_label}{prob_line}",
        tpl_line.rstrip("\n"),
        f"🎯 Уровень: <code>{level:.4f}</code>  [{card['kind']}]",
        f"📥 Вход: <code>{entry:.4f}</code>  (расст. {dist_pct:.0f}% ATR)",
        f"⛔ Стоп-лосс: <code>{stop:.4f}</code>  (−{risk_pct:.1f}%)",
        f"💰 Тейк-профит: <code>{take:.4f}</code>  (+{tp_pct:.1f}%)",
        f"💪 Сила: {'★' * card['strength']}{'☆' * (5 - card['strength'])}",
        f"📊 Тренд D1: {bias}",
        f"🕐 Сигнал: {time_str}",
    ])


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
        try:
            card_key = f"{card['ticker']}:{card['side']}:{card.get('strategy','brk')}:{int(card['level']*1e6)}"
            _pending_cards[card_key] = {**card, "chat_id": chat_id}
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="👁 Отслеживать", callback_data=f"watch:{card_key[:60]}"),
            ]])
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logger.warning("Не удалось отправить %s: %s", chat_id, e)


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
        "/syncurl — постоянная ссылка синхронизации (один раз)\n"
        "🔄 Обновить шаблоны — Mini App без повторного URL\n"
        "/scan — запустить скан сейчас\n"
        "/status — текущие настройки\n"
        "/stop — остановить сигналы",
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
@dp.message(F.text == "🔗 Sync")
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
                text="📱 Открыть Mini App (авто-sync)",
                web_app=WebAppInfo(url=app),
            )
        ]])
    await msg.answer(
        f"🔗 <b>URL синхронизации</b> (постоянный)\n\n"
        f"<code>{url}</code>\n\n"
        f"Вставь <b>один раз</b> в HTML (📬) — сохранится в браузере.\n"
        f"Дальше жми «🔄 Обновить шаблоны» или 📬 — без новой ссылки.\n\n"
        f"Ссылка не протухает после деплоя (пока не сменится токен бота).",
        parse_mode="HTML",
        reply_markup=kb_extra or main_keyboard(chat_id),
    )
    if kb_extra:
        await msg.answer(
            "Клавиатура обновлена.",
            reply_markup=main_keyboard(chat_id),
        )


# ── 🔄 Обновить шаблоны — Mini App с pre-injected sync URL ───────────────────
@dp.message(Command("refresh_tpl"))
@dp.message(F.text == "🔄 Обновить шаблоны")
async def cmd_refresh_tpl(msg: Message):
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
@dp.message(Command("status"))
@dp.message(F.text == "📊 Статус")
async def cmd_status(msg: Message):
    chat_id   = msg.chat.id
    cfg       = storage.get_active_config(chat_id)
    tpls      = storage.get_html_templates(chat_id)
    subscribed = chat_id in _subscribers

    by_mkt         = cfg.get("names_by_market") or {"crypto": [], "ru": []}
    active_markets = cfg["markets"]

    mkt_str = " + ".join(
        {"crypto": "Крипта", "ru": "Мосбиржа"}.get(m, m)
        for m in active_markets
    ) or "нет (включи шаблоны в /filter)"

    if not tpls:
        tpl_str = "Шаблоны не синхронизированы.\nИспользуй /syncurl → кнопку 📬 в HTML."
    elif not any(by_mkt.get(m) for m in ("crypto", "ru")):
        tpl_str = f"Шаблонов загружено: {len(tpls)}\nАктивных: нет — выбери через /filter"
    else:
        overrides = storage.get_template_strategy_overrides(chat_id)
        label = {"fbo": "🔻ЛП", "brk": "📈Проб", "both": "📈🔻Оба"}
        blocks = []
        for m, mlabel in (("crypto", "🌐 Крипта"), ("ru", "🇷🇺 Мосбиржа")):
            names = by_mkt.get(m) or []
            if not names:
                blocks.append(f"{mlabel}: —")
                continue
            lines = []
            for n in names:
                strat = _effective_strategy(
                    chat_id, n, tpls.get(n, {}).get("filters", {}), overrides
                )
                lines.append(f"  ✅ {label.get(strat, '')} {n}")
            blocks.append(f"{mlabel}:\n" + "\n".join(lines))
        tpl_str = "\n".join(blocks)

    await msg.answer(
        f"📋 <b>Статус</b>\n\n"
        f"Подписка: {'✅ активна' if subscribed else '❌ остановлена'}\n"
        f"Рынки: {mkt_str}\n\n"
        f"{tpl_str}\n\n"
        f"Интервал скана: каждые {SCAN_INTERVAL // 60} мин",
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
            "3. Один раз: /syncurl или «🔄 Обновить шаблоны»\n"
            "4. В Mini App нажми 📬 (URL сохранится сам)"
        )
        return
    await _send_filter_menu(chat_id)


_STRAT_CYCLE = {"fbo": "brk", "brk": "both", "both": "fbo"}
_STRAT_ICON  = {"fbo": "🔻ЛП", "brk": "📈Проб", "both": "📈🔻Оба"}
_MKT_TAB     = {"crypto": "🌐 Крипта", "ru": "🇷🇺 Мосбиржа"}


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
    by = cfg.get("names_by_market") or {"crypto": [], "ru": []}
    enabled = set(cfg.get("markets") or [])

    buttons = [[
        InlineKeyboardButton(text="🌐 Крипта", callback_data="filter_mkt:crypto"),
        InlineKeyboardButton(text="🇷🇺 Мосбиржа", callback_data="filter_mkt:ru"),
    ]]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    def _mkt_line(mkt: str, label: str) -> str:
        on = mkt in enabled
        n = len(by.get(mkt) or [])
        state = "🟢 вкл" if on else "⚪ выкл"
        return f"{label}: {state}, шаблонов: {n}"

    txt = (
        "🎛 <b>Фильтр</b>\n\n"
        "Выбери рынок:\n"
        f"• {_mkt_line('crypto', '🌐 Крипта')}\n"
        f"• {_mkt_line('ru', '🇷🇺 Мосбиржа')}"
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
    if market not in ("crypto", "ru"):
        await _send_filter_root(chat_id, edit_msg=edit_msg)
        return

    _filter_tab[chat_id] = market
    tpls = storage.get_html_templates(chat_id)
    cfg = storage.get_active_config(chat_id)
    by_mkt = cfg.get("names_by_market") or {"crypto": [], "ru": []}
    active = set(by_mkt.get(market) or [])
    enabled = market in (cfg.get("markets") or [])
    overrides = storage.get_template_strategy_overrides(chat_id)

    # Общий список HTML-шаблонов на обоих рынках (не режем по market тегу)
    shared_names = list(tpls.keys())

    buttons = []
    on_off = "🟢 Рынок ВКЛ" if enabled else "⚪ Рынок ВЫКЛ"
    buttons.append([InlineKeyboardButton(
        text=on_off,
        callback_data=f"mkt_on:{market}",
    )])

    if not shared_names:
        buttons.append([InlineKeyboardButton(
            text="(нет шаблонов — синхронизируй из HTML)",
            callback_data="noop",
        )])
    else:
        for name in shared_names:
            check = "✅" if name in active else "⬜"
            strat = _effective_strategy(
                chat_id, name, (tpls[name].get("filters") or {}), overrides
            )
            strat_tag = _STRAT_ICON.get(strat, "🔻ЛП")
            key = name[:40]
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
    txt = (
        f"🎛 <b>{_MKT_TAB[market]}</b>\n\n"
        f"Рынок: <b>{'вкл' if enabled else 'выкл'}</b> · "
        f"активных шаблонов: <b>{n_on}</b>\n\n"
        "• Вкл/выкл рынок — получать сигналы по нему\n"
        "• Слева — шаблон для этого рынка (список общий)\n"
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
    if mkt not in ("crypto", "ru"):
        await call.answer()
        return
    await _send_filter_menu(chat_id, edit_msg=call.message, market=mkt)
    await call.answer(_MKT_TAB[mkt])


# Совместимость со старыми callback
@dp.callback_query(F.data.startswith("fmkt:"))
async def cb_filter_tab(call: CallbackQuery):
    chat_id = call.message.chat.id
    mkt = call.data.split(":", 1)[1]
    if mkt not in ("crypto", "ru"):
        await call.answer()
        return
    await _send_filter_menu(chat_id, edit_msg=call.message, market=mkt)
    await call.answer(_MKT_TAB[mkt])


@dp.callback_query(F.data.startswith("mkt:"))
async def cb_market(call: CallbackQuery):
    chat_id = call.message.chat.id
    mkt = call.data.split(":", 1)[1]
    if mkt not in ("crypto", "ru"):
        await call.answer()
        return
    await _send_filter_menu(chat_id, edit_msg=call.message, market=mkt)
    await call.answer(_MKT_TAB[mkt])


@dp.callback_query(F.data.startswith("mkt_on:"))
async def cb_mkt_on(call: CallbackQuery):
    chat_id = call.message.chat.id
    mkt = call.data.split(":", 1)[1]
    if mkt not in ("crypto", "ru"):
        await call.answer()
        return
    cfg = storage.get_active_config(chat_id)
    enabled = set(cfg.get("markets") or [])
    turn_on = mkt not in enabled
    storage.set_market_enabled(chat_id, mkt, turn_on)
    await _send_filter_menu(chat_id, edit_msg=call.message, market=mkt)
    await call.answer("вкл" if turn_on else "выкл")


@dp.callback_query(F.data.startswith("tpl:"))
async def cb_tpl(call: CallbackQuery):
    chat_id = call.message.chat.id
    key = call.data.split(":", 1)[1]
    tpls = storage.get_html_templates(chat_id)
    full_name = _resolve_tpl_name(tpls, key)
    if not full_name:
        await call.answer("Шаблон не найден")
        return
    market = _filter_tab.get(chat_id)
    if market not in ("crypto", "ru"):
        market = "crypto"
    cfg = storage.get_active_config(chat_id)
    active = list((cfg.get("names_by_market") or {}).get(market) or [])
    if full_name in active:
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
    tpls = storage.get_html_templates(chat_id)
    full_name = _resolve_tpl_name(tpls, key)
    if not full_name:
        await call.answer("Шаблон не найден")
        return
    market = _filter_tab.get(chat_id)
    if market not in ("crypto", "ru"):
        market = "crypto"
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
    _filter_tab[chat_id] = market if market in ("crypto", "ru") else "crypto"
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
    if chat_id in _pending_rename:
        _pending_rename.pop(chat_id, None)
        await msg.answer("Отменено.")
        await _send_filter_menu(chat_id, market=_filter_tab.get(chat_id))
    else:
        await msg.answer("Нечего отменять.")


@dp.message(F.text & ~F.text.startswith("/"))
async def on_rename_text(msg: Message):
    """Перехват следующего сообщения после ✏️ — только если ждём rename."""
    chat_id = msg.chat.id
    old = _pending_rename.get(chat_id)
    if not old:
        raise SkipHandler
    menu_labels = {
        "📡 Скан", "⚙️ Фильтр", "📊 Статус", "🔗 Sync",
        "🔄 Обновить шаблоны", "▶️ Старт", "⛔ Стоп",
    }
    if (msg.text or "") in menu_labels:
        _pending_rename.pop(chat_id, None)
        raise SkipHandler

    _pending_rename.pop(chat_id, None)
    new_name = (msg.text or "").strip()
    ok, err = storage.rename_html_template(chat_id, old, new_name)
    if not ok:
        await msg.answer(f"❌ {err}\nПопробуй ещё раз или /cancel")
        _pending_rename[chat_id] = old
        return
    await msg.answer(f"✅ «{old}» → «{new_name}»")
    await _send_filter_menu(chat_id, market=_filter_tab.get(chat_id))


@dp.callback_query(F.data.startswith("tpl_all:"))
async def cb_tpl_all(call: CallbackQuery):
    chat_id = call.message.chat.id
    enable = call.data.endswith(":1")
    market = _filter_tab.get(chat_id, "crypto")
    if market not in ("crypto", "ru"):
        market = "crypto"
    tpls = storage.get_html_templates(chat_id)
    # Общий список — все HTML-шаблоны
    all_names = list(tpls.keys())
    storage.set_active_templates_for_market(
        chat_id, market, all_names if enable else []
    )
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
    for m, label in (("crypto", "крипта"), ("ru", "мосбиржа")):
        if m not in enabled:
            continue
        n = len(by.get(m) or [])
        parts.append(f"{label}: {n}")
    mkts = ", ".join(parts) or "нет активных"
    _subscribers.add(chat_id)
    await call.message.edit_text(
        f"✅ Настройки сохранены.\n\n"
        f"Активно: <b>{mkts}</b>\n\n"
        f"Сигналы каждые {SCAN_INTERVAL // 60} мин "
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
        n = await run_scan(
            on_signal=send_signal,
            subscribers=[msg.chat.id],
            chat_filters=_build_filter_for_chat,
            incremental=True,
        )
        await msg.answer(f"✅ Скан завершён. Новых карточек: <b>{n}</b>.", parse_mode="HTML")
    except Exception as e:
        logger.exception("Ошибка скана")
        await msg.answer(f"❌ Ошибка: {e}")


# ── Построить фильтр для чата из активных шаблонов ───────────────────────────
def _build_filter_for_chat(chat_id: int) -> dict:
    """Фильтр чата: активные шаблоны per-market + явный вкл рынка.

    Список шаблонов общий (HTML); активный набор — names_by_market.
    Рынок сканируется только если он включён и есть ≥1 активный шаблон.
    """
    cfg  = storage.get_active_config(chat_id)
    tpls = storage.get_html_templates(chat_id)
    by   = cfg.get("names_by_market") or {"crypto": [], "ru": []}
    enabled = set(cfg.get("markets") or [])

    overrides = storage.get_template_strategy_overrides(chat_id)
    multi = []
    markets = []

    for mkt in ("crypto", "ru"):
        if mkt not in enabled:
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
            strat = _effective_strategy(chat_id, n, tpl_filters, overrides)
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
    chat_id  = call.message.chat.id
    card_key = call.data[6:]
    card     = _pending_cards.get(card_key)
    if not card:
        await call.answer("⏰ Сигнал устарел, данные не сохранились", show_alert=True)
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
        "risk_pct":  risk_pct,
        "signal_ts": int(time.time() * 1000),
    }
    watch_id = storage.add_to_watchlist(chat_id, item)

    # Обновляем кнопку → ✅ Отслеживается (с id для удаления)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Отслеживается — убрать",
            callback_data=f"unwatch:{watch_id}",
        )
    ]])
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    await call.answer("👁 Добавлено в отслеживаемые!")


@dp.callback_query(F.data.startswith("unwatch:"))
async def cb_unwatch(call: CallbackQuery):
    chat_id  = call.message.chat.id
    watch_id = int(call.data.split(":", 1)[1])
    storage.remove_from_watchlist(chat_id, watch_id)

    # Восстанавливаем кнопку "Отслеживать"
    # Находим card_key из текста сообщения — просто убираем кнопку
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👁 Отслеживать", callback_data="watch:expired")
    ]])
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
    market  = request.rel_url.query.get("market", "crypto")
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

    storage.save_html_templates(chat_id, templates, market)

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



async def handle_index(request: web.Request) -> web.Response:
    """Отдать HTML-скринер (Mini App / браузер)."""
    base = Path(__file__).resolve().parent
    for name in ("HW_FBO_scanner_6.html", "webapp/screener.html"):
        path = base / name
        if path.is_file():
            return web.FileResponse(path)
    return web.Response(text="screener html missing", status=404)


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# ── Фоновый скан ─────────────────────────────────────────────────────────────
async def scan_loop():
    await asyncio.sleep(15)
    while True:
        if _subscribers:
            logger.info("Авто-скан для %d подписчиков", len(_subscribers))
            try:
                n = await run_scan(
                    on_signal=send_signal,
                    subscribers=list(_subscribers),
                    chat_filters=_build_filter_for_chat,
                    incremental=True,
                )
                logger.info("Авто-скан: отправлено новых карточек=%d", n)
            except Exception:
                logger.exception("Ошибка авто-скана")
        await asyncio.sleep(SCAN_INTERVAL)


# ── Запуск ────────────────────────────────────────────────────────────────────
async def main():
    storage.init()
    _sync_tokens.update(storage.load_all_sync_tokens())
    logger.info("Загружено sync-токенов: %d", len(_sync_tokens))

    await bot.set_my_commands([
        BotCommand(command="scan", description="Запустить скан сейчас"),
        BotCommand(command="filter", description="Шаблоны и рынки"),
        BotCommand(command="status", description="Текущие настройки"),
        BotCommand(command="syncurl", description="Ссылка синхронизации HTML"),
        BotCommand(command="refresh_tpl", description="Обновить шаблоны из Mini App"),
        BotCommand(command="start", description="Подписаться на сигналы"),
        BotCommand(command="stop", description="Остановить сигналы"),
    ])

    # HTTP-сервер для webhook
    app = web.Application()
    app.router.add_post("/sync/{token}", handle_sync)
    app.router.add_get("/watchlist/{token}", handle_watchlist)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_index)
    app.router.add_get("/app", handle_index)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("HTTP сервер запущен на порту %d", PORT)

    asyncio.create_task(scan_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
