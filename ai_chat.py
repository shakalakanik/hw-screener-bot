"""ai_chat.py — Google Gemini assistant for HW Screener Bot.

Minimal text chat: explains signals/settings, proposes setting changes.
NEVER applies settings itself — returns optional structured proposals for
the bot confirm flow («да» / callback).

Free-form Russian is supported. Obvious intents (включи мосбиржу, …) are
handled locally before calling Gemini so 503 overload cannot block them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

def _api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or "").strip()


def _model() -> str:
    return (os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash").strip() or "gemini-3.6-flash"


_DEFAULT_FALLBACK_MODELS = (
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.7-flash",
)


def _fallback_models() -> list[str]:
    """Primary model first, then GEMINI_FALLBACK_MODELS or defaults (deduped, order kept)."""
    primary = _model()
    raw = (os.environ.get("GEMINI_FALLBACK_MODELS") or "").strip()
    if raw:
        extras = [m.strip() for m in raw.split(",") if m.strip()]
    else:
        extras = list(_DEFAULT_FALLBACK_MODELS)
    seen: set[str] = set()
    out: list[str] = []
    for m in [primary, *extras]:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


# Back-compat aliases (tests / status); prefer _api_key()/_model() at call time
GEMINI_API_KEY = _api_key()
GEMINI_MODEL = _model()

# Soft limits (free tier ~10 RPM / 250 RPD — keep prompts small)
MAX_HISTORY_TURNS = 8          # user+model pairs kept in DB (we store messages)
MAX_HISTORY_MESSAGES = 16      # last N messages sent to the model
MAX_REPLY_CHARS = 3500
ACTION_MARKER = "---ACTION---"

_OVERLOAD_FRIENDLY = (
    "⏳ Google Gemini сейчас перегружен (высокий спрос). "
    "Попробуй через минуту — свободный текст поддерживается, "
    "это временная недоступность API."
)

SYSTEM_PROMPT = """Ты — ИИ-помощник Telegram-бота HW FBO Screener (скринер Герчика).

ВАЖНО ПРО СВОБОДНЫЙ ТЕКСТ:
Пользователь пишет СВОБОДНО на естественном русском — любой формулировкой,
не только командами и не только про шаблоны. Примеры нормальных запросов:
«включи мосбиржу», «выключи крипту», «подпишись на сигналы», «что такое ATR»,
«мало сигналов — что сделать», «чем fbo отличается от brk».
Шаблоны и стратегии — инструменты, которыми ты можешь помочь управлять,
а НЕ единственная тема разговора. Отвечай на любой вопрос про этот бот,
сигналы, рынки, фильтры, подписку, Mini App, Герчика (пробой/ЛП).

О боте:
- Рынки: крипта (Bybit→OKX) и Мосбиржа/MOEX (TQBR).
- Стратегии шаблона: fbo (ложный пробой / ЛП), brk (пробой), both (оба).
- Пользователь синхронизирует HTML-шаблоны фильтров из Mini App (/syncurl, «Обновить шаблоны»).
- В /filter включает рынки и шаблоны, переключает стратегию per-шаблон.
- Авто-скан шлёт только новые сигналы ≤12ч; дедуп по ticker+side+level+strategy.
- Ночной фильтр (noNight): для крипты по умолчанию пропускает часы сигнала 23:00–09:00 МСК (не отключает весь скан).
- Watchlist: «Отслеживать» на карточке сигнала; split crypto/ru.
- Команды: /start /stop /scan /filter /status /syncurl /refresh_tpl /ai /ai_on /ai_off /cancel.

Твои задачи (примеры use-case):
1) Объяснить карточку сигнала (сила, ATR-дистанция, стоп/тейк, D1 bias, fbo vs brk).
2) Подсказать какие шаблоны/рынки включить под стиль торговли.
3) Ответить на вопросы по правилам Герчика / пробой / ложный пробой (кратко, без воды).
4) Помочь сформулировать настройки фильтра (сила, дистанция ATR, long/short, тренд).
5) Предложить сменить стратегию шаблона (fbo/brk/both) — только как предложение.
6) Объяснить sync Mini App и почему шаблоны «пустые».
7) Объяснить статус подписки и интервал скана.
8) Подсказать, что делать если сигналов мало/много.
9) Разобрать, почему ночные сигналы режутся.
10) Помочь с MOEX vs crypto различиями.
11) Выполнить просьбу включить/выключить рынок или подписку — через ---ACTION---.
…и любые похожие вопросы про этот бот свободным текстом.

