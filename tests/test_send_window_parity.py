"""Окно отправки: подписка = только последний закрытый час; ручной скан = 12ч неотправленных.
Реальный конвейер на кэше OKX (crypto+algo) vs HTML-эталон + синтетика для ru. Рестарт = новый процесс."""
import sys, os, json, time, asyncio, tempfile, subprocess
code_dir = sys.argv[1]; sys.path.insert(0, code_dir)
cache = json.load(open('/workspace/parity/data/cache.json')); NOW = cache['now_ms']
time.time = lambda: NOW / 1000.0
DB = os.environ.get('SW_DB') or os.path.join(tempfile.mkdtemp(), 'x.db')
os.environ['DB_PATH'] = DB; os.environ['DATA_SOURCE'] = 'okx'
import storage, screener
screener.OKX_BASE = 'http://127.0.0.1:8765'; screener.TOP_N = 50
storage.init()
TPL = {'_name': 'Авто', '_market': 'crypto', '_strategy': 'both', 'filters': {}}
ATPL = {'_name': 'RSI', '_market': 'algo', '_strategy': 'algo', 'filters': {'ind_rsi': 'oversold'}}
flt = lambda cid: {'markets': ['crypto', 'algo'], '_multi': [TPL, ATPL]}
sent = []
async def on_signal(card, chat_ids):
    sent.append(card)
    for cid in chat_ids: storage.record_signal_delivery(cid, card)
