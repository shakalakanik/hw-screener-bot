# HW Screener Bot

Telegram-бот для сигналов пробоя по методике Герчика. Использует алгоритм `hwv1.py` напрямую.

## Структура

```
bot.py        — Telegram-бот (aiogram 3)
screener.py   — Bybit API + цикл скана
hwv1.py       — алгоритм (копия из проекта)
storage.py    — SQLite (дедупликация, фильтры)
filters.py    — шаблоны фильтров (встроены в storage.py)
```

## Локальный запуск

```bash
pip install -r requirements.txt
export TG_BOT_TOKEN=your_token_here
export SCAN_INTERVAL_MIN=15   # интервал скана в минутах
python bot.py
```

## Деплой на Railway (бесплатно)

1. Зарегистрируйся на [railway.app](https://railway.app)
2. Создай новый проект → **Deploy from GitHub repo**
3. Залей папку `hw_bot/` в отдельный GitHub-репозиторий
4. В Railway → Variables добавь:
   - `TG_BOT_TOKEN` = токен от @BotFather
   - `SCAN_INTERVAL_MIN` = `15`
5. Railway сам найдёт Dockerfile и задеплоит

## Обновление hwv1.py

Просто замени `hwv1.py` в репозитории — Railway автоматически пересоберёт контейнер.
Никакой другой код трогать не нужно: screener.py импортирует `evaluate` напрямую из `hwv1.py`.

## Команды бота

| Команда | Действие |
|---------|----------|
| `/start` | Подписаться на сигналы |
| `/stop` | Остановить сигналы |
| `/status` | Текущий шаблон и настройки |
| `/filter` | Выбрать шаблон фильтра |
| `/scan` | Запустить скан прямо сейчас |

## Шаблоны фильтров

| Шаблон | Описание |
|--------|----------|
| `default` | Сила ≥3, дистанция ≤0.5 ATR |
| `strong` | Сила ≥4, дистанция ≤0.35 ATR |
| `long_only` | Только лонги |
| `short_only` | Только шорты |
| `trend` | По тренду D1 |
| `scalp` | Дистанция ≤0.2 ATR, сила ≥2 |

## Как получить токен бота

1. Напиши [@BotFather](https://t.me/BotFather) в Telegram
2. `/newbot` → введи имя и username
3. Скопируй токен вида `1234567890:AAF...`