Правила:
- Отвечай ТОЛЬКО на русском, кратко и по делу (Telegram).
- Не выдумывай наличие шаблонов/рынков — опирайся на блок «Контекст пользователя».
- Не проси API-ключи и не обсуждай чужие секреты.
- Оставайся помощником ЭТОГО скринер-бота. Если вопрос совсем не про бот/торговлю/
  сигналы/настройки (быт, медицина, общие советы и т.п.) — коротко скажи, что
  ты только про HW Screener, и предложи спросить про рынки/шаблоны/сигналы.
- Ты НЕ меняешь настройки сам. Если пользователь просит изменить настройку,
  опиши изменение простым языком и в КОНЦЕ ответа добавь ровно один блок:

---ACTION---
{"action":"<код>","params":{...},"summary":"<одна строка что изменится>"}

Допустимые action:
- set_market: params {"market":"crypto"|"ru","enabled":true|false}
- set_strategy: params {"template":"<имя>","strategy":"fbo"|"brk"|"both"}
- set_templates: params {"market":"crypto"|"ru","names":["..."]}  (полная замена активных для рынка)
- add_templates: params {"market":"crypto"|"ru","names":["..."]}
- remove_templates: params {"market":"crypto"|"ru","names":["..."]}
- subscribe: params {}
- unsubscribe: params {}

