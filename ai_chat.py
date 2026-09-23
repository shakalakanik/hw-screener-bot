"""ai_chat.py — Google Gemini assistant for HW Screener Bot.

Minimal text chat: explains signals/settings, proposes setting changes.
NEVER applies settings itself — returns optional structured proposals for
the bot confirm flow («да» / callback).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

# Soft limits (free tier ~10 RPM / 250 RPD — keep prompts small)
MAX_HISTORY_TURNS = 8          # user+model pairs kept in DB (we store messages)
MAX_HISTORY_MESSAGES = 16      # last N messages sent to the model
MAX_REPLY_CHARS = 3500
ACTION_MARKER = "---ACTION---"

SYSTEM_PROMPT = """Ты — ИИ-помощник Telegram-бота HW FBO Screener (скринер Герчика).

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
…и похожие вопросы про этот бот.

Правила:
- Отвечай ТОЛЬКО на русском, кратко и по делу (Telegram).
- Не выдумывай наличие шаблонов/рынков — опирайся на блок «Контекст пользователя».
- Не проси API-ключи и не обсуждай чужие секреты.
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
    return bool(GEMINI_API_KEY)


def _client():
    from google import genai
    return genai.Client(api_key=GEMINI_API_KEY)


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


async def chat(
    user_text: str,
    history: list[dict],
    context: dict,
) -> AiReply:
    """Call Gemini. history: list of {role: user|model, content: str} oldest→newest."""
    if not GEMINI_API_KEY:
        return AiReply(
            text=(
                "🔑 Ключ Gemini не задан.\n\n"
                "Добавь переменную <code>GEMINI_API_KEY</code> в Railway → Variables "
                "и сделай Redeploy. Ключ: aistudio.google.com → API keys."
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
        # Gemini roles: user / model
        contents.append(types.Content(role=role, parts=[types.Part(text=content)]))

    # Current user turn with fresh context
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

    try:
        client = _client()
        resp = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=config,
        )
        raw = (resp.text or "").strip() if resp is not None else ""
        if not raw:
            return AiReply(text="Пустой ответ модели. Попробуй переформулировать вопрос.", error="empty")
        text, proposal = _parse_action_block(raw)
        proposal = sanitize_proposal(proposal)
        if not text:
            text = "Готово." if proposal else "Не понял запрос — уточни, пожалуйста."
        return AiReply(text=text, proposal=proposal)
    except Exception as e:
        logger.exception("Gemini chat failed")
        err = str(e)
        if "API_KEY" in err.upper() or "401" in err or "403" in err:
            return AiReply(
                text="⚠️ Ошибка ключа Gemini. Проверь <code>GEMINI_API_KEY</code> в Railway.",
                error="auth",
            )
        if "429" in err or "RESOURCE_EXHAUSTED" in err.upper():
            return AiReply(
                text="⏳ Лимит Gemini (RPM/RPD). Подожди минуту и попробуй снова.",
                error="rate",
            )
        return AiReply(
            text=f"⚠️ Ошибка ИИ: <code>{err[:200]}</code>",
            error="api",
        )
