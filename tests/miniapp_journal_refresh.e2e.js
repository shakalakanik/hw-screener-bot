// «🔄 Обновить» в журнале сигналов (MOEX + крипта), прямые хосты бирж заблокированы, продовый app (tests/run_bot_http.py),
// реальные строки журнала: python3 tests/seed_journal_e2e.py (пользователь 77).
const puppeteer = require('puppeteer-core');
const B = 'http://127.0.0.1:8811';
const BLOCK = /^https:\/\/(api\.bybit\.com|www\.okx\.com|iss\.moex\.com)\//;
let fails = 0; const ok = (n, c, x) => { if (!c) fails++; console.log((c ? 'OK   ' : 'FAIL ') + n + ' ' + JSON.stringify(x || '')); };
(async () => {
  const br = await puppeteer.launch({ executablePath: '/usr/bin/google-chrome', headless: 'new', args: ['--no-sandbox'] });
  const pg = await br.newPage(); await pg.setRequestInterception(true);
  let blocked = 0, prox = 0; const bad = [], urls = [];
  pg.on('request', r => { const u = r.url(); if (BLOCK.test(u)) { blocked++; return r.abort(); }
    if (u.startsWith(B) || u.startsWith('data:') || u.startsWith('blob:')) { if (u.includes('/api/mkt/')) { prox++; urls.push(u); } return r.continue(); } r.abort(); });
  pg.on('response', r => { if (r.url().includes('/api/mkt/') && r.status() !== 200) bad.push(r.status() + ' ' + r.url().slice(0, 110)); });
  await pg.goto(B + '/app?debug_user_id=77', { waitUntil: 'domcontentloaded' });
  await new Promise(r => setTimeout(r, 2500));
  const CLOSED = { ru: 'SBER', crypto: 'BTCUSDT' }, OPEN = { ru: 'GAZP', crypto: 'ETHUSDT' };
  const statuses = () => pg.evaluate(() => Object.fromEntries([...document.querySelectorAll('.hw-journal-card')].map(c =>
    [c.querySelector('.hw-j-title').textContent.replace(/^\W+/, '').split(' ')[0], c.querySelector('.hw-j-status').textContent])));
  async function clickRefresh() {
    const u0 = urls.length;
    await pg.evaluate(() => document.querySelector('.hw-j-refresh').click());
    const t0 = Date.now(); await new Promise(r => setTimeout(r, 500));
    while (Date.now() - t0 < 60000) { await new Promise(r => setTimeout(r, 1000));
      if (!(await pg.evaluate(() => document.querySelector('.hw-j-refresh').disabled))) break; }
    return urls.slice(u0);
  }
  for (const mkt of (process.env.E2E_MKTS || 'ru,crypto').split(',')) {   // E2E_MKTS=ru, если Bybit недоступен с машины теста
    await pg.evaluate(m => { localStorage.removeItem('hw_jstatus'); const s = document.getElementById('mkt'); s.value = m; s.dispatchEvent(new Event('change')); }, mkt);
    await new Promise(r => setTimeout(r, 800));
    const a0 = urls.length;
    await pg.evaluate(() => document.getElementById('tabJournal').click());
    await pg.waitForSelector('.hw-j-refresh', { timeout: 20000 });
    await new Promise(r => setTimeout(r, 8000));                         // авто-пересчёт при открытии (нет статусов → считает все)
    const st1 = await statuses(); console.log(mkt, 'auto', JSON.stringify(st1), 'req', urls.length - a0);
    ok(mkt + ': авто посчитал статусы', Object.values(st1).every(s => s && s !== '—'), st1);
    ok(mkt + ': старая сделка закрыта', /тейк|стоп|безубыток|закрыта/.test(st1[CLOSED[mkt]] || ''), st1);
    // закрытой подменяем статус на «тейк +9.99R» (кнопка не должна трогать его и качать её свечи);
    // свежей ставим «активна · БУ» (на выходных у MOEX открытых нет — пятничная отсечка) — кнопка обязана её пересчитать
    await pg.evaluate((cl, op) => { const c = JSON.parse(localStorage.getItem('hw_jstatus') || '{}');
      const id = sym => [...document.querySelectorAll('.hw-journal-card')].find(x => x.querySelector('.hw-j-title').textContent.includes(sym)).getAttribute('data-id');
      c[id(cl)] = { o: 'take', r: 9.99, at: 1 }; c[id(op)] = { o: 'open', r: 0.1, be: true, at: 1 };
      localStorage.setItem('hw_jstatus', JSON.stringify(c)); }, CLOSED[mkt], OPEN[mkt]);
    await pg.evaluate(() => document.getElementById('tabScan').click()); await pg.reload({ waitUntil: 'domcontentloaded' });
    await new Promise(r => setTimeout(r, 2500));
    await pg.evaluate(m => { const s = document.getElementById('mkt'); s.value = m; s.dispatchEvent(new Event('change')); }, mkt);
    await new Promise(r => setTimeout(r, 800)); const a1 = urls.length;
    await pg.evaluate(() => document.getElementById('tabJournal').click());
    await pg.waitForSelector('.hw-j-refresh', { timeout: 20000 }); await new Promise(r => setTimeout(r, 6000));
    const auto2 = urls.slice(a1);
    ok(mkt + ': авто при открытии не качает закрытую', !auto2.some(u => u.includes(CLOSED[mkt])), auto2.length);
    ok(mkt + ': авто при открытии пересчитал открытую', auto2.some(u => u.includes(OPEN[mkt])), auto2.length);
    // свежая «активна · БУ» (пересчитана только что) → авто её пропускает (15 мин), а кнопка обязана пересчитать
    await pg.evaluate((op) => { const c = JSON.parse(localStorage.getItem('hw_jstatus') || '{}');
      const id = [...document.querySelectorAll('.hw-journal-card')].find(x => x.querySelector('.hw-j-title').textContent.includes(op)).getAttribute('data-id');
      c[id] = { o: 'open', r: 0.1, be: true, at: Date.now() }; localStorage.setItem('hw_jstatus', JSON.stringify(c)); }, OPEN[mkt]);
    await pg.reload({ waitUntil: 'domcontentloaded' }); await new Promise(r => setTimeout(r, 2500));
    await pg.evaluate(m => { const s = document.getElementById('mkt'); s.value = m; s.dispatchEvent(new Event('change')); }, mkt);
    await new Promise(r => setTimeout(r, 800)); const a2 = urls.length;
    await pg.evaluate(() => document.getElementById('tabJournal').click());
    await pg.waitForSelector('.hw-j-refresh', { timeout: 20000 }); await new Promise(r => setTimeout(r, 5000));
    ok(mkt + ': авто не трогает свежепересчитанную', urls.length === a2, urls.slice(a2));
    const req = await clickRefresh(); const st2 = await statuses();
    console.log(mkt, 'button', JSON.stringify(st2), req.map(u => u.replace(/^.*\/api\/mkt\//, '').slice(0, 90)));
    ok(mkt + ': кнопка качает свечи открытой', req.some(u => u.includes(OPEN[mkt])), req.length);
    ok(mkt + ': кнопка НЕ качает закрытую', !req.some(u => u.includes(CLOSED[mkt])), req.filter(u => u.includes(CLOSED[mkt])));
    ok(mkt + ': открытая пересчитана (не прежние +0.10R)', st2[OPEN[mkt]] && !/0\.10R/.test(st2[OPEN[mkt]]), st2);
    ok(mkt + ': закрытая сохранила статус', /тейк \+9\.99R/.test(st2[CLOSED[mkt]] || ''), st2);
  }
  ok('прямых запросов к биржам 0', blocked === 0, { blocked });
  ok('ошибок /api/mkt 0', bad.length === 0, bad.slice(0, 5));
  console.log('FAILS', fails); await br.close(); process.exit(fails ? 1 : 0);
})();
