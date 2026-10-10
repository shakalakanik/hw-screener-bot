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
- Пользователь синхронизирует HTML-шаблоны фильтров из Mini App (/syncurl, кнопка «🔗 Синхронизация шаблонов»).
- В /filter включает рынки и шаблоны, переключает стратегию per-шаблон.
- Подписка (авто-скан раз в час, HH:00:40): приходят только сигналы только что закрывшегося часа (время сигнала = HH:00). Ручной «📡 Скан» /scan: сигналы последних 5 часов, которые ещё не приходили. Повторов нет (дедуп по ticker+side+время+стратегия и уровню).
- Ночной фильтр (noNight): для крипты по умолчанию пропускает часы сигнала 23:00–09:00 МСК (не отключает весь скан).
- Watchlist («Отслеживаемые»): под карточкой сигнала в Telegram только кнопка «🔎 Посмотреть сигнал» (открывает журнал Mini App); добавить в отслеживаемые — значок 👁 в правом верхнем углу карточки в «Журнале сигналов» Mini App (повторное нажатие убирает); раздельно по рынкам crypto/moex/algo.
- Команды: /start /stop /scan /filter /status /syncurl /ai /cancel. ИИ-режим всегда включён (выключить нельзя).

Твои задачи (примеры use-case):
1) Объяснить карточку сигнала (сила, ATR-дистанция, стоп/тейк, D1 bias, fbo vs brk).
2) Подсказать какие шаблоны/рынки включить под стиль торговли.
3) Ответить на вопросы по правилам Герчика / пробой / ложный пробой (кратко, без воды).
4) Помочь сформулировать настройки фильтра (сила, дистанция ATR, long/short, тренд).
5) Предложить сменить стратегию шаблона (fbo/brk/both) — только как предложение.
6) Объяснить sync Mini App и почему шаблоны «пустые».
7) Объяснить статус подписки и расписание авто-скана (раз в час после закрытия H1, сигналы в HH:00–HH:05 МСК).
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
- Ты НЕ меняешь настройки сам. Если пользователь просит изменить настройки,
  опиши изменения простым языком и в КОНЦЕ ответа добавь ровно один блок
  ---ACTION--- с JSON-МАССИВОМ действий (одно сообщение может содержать много
  изменений — перечисли ВСЕ, в порядке применения):

---ACTION---
[{"action":"<код>","params":{...}}, {"action":"<код>","params":{...}}]

Допустимые action (всё, что можно нажать в /filter):
- set_market: {"market":"crypto"|"ru"|"algo","enabled":true|false}  — рынок вкл/выкл
- set_auto: {"market":"crypto"|"ru","enabled":true|false,"strategy":"fbo"|"brk"|"both"(необязательно)}
    — встроенный шаблон «🤖 Авто» рынка и его стратегия. У «Крипта (Алго)» (algo) Авто НЕТ —
    не предлагай, объясни что нужен алго-шаблон.
- set_auto_strategy: {"market":"crypto"|"ru","strategy":"fbo"|"brk"|"both"}
- set_best_only: {"market":"crypto"|"ru"|"algo","enabled":true|false} — «только лучший сигнал»
- set_templates: {"market":..,"names":["..."]}  — полная замена активных шаблонов рынка (выключает Авто)
- add_templates / remove_templates: {"market":..,"names":["..."]}
- set_strategy: {"market":..,"template":"<имя>","strategy":"fbo"|"brk"|"both"} — ЛП/Пробой/Оба шаблона
- subscribe: {} / unsubscribe: {}
Стратегии: fbo = ложный пробой (ЛП), brk = пробой, both = оба.
Имена шаблонов бери ТОЛЬКО из контекста (шаблоны своего рынка).

Примеры:
«включи крипту авто на ложный пробой, а Мосбиржу включи на пробой авто» →
[{"action":"set_market","params":{"market":"crypto","enabled":true}},
 {"action":"set_auto","params":{"market":"crypto","enabled":true,"strategy":"fbo"}},
 {"action":"set_market","params":{"market":"ru","enabled":true}},
 {"action":"set_auto","params":{"market":"ru","enabled":true,"strategy":"brk"}}]
