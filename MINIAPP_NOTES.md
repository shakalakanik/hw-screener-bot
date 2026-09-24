# Mini App — что сделано

Краткий отчёт для родительского агента / деплоя. Работа только в `/workspace/hw-screener-bot`. GitHub/Railway **не** трогались.

## Файлы

| Путь | Назначение |
|------|------------|
| `webapp/screener.html` | Drop-in копия исходного HW FBO HTML (~374 KB). Не переписывался. |
| `webapp/mobile.css` | Портрет: колонки `.row/.frow` ≤700px, tabs scroll, sticky bar |
| `webapp/bridge.js` | Telegram.WebApp ready/expand; sync `hw_fbo_tpl` ↔ API; кнопка «Шаблоны → боту» |
| `webapp_server.py` | aiohttp: `/`, `/app`, `/health`, static, JSON API + initData HMAC |
| `storage.py` | +таблица `html_templates`; helpers + `map_html_filter_to_bot` / `set_custom_filter` |
| `bot.py` | HTTP + polling в одном процессе; `/app`; WebApp кнопка; MenuButtonWebApp |
| `Dockerfile` | `EXPOSE 8080` |
| `requirements.txt` | +`aiohttp>=3.9.0` |
| `README.md` | секция Mini App / замена HTML / env (RU) |

## Env для деплоя

- `TG_BOT_TOKEN` — обязателен
- `SCAN_INTERVAL_MIN` — по умолчанию 15
- `WEBAPP_URL` — публичный HTTPS (тот же Railway URL), без trailing slash
- `PORT` — Railway выставляет сам; код читает `os.environ.get("PORT", "8080")`
- `ALLOW_DEBUG_WEBAPP=1` — только локально, для `?debug_user_id=`

## Шаги деплоя (человек / родительский агент)

1. Commit изменений в репозиторий `hw-screener-bot` (этот агент **не** пушит).
2. Railway: публичный домен + Variables (`TG_BOT_TOKEN`, `WEBAPP_URL=https://…`, `SCAN_INTERVAL_MIN`).
3. BotFather: привязать домен Web App / Menu Button к `WEBAPP_URL`.
4. После деплоя: `/start` → кнопка «Открыть скринер»; меню «Скринер».
5. В Mini App: сохранить шаблоны (уходят на `PUT /api/templates`); «Шаблоны → боту» пишет mapped фильтр в `user_filters.custom_json`.

## API (нужен валидный initData)

- `GET /api/me`
- `GET|PUT /api/templates`
- `PUT|DELETE /api/templates/{name}`
- `PUT /api/active-template` `{name}` → active + mapped bot filter
- `GET|PUT /api/signal-filter`
- Заголовок `X-Telegram-Init-Data` (или `?initData=`); HMAC: `secret_key = HMAC_SHA256(key=b"WebAppData", msg=bot_token)`

## Маппинг HTML → bot filter

- `str` → `strength_min` (default 3)
- `dist` → `dist_atr_max` (default 0.5)
- `side` long/short → `sides`
- `bias` ≠ auto → `bias_filter=True`
- `desc = "miniapp:" + name`
- Полный HTML-объект хранится в `html_templates`; mapped — в `user_filters.custom_json`

## Проверки после сборки

```bash
cd /workspace/hw-screener-bot
python -c "import storage; storage.init(); print(storage.get_html_templates(1))"
python -c "from webapp_server import create_app, validate_init_data; print('app', create_app())"
python -c "from pathlib import Path; assert Path('webapp/screener.html').stat().st_size>100000"
test -f webapp/bridge.js && test -f webapp/mobile.css && echo OK
```

## Замена HTML в будущем

Перезаписать только `webapp/screener.html` → commit → redeploy. Инъекция перед `</body>` остаётся на сервере.


## Persist отслеживаемых / бэктеста / сигналов (2026-09-24)

Сервер = source of truth, ключ = Telegram `user_id` из initData (HMAC как у templates).

### Новые таблицы SQLite (не трогают html_templates)
- `miniapp_watch` (user_id PK, data_json, updated_at, cleared)
- `miniapp_backtest` (user_id PK, data_json `{crypto:[],ru:[]}`, updated_at, cleared)
- `miniapp_signals` (user_id PK, data_json LAST rows per market, updated_at, cleared)

### API
- `GET /api/miniapp/state` → `{ok, watch, backtest, signals, updated_at, cleared}`
- `PUT /api/miniapp/state` body partial: `watch`, `backtest`, `signals`, `clear_watch|clear_backtest|clear_signals`, `updated_at`, `force` / `?force=1`

### Empty-overwrite guard
Пустой `watch`/`backtest`/`signals` при непустом сервере **не** затирает данные, если нет `clear_*=true` (кнопки «Очистить…» после confirm) и нет совпадения `updated_at` с сервером (осознанное опустошение после hydrate). Отклонённые ключи в `rejected[]`; HTTP 409 если отклонены все переданные.

### Клиент
`webapp/bridge.js` — GET при открытии, debounce PUT ~700ms на saveWatch / renderBt / renderScan. localStorage `hw_fbo_watch_all` — кэш. Шаблоны (`/api/templates`) не менялись.