b = lambda t: t[:-4] if t.endswith('USDT') else t
key = lambda c: (c['market'], b(c['ticker']), c['signal_ts'], 1 if c['side'] == 'LONG' else 0)
HS = (NOW // 3600000) * 3600000
h = {r['name']: r for r in json.load(open('/workspace/parity/out_html_3mkt.json'))['results']}['w12|Авто|both|wm20']['rows']
A = json.load(open('/workspace/parity/out_algo_html_24.json'))['res'][0]['rows']
HTML = {('crypto', r['base'], r['t'], r['d']) for r in h} | {('algo', r['base'], r['t'], r['d']) for r in A if NOW - r['t'] <= 12*3600000}
fails = 0
def ok(n, c, x=''):
    global fails; fails += not c; print(('OK   ' if c else 'FAIL ') + n + ('' if c else f'  {x}'))
mode = sys.argv[2] if len(sys.argv) > 2 else 'main'
if mode == 'restart':   # новый процесс, та же БД: подписка + повтор не шлют уже отправленное
    n = asyncio.run(screener.run_scan(on_signal, [1], flt, send_hours=1))
    n5 = asyncio.run(screener.run_scan(on_signal, [1], flt, send_hours=12))
    ok('after restart: subscription + manual resend nothing', n == 0 and n5 == 0, (n, n5))
    ok('after restart: subscriber persisted', 1 in storage.get_subscribers())
    sys.exit(1 if fails else 0)
exp1 = {k for k in HTML if k[2] == HS - 3600000}
exp5 = {k for k in HTML if k[2] >= HS - screener.MANUAL_SEND_HOURS * 3600000}   # ручной = 12 ч
print(f'HTML 12h list: {len(HTML)}; last closed hour (bar {time.strftime("%H:%M", time.gmtime(HS/1000 - 3600 + 3*3600))} МСК open → shown {time.strftime("%H:%M", time.gmtime(HS/1000 + 3*3600))}): {len(exp1)}; last 12h: {len(exp5)}')
# 1) подписка: только последний закрытый час
n1 = asyncio.run(screener.run_scan(on_signal, [1], flt, send_hours=1)); S1 = {key(c) for c in sent}
ok(f'subscription sends only last closed hour ({len(S1)}) == HTML filtered', S1 == exp1 and n1 == len(exp1), (sorted(S1 ^ exp1)[:5]))
ok('subscription: nothing older than last hour', all(c['signal_ts'] == HS - 3600000 for c in sent))
# 1b) подписка «в прошлом часе с сигналами»: время = закрытие последнего бара с сигналами + 40с
last_t = max(k[2] for k in HTML); NOW2 = last_t + 3600000 + 40000
time.time = lambda: NOW2 / 1000.0
os.environ['DB_PATH'] = os.path.join(tempfile.mkdtemp(), 'z.db'); storage.init()
sent.clear(); asyncio.run(screener.run_scan(on_signal, [7], flt, send_hours=12, now_ms=NOW2)); full = {key(c) for c in sent}
os.environ['DB_PATH'] = os.path.join(tempfile.mkdtemp(), 'z2.db'); storage.init()
sent.clear(); n1b = asyncio.run(screener.run_scan(on_signal, [7], flt, send_hours=1, now_ms=NOW2)); S1b = {key(c) for c in sent}
exp1b = {k for k in full if k[2] == last_t}
ok(f'subscription at {time.strftime("%H:%M:%S", time.gmtime(NOW2/1000 + 3*3600))} МСК sends only that hour ({len(S1b)} of {len(full)} in 12h)', S1b == exp1b and len(S1b) > 0 and all(k[2] == last_t for k in S1b), (len(S1b), len(exp1b)))
time.time = lambda: NOW / 1000.0; os.environ['DB_PATH'] = DB
# 2) ручной: 5ч, без уже отправленных
sent.clear(); n5 = asyncio.run(screener.run_scan(on_signal, [1], flt, send_hours=12)); S5 = {key(c) for c in sent}
ok(f'manual sends last-12h unsent ({len(S5)}) == HTML 12h minus already sent', S5 == exp5 - exp1, sorted(S5 ^ (exp5 - exp1))[:5])
ok('manual covers markets crypto+algo', {k[0] for k in S5} | {k[0] for k in S1} >= {'crypto', 'algo'} or not exp5, {k[0] for k in exp5})
# 3) повторы: подписка и ручной снова — 0
sent.clear()
ok('subscription after manual resends nothing', asyncio.run(screener.run_scan(on_signal, [1], flt, send_hours=1)) == 0)
ok('manual again resends nothing', asyncio.run(screener.run_scan(on_signal, [1], flt, send_hours=12)) == 0)
# 4) порядок «сначала ручной, потом подписка» на чистой БД (чат 2)
sent.clear(); flt2 = flt
os.environ['DB_PATH'] = os.path.join(tempfile.mkdtemp(), 'y.db')
storage.init()
m = asyncio.run(screener.run_scan(on_signal, [2], flt2, send_hours=12)); s = asyncio.run(screener.run_scan(on_signal, [2], flt2, send_hours=1))
ok('chat2: manual first sends 12h incl. last hour, subscription then 0', m == len(exp5) and s == 0, (m, len(exp5), s))
os.environ['DB_PATH'] = DB; storage.set_subscribed(1, True)
r = subprocess.run([sys.executable, __file__, code_dir, 'restart'], env={**os.environ, 'SW_DB': DB}, capture_output=True, text=True)
print(r.stdout.strip()); fails += r.returncode != 0
# 5) синтетика: правило одинаково для ru / algo / crypto (бар 17:00 МСК открыт → показ 18:00, скан 18:00:40)
from datetime import datetime, timezone, timedelta
MSK = timezone(timedelta(hours=3))
now2 = int(datetime(2026, 10, 7, 18, 0, 40, tzinfo=MSK).timestamp() * 1000)
mk = lambda m, h: {'market': m, 'ticker': 'SBER' if m == 'ru' else 'XUSDT', 'side': 'LONG', 'level': 100 + h, 'strategy': 'algo' if m == 'algo' else 'fbo',
                   'signal_ts': int(datetime(2026, 10, 7, h, 0, tzinfo=MSK).timestamp() * 1000), 'score': 5, 'last': 1, 'stop': 0.9, 'take': 1.3}
import html_pipeline, algo as algo_mod
for m in ('crypto', 'ru'):   # «Крипта (Алго)» убрана 16.10
    cards = [mk(m, hh) for hh in range(4, 18)]
    orig = (html_pipeline.apply_all, algo_mod.cards_for)
    html_pipeline.apply_all = lambda cs, *a, **k: [c for c in cards]
    algo_mod.cards_for = lambda ctx, raw, name: [c for c in cards]
    got = []
    async def cap(c, ids): got.append(c)
    f = lambda cid: {'markets': [m], '_multi': [{'_name': 'T', '_market': m, '_strategy': 'both', 'filters': {'ind_rsi': 'x'}}]}
    asyncio.run(screener._deliver_html(cards if m != 'algo' else [], [900 + len(m)], f, now2, 12, cap, None, None,
                                       algo_ctx={}, min_send_ts=screener.send_min_ts(now2, 1)))
    hrs = [datetime.fromtimestamp(c['signal_ts'] / 1000 + 3600, MSK).strftime('%H:%M') for c in got]
    ok(f'{m}: scan 18:00:40 → only shown-time 18:00 (got {hrs})', hrs == ['18:00'])
    got.clear(); cards = [{**c, 'level': c['level'] + 50} for c in cards]
    asyncio.run(screener._deliver_html(cards if m != 'algo' else [], [800 + len(m)], f, now2, 12, cap, None, None,
                                       algo_ctx={}, min_send_ts=screener.send_min_ts(now2, screener.MANUAL_SEND_HOURS)))
    hrs = sorted(datetime.fromtimestamp(c['signal_ts'] / 1000 + 3600, MSK).strftime('%H:%M') for c in got)
    exp = [f'{h:02d}:00' for h in range(8, 19)            # HTML 7.86 inW: бар t ≥ now−12ч → показ 08:00–18:00
           if m != 'crypto' or screener.crypto_send_hour_ok(int(datetime(2026, 10, 7, h - 1, 0, tzinfo=MSK).timestamp() * 1000))]
    ok(f'{m}: manual at 18:00:40 → last 12h (bar t≥now−12h) shown 08:00–18:00 (crypto: часы отправки) (got {hrs})', hrs == exp and '06:00' not in hrs, exp)
    html_pipeline.apply_all, algo_mod.cards_for = orig
print('FAILS', fails); sys.exit(1 if fails else 0)
