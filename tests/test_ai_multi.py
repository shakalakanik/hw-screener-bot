"""Тесты мульти-действий ИИ: rule-парсер, sanitize, apply == кнопки /filter. Запуск:
DB_PATH=/tmp/x.db TG_BOT_TOKEN=1:x python tests/test_ai_multi.py"""
import asyncio, os, sys, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("TG_BOT_TOKEN", "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
os.environ["GEMINI_API_KEY"] = ""
import storage, ai_chat, bot
storage.init()

def M(m, e): return ("set_market", m, e)
def A(m, e, s=None): return ("set_auto", m, e, s)
def B(m, e): return ("set_best_only", m, e)
def norm(acts):
    out = []
    for a in acts or []:
        p = a["params"]
        if a["action"] == "set_market": out.append(M(p["market"], p["enabled"]))
        elif a["action"] == "set_auto": out.append(A(p["market"], p["enabled"], p.get("strategy")))
        elif a["action"] == "set_best_only": out.append(B(p["market"], p["enabled"]))
        else: out.append((a["action"], json.dumps(p, ensure_ascii=False)))
    return out

CASES = [
 ("включи крипту авто на ложный пробой, а Мосбиржу включи на пробой авто",
  [M("crypto",True), A("crypto",True,"fbo"), M("ru",True), A("ru",True,"brk")]),
 ("включи мосбиржу и выключи крипту", [M("ru",True), M("crypto",False)]),
 ("Выключи КРИПТУ, включи МОСБИРЖУ", [M("crypto",False), M("ru",True)]),
 ("всё выключи кроме крипты", [M("crypto",True), M("ru",False)]),
 ("включи крипту и мосбиржу", [M("crypto",True), M("ru",True)]),
 ("вкючи крепту авто оба", [M("crypto",True), A("crypto",True,"both")]),          # опечатки
 ("влючи мосбржу авто на лп", [M("ru",True), A("ru",True,"fbo")]),
 ("выключи авто на крипте", [A("crypto",False)]),
 ("включи алго и выключи мосбиржу", []),
 ("включи только крипту", [M("crypto",True), M("ru",False)]),
 ("включи все рынки", [M("crypto",True), M("ru",True)]),
 ("мосбиржу авто пробой, крипту выключи", [M("ru",True), A("ru",True,"brk"), M("crypto",False)]),
]
AMBIG = ["на крипте только лучший сигнал", "включи на пробой", "поставь крипте пробой", "что такое ATR", "сделай хорошо"]

fails = 0
for txt, exp in CASES:
    got = norm(ai_chat.parse_rules(txt))
    # «вкючи» (опечатка без «л») — допускаем None? нет: проверяем строго
    ok = got == exp
    fails += not ok
    print("OK  " if ok else "FAIL", txt, "" if ok else f"\n     got {got}\n     exp {exp}")
for txt in AMBIG:
    got = ai_chat.parse_rules(txt)
    ok = got is None
    fails += not ok
    print("OK  " if ok else "FAIL", "ambiguous→None:", txt, "" if ok else got)

# sanitize: массив из модели, algo-авто отклонён с пояснением, мусор выброшен, старый одиночный формат
raw = 'Сделаю.\n---ACTION---\n[{"action":"set_market","params":{"market":"crypto","enabled":true}},' \
      '{"action":"set_auto","params":{"market":"algo","enabled":true}},{"action":"rm_rf","params":{}},' \
      '{"action":"set_auto","params":{"market":"ru","enabled":"true","strategy":"brk"}}]'
txt, prop = ai_chat._parse_action_block(raw); prop = ai_chat.sanitize_proposal(prop)
ok = txt == "Сделаю." and norm(prop["actions"]) == [M("crypto",True), A("ru",True,"brk")] and prop["notes"] and "Авто" in prop["notes"][0]
fails += not ok; print("OK  " if ok else "FAIL", "sanitize multi + algo-auto reject", prop)
_, single = ai_chat._parse_action_block('x\n---ACTION---\n{"action":"set_market","params":{"market":"ru","enabled":false},"summary":"s"}')
single = ai_chat.sanitize_proposal(single)
ok = single["action"] == "set_market" and single["params"] == {"market": "ru", "enabled": False}
fails += not ok; print("OK  " if ok else "FAIL", "backward-compat single", single)

# apply == кнопки /filter
U1, U2 = 111, 222
tpl = {"Широкий": {"filters": {"_strat": "fbo"}}, "Узкий тренд": {"filters": {"_strat": "brk"}}}
for u in (U1, U2):
    for n, e in tpl.items():
        storage.save_html_template(u, n, e["filters"], market="crypto") if hasattr(storage, "save_html_template") else None
def snap(u):
    c = storage.get_active_config(u)
    return {"markets": sorted(c.get("markets") or []), "auto": storage.get_auto_mode(u),
            "auto_st": {m: storage.get_auto_strategy(u, m) for m in ("crypto", "ru")},
            "best": storage.get_best_only(u), "names": c.get("names_by_market"),
            "ov": storage.get_template_strategy_overrides(u)}
# U1 через ИИ
prop = ai_chat.sanitize_proposal({"action": "multi", "actions": ai_chat.parse_rules(
    "включи крипту авто на ложный пробой, а Мосбиржу включи на пробой авто")})
res = bot._apply_ai_proposal(U1, prop)
print(res)
# U2 «кнопками» (те же функции, что callback-и /filter: mkt_on, auto_on, auto_strat ×N)
storage.set_market_enabled(U2, "crypto", True); storage.set_auto_mode(U2, "crypto", True)   # auto_strat по умолчанию fbo
storage.set_market_enabled(U2, "ru", True); storage.set_auto_mode(U2, "ru", True)
storage.set_auto_strategy(U2, "ru", bot._STRAT_CYCLE["fbo"])   # одно нажатие auto_strat: fbo→brk
ok = snap(U1) == snap(U2)
fails += not ok; print("OK  " if ok else "FAIL", "apply == /filter buttons", snap(U1), "" if ok else snap(U2))
# формат карточки
print("--- card ---\n" + bot._format_proposal(prop))

# chat() без ключа: мульти → rule-парсер; одиночный → локальный интент
r = asyncio.run(ai_chat.chat("включи мосбиржу и выключи крипту", [], {}))
ok = r.proposal and r.proposal["action"] == "multi" and len(r.proposal["actions"]) == 2
fails += not ok; print("OK  " if ok else "FAIL", "chat no-key multi via rules")
r = asyncio.run(ai_chat.chat("включи мосбиржу", [], {}))
ok = r.proposal and r.proposal["action"] == "set_market"
fails += not ok; print("OK  " if ok else "FAIL", "chat single local")

# Gemini замокан: возвращает массив
class _R: text = 'Ок.\n---ACTION---\n[{"action":"set_market","params":{"market":"algo","enabled":false}},{"action":"set_auto","params":{"market":"crypto","enabled":false}}]'
class _Models:
    async def generate_content(self, **k): return _R()
class _C:
    class aio: models = _Models()
os.environ["GEMINI_API_KEY"] = "fake"; ai_chat._client = lambda: _C()
r = asyncio.run(ai_chat.chat("выключи алго, а на крипте убери авто — и подумай что ещё", [], bot._ai_user_context(U1)))
ok = r.proposal and norm(r.proposal["actions"]) == [A("crypto",False)]
fails += not ok; print("OK  " if ok else "FAIL", "mocked Gemini multi", r.proposal)
# шаблоны: fuzzy-имена, set_strategy, ненайденный → заметка; результат == кнопки tpl:/strat:
for u in (U1, U2):
    storage.save_html_templates(u, {"Широкий": {"thr": "0", "_strat": "fbo"}, "Узкий тренд": {"thr": "5", "_strat": "fbo"}}, "crypto")
os.environ["GEMINI_API_KEY"] = ""
prop = {"action": "multi", "actions": [
    {"action": "add_templates", "params": {"market": "crypto", "names": ["широкии", "Нет такого"]}},
    {"action": "set_strategy", "params": {"market": "crypto", "template": "узкий", "strategy": "brk"}}]}
prop = bot._ai_resolve_names(U1, ai_chat.sanitize_proposal(prop))
print(bot._format_proposal(prop)); print(bot._apply_ai_proposal(U1, prop))
# U2 кнопками: tpl:Широкий (Авто выкл, шаблон активен), strat:Узкий тренд fbo→brk
storage.set_auto_mode(U2, "crypto", False); storage.set_active_templates_for_market(U2, "crypto", ["Широкий"])
storage.set_template_strategy(U2, "Узкий тренд", "brk")
ok = snap(U1) == snap(U2) and "Нет такого" in prop["notes"][0]
fails += not ok; print("OK  " if ok else "FAIL", "templates apply == buttons", snap(U1), "" if ok else snap(U2))
print("context:\n" + ai_chat.format_user_context(bot._ai_user_context(U1)))
print("FAILS", fails); sys.exit(1 if fails else 0)
