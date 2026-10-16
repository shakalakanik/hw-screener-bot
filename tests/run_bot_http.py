"""Запуск ИМЕННО продового HTTP-приложения bot.build_http_app() для e2e (без Telegram polling)."""
import os, sys, asyncio
os.environ.setdefault("TG_BOT_TOKEN", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
os.environ.setdefault("DB_PATH", "/tmp/hw_e2e_bot.db")
os.environ.setdefault("ALLOW_DEBUG_WEBAPP", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aiohttp import web
import bot
port = int(os.environ.get("PORT_E2E", "8811"))
def _legacy():   # для воспроизведения на старом коде (до build_http_app)
    app = web.Application(); app.router.add_get("/app", bot.handle_index); bot._mount_miniapp_routes(app); return app
web.run_app(bot.build_http_app() if hasattr(bot, "build_http_app") else _legacy(), host="127.0.0.1", port=port)