«включи мосбиржу и выключи крипту» →
[{"action":"set_market","params":{"market":"ru","enabled":true}},
 {"action":"set_market","params":{"market":"crypto","enabled":false}}]
«всё выключи кроме крипты» →
[{"action":"set_market","params":{"market":"crypto","enabled":true}},
 {"action":"set_market","params":{"market":"ru","enabled":false}},
 {"action":"set_market","params":{"market":"algo","enabled":false}}]
«на крипте только лучший, авто оба» →
[{"action":"set_best_only","params":{"market":"crypto","enabled":true}},
 {"action":"set_auto","params":{"market":"crypto","enabled":true,"strategy":"both"}}]
Если просьба неоднозначна (непонятно какой рынок, шаблон или стратегия, «на пробой» без
указания — Авто это или шаблон) — НЕ угадывай и НЕ добавляй ACTION, задай короткий
уточняющий вопрос.
Если менять нечего — НЕ добавляй ---ACTION---.
ФОРМАТ ТЕКСТА: НЕ используй Markdown (никаких **, *, _, #, `). Пиши обычным текстом —
бот сам оформит ответ. Списки — с «• » в начале строки.
Когда предлагаешь изменить настройки: формулируй как ПРЕДЛОЖЕНИЕ («Предлагаю: включить
Мосбиржу с Авто на пробой. Подтверди»). НИКОГДА не пиши «я включил / применил / готово /
сделал» — до ответа «да» ничего не меняется. Будь краток (1–3 предложения): карточка
подтверждения сама перечислит все изменения, не дублируй список.
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


_TG_TAGS = ("b", "i", "code", "u", "s")


def md_to_tg_html(text: str) -> str:
    """Markdown ответа Gemini → безопасный Telegram HTML (b/i/code). Неизвестные/непарные
    маркеры вырезаются; уже имеющиеся простые теги <b>/<i>/<code> сохраняются."""
    import html as _html
    if not text:
        return ""
    keep: list[str] = []

    def _ph(tag: str) -> str:
        keep.append(tag)
        return f"\x00{len(keep) - 1}\x00"

    t = re.sub(r"(?i)</?(?:%s)>" % "|".join(_TG_TAGS), lambda m: _ph(m.group(0).lower()), text)
    t = _html.escape(t, quote=False)
    codes: list[str] = []

    def _code(m):
        codes.append(m.group(1))
        return f"\x01{len(codes) - 1}\x01"

    t = re.sub(r"```(?:\w+)?\n?(.*?)```", _code, t, flags=re.DOTALL)
    t = re.sub(r"`([^`\n]+)`", _code, t)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", r"**\1**", t)
    t = re.sub(r"(?m)^(\s*)[*\-+]\s+", r"\1• ", t)
    t = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", t, flags=re.DOTALL)
    t = re.sub(r"(?<![\w])__(?=\S)(.+?)(?<=\S)__(?![\w])", r"<b>\1</b>", t, flags=re.DOTALL)
    t = re.sub(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"(?<![\w])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w])", r"<i>\1</i>", t)
    # остатки разметки: «**», одиночные * вне чисел (2*3 оставляем), _ вне слов
    t = t.replace("**", "")
    t = re.sub(r"(?<!\d)\*|\*(?!\d)", "", t)
    t = re.sub(r"(?<![\w])_+|_+(?![\w])", "", t)
    t = re.sub(r"\x01(\d+)\x01", lambda m: "<code>" + codes[int(m.group(1))] + "</code>", t)
    t = re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], t)
    if not _html_balanced(t):
        t = re.sub(r"</?(?:%s)>" % "|".join(_TG_TAGS), "", t)
    return t


def _html_balanced(t: str) -> bool:
    stack = []
    for m in re.finditer(r"<(/?)(%s)>" % "|".join(_TG_TAGS), t):
        if not m.group(1):
            stack.append(m.group(2))
        elif not stack or stack.pop() != m.group(2):
            return False
    return not stack


