// «🔄 Обновить» в журнале сигналов (MOEX + крипта), прямые хосты бирж заблокированы, продовый app (tests/run_bot_http.py),
// реальные строки журнала: python3 tests/seed_journal_e2e.py (пользователь 77).
const puppeteer = require('puppeteer-core');
const B = 'http://127.0.0.1:8811';
const BLOCK = /^https:\/\/(api\.bybit\.com|www\.okx\.com|iss\.moex\.com)\//;
let fails = 0; const ok = (n, c, x) => { if (!c) fails++; console.log((c ? 'OK   ' : 'FAIL ') + n + ' ' + JSON.stringify(x || '')); };
(async () => {
  const br = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', headless: 'new', args: ['--no-sandbox'] });
  const pg = await br.newPage(); await pg.setRequestInterception(true);
  let blocked = 0, prox = 0; const bad = [];
  pg.on('request', r => { const u = r.url(); if (BLOCK.test(u)) { blocked++; return r.abort(); }
    if (u.startsWith(B) || u.startsWith('data:') || u.startsWith('blob:')) { if (u.includes('/api/mkt/')) prox++; return r.continue(); } r.abort(); });
  pg.on('response', r => { if (r.url().includes('/api/mkt/') && r.status() !== 200) bad.push(r.status() + ' ' + r.url().slice(0, 110)); });
  await pg.goto(B + '/app?debug_user_id=77', { waitUntil: 'domcontentloaded' });
  await new Promise(r => setTimeout(r, 2500));
  for (const mkt of ['ru', 'crypto']) {
    await pg.evaluate(m => { localStorage.removeItem('hw_jstatus'); const s = document.getElementById('mkt'); s.value = m; s.dispatchEvent(new Event('change')); }, mkt);
    await new Promise(r => setTimeout(r, 800));
    await pg.evaluate(() => document.getElementById('tabJournal').click());
    await pg.waitForSelector('.hw-j-refresh', { timeout: 20000 });
    await new Promise(r => setTimeout(r, 6000));                         // авто-пересчёт при открытии
    const p0 = prox;
    await pg.evaluate(() => document.querySelector('.hw-j-refresh').click());
    const t0 = Date.now(); await new Promise(r => setTimeout(r, 500));
    while (Date.now() - t0 < 60000) { await new Promise(r => setTimeout(r, 1000));
      if (!(await pg.evaluate(() => document.querySelector('.hw-j-refresh').disabled))) break; }
    const st = await pg.evaluate(() => [...document.querySelectorAll('.hw-j-status')].map(e => e.textContent));
    console.log(mkt, JSON.stringify(st), 'req after click', prox - p0);
    ok(mkt + ': кнопка ходит на сервер', prox - p0 > 0, { req: prox - p0 });
    ok(mkt + ': статусы посчитаны (не «—»)', st.length >= 2 && st.every(s => s && s !== '—'), st);
    ok(mkt + ': старая сделка закрыта', st.some(s => /тейк|стоп|безубыток|закрыта/.test(s)), st);
  }
  ok('прямых запросов к биржам 0', blocked === 0, { blocked });
  ok('ошибок /api/mkt 0', bad.length === 0, bad.slice(0, 5));
  console.log('FAILS', fails); await br.close(); process.exit(fails ? 1 : 0);
})();
