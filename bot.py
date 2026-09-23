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
from aiogram.types import (
    Message, CallbackQuery,
    BotCommand, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup,
)

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

# ── Sync-токены: chat_id → token ──────────────────────────────────────────────
_sync_tokens: dict[str, int] = {}   # token → chat_id

# ── Основная клавиатура ──────────────────────────────────────────────────────
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📡 Скан"), KeyboardButton(text="⚙️ Фильтр")],
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="🔗 Sync")],
        [KeyboardButton(text="▶️ Старт"), KeyboardButton(text="⛔ Стоп")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

def _make_token(chat_id: int) -> str:
    raw = f"{chat_id}:{TOKEN}:{int(time.time() // 3600)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


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
        "/syncurl — получить ссылку для синхронизации из HTML\n"
        "/scan — запустить скан сейчас\n"
        "/status — текущие настройки\n"
        "/stop — остановить сигналы",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


# ── /stop ─────────────────────────────────────────────────────────────────────
@dp.message(Command("stop"))
@dp.message(F.text == "⛔ Стоп")
async def cmd_stop(msg: Message):
    _subscribers.discard(msg.chat.id)
    await msg.answer(
        "⛔ Сигналы остановлены. Нажми «▶️ Старт», чтобы возобновить.",
        reply_markup=MAIN_KEYBOARD,
    )


# ── /syncurl — выдать ссылку для HTML ────────────────────────────────────────
@dp.message(Command("syncurl"))
@dp.message(F.text == "🔗 Sync")
async def cmd_syncurl(msg: Message):
    if not PUBLIC_URL:
        await msg.answer(
            "⚠️ Переменная <code>PUBLIC_URL</code> не задана в Railway.\n\n"
            "Зайди в Railway → Variables → добавь:\n"
            "<code>PUBLIC_URL = https://&lt;твой домен&gt;.railway.app</code>",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    token = _make_token(msg.chat.id)
    _sync_tokens[token] = msg.chat.id
    url = f"{PUBLIC_URL}/sync/{token}"
    await msg.answer(
        f"🔗 <b>URL для синхронизации шаблонов</b>\n\n"
        f"<code>{url}</code>\n\n"
        f"Скопируй этот URL и вставь в HTML-скринер когда он попросит "
        f"(кнопка 📬 <b>Синхронизировать с ботом</b>).\n\n"
        f"Ссылка действует 1 час.",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


# ── /status ───────────────────────────────────────────────────────────────────
@dp.message(Command("status"))
@dp.message(F.text == "📊 Статус")
async def cmd_status(msg: Message):
    chat_id   = msg.chat.id
    cfg       = storage.get_active_config(chat_id)
    tpls      = storage.get_html_templates(chat_id)
    subscribed = chat_id in _subscribers

    active_names   = cfg["names"]
    active_markets = cfg["markets"]

    mkt_str = " + ".join(
        {"crypto": "Крипта (Bybit)", "ru": "MOEX"}.get(m, m)
        for m in active_markets
    ) or "не выбраны"

    if not tpls:
        tpl_str = "Шаблоны не синхронизированы.\nИспользуй /syncurl → кнопку 📬 в HTML."
    elif not active_names:
        tpl_str = f"Шаблонов загружено: {len(tpls)}\nАктивных: нет — выбери через /filter"
    else:
        overrides = storage.get_template_strategy_overrides(chat_id)
        label = {"fbo": "🔻ЛП", "brk": "📈Проб", "both": "📈🔻Оба"}
        lines = []
        for n in active_names:
            strat = _effective_strategy(chat_id, n, tpls.get(n, {}).get("filters", {}), overrides)
            lines.append(f"  ✅ {label.get(strat, '')} {n}")
        tpl_str = f"Активных шаблонов: {len(active_names)}\n" + "\n".join(lines)

    await msg.answer(
        f"📋 <b>Статус</b>\n\n"
        f"Подписка: {'✅ активна' if subscribed else '❌ остановлена'}\n"
        f"Рынки: {mkt_str}\n\n"
        f"{tpl_str}\n\n"
        f"Интервал скана: каждые {SCAN_INTERVAL // 60} мин",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


# ── /filter — выбор шаблонов и рынков ────────────────────────────────────────
@dp.message(Command("filter"))
@dp.message(F.text == "⚙️ Фильтр")
async def cmd_filter(msg: Message):
    chat_id = msg.chat.id
    tpls    = storage.get_html_templates(chat_id)
    if not tpls:
        await msg.answer(
            "📭 Шаблоны ещё не синхронизированы.\n\n"
            "1. Открой HTML-скринер\n"
            "2. Сохрани шаблоны кнопкой «Сохранить как шаблон»\n"
            "3. Получи ссылку через /syncurl\n"
            "4. Нажми 📬 Синхронизировать с ботом в HTML"
        )
        return
    await _send_filter_menu(msg.chat.id)


_STRAT_CYCLE = {"fbo": "brk", "brk": "both", "both": "fbo"}
_STRAT_ICON  = {"fbo": "🔻ЛП", "brk": "📈Проб", "both": "📈🔻Оба"}


def _effective_strategy(chat_id: int, name: str, tpl_filters: dict, overrides: dict) -> str:
    """Явное переопределение из бота важнее _strat, записанного в HTML."""
    if name in overrides:
        return overrides[name]
    return tpl_filters.get("_strat", "fbo")  # HTML stratOf fallback: e?e.value:'fbo'


async def _send_filter_menu(chat_id: int, edit_msg=None):
    tpls      = storage.get_html_templates(chat_id)
    cfg       = storage.get_active_config(chat_id)
    active    = set(cfg["names"])
    active_markets = set(cfg["markets"])
    overrides = storage.get_template_strategy_overrides(chat_id)

    buttons = []

    # Рынки
    buttons.append([InlineKeyboardButton(text="── Рынки ──", callback_data="noop")])
    mkt_row = []
    for mkt, label in [("crypto", "🌐 Крипта"), ("ru", "🇷🇺 MOEX")]:
        check = "✅" if mkt in active_markets else "⬜"
        mkt_row.append(InlineKeyboardButton(
            text=f"{check} {label}", callback_data=f"mkt:{mkt}"
        ))
    buttons.append(mkt_row)

    # Шаблоны — для каждого своя строка: чекбокс включения + переключатель стратегии
    buttons.append([InlineKeyboardButton(
        text="── Шаблоны (жми на стратегию чтобы сменить) ──", callback_data="noop"
    )])
    for name in tpls:
        check = "✅" if name in active else "⬜"
        market_tag = tpls[name]["market"]
        mkt_icon = "🌐" if market_tag == "crypto" else "🇷🇺"
        strat = _effective_strategy(chat_id, name, tpls[name]["filters"], overrides)
        strat_tag = _STRAT_ICON.get(strat, "")
        key = name[:40]
        buttons.append([
            InlineKeyboardButton(text=f"{check} {mkt_icon} {name}", callback_data=f"tpl:{key}"),
            InlineKeyboardButton(text=strat_tag, callback_data=f"strat:{key}"),
        ])

    # Кнопки управления
    buttons.append([
        InlineKeyboardButton(text="✅ Включить все", callback_data="tpl_all:1"),
        InlineKeyboardButton(text="⬜ Выключить все", callback_data="tpl_all:0"),
    ])
    buttons.append([InlineKeyboardButton(text="💾 Сохранить и закрыть", callback_data="filter_done")])

    kb  = InlineKeyboardMarkup(inline_keyboard=buttons)
    txt = (
        "🎛 <b>Настройка фильтров</b>\n\n"
        "Слева — включить/выключить шаблон.\n"
        "Справа — нажми, чтобы переключить стратегию шаблона: "
        "📈 Пробой → 🔻 Ложный пробой → 📈🔻 Оба → по кругу.\n\n"
        "Рынки:"
    )

    if edit_msg:
        await edit_msg.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    else:
        await bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@dp.callback_query(F.data.startswith("mkt:"))
async def cb_market(call: CallbackQuery):
    chat_id = call.message.chat.id
    mkt     = call.data.split(":", 1)[1]
    cfg     = storage.get_active_config(chat_id)
    markets = set(cfg["markets"])
    if mkt in markets:
        markets.discard(mkt)
    else:
        markets.add(mkt)
    storage.set_active_markets(chat_id, list(markets))
    await _send_filter_menu(chat_id, edit_msg=call.message)
    await call.answer()


@dp.callback_query(F.data.startswith("tpl:"))
async def cb_tpl(call: CallbackQuery):
    chat_id = call.message.chat.id
    name    = call.data.split(":", 1)[1]
    tpls    = storage.get_html_templates(chat_id)
    # Найти полное имя (callback обрезает до 40 символов)
    full_name = next((k for k in tpls if k[:40] == name), name)
    cfg     = storage.get_active_config(chat_id)
    active  = set(cfg["names"])
    if full_name in active:
        active.discard(full_name)
    else:
        active.add(full_name)
    storage.set_active_templates(chat_id, list(active))
    await _send_filter_menu(chat_id, edit_msg=call.message)
    await call.answer()


@dp.callback_query(F.data.startswith("strat:"))
async def cb_strat(call: CallbackQuery):
    chat_id = call.message.chat.id
    name    = call.data.split(":", 1)[1]
    tpls    = storage.get_html_templates(chat_id)
    full_name = next((k for k in tpls if k[:40] == name), name)
    if full_name not in tpls:
        await call.answer("Шаблон не найден")
        return
    overrides = storage.get_template_strategy_overrides(chat_id)
    current = _effective_strategy(chat_id, full_name, tpls[full_name]["filters"], overrides)
    new_strat = _STRAT_CYCLE[current]
    storage.set_template_strategy(chat_id, full_name, new_strat)
    await _send_filter_menu(chat_id, edit_msg=call.message)
    label = {"fbo": "Ложный пробой", "brk": "Пробой", "both": "Оба"}[new_strat]
    await call.answer(f"{full_name}: {label}")


@dp.callback_query(F.data.startswith("tpl_all:"))
async def cb_tpl_all(call: CallbackQuery):
    chat_id = call.message.chat.id
    enable  = call.data.endswith(":1")
    tpls    = storage.get_html_templates(chat_id)
    storage.set_active_templates(chat_id, list(tpls.keys()) if enable else [])
    await _send_filter_menu(chat_id, edit_msg=call.message)
    await call.answer("Все включены" if enable else "Все выключены")


@dp.callback_query(F.data == "filter_done")
async def cb_filter_done(call: CallbackQuery):
    chat_id = call.message.chat.id
    cfg     = storage.get_active_config(chat_id)
    n       = len(cfg["names"])
    mkts    = ", ".join(cfg["markets"]) or "нет"
    _subscribers.add(chat_id)
    await call.message.edit_text(
        f"✅ Настройки сохранены.\n\n"
        f"Активных шаблонов: <b>{n}</b>\n"
        f"Рынки: <b>{mkts}</b>\n\n"
        f"Сигналы будут приходить каждые {SCAN_INTERVAL // 60} мин.",
        parse_mode="HTML",
    )
    await call.answer()


# ── /scan ─────────────────────────────────────────────────────────────────────
@dp.message(Command("scan"))
@dp.message(F.text == "📡 Скан")
async def cmd_scan(msg: Message):
    await msg.answer(
        "🔍 Запускаю скан... 1–3 минуты.\nТолько новые сигналы ≤12ч (без дампа истории).",
        reply_markup=MAIN_KEYBOARD,
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
    cfg    = storage.get_active_config(chat_id)
    tpls   = storage.get_html_templates(chat_id)
    active = cfg["names"]
    if not active or not tpls:
        # Без синхронизированных шаблонов: FBO + DEF.thr=0.20, по каждому
        # рынку из /filter (crypto и/или ru) — чтобы MOEX сканировался без tpl.
        markets = cfg.get("markets") or ["crypto"]
        return {
            "markets": markets,
            "_multi": [{
                "_name": "HTML UI",
                "_market": m,
                "_strategy": "fbo",
                "filters": {},
            } for m in markets],
        }

    overrides = storage.get_template_strategy_overrides(chat_id)

    # Объединяем активные шаблоны: сигнал проходит если подходит хотя бы под один.
    # Каждый элемент несёт своё имя (_name) и свой рынок (_market), чтобы screener.py
    # мог сообщить точно какой шаблон совпал и не путать рынки между шаблонами.
    multi = []
    covered = set()
    for n in active:
        if n not in tpls:
            continue
        entry = tpls[n]
        tpl_filters = entry["filters"]
        strat = _effective_strategy(chat_id, n, tpl_filters, overrides)
        mkt = entry.get("market", "crypto")
        covered.add(mkt)
        multi.append({
            "_name": n,
            "_market": mkt,
            "_strategy": strat,
            "filters": tpl_filters,
        })
    # Рынок включён в /filter, но нет активного шаблона под него → HTML UI FBO fallback
    for m in cfg.get("markets") or []:
        if m not in covered:
            multi.append({
                "_name": "HTML UI",
                "_market": m,
                "_strategy": "fbo",
                "filters": {},
            })
    merged = {"_multi": multi, "markets": cfg["markets"]}
    return merged



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
    chat_id = _sync_tokens.get(token)
    if not chat_id:
        return web.json_response({"error": "invalid or expired token"}, status=401)
    market  = request.rel_url.query.get("market", "crypto")
    items   = storage.get_watchlist(chat_id, market)
    return web.json_response({"ok": True, "watchlist": items})


# ── HTTP webhook — принимает шаблоны из HTML ─────────────────────────────────
async def handle_sync(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    chat_id = _sync_tokens.get(token)
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

    await bot.set_my_commands([
        BotCommand(command="scan", description="Запустить скан сейчас"),
        BotCommand(command="filter", description="Шаблоны и рынки"),
        BotCommand(command="status", description="Текущие настройки"),
        BotCommand(command="syncurl", description="Ссылка синхронизации HTML"),
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
