"""webapp_server.py — aiohttp HTTP for Telegram Mini App + templates API."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from aiohttp import web

import storage

logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"
SCREENER_HTML = WEBAPP_DIR / "screener.html"

INJECT_SNIPPET = """
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<link rel="stylesheet" href="/mobile.css">
<script src="/bridge.js"></script>
"""


def _bot_token() -> str:
    return os.environ.get("TG_BOT_TOKEN", "")


def validate_init_data(init_data: str, bot_token: str | None = None) -> dict[str, Any] | None:
    """Validate Telegram WebApp initData (HMAC-SHA256 per Telegram docs).

    secret_key = HMAC_SHA256(key=b"WebAppData", msg=bot_token)
    then check hash of data-check-string.
    """
    if not init_data or not isinstance(init_data, str):
        return None
    token = bot_token if bot_token is not None else _bot_token()
    if not token:
        return None

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    calc = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        return None

    user = None
    if "user" in pairs:
        try:
            user = json.loads(pairs["user"])
        except json.JSONDecodeError:
            return None
    return {"fields": pairs, "user": user}


def _cors_headers(request: web.Request) -> dict[str, str]:
    origin = request.headers.get("Origin", "*")
    # Mini App is same-origin; allow Telegram origins + * for debug
    allow = origin if origin else "*"
    return {
        "Access-Control-Allow-Origin": allow,
        "Access-Control-Allow-Headers": "Content-Type, X-Telegram-Init-Data",
        "Access-Control-Allow-Methods": "GET, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Credentials": "true",
    }


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=_cors_headers(request))
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        # Attach CORS even on HTTP errors
        for k, v in _cors_headers(request).items():
            e.headers[k] = v
        raise
    for k, v in _cors_headers(request).items():
        resp.headers[k] = v
    return resp


def _extract_init_data(request: web.Request) -> str:
    h = request.headers.get("X-Telegram-Init-Data") or ""
    if h:
        return h
    return request.rel_url.query.get("initData") or ""


def require_user(request: web.Request) -> int:
    """Resolve Telegram user_id from initData (or debug). Raises HTTPUnauthorized/Forbidden."""
    init_data = _extract_init_data(request)
    if init_data:
        parsed = validate_init_data(init_data)
        if not parsed or not parsed.get("user"):
            raise web.HTTPUnauthorized(text=json.dumps({"error": "invalid initData"}),
                                       content_type="application/json")
        uid = parsed["user"].get("id")
        if not uid:
            raise web.HTTPUnauthorized(text=json.dumps({"error": "no user in initData"}),
                                       content_type="application/json")
        request["tg_user"] = parsed["user"]
        return int(uid)

    # Local debug
    if os.environ.get("ALLOW_DEBUG_WEBAPP") == "1":
        dbg = request.rel_url.query.get("debug_user_id")
        if dbg:
            try:
                uid = int(dbg)
                request["tg_user"] = {"id": uid, "username": f"debug_{uid}"}
                return uid
            except ValueError:
                pass

    raise web.HTTPUnauthorized(
        text=json.dumps({"error": "missing initData"}),
        content_type="application/json",
    )


def _inject_html(raw: str) -> str:
    marker = "</body>"
    idx = raw.lower().rfind(marker)
    if idx < 0:
        return raw + INJECT_SNIPPET
    return raw[:idx] + INJECT_SNIPPET + raw[idx:]


async def handle_app(request: web.Request) -> web.Response:
    if not SCREENER_HTML.exists():
        raise web.HTTPNotFound(text="screener.html not found")
    html = SCREENER_HTML.read_text(encoding="utf-8")
    html = _inject_html(html)
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_mobile_css(request: web.Request) -> web.Response:
    path = WEBAPP_DIR / "mobile.css"
    return web.FileResponse(path)


async def handle_bridge_js(request: web.Request) -> web.Response:
    path = WEBAPP_DIR / "bridge.js"
    return web.FileResponse(path)


async def api_me(request: web.Request) -> web.Response:
    uid = require_user(request)
    user = request.get("tg_user") or {}
    return web.json_response({
        "user_id": uid,
        "username": user.get("username"),
    })


def _templates_for_client(tpls: dict | None) -> dict:
    """Flatten {name: {filters, market}} → {name: {..filters, _market}} for Mini App."""
    out: dict = {}
    for name, entry in (tpls or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("filters"), dict):
            flat = dict(entry["filters"])
            m = entry.get("market")
            if m and flat.get("_market") not in ("crypto", "ru"):
                flat["_market"] = m
            out[name] = flat
        elif isinstance(entry, dict):
            out[name] = entry
        else:
            out[name] = entry
    return out


async def api_templates_get(request: web.Request) -> web.Response:
    uid = require_user(request)
    templates = _templates_for_client(storage.get_html_templates(uid))
    active = storage.get_active_config(uid)
    return web.json_response({"templates": templates, "active": active})


async def api_templates_put(request: web.Request) -> web.Response:
    uid = require_user(request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid json"}),
                                 content_type="application/json")
    templates = body.get("templates", body)
    if not isinstance(templates, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "templates must be object"}),
                                 content_type="application/json")
    # Refuse empty wipe — keep existing templates for this Telegram user
    if not templates:
        existing_tpl = _templates_for_client(storage.get_html_templates(uid))
        active = storage.get_active_config(uid)
        return web.json_response({
            "ok": True,
            "skipped": "empty_templates_not_applied",
            "templates": existing_tpl,
            "active": active,
        })
    # Merge-only upsert (never delete missing names)
    storage.save_html_templates(uid, templates, remove_missing=False)
    templates = _templates_for_client(storage.get_html_templates(uid))
    active = storage.get_active_config(uid)
    return web.json_response({"ok": True, "templates": templates, "active": active})


async def api_template_one_put(request: web.Request) -> web.Response:
    uid = require_user(request)
    name = request.match_info["name"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid json"}),
                                 content_type="application/json")
    filt = body.get("filter", body)
    if not isinstance(filt, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "filter must be object"}),
                                 content_type="application/json")
    storage.set_one_html_template(uid, name, filt)
    return web.json_response({"ok": True, "name": name})


async def api_template_one_delete(request: web.Request) -> web.Response:
    uid = require_user(request)
    name = request.match_info["name"]
    ok = storage.delete_html_template(uid, name)
    if not ok:
        raise web.HTTPNotFound(text=json.dumps({"error": "not found"}),
                               content_type="application/json")
    return web.json_response({"ok": True})


async def api_active_template_put(request: web.Request) -> web.Response:
    uid = require_user(request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid json"}),
                                 content_type="application/json")
    name = body.get("name")
    if not name:
        raise web.HTTPBadRequest(text=json.dumps({"error": "name required"}),
                                 content_type="application/json")
    try:
        bot_filt = storage.apply_active_template_to_bot_filter(uid, name)
    except KeyError:
        raise web.HTTPNotFound(text=json.dumps({"error": "template not found"}),
                               content_type="application/json")
    return web.json_response({"ok": True, "active": name, "bot_filter": bot_filt})


async def api_signal_filter_get(request: web.Request) -> web.Response:
    uid = require_user(request)
    f = storage.get_filter(uid)
    active = storage.get_active_config(uid)
    return web.json_response({"filter": f, "active_template": active.get("names") or []})


async def api_signal_filter_put(request: web.Request) -> web.Response:
    """Accept HTML filter dict and/or template name / mapped bot filter."""
    uid = require_user(request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid json"}),
                                 content_type="application/json")

    bot_filt: dict | None = None
    name = body.get("name")

    # Case: only a template name string as body? unlikely; handle name key
    if name and not body.get("html") and not body.get("filter") and set(body.keys()) <= {"name"}:
        try:
            bot_filt = storage.apply_active_template_to_bot_filter(uid, name)
        except KeyError:
            raise web.HTTPNotFound(text=json.dumps({"error": "template not found"}),
                                   content_type="application/json")
        return web.json_response({"ok": True, "filter": bot_filt, "active_template": name})

    html = body.get("html")
    filt = body.get("filter")

    # Body itself looks like HTML keys (thr/str/dist/…)
    html_keys = {"thr", "stop", "str", "cross", "touch", "acc", "dist", "over", "vc",
                 "vol", "bias", "side", "cap", "age", "kind", "maxrisk", "hours", "daycap"}
    if html is None and filt is None and html_keys & set(body.keys()):
        html = {k: v for k, v in body.items() if k != "name"}

    # Body itself is already a bot filter
    bot_keys = {"strength_min", "dist_atr_max", "sides", "bias_filter"}
    if html is None and filt is None and bot_keys & set(body.keys()):
        filt = body

    if name and html is None and filt is None:
        # name + nothing else — apply template
        try:
            bot_filt = storage.apply_active_template_to_bot_filter(uid, name)
        except KeyError:
            raise web.HTTPNotFound(text=json.dumps({"error": "template not found"}),
                                   content_type="application/json")
        return web.json_response({"ok": True, "filter": bot_filt, "active_template": name})

    if isinstance(html, dict):
        # Persist under name if provided
        if name:
            storage.set_one_html_template(uid, name, html)
            try:
                storage.set_active_html_template(uid, name)
            except KeyError:
                pass
        bot_filt = storage.map_html_filter_to_bot(html, name or "custom")
        # Prefer explicit mapped filter if client sent one with bot shape
        if isinstance(filt, dict) and ("strength_min" in filt or "sides" in filt):
            # Merge: keep client bot fields, ensure desc
            bot_filt = {**bot_filt, **{k: v for k, v in filt.items() if k != "html"}}
            bot_filt.setdefault("desc", f"miniapp:{name or 'custom'}")
            bot_filt["html"] = html
        storage.set_custom_filter(uid, bot_filt)
        return web.json_response({"ok": True, "filter": bot_filt, "active_template": name})

    if isinstance(filt, dict):
        # Raw custom bot filter (or HTML-looking inside)
        if html_keys & set(filt.keys()) and "strength_min" not in filt:
            bot_filt = storage.map_html_filter_to_bot(filt, name or "custom")
            if name:
                storage.set_one_html_template(uid, name, filt)
                try:
                    storage.set_active_html_template(uid, name)
                except KeyError:
                    pass
        else:
            bot_filt = dict(filt)
            bot_filt.setdefault("desc", f"miniapp:{name or 'custom'}")
            bot_filt.setdefault("strength_min", 3)
            bot_filt.setdefault("dist_atr_max", 0.5)
            bot_filt.setdefault("sides", ["LONG", "SHORT"])
            bot_filt.setdefault("bias_filter", False)
        storage.set_custom_filter(uid, bot_filt)
        if name:
            try:
                storage.set_active_html_template(uid, name)
            except KeyError:
                pass
        return web.json_response({"ok": True, "filter": bot_filt, "active_template": name})

    raise web.HTTPBadRequest(
        text=json.dumps({"error": "provide name, html, or filter"}),
        content_type="application/json",
    )



async def api_miniapp_state_get(request: web.Request) -> web.Response:
    """Hydrate Mini App watch / backtest / signals for Telegram user."""
    uid = require_user(request)
    state = storage.get_miniapp_state(uid)
    return web.json_response({"ok": True, **state})


async def api_miniapp_state_put(request: web.Request) -> web.Response:
    """Persist Mini App state. Empty overwrite guarded in storage.put_miniapp_state.

    Body (all keys optional):
      watch: list
      backtest: {crypto: [...], ru: [...]}
      signals: {crypto: [...], ru: [...]}  # LAST scan rows per market
      clear_watch / clear_backtest / clear_signals: bool  # explicit UI reset
      updated_at: {watch, backtest, signals}  # last known server stamps (for in-sync empty)
      force: bool  # alias: sets all clear_* true
    """
    uid = require_user(request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid json"}),
                                 content_type="application/json")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "body must be object"}),
                                 content_type="application/json")

    force = bool(body.get("force") or body.get("clear"))
    # ?force=1 query also counts
    qforce = request.rel_url.query.get("force", "")
    if str(qforce) in ("1", "true", "yes"):
        force = True

    clear_watch = bool(body.get("clear_watch")) or force
    clear_backtest = bool(body.get("clear_backtest")) or force
    clear_signals = bool(body.get("clear_signals")) or force

    kwargs = {
        "clear_watch": clear_watch,
        "clear_backtest": clear_backtest,
        "clear_signals": clear_signals,
        "client_updated_at": body.get("updated_at") if isinstance(body.get("updated_at"), dict) else {},
    }
    if "watch" in body:
        kwargs["watch"] = body.get("watch")
    if "backtest" in body:
        kwargs["backtest"] = body.get("backtest")
    if "signals" in body:
        kwargs["signals"] = body.get("signals")

    if not any(k in kwargs for k in ("watch", "backtest", "signals")):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "provide watch, backtest, and/or signals"}),
            content_type="application/json",
        )

    result = storage.put_miniapp_state(uid, **kwargs)
    status = 200
    if result.get("rejected"):
        # Partial apply: 409 if ALL provided keys were rejected, else 200 with rejected list
        provided = [k for k in ("watch", "backtest", "signals") if k in kwargs]
        if provided and set(result["rejected"]) >= set(provided):
            status = 409
    return web.json_response(result, status=status)



def create_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_app)
    app.router.add_get("/app", handle_app)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/mobile.css", handle_mobile_css)
    app.router.add_get("/bridge.js", handle_bridge_js)

    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/templates", api_templates_get)
    app.router.add_put("/api/templates", api_templates_put)
    app.router.add_put("/api/templates/{name}", api_template_one_put)
    app.router.add_delete("/api/templates/{name}", api_template_one_delete)
    app.router.add_put("/api/active-template", api_active_template_put)
    app.router.add_get("/api/signal-filter", api_signal_filter_get)
    app.router.add_put("/api/signal-filter", api_signal_filter_put)
    app.router.add_get("/api/miniapp/state", api_miniapp_state_get)
    app.router.add_put("/api/miniapp/state", api_miniapp_state_put)

    # CORS preflight
    async def _options(request: web.Request) -> web.Response:
        return web.Response(status=204, headers=_cors_headers(request))

    app.router.add_route("OPTIONS", "/api/{tail:.*}", _options)
    return app


async def start_web_server(host: str = "0.0.0.0", port: int | None = None) -> web.AppRunner:
    if port is None:
        port = int(os.environ.get("PORT", "8080"))
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("WebApp HTTP on http://%s:%s", host, port)
    return runner