def strip_markup(text: str) -> str:
    """Plain-текст запасной вариант: без тегов и markdown-маркеров."""
    import html as _html
    t = re.sub(r"</?[a-zA-Z][^>]*>", "", text or "")
    t = _html.unescape(t)
    t = re.sub(r"(?m)^(\s*)[*\-+]\s+", r"\1• ", t)
    t = t.replace("**", "").replace("__", "").replace("`", "")
    t = re.sub(r"(?<!\d)\*|\*(?!\d)", "", t)
    t = re.sub(r"(?<![\w])_+|_+(?![\w])", "", t)
    return re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", t)


_RE_FAKE_DONE = re.compile(r"(?i)\b(?:я\s+)?(?:включил|выключил|отключил|применил|поменял|изменил|установил|переключил)(?:а)?\b")


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
        proposal = _parse_actions_json(blob)
        if proposal is None and blob:
            logger.warning("AI ACTION JSON parse failed: %s", blob[:200])
    # strip accidental fences
    text = re.sub(r"^```(?:html|markdown)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if len(text) > MAX_REPLY_CHARS:
        text = text[: MAX_REPLY_CHARS - 20] + "…"
    return text, proposal


def _parse_actions_json(blob: str) -> dict | None:
    """ACTION JSON: массив действий, {"actions":[...]} или одиночный объект (старый формат)."""
    blob = re.sub(r"^```(?:json)?\s*|\s*```$", "", (blob or "").strip())
    cands = []
    m = re.search(r"\[.*\]", blob, flags=re.DOTALL)
    if m:
        cands.append(m.group(0))
    m = re.search(r"\{.*\}", blob, flags=re.DOTALL)
    if m:
        cands.append(m.group(0))
    for c in cands:
        try:
            obj = json.loads(c)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("actions"), list):
            obj = obj["actions"]
        if isinstance(obj, list):
            items = [o for o in obj if isinstance(o, dict) and o.get("action")]
            if items:
                return {"action": "multi", "actions": items, "summary": f"{len(items)} изменений"}
        elif isinstance(obj, dict) and obj.get("action"):
            return {
                "action": str(obj["action"]).strip(),
                "params": obj.get("params") if isinstance(obj.get("params"), dict) else {},
                "summary": str(obj.get("summary") or obj["action"]).strip(),
            }
    return None


_ALLOWED_ACTIONS = {
    "set_auto",
    "set_auto_strategy",
    "set_best_only",
    "set_market",
    "set_strategy",
    "set_templates",
    "add_templates",
    "remove_templates",
    "subscribe",
    "unsubscribe",
}


_MKTS = ("crypto", "ru", "algo")
_STRATS = ("fbo", "brk", "both")
_ALGO_NO_AUTO = ("У «Крипта (Алго)» нет режима 🤖 Авто — алго сканирует только по выбранному "
                 "алго-шаблону (/filter → ⚡ Алго).")


def _as_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "да", "вкл")
    return bool(v)


def _sanitize_one(item: dict, notes: list[str]) -> dict | None:
    action = str(item.get("action") or "").strip()
    if action not in _ALLOWED_ACTIONS:
        return None
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    clean: dict[str, Any] = {"action": action, "params": {}, "summary": str(item.get("summary") or action)}
    market = params.get("market")
    if action in ("set_market", "set_best_only"):
        if market not in _MKTS:
            return None
        clean["params"] = {"market": market, "enabled": _as_bool(params.get("enabled"))}
    elif action in ("set_auto", "set_auto_strategy"):
        if market == "algo":
            if _ALGO_NO_AUTO not in notes:
                notes.append(_ALGO_NO_AUTO)
            return None
        if market not in ("crypto", "ru"):
            return None
        strat = params.get("strategy")
        if strat is not None and strat not in _STRATS:
            return None
        if action == "set_auto_strategy":
            if not strat:
                return None
            clean["params"] = {"market": market, "strategy": strat}
        else:
            enabled = _as_bool(params.get("enabled", True))
            clean["params"] = {"market": market, "enabled": enabled}
            if strat and enabled:
                clean["params"]["strategy"] = strat
    elif action == "set_strategy":
        strat = params.get("strategy")
        name = str(params.get("template") or "").strip()
        if not name or strat not in _STRATS:
            return None
        clean["params"] = {"template": name, "strategy": strat}
        if market in _MKTS:
            clean["params"]["market"] = market
    elif action in ("set_templates", "add_templates", "remove_templates"):
        names = params.get("names")
        if isinstance(names, str):
            names = [names]
        if market not in _MKTS or not isinstance(names, list):
            return None
        clean["params"] = {"market": market, "names": [str(n).strip() for n in names if str(n).strip()]}
    else:
        clean["params"] = {}
    return clean


