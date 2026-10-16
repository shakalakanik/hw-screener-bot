// «Обновить» в «Отслеживаемые» (MOEX + крипта) при заблокированных прямых хостах — на ПРОДОВОМ app (tests/run_bot_http.py).
// Запуск: python3 tests/run_bot_http.py & ; NODE_PATH=<puppeteer-core> node tests/miniapp_watch_refresh.e2e.js
const puppeteer = require('puppeteer-core');
const B = 'http://127.0.0.1:8811';
const BLOCK = /^https:\/\/(api\.bybit\.com|www\.okx\.com|iss\.moex\.com)\//;
let fails = 0; const ok = (n, c, x) => { if (!c) fails++; console.log((c ? 'OK   ' : 'FAIL ') + n + ' ' + JSON.stringify(x || '')); };
(async () => {
  const br = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', headless: 'new', args: ['--no-sandbox'] });
  const pg = await br.newPage();
  await pg.setRequestInterception(true);
  let blocked = 0; const bad = [];
  pg.on('request', r => { const u = r.url();
    if (BLOCK.test(u)) { blocked++; return r.abort(); }
    if (u.startsWith(B) || u.startsWith('data:') || u.startsWith('blob:')) return r.continue();
    r.abort(); });
  pg.on('response', r => { if (r.url().includes('/api/mkt/') && r.status() !== 200) bad.push(r.status() + ' ' + r.url().slice(0, 120)); });
  await pg.goto(B + '/app?debug_user_id=77', { waitUntil: 'domcontentloaded' });
  await new Promise(r => setTimeout(r, 2500));
  async function run(mkt, sym, base) {
    await pg.evaluate(m => { const s = document.getElementById('mkt'); s.value = m; s.dispatchEvent(new Event('change')); EX = null; }, mkt);
    await new Promise(r => setTimeout(r, 800));
    const H = 3600000, now = Date.now();
    // реальные цены берём в node напрямую (не через страницу), чтобы элементы были «настоящими» и на старом коде
    async function closeAt(agoH) {
      const ts = now - agoH * H;
      if (mkt === 'ru') { const d = new Date(ts + 3 * H).toISOString().slice(0, 10);
        const j = await (await fetch(`https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/${sym}/candles.json?interval=60&from=${d}&iss.meta=off`)).json();
        const r = j.candles.data[0]; const c = j.candles.columns; return { ts: Date.parse(r[c.indexOf('begin')].replace(' ', 'T') + '+03:00'), e: r[c.indexOf('close')] }; }
      const j = await (await fetch(`https://api.bybit.com/v5/market/kline?category=linear&symbol=${sym}&interval=60&end=${ts}&limit=1`)).json();
      const r = j.result.list[0]; return { ts: +r[0], e: +r[4] };
    }
    const mk = async (agoH, dd) => { const b = await closeAt(agoH); return { t: b.ts, sym, base, mkt, type: 'fbo', d: 1, e: b.e, st: b.e * (1 - dd), tk: b.e * (1 + 3 * dd) }; };
    const w = [await mk(mkt === 'ru' ? 30 : 20, 0.05), await mk(300, 0.002)];   // свежая (открыта) + старая с узким стопом (должна закрыться)
    await pg.evaluate((mkt, w) => { const all = loadWatchAll().filter(t => t.mkt !== mkt).concat(w);
      localStorage.setItem('hw_fbo_watch_all', JSON.stringify(all)); renderWatch(); }, mkt, w);
    await pg.evaluate(() => document.getElementById('refreshWatch').click());
    const t0 = Date.now();
    while (Date.now() - t0 < 90000) { await new Promise(r => setTimeout(r, 1000));
      if (!(await pg.evaluate(() => document.getElementById('refreshWatch').disabled))) break; }
    return pg.evaluate(() => ({ w: loadWatch().map(t => ({ base: t.base, now: t._now, res: t._res, score: t.score })),
      cells: [...document.querySelectorAll('#wtb tr')].map(r => r.textContent.slice(0, 90)) }));
  }
  const ru = await run('ru', 'SBER', 'SBER'); console.log('MOEX', JSON.stringify(ru));
  ok('MOEX: цена обновлена', ru.w.every(t => t.now > 0), ru.w);
  ok('MOEX: старая сделка закрыта (статус не «в работе»)', ru.w.some(t => t.res), ru.w);
  const cr = await run('crypto', 'BTCUSDT', 'BTC'); console.log('CRYPTO', JSON.stringify(cr));
  ok('crypto: цена обновлена', cr.w.every(t => t.now > 0), cr.w);
  ok('crypto: старая сделка закрыта', cr.w.some(t => t.res), cr.w);
  // «Обновить всё» — пересчёт оценки
  await pg.evaluate(() => document.getElementById('refreshWatchAll').click());
  const t0 = Date.now(); while (Date.now() - t0 < 120000) { await new Promise(r => setTimeout(r, 1000));
    if (!(await pg.evaluate(() => document.getElementById('refreshWatchAll').disabled))) break; }
  const sc = await pg.evaluate(() => loadWatch().map(t => ({ base: t.base, res: t._res, now: t._now, score: t.score })));
  console.log('ALL', JSON.stringify(sc)); ok('«Обновить всё»: оценка посчитана', sc.every(t => t.score != null), sc);
  ok('прямых запросов к биржам 0', blocked === 0, { blocked });
  ok('ошибок /api/mkt 0', bad.length === 0, bad.slice(0, 5));
  console.log('FAILS', fails); await br.close(); process.exit(fails ? 1 : 0);
})();