Если менять нечего — НЕ добавляй ---ACTION---.
Не применяй несколько ACTION сразу — максимум один.
HTML/Markdown: можно лёгкий Telegram HTML (<b>, <code>), без сложных тегов.
"""


@dataclass
class AiReply:
    text: str
    proposal: dict | None = None
    error: str | None = None


def is_configured() -> bool:
    return bool(_api_key())


def _client():
    from google import genai
    return genai.Client(api_key=_api_key())


def _parse_action_block(raw: str) -> tuple[str, dict | None]:
    """Split model text into user-facing reply + optional ACTION JSON."""
    if not raw:
        return "", None
    text = raw.strip()
    proposal = None
    if ACTION_MARKER in text:
        head, _, tail = text.partition(ACTION_MARKER)
        text = head.strip()
        blob = tail.strip()
        # take first JSON object
        m = re.search(r"\{.*\}", blob, flags=re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict) and obj.get("action"):
                    proposal = {
                        "action": str(obj["action"]).strip(),
                        "params": obj.get("params") if isinstance(obj.get("params"), dict) else {},
                        "summary": str(obj.get("summary") or obj["action"]).strip(),
                    }
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.warning("AI ACTION JSON parse failed: %s", blob[:200])
    # strip accidental fences
    text = re.sub(r"^```(?:html|markdown)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if len(text) > MAX_REPLY_CHARS:
        text = text[: MAX_REPLY_CHARS - 20] + "…"
    return text, proposal


_ALLOWED_ACTIONS = {
    "set_market",
    "set_strategy",
    "set_templates",
    "add_templates",
    "remove_templates",
    "subscribe",
    "unsubscribe",
}


def sanitize_proposal(proposal: dict | None) -> dict | None:
    if not proposal or not isinstance(proposal, dict):
        return None
    action = str(proposal.get("action") or "").strip()
    if action not in _ALLOWED_ACTIONS:
        return None
    params = proposal.get("params") if isinstance(proposal.get("params"), dict) else {}
    clean: dict[str, Any] = {"action": action, "params": {}, "summary": str(proposal.get("summary") or action)}

    if action == "set_market":
        market = params.get("market")
        if market not in ("crypto", "ru"):
            return None
        clean["params"] = {"market": market, "enabled": bool(params.get("enabled"))}
    elif action == "set_strategy":
        strat = params.get("strategy")
        name = str(params.get("template") or "").strip()
        if not name or strat not in ("fbo", "brk", "both"):
            return None
        clean["params"] = {"template": name, "strategy": strat}
    elif action in ("set_templates", "add_templates", "remove_templates"):
        market = params.get("market")
        names = params.get("names")
        if market not in ("crypto", "ru") or not isinstance(names, list):
            return None
        clean_names = [str(n).strip() for n in names if str(n).strip()]
        clean["params"] = {"market": market, "names": clean_names}
    else:
        clean["params"] = {}

    if not clean["summary"]:
        clean["summary"] = action
    return clean


def format_user_context(ctx: dict) -> str:
    """Human-readable context block for the model."""
    lines = [
        f"Подписка: {'да' if ctx.get('subscribed') else 'нет'}",
        f"ИИ-режим: {'вкл' if ctx.get('ai_enabled') else 'выкл'}",
        f"Интервал скана: {ctx.get('scan_interval_min', '?')} мин",
        f"Рынки включены: {', '.join(ctx.get('markets') or []) or 'нет'}",
    ]
    by = ctx.get("names_by_market") or {}
    for m, label in (("crypto", "Крипта"), ("ru", "MOEX")):
        names = by.get(m) or []
        lines.append(f"Активные шаблоны {label}: {', '.join(names) if names else '—'}")
    tpls = ctx.get("templates") or {}
    if tpls:
        overrides = ctx.get("strategy_overrides") or {}
        brief = []
        for name, entry in list(tpls.items())[:30]:
            flt = (entry or {}).get("filters") or {}
            strat = overrides.get(name) or flt.get("_strat") or "fbo"
            brief.append(f"{name}[{strat}]")
        lines.append("Все синхронизированные шаблоны: " + ", ".join(brief))
    else:
        lines.append("Синхронизированных шаблонов: нет")
    return "\n".join(lines)


# ── Local fast-path (no Gemini) for obvious Russian intents ───────────────────

_RE_MOEX = re.compile(
    r"(?i)(?:"
    r"мосбирж\w*|мос[\s\-]?бирж\w*|moex|мoex|"
    r"акци\w*\s+рф|рф\s+акци\w*|российск\w*\s+акци\w*"
    r")"
)
_RE_CRYPTO = re.compile(r"(?i)(?:крипт\w*|crypto|биткоин|bitcoin|bybit|okx)")
_RE_ON = re.compile(r"(?i)(?:включи|включить|добавь|добавить|вруби|включить|enable|on\b)")
_RE_OFF = re.compile(r"(?i)(?:выключи|выключить|отключи|отключить|убери|убрать|disable|off\b)")
_RE_SUB = re.compile(
    r"(?i)(?:подпишись|подписаться|подписка\s+на\s+сигнал|включи\s+сигнал|"
    r"старт\s+сигнал|subscribe)"
)
_RE_UNSUB = re.compile(
    r"(?i)(?:стоп\s+сигнал|останови\s+сигнал|отпишись|отписаться|"
    r"выключи\s+сигнал|unsubscribe|/stop)"
)


def try_local_intent(user_text: str) -> AiReply | None:
    """Parse obvious RU intents without Gemini. Returns AiReply+proposal or None.

    Never auto-applies — only proposes. Ambiguous text → None (fall through).
    """
    text = (user_text or "").strip()
    if not text or len(text) > 120:
        return None

    # Subscribe / unsubscribe (check before markets — "стоп сигналы" ≠ market)
    if _RE_UNSUB.search(text) and not _RE_MOEX.search(text) and not _RE_CRYPTO.search(text):
        # avoid "стоп" alone if it's about something else with market words already excluded
        if re.search(r"(?i)сигнал|подпис|unsubscribe|stop", text) or re.search(
            r"(?i)^(?:стоп|отпишись|отписаться)\b", text
        ):
            return AiReply(
                text="Остановить авто-сигналы? Подтверди ниже.",
                proposal={
                    "action": "unsubscribe",
                    "params": {},
                    "summary": "остановить сигналы",
                },
            )
    if _RE_SUB.search(text) and not _RE_MOEX.search(text) and not _RE_CRYPTO.search(text):
        return AiReply(
            text="Включить подписку на сигналы? Подтверди ниже.",
            proposal={
                "action": "subscribe",
                "params": {},
                "summary": "подписка на сигналы",
            },
        )

    has_moex = bool(_RE_MOEX.search(text))
    has_crypto = bool(_RE_CRYPTO.search(text))
    wants_on = bool(_RE_ON.search(text))
    wants_off = bool(_RE_OFF.search(text))

    if has_moex and has_crypto:
        return None  # ambiguous — let Gemini decide
    if wants_on and wants_off:
        return None
    if not (has_moex or has_crypto):
        return None
    if not (wants_on or wants_off):
        return None

    market = "ru" if has_moex else "crypto"
    enabled = wants_on
    label = "мосбиржу" if market == "ru" else "крипту"
    verb = "Включить" if enabled else "Выключить"
    return AiReply(
        text=f"{verb} рынок <b>{label}</b>? Подтверди ниже — без «да» ничего не меняю.",
        proposal={
            "action": "set_market",
            "params": {"market": market, "enabled": enabled},
            "summary": f"{'включить' if enabled else 'выключить'} {label}",
        },
    )


def _is_overload_error(err: str) -> bool:
    u = (err or "").upper()
    return (
        "503" in err
        or "429" in err
        or "UNAVAILABLE" in u
        or "RESOURCE_EXHAUSTED" in u
        or "HIGH DEMAND" in u
        or "HIGH_DEMAND" in u
        or "OVERLOADED" in u
        or "TRY AGAIN LATER" in u
    )


def _friendly_error(exc: BaseException) -> AiReply:
    err = str(exc)
    if "API_KEY" in err.upper() or "401" in err or "403" in err:
        return AiReply(
            text="⚠️ Ошибка ключа Gemini. Проверь <code>GEMINI_API_KEY</code> в Railway.",
            error="auth",
        )
    if _is_overload_error(err):
        return AiReply(text=_OVERLOAD_FRIENDLY, error="overload")
    # Never dump raw JSON / long traceback to the user
    return AiReply(
        text="⚠️ Временная ошибка ИИ. Попробуй ещё раз через минуту.",
        error="api",
    )


async def chat(
    user_text: str,
    history: list[dict],
    context: dict,
) -> AiReply:
    """Call Gemini. history: list of {role: user|model, content: str} oldest→newest."""
    # Local fast-path — works even without API key / during 503
    local = try_local_intent(user_text)
    if local is not None:
        local.proposal = sanitize_proposal(local.proposal)
        return local

    if not _api_key():
        return AiReply(
            text=(
                "🔑 Ключ Gemini не задан.\n\n"
                "Добавь переменную <code>GEMINI_API_KEY</code> в Railway → Variables "
                "и сделай Redeploy. Ключ: aistudio.google.com → API keys.\n\n"
                "Простые команды вроде «включи мосбиржу» / «подпишись» "
                "работают и без ключа (локальный разбор)."
            ),
            error="missing_key",
        )

    from google.genai import types

    contents: list[types.Content] = []
    for msg in history[-MAX_HISTORY_MESSAGES:]:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if not content or role not in ("user", "model"):
            continue
        contents.append(types.Content(role=role, parts=[types.Part(text=content)]))

    prompt = (
        "Контекст пользователя (актуальный):\n"
        f"{format_user_context(context)}\n\n"
        f"Сообщение пользователя:\n{user_text.strip()}"
    )
    contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.4,
        max_output_tokens=1024,
    )

    models = _fallback_models()
    last_exc: BaseException | None = None

    try:
        client = _client()
    except Exception as e:
        logger.exception("Gemini client init failed")
        return _friendly_error(e)

    for idx, model_name in enumerate(models):
        if idx > 0:
            # brief pause before switching models after overload
            await asyncio.sleep(1.6)

        for attempt in range(2):  # initial + one retry on same model
            if attempt == 1:
                await asyncio.sleep(0.8)
            try:
                resp = await client.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                )
                raw = (resp.text or "").strip() if resp is not None else ""
                if not raw:
                    logger.warning("Gemini empty response model=%s", model_name)
                    break  # next model
                text, proposal = _parse_action_block(raw)
                proposal = sanitize_proposal(proposal)
                if not text:
                    text = "Готово." if proposal else "Не понял запрос — уточни, пожалуйста."
                return AiReply(text=text, proposal=proposal)
            except Exception as e:
                last_exc = e
                err = str(e)
                logger.warning(
                    "Gemini fail model=%s attempt=%s: %s",
                    model_name,
                    attempt,
                    err[:300],
                )
                if "API_KEY" in err.upper() or "401" in err or "403" in err:
                    return _friendly_error(e)
                if _is_overload_error(err):
                    if attempt == 0:
                        continue  # retry same model after 0.8s
                    break  # next model after 1.6s
                # non-overload API error — try next model
                break

    if last_exc is not None:
        logger.exception("Gemini chat failed after fallbacks")
        return _friendly_error(last_exc)
    return AiReply(text=_OVERLOAD_FRIENDLY, error="overload")
