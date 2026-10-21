// Уровень безубытка (HTML v8.01 fmtBE) в карточке журнала и в «Отслеживаемых» — оба рынка.
// Сервер: python tests/miniapp_e2e_serve.py (порт 8811). Сид: вход 100, стоп 98 (LONG) → безубыток 103.000.
// SHOTS=<dir> — сохранить скриншоты.
const puppeteer = require('/workspace/parity/node_modules/puppeteer-core');
const B = process.env.E2E_BASE || 'http://127.0.0.1:8811', SHOTS = process.env.SHOTS;
let fails = 0; const ok = (n, c, x) => { if (!c) fails++; console.log((c ? 'OK   ' : 'FAIL ') + n + (c ? '' : ' ' + JSON.stringify(x || ''))); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const br = await puppeteer.launch({executablePath: '/usr/bin/google-chrome', headless: 'new', args: ['--no-sandbox']});
  const pg = await br.newPage();
  await pg.setViewport({width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2});
  await pg.setRequestInterception(true);
  pg.on('request', r => { const u = r.url(); (u.startsWith(B) || u.startsWith('data:')) ? r.continue() : r.abort(); });
  let pe = 0; pg.on('pageerror', e => { pe++; console.log('pageerror', String(e).slice(0, 300)); });
  const waitApp = async () => { await pg.waitForFunction(() => document.getElementById('hwMktBar'), {timeout: 20000}); await sleep(600); };
  await pg.goto(B + '/app?debug_user_id=42', {waitUntil: 'domcontentloaded'}); await waitApp();
  await pg.evaluate(() => { localStorage.setItem('hw_fbo_watch_all', '[]'); localStorage.removeItem('hw_fbo_watch_tomb'); });
  for (const [slug, m, id] of [['crypto', 'crypto', 9], ['moex', 'ru', 22]]) {
    await pg.goto(`${B}/app/journal/${slug}/${id}?tab=journal&fm=${m}&jid=${id}&debug_user_id=42`, {waitUntil: 'domcontentloaded'});
    await waitApp(); await sleep(2500);
    const j = await pg.evaluate(id => { const c = document.getElementById('jcard-' + id); if (!c) return null;
      const rows = [...c.querySelectorAll('.tradeInfo > div')].map(d => [d.querySelector('span') && d.querySelector('span').textContent, d.querySelector('b') && d.querySelector('b').textContent]);
      return rows; }, id);
    const i = (j || []).findIndex(r => r[0] === 'безубыток');
    ok(`${slug}: journal card has «безубыток 103.000»`, i > 0 && j[i][1] === '103.000', j);
    ok(`${slug}: placed between стоп-лосс and тейк-профит`, i > 0 && j[i - 1][0] === 'стоп-лосс' && j[i + 1][0] === 'тейк-профит', j);
    if (SHOTS) { const el = await pg.$('#jcard-' + id + ' .tradeInfo'); await pg.evaluate(id => document.getElementById('jcard-' + id).scrollIntoView(), id); await sleep(300);
      await (await pg.$('#jcard-' + id)).screenshot({path: `${SHOTS}/journal_${slug}.png`}); }
    await pg.tap(`#jcard-${id} .hw-j-eye`); await sleep(500);
    await pg.evaluate(() => document.getElementById('tabWatch').click()); await sleep(1500);
    const w = await pg.evaluate(() => { const ths = [...document.querySelectorAll('#paneWatch thead th')].map(t => t.textContent.trim());
      const k = ths.indexOf('Безубыток'); const tr = document.querySelector('#wtb tr:not(.detail)');
      return {k, ths, cell: tr && k >= 0 ? tr.children[k].textContent.trim() : null, cls: tr && k >= 0 ? tr.children[k].className : null}; });
    ok(`${slug}: watch row Безубыток = 103.000 (muted, after Стоп)`, w.cell === '103.000' && w.cls === 'muted' && w.ths[w.k - 1] === 'Стоп', w);
    await pg.evaluate(() => { const tr = document.querySelector('#wtb tr:not(.detail)'); tr && tr.click(); }); await sleep(600);
    const d = await pg.evaluate(() => { const x = [...document.querySelectorAll('#wtb tr.detail .tradeInfo > div')].find(e => (e.querySelector('span') || {}).textContent === 'безубыток'); return x && x.querySelector('b').textContent; });
    ok(`${slug}: watch details «безубыток 103.000»`, d === '103.000', d);
    if (SHOTS) { await pg.evaluate(() => document.querySelector('#paneWatch .scroll').scrollIntoView()); await sleep(300);
      await (await pg.$('#paneWatch')).screenshot({path: `${SHOTS}/watch_${slug}.png`});
      await pg.evaluate(() => { const s = document.querySelector('#paneWatch .scroll'); const th = [...s.querySelectorAll('th')].find(t => t.textContent.trim() === 'Стоп'); s.scrollLeft = th.offsetLeft - 20; }); await sleep(200);
      await (await pg.$('#paneWatch .scroll')).screenshot({path: `${SHOTS}/watch_${slug}_be_column.png`}); }
    await pg.evaluate(() => { localStorage.setItem('hw_fbo_watch_all', '[]'); });
  }
  ok('no page errors', pe === 0, pe);
  await br.close(); console.log('FAILS', fails); process.exit(fails ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
