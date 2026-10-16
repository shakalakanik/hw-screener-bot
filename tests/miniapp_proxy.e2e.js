// Скан Mini App при заблокированных прямых хостах бирж: все данные — через /api/mkt (сервер).
// Нужен сервер: ALLOW_DEBUG_WEBAPP=1 на 127.0.0.1:8811. Запуск: NODE_PATH=<puppeteer-core> node tests/miniapp_proxy.e2e.js
const puppeteer = require('puppeteer-core');
const B = 'http://127.0.0.1:8811';
const BLOCK = /^https:\/\/(api\.bybit\.com|www\.okx\.com|iss\.moex\.com)\//;
let fails = 0; const ok = (n, c, x) => { if (!c) fails++; console.log((c ? 'OK   ' : 'FAIL ') + n + (c ? '' : ' ' + JSON.stringify(x || ''))); };
(async () => {
  const br = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', headless: 'new', args: ['--no-sandbox'] });
  const pg = await br.newPage();
  await pg.setRequestInterception(true);
  let blocked = 0, proxied = 0, proxErr = 0;
  pg.on('request', r => { const u = r.url();
    if (BLOCK.test(u)) { blocked++; return r.abort(); }
    if (u.startsWith(B) || u.startsWith('data:') || u.startsWith('blob:')) { if (u.includes('/api/mkt/')) proxied++; return r.continue(); }
    r.abort(); });
  pg.on('response', r => { if (r.url().includes('/api/mkt/') && r.status() !== 200) proxErr++; });
  await pg.goto(B + '/app?debug_user_id=1', { waitUntil: 'domcontentloaded' });
  await new Promise(r => setTimeout(r, 2500));
  async function scan(mkt, winH, maxMs) {
    await pg.evaluate((m, w) => { const s = document.getElementById('mkt'); s.value = m; s.dispatchEvent(new Event('change'));
      document.getElementById('win').value = String(w); document.getElementById('winUnit').value = '1'; LAST = []; }, mkt, winH);
    await new Promise(r => setTimeout(r, 800));
    const p0 = proxied;
    await pg.evaluate(() => document.getElementById('run').click());
    const t0 = Date.now(); let st = null;
    while (Date.now() - t0 < maxMs) {
      await new Promise(r => setTimeout(r, 2000));
      st = await pg.evaluate(() => ({ busy: document.getElementById('run').disabled, n: (LAST || []).length,
        rows: (window.__SCAN_ROWS__ || []).length, status: (document.getElementById('status') || {}).textContent || '' }));
      if (!st.busy && Date.now() - t0 > 4000) break;
    }
    return { ...st, req: proxied - p0, sec: Math.round((Date.now() - t0) / 1000) };
  }
  const ru = await scan('ru', 72, 180000);
  console.log('MOEX', JSON.stringify(ru));
  ok('MOEX scan via proxy: requests went to /api/mkt', ru.req > 20, ru);
  ok('MOEX scan finished with signals', !ru.busy && ru.n > 0, ru);
  const cr = await scan('crypto', 12, 420000);
  console.log('CRYPTO', JSON.stringify(cr));
  ok('crypto scan via proxy', cr.req > 100 && !cr.busy && cr.n > 0, cr);
  ok('no direct exchange requests reached network (all aborted attempts = 0)', blocked === 0, { blocked });
  ok('proxy errors', proxErr === 0, { proxErr });
  console.log('FAILS', fails); await br.close(); process.exit(fails ? 1 : 0);
})();