def sanitize_proposal(proposal: dict | None) -> dict | None:
    """Одиночное действие (старый формат) или {"action":"multi","actions":[...]}.

    Мульти-предложение → {"action":"multi","actions":[clean...],"notes":[...]}; одно валидное
    действие без заметок схлопывается в одиночный формат. Нечего применять → None
    (или {"action":"multi","actions":[],"notes":[...]} чтобы показать объяснение).
    """
    if not proposal or not isinstance(proposal, dict):
        return None
    notes: list[str] = list(proposal.get("notes") or [])
    if proposal.get("action") == "multi" or isinstance(proposal.get("actions"), list):
        items = proposal.get("actions") or []
    else:
        items = [proposal]
    out: list[dict] = []
    seen = set()
    for it in items[:20]:
        if not isinstance(it, dict):
            continue
        c = _sanitize_one(it, notes)
        if not c:
            continue
        key = json.dumps([c["action"], c["params"]], sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    if not out:
        return {"action": "multi", "actions": [], "notes": notes, "summary": ""} if notes else None
    if len(out) == 1 and not notes and proposal.get("action") != "multi":
        return out[0]
    return {"action": "multi", "actions": out, "notes": notes, "summary": f"{len(out)} изменений"}


# ── Детерминированный разбор частых фраз (работает без Gemini) ────────────────
_R_CRYPTO = re.compile(r"(?i)(?<!алго )\b(?:кр[иеы]п\w*|крепт\w*|crypto\w*|bybit|okx)")
_R_RU = re.compile(r"(?i)(?:мос\s*-?\s*б[иы]?рж\w*|мосбр\w*|moex|мое?кс\w*|мб\b|рф\b|российск\w*|акци\w*)")
_R_ALGO = re.compile(r"(?i)(?:алг[оа]\w*|algo\w*|индикатор\w*)")
_R_ALL = re.compile(r"(?i)\b(?:все|всё|всех|оба\s+рынк\w*|все\s+рынк\w*)\b")
_R_EXCEPT = re.compile(r"(?i)\bкроме\b")
_R_ONV = re.compile(r"(?i)(?:вкл\w*|вклю\w*|влючи\w*|вруби\w*|запусти\w*|активир\w*|добав\w*|поставь|"
                    r"сделай|переключи|\bon\b|enable)")
_R_OFFV = re.compile(r"(?i)(?:выкл\w*|выклю\w*|отключ\w*|выруби\w*|убери|убрать|останови\w*|\boff\b|disable)")
_R_AUTO = re.compile(r"(?i)(?:авто\w*|\bauto\b|🤖)")
_R_FBO = re.compile(r"(?i)(?:ложн\w*\s*-?\s*пр[оа]бо\w*|\bлп\b|\bfbo\b|ложняк\w*|ложн\w*)")
_R_BRK = re.compile(r"(?i)(?:пр[оа]бо[йияюе]\w*|\bbrk\b|breakout)")
_R_BOTH = re.compile(r"(?i)\b(?:оба|обе|обоих|both|все\s+стратеги\w*)\b")
_R_BEST = re.compile(r"(?i)(?:лучш\w*|best)")
_R_SPLIT = re.compile(r"(?i)\s*(?:[,;.]|\bа\s+также\b|\bа\b|\bи\b|\bно\b|\bплюс\b|\bзатем\b|\bпотом\b)\s*")
_MKT_RU_LABEL = {"crypto": "крипта", "ru": "мосбиржа", "algo": "крипта (алго)"}


def _seg_markets(seg: str) -> list[str]:
    out = []
    algo = bool(_R_ALGO.search(seg))
    if algo:
        out.append("algo")
    seg_wo = _R_ALGO.sub(" ", seg)
    if _R_CRYPTO.search(seg_wo) and not (algo and re.search(r"(?i)крипт\w*\s*\(?\s*алг", seg)):
        out.append("crypto")
    if _R_RU.search(seg):
        out.append("ru")
    return out


def _seg_strategy(seg: str) -> str | None:
    if _R_BOTH.search(seg):
        return "both"
    if _R_FBO.search(seg):
        return "fbo"
    if _R_BRK.search(seg):
        return "brk"
    return None


def parse_rules(user_text: str) -> list[dict] | None:
    """Частые фразы: рынок + вкл/выкл + авто + ЛП/пробой/оба + «только лучший» + «кроме».
    Разбивает по «а», «и», запятым. Возвращает список действий или None (если что-то не понял —
    тогда пусть решает Gemini / уточнит)."""
    text = (user_text or "").strip().lower().replace("ё", "е")
    if not text or len(text) > 300:
        return None
    # «всё выключи кроме крипты» / «включи только мосбиржу»
    if _R_EXCEPT.search(text):
        head, _, tail = _R_EXCEPT.split(text, maxsplit=1)[0], None, _R_EXCEPT.split(text, maxsplit=1)[1]
        keep = _seg_markets(tail)
        if not keep or not (_R_OFFV.search(head) or _R_ONV.search(head)):
            return None
        on_keep = bool(_R_OFFV.search(head))   # «выключи всё кроме X» → X вкл, остальные выкл
        acts = [{"action": "set_market", "params": {"market": m, "enabled": on_keep}} for m in keep]
        acts += [{"action": "set_market", "params": {"market": m, "enabled": not on_keep}}
                 for m in _MKTS if m not in keep]
        return acts
    m_only = re.search(r"(?i)\bтолько\b", text)
    segs = [x for x in _R_SPLIT.split(text) if x and x.strip()]
    acts: list[dict] = []
    verb: bool | None = None
    last_mkts: list[str] = []
    for seg in segs:
        mk = _seg_markets(seg)
        if not mk and _R_ALL.search(seg) and (_R_ONV.search(seg) or _R_OFFV.search(seg)):
            mk = list(_MKTS)
        on, off = bool(_R_ONV.search(seg)), bool(_R_OFFV.search(seg))
        if on and off:
            return None
        if on or off:
            verb = on
        auto = bool(_R_AUTO.search(seg))
        strat = _seg_strategy(seg)
        best = bool(_R_BEST.search(seg))
        if not mk:
            # «…, авто на пробой» — относится к рынку предыдущего сегмента
            if (auto or strat or best) and last_mkts:
                mk = last_mkts
            elif not (on or off or auto or strat or best) and re.fullmatch(r"[\s\w]{0,12}", seg) and re.search(r"(?i)^(?:пожалуйста|плиз|тоже|еще|ещё|сразу)?$", seg.strip()):
                continue
            else:
                return None
        if verb is None and not (best or auto or strat):
            return None
        last_mkts = mk
        for m in mk:
            if best:
                en = True if verb is None else verb
                acts.append({"action": "set_best_only", "params": {"market": m, "enabled": en}})
                if not (auto or strat):
                    continue
            if auto or strat:
                if strat and not auto:
                    return None   # «на пробой» без «авто» — неясно: Авто или шаблон → уточнить
                en = True if verb is None else verb
                if verb is not False and not any(a["action"] == "set_market" and a["params"]["market"] == m for a in acts):
                    acts.append({"action": "set_market", "params": {"market": m, "enabled": True}})
                p = {"market": m, "enabled": en}
                if strat and en:
                    p["strategy"] = strat
                acts.append({"action": "set_auto", "params": p})
            elif not best:
                acts.append({"action": "set_market", "params": {"market": m, "enabled": bool(verb)}})
    if not acts:
        return None
    if m_only and verb:
        on_mk = {a["params"]["market"] for a in acts if a["action"] == "set_market" and a["params"]["enabled"]}
        if on_mk:
            acts += [{"action": "set_market", "params": {"market": m, "enabled": False}}
                     for m in _MKTS if m not in on_mk]
    return acts


def format_user_context(ctx: dict) -> str:
    """Human-readable context block for the model."""
    lines = [
        f"Подписка: {'да' if ctx.get('subscribed') else 'нет'}",
        f"ИИ-режим: {'вкл' if ctx.get('ai_enabled') else 'выкл'}",
        f"Авто-скан: {ctx.get('scan_schedule') or 'раз в час после закрытия H1 (HH:00–HH:05 МСК)'}",
        f"Рынки включены: {', '.join(ctx.get('markets') or []) or 'нет'}",
    ]
    by = ctx.get("names_by_market") or {}
    enabled = set(ctx.get("markets") or [])
    auto = ctx.get("auto") or {}
    auto_st = ctx.get("auto_strategy") or {}
    best = ctx.get("best_only") or {}
    sl = {"fbo": "ЛП", "brk": "Пробой", "both": "Оба"}
    tstr = ctx.get("template_strategies") or {}
    for m, label in (("crypto", "Крипта"), ("ru", "MOEX"), ("algo", "Крипта (Алго)")):
        names = by.get(m) or []
        a = ("Авто: нет (у алго Авто не бывает)" if m == "algo" else
             f"🤖 Авто: {'вкл' if auto.get(m) else 'выкл'} (стратегия {auto_st.get(m, 'fbo')} = {sl.get(auto_st.get(m, 'fbo'))})")
        lines.append(f"[{m}] {label}: рынок {'ВКЛ' if m in enabled else 'выкл'}; {a}; "
                     f"только лучший: {'вкл' if best.get(m) else 'выкл'}; "
                     f"активные шаблоны: {', '.join(names) if names else '—'}")
        if tstr.get(m) is not None:
            lines.append(f"  доступные шаблоны [{m}]: " +
                         (", ".join(f"{n}[{st}]" for n, st in list(tstr[m].items())[:40]) or "—"))
    tbm = ctx.get("templates_by_market") or {}
    for m, label in (("crypto", "Крипта"), ("ru", "MOEX"), ("algo", "Крипта (Алго)")):
        if tbm.get(m) is not None:
            lines.append(f"Шаблоны рынка {label} (у каждого рынка свои): {', '.join(tbm[m][:30]) or '—'}")
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
_RE_ALGO = re.compile(r"(?i)(?:алго\w*|algo\w*|индикатор\w*)")
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
    # только тривиальные одиночные команды; авто/стратегии/лучший/несколько частей → Gemini
    if re.search(r"(?i)авто|auto|пробо|ложн|\bлп\b|лучш|кроме|только|\bоба\b|шаблон|стратег|[,;]|\bа\b|\bи\b", text):
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
    if not (has_moex or has_crypto or _RE_ALGO.search(text)):
        return None
    if not (wants_on or wants_off):
        return None

    has_algo = bool(_RE_ALGO.search(text))
    if has_algo and has_moex:
        return None
    market = "ru" if has_moex else ("algo" if has_algo else "crypto")
    enabled = wants_on
    label = {"ru": "мосбиржу", "algo": "крипту (алго)"}.get(market, "крипту")
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
    # Local fast-path — works even without API key / during 503. Только тривиальные одиночные.
    rules = None
    try:
        rules = parse_rules(user_text)
    except Exception:
        logger.exception("parse_rules failed")
    local = try_local_intent(user_text)
    if local is not None and not (rules and len(rules) > 1):
        local.proposal = sanitize_proposal(local.proposal)
        return local
    rules_reply = None
    if rules:
        prop = sanitize_proposal({"action": "multi", "actions": rules})
        if prop:
            rules_reply = AiReply(text="Понял так (разбор без ИИ):", proposal=prop)

    if not _api_key():
        if rules_reply:
            return rules_reply
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
        return rules_reply or _friendly_error(e)

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
                text = md_to_tg_html(text)
                if proposal and _RE_FAKE_DONE.search(text):
                    # модель написала «я включил» до подтверждения — не вводим в заблуждение
                    text = "Предлагаю изменить настройки — подтверди ниже."
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
                    return rules_reply or _friendly_error(e)
                if _is_overload_error(err):
                    if attempt == 0:
                        continue  # retry same model after 0.8s
                    break  # next model after 1.6s
                # non-overload API error — try next model
                break

    if rules_reply:   # Gemini недоступен — детерминированный разбор частых фраз
        return rules_reply
    if last_exc is not None:
        logger.exception("Gemini chat failed after fallbacks")
        return _friendly_error(last_exc)
    return AiReply(text=_OVERLOAD_FRIENDLY, error="overload")
