# HW Screener Bot

Telegram-бот для сигналов пробоя по методике Герчика. Использует алгоритм `hwv1.py` напрямую.
Включает **Telegram Mini App** — HTML-скринер (Bybit / OKX / MOEX) внутри Telegram.

## Структура

```
bot.py            — Telegram-бот (aiogram 3) + запуск HTTP
webapp_server.py  — aiohttp: отдача Mini App + API шаблонов
storage.py        — SQLite (дедупликация, фильтры, html_templates, AI history)
ai_chat.py        — Google Gemini (ИИ-помощник, confirm перед сменой настроек)
screener.py       — Bybit API + цикл скана
hwv1.py           — алгоритм
webapp/
  screener.html   — drop-in HTML скринер (можно заменить целиком)
  mobile.css      — адаптация под портрет
  bridge.js       — синхронизация шаблонов с бэкендом
```

## Переменные окружения

| Переменная | Описание |
|------------|----------|
| `TG_BOT_TOKEN` | токен от @BotFather (**обязательно**) |
| `SCAN_INTERVAL_MIN` | интервал авто-скана в минутах (по умолчанию `15`) |
| `WEBAPP_URL` | публичный HTTPS URL Mini App, напр. `https://xxx.up.railway.app` |
| `PORT` | порт HTTP (Railway задаёт сам; по умолчанию `8080`) |
| `ALLOW_DEBUG_WEBAPP` | `1` — разрешить `?debug_user_id=` без initData (только для отладки) |
| `GEMINI_API_KEY` | ключ Google Gemini для ИИ-чата (`/ai`). Без ключа бот не падает — вежливо пишет, что ключ не задан |
| `GEMINI_MODEL` | опционально, по умолчанию `gemini-3.6-flash` |
| `GEMINI_FALLBACK_MODELS` | опционально, comma-list запасных моделей при 503/429 |

## Локальный запуск

```bash
pip install -r requirements.txt
export TG_BOT_TOKEN=your_token_here
export SCAN_INTERVAL_MIN=15
export WEBAPP_URL=https://your-public-https-url   # опционально
export ALLOW_DEBUG_WEBAPP=1                       # опционально для браузера
python bot.py
# Открыть http://localhost:8080/app?debug_user_id=123
```

## Деплой на Railway

1. Зарегистрируйся на [railway.app](https://railway.app)
2. Создай проект → **Deploy from GitHub repo**
3. В Variables добавь:
   - `TG_BOT_TOKEN`
   - `SCAN_INTERVAL_MIN=15`
   - `WEBAPP_URL=https://<твой-сервис>.up.railway.app` (после появления публичного домена)
   - `GEMINI_API_KEY` (опционально, для `/ai`)
4. Включи **публичный HTTP** у сервиса (Generate Domain)
5. У @BotFather: `/setdomain` (или Menu Button / Web App URL) — укажи домен Railway
6. Redeploy после установки `WEBAPP_URL`

## Как заменить HTML скринер

1. Перезапиши файл `webapp/screener.html` новой версией (без правок `bridge.js` / сервера)
2. Commit + push → Railway пересоберёт контейнер
3. Сервер сам подставит перед `</body>`: `telegram-web-app.js`, `/mobile.css`, `/bridge.js`

Ничего в теле HTML трогать не нужно — Mini App работает через инъекцию и `bridge.js`.

## Команды бота

| Команда | Действие |
|---------|----------|
| `/start` | Подписаться на сигналы (+ кнопка Mini App) |
| `/app` | Открыть Mini App скринер |
| `/stop` | Остановить сигналы |
| `/status` | Текущий шаблон и настройки |
| `/filter` | Выбрать встроенный шаблон фильтра |
| `/scan` | Запустить скан прямо сейчас |
| `/ai` | ИИ-помощник (Gemini): вопросы по сигналам и настройкам |
| `/ai_on` / `/ai_off` | Включить / выключить ИИ-режим (свободный текст) |

## Шаблоны фильтров (бот)

| Шаблон | Описание |
|--------|----------|
| `default` | Сила ≥3, дистанция ≤0.5 ATR |
| `strong` | Сила ≥4, дистанция ≤0.35 ATR |
| `long_only` | Только лонги |
| `short_only` | Только шорты |
| `trend` | По тренду D1 |
| `scalp` | Дистанция ≤0.2 ATR, сила ≥2 |

Пользовательские HTML-шаблоны из Mini App хранятся отдельно (`html_templates`) и могут быть назначены как фильтр авто-сигналов кнопкой «Шаблоны → боту».

## Как получить токен бота

1. Напиши [@BotFather](https://t.me/BotFather) в Telegram
2. `/newbot` → введи имя и username
3. Скопируй токен вида `1234567890:AAF...`

## MOEX (акции РФ)

В боте: `/filter` → включи **🇷🇺 MOEX**. Скринер тянет TQBR через ISS (`moex.py`).
Крипта остаётся Bybit→OKX, регион Railway `europe-west4` не менять.

Опционально (если ISS начнёт резать non-RU IP):
- `MOEX_PROXY` или `HTTPS_PROXY` — URL HTTP(S) прокси для httpx
- `MOEX_MIN_TURN` — мин. оборот ₽/сут (по умолчанию `10000000` = 10 млн, как HTML)

