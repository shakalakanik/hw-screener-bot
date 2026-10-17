// Телефонная раскладка Mini App (HTML 7.92 «тёмная»): вкладки — одна строка из 3 равных сегментов, KPI без внутренней прокрутки.
// Ширины 320–412, масштаб шрифта 1 / 1.15 / 1.3 (CSS zoom ≈ «крупный шрифт» Android), шрифты Roboto и широкий DejaVu Sans; обе биржи.
const puppeteer = require(process.env.PPTR || '/workspace/scratch_hw/node_modules/puppeteer-core');
const B = 'http://127.0.0.1:8811';
const SHOTS = process.env.SHOTS || '/workspace/up41/shots';
let fails = 0; const ok = (n, c, x) => { if (!c) fails++; if (!c || process.env.V) console.log((c ? 'OK   ' : 'FAIL ') + n + (c ? '' : ' ' + JSON.stringify(x || ''))); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const br = await puppeteer.launch({executablePath: '/usr/bin/google-chrome', headless: 'new', args: ['--no-sandbox']});
  let n = 0;
  for (const font of ['Roboto', 'DejaVu Sans']) for (const w of [320, 340, 360, 375, 390, 412]) for (const z of [1, 1.15, 1.3]) {
    const pg = await br.newPage();
    await pg.setViewport({width: w, height: 800, isMobile: true, deviceScaleFactor: 2});
    await pg.setRequestInterception(true);
    pg.on('request', r => { const u = r.url(); (u.startsWith(B) || u.startsWith('data:')) ? r.continue() : r.abort(); });
    await pg.evaluateOnNewDocument((font, z) => { document.addEventListener('DOMContentLoaded', () => {
      const s = document.createElement('style'); s.textContent = `html{zoom:${z}} body,button,input,select{font-family:"${font}",sans-serif !important}`; document.head.appendChild(s); }); }, font, z);
    await pg.goto(B + '/app?debug_user_id=42', {waitUntil: 'domcontentloaded'});
    await pg.waitForFunction(() => document.getElementById('tabJournal'), {timeout: 20000}); await sleep(900);
    const tag = `${font} w=${w} z=${z}`;
    for (const m of ['crypto', 'ru']) {
      await pg.evaluate(m => document.querySelector(`#hwMktBar button[data-m="${m}"]`).click(), m); await sleep(500);
      for (const t of ['tabWatch', 'tabTester']) {
        await pg.evaluate(t => document.getElementById(t).click(), t); await sleep(400);
        if (t === 'tabTester') await pg.evaluate(() => {   // в тестовой БД тестировщик пуст — вставляем KPI как у renderTester/сценариев (длинные значения)
          const p = document.getElementById('paneTester'); if (p.querySelector('.kpi.__t')) return;
          const d = document.createElement('div'); d.className = 'kpi __t';
          d.innerHTML = [['Сделок', '128'], ['Винрейт (тейк/стоп)', '41%'], ['Итог закрытых', '+37.5R'], ['Открыто сейчас', '3'], ['Средний стоп', '1.84%'],
            ['Винрейт: лучший … медиана … худший', '<b style="font-size:16px">56% … 50% … 40%</b>'], ['Макс. стопов подряд', '<b style="font-size:16px">3 … 5 … 9</b>'], ['Итог (R)', '+52.0 … +31.5 … −4.0']]
            .map(([l, v]) => `<div><label>${l}</label>${v.startsWith('<b') ? v : '<b>' + v + '</b>'}</div>`).join('');
          p.insertBefore(d, p.firstChild);
        });
        const r = await pg.evaluate(() => {
          const ids = ['tabWatch', 'tabBt', 'tabTester'], R = ids.map(i => document.getElementById(i).getBoundingClientRect());
          const J = document.getElementById('tabJournal').getBoundingClientRect(), T = document.querySelector('.tabs').getBoundingClientRect();
          const kp = [...document.querySelectorAll('.kpi')].filter(k => k.getBoundingClientRect().height > 0);
          return {rows: new Set(R.map(x => Math.round(x.top))).size, eq: Math.max(...R.map(x => x.width)) - Math.min(...R.map(x => x.width)),
            inside: R.every(x => x.left >= T.left - 1 && x.right <= T.right + 1), jBelow: J.top > R[0].bottom - 1,
            clipped: ids.filter(i => { const b = document.getElementById(i); return b.scrollWidth > b.clientWidth + 1; }),
            kpi: kp.map(k => ({id: k.id, sw: k.scrollWidth, cw: k.clientWidth, cellOv: [...k.children].filter(c => c.scrollWidth > c.clientWidth + 1).length})),
            docOv: document.documentElement.scrollWidth - document.documentElement.clientWidth};
        });
        n++;
        ok(`${tag} ${m} ${t}: tabs one row, equal, inside capsule, Журнал below`, r.rows === 1 && r.eq < 2 && r.inside && r.jBelow, r);
        ok(`${tag} ${m} ${t}: tab labels not truncated`, r.clipped.length === 0, r.clipped);
        ok(`${tag} ${m} ${t}: KPI present, no inner scroll / cell overflow`, r.kpi.length > 0 && r.kpi.every(k => k.sw <= k.cw + 1 && k.cellOv === 0), r.kpi);
        ok(`${tag} ${m} ${t}: no page horizontal overflow`, r.docOv <= 1, r.docOv);
        if (w === 360 && z === 1.3 && font === 'Roboto') {
          await pg.evaluate(() => { const k = [...document.querySelectorAll('.kpi')].find(k => k.getBoundingClientRect().height > 0); window.scrollTo(0, 0); if (k && k.getBoundingClientRect().bottom > innerHeight) k.scrollIntoView({block: 'center'}); }); await sleep(200);
          await pg.screenshot({path: `${SHOTS}/360_z1.3_${m}_${t === 'tabWatch' ? 'watch' : 'tester'}.png`});
        }
        if (w === 360 && z === 1 && font === 'Roboto') { await pg.evaluate(() => window.scrollTo(0, 0)); await pg.screenshot({path: `${SHOTS}/360_z1_${m}_${t === 'tabWatch' ? 'watch' : 'tester'}.png`}); }
      }
    }
    await pg.close();
  }
  console.log('cases', n, 'FAILS', fails); await br.close(); process.exit(fails ? 1 : 0);
})();
