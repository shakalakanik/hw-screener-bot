"""bot.py — Telegram-бот HW Screener."""
import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

import storage
from screener import run_scan, TEMPLATES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TG_BOT_TOKEN"]
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_MIN", "15")) * 60  # секунды

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ── Подписчики ────────────────────────────────────────────────────────────────
# Простое хранилище в памяти (дополнительно можно вынести в SQLite)
_subscribers: set[int] = set()


def _format_card(card: dict) -> str:
    side_emoji = "🟢" if card["side"] == "LONG" else "🔴"
    bias_map = {"up": "↑ up", "down": "↓ down", "flat": "→ flat"}
    bias = bias_map.get(card.get("d1_bias", "flat"), "flat")

    entry = card["last"]
    stop = card["stop"]
    take = card["take"]
    risk_pct = abs(entry - stop) / entry * 100
    tp_pct = abs(take - entry) / entry * 100

    lines = [
        f"{side_emoji} <b>{card['ticker']}</b> — {card['side']}",
        f"🎯 Уровень: <code>{card['level']:.4f}</code>  [{card['kind']}]",
        f"📍 Цена: <code>{entry:.4f}</code>  (расст. {card['dist_atr']:.2f} ATR)",
        f"⛔ Стоп: <code>{stop:.4f}</code>  (−{risk_pct:.1f}%)",
        f"💰 Тейк: <code>{take:.4f}</code>  (+{tp_pct:.1f}%)",
        f"💪 Сила уровня: {'★' * card['strength']}{'☆' * (5 - card['strength'])}  ({card['strength']}/5)",
        f"📊 Тренд D1: {bias}",
        f"📝 {' | '.join(card.get('why', []))}",
        f"🕐 {datetime.utcnow().strftime('%H:%M UTC')}",
    ]
    return "\n".join(lines)


async def send_signal(card: dict, chat_ids: list[int]):
    text = _format_card(card)
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as e:
            logger.warning("Не удалось отправить %s: %s", chat_id, e)


# ── Команды бота ──────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    _subscribers.add(msg.chat.id)
    storage.get_filter(msg.chat.id)  # инициализировать запись
    await msg.answer(
        "👋 <b>HW Screener Bot</b> запущен!\n\n"
        "Я буду присылать сигналы пробоев по методике Герчика каждые "
        f"{SCAN_INTERVAL // 60} минут.\n\n"
        "Команды:\n"
        "/status — текущие настройки\n"
        "/filter — выбрать шаблон фильтра\n"
        "/scan — запустить скан прямо сейчас\n"
        "/stop — остановить сигналы",
        parse_mode="HTML",
    )


@dp.message(Command("stop"))
async def cmd_stop(msg: Message):
    _subscribers.discard(msg.chat.id)
    await msg.answer("⛔ Сигналы остановлены. /start чтобы возобновить.")


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    chat_id = msg.chat.id
    subscribed = chat_id in _subscribers
    tpl_name = storage.get_template_name(chat_id)
    f = storage.get_filter(chat_id)
    await msg.answer(
        f"📋 <b>Статус</b>\n\n"
        f"Подписка: {'✅ активна' if subscribed else '❌ остановлена'}\n"
        f"Шаблон: <b>{tpl_name}</b>\n"
        f"Описание: {f.get('desc', '—')}\n\n"
        f"Сила уровня ≥ {f['strength_min']}\n"
        f"Дистанция ≤ {f['dist_atr_max']} ATR\n"
        f"Направления: {', '.join(f['sides'])}\n"
        f"Только по тренду: {'да' if f.get('bias_filter') else 'нет'}\n\n"
        f"Интервал скана: каждые {SCAN_INTERVAL // 60} мин",
        parse_mode="HTML",
    )


@dp.message(Command("filter"))
async def cmd_filter(msg: Message):
    buttons = []
    for name, tpl in TEMPLATES.items():
        buttons.append([InlineKeyboardButton(
            text=f"{'✅ ' if storage.get_template_name(msg.chat.id) == name else ''}{name} — {tpl['desc']}",
            callback_data=f"tpl:{name}",
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await msg.answer("Выбери шаблон фильтра:", reply_markup=kb)


@dp.callback_query(F.data.startswith("tpl:"))
async def cb_template(call: CallbackQuery):
    name = call.data.split(":", 1)[1]
    if name not in TEMPLATES:
        await call.answer("Неизвестный шаблон")
        return
    storage.set_template(call.message.chat.id, name)
    _subscribers.add(call.message.chat.id)
    f = TEMPLATES[name]
    await call.message.edit_text(
        f"✅ Шаблон <b>{name}</b> установлен.\n{f['desc']}",
        parse_mode="HTML",
    )
    await call.answer()


@dp.message(Command("scan"))
async def cmd_scan(msg: Message):
    await msg.answer("🔍 Запускаю скан... Это займёт 1–3 минуты.")
    try:
        await run_scan(
            on_signal=send_signal,
            subscribers=[msg.chat.id],
            chat_filters=storage.get_filter,
        )
        await msg.answer("✅ Скан завершён.")
    except Exception as e:
        logger.exception("Ошибка скана")
        await msg.answer(f"❌ Ошибка скана: {e}")


# ── Фоновый цикл ─────────────────────────────────────────────────────────────

async def scan_loop():
    await asyncio.sleep(10)  # дать боту запуститься
    while True:
        if _subscribers:
            logger.info("Авто-скан для %d подписчиков", len(_subscribers))
            try:
                await run_scan(
                    on_signal=send_signal,
                    subscribers=list(_subscribers),
                    chat_filters=storage.get_filter,
                )
            except Exception:
                logger.exception("Ошибка авто-скана")
        await asyncio.sleep(SCAN_INTERVAL)


async def main():
    storage.init()
    asyncio.create_task(scan_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
