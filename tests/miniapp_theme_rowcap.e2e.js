// HTML 7.92 UI → Mini App: переключатель темы (тёмная/светлая, localStorage hwThemeMode, по умолчанию — colorScheme Telegram) и ROWCAP/«Показать все».
const puppeteer = require('/workspace/scratch_hw/node_modules/puppeteer-core');
const B = 'http://127.0.0.1:8811';
const SHOTS = process.env.SHOTS || '/workspace/up39/shots';
let fails = 0; const ok = (n, c, x) => { if (!c) fails++; console.log((c ? 'OK   ' : 'FAIL ') + n + (c ? '' : ' ' + JSON.stringify(x || ''))); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const br = await puppeteer.launch({executablePath: '/usr/bin/google-chrome', headless: 'new', args: ['--no-sandbox']});
  const pg = await br.newPage();
  await pg.setViewport({width: 390, height: 844, isMobile: true, deviceScaleFactor: 2});
  await pg.setRequestInterception(true);
  pg.on('request', r => { const u = r.url(); (u.startsWith(B) || u.startsWith('data:')) ? r.continue() : r.abort(); });
  const errs = []; pg.on('pageerror', e => errs.push(String(e).slice(0, 300)));
  const waitApp = async () => { await pg.waitForFunction(() => document.getElementById('hwMktBar') && document.getElementById('themeToggle'), {timeout: 20000}); await sleep(700); };
  const theme = () => pg.evaluate(() => ({t: document.documentElement.getAttribute('data-theme'), lbl: document.getElementById('themeToggle').textContent,
    bg: getComputedStyle(document.body).backgroundColor, vis: (() => { const r = document.getElementById('themeToggle').getBoundingClientRect(); return r.width > 0 && r.height > 0; })()}));

  // 1) Telegram colorScheme=light как тема по умолчанию (нет сохранённого выбора)
  await pg.evaluateOnNewDocument(() => { if (!sessionStorage.getItem('noTG')) window.Telegram = {WebApp: {colorScheme: 'light', themeParams: {}, initData: '', initDataUnsafe: {}, ready(){}, expand(){}, onEvent(){}, offEvent(){}, BackButton: {show(){}, hide(){}, onClick(){}, offClick(){}}, MainButton: {show(){}, hide(){}, onClick(){}, offClick(){}, setText(){}}, HapticFeedback: {impactOccurred(){}, notificationOccurred(){}, selectionChanged(){}}}}; });
  await pg.goto(B + '/app?debug_user_id=42', {waitUntil: 'domcontentloaded'});
  await pg.evaluate(() => localStorage.removeItem('hwThemeMode')); await pg.reload({waitUntil: 'domcontentloaded'}); await waitApp();
  let s = await theme(); ok('default follows Telegram colorScheme=light', s.t === 'light' && s.lbl.includes('Светлая'), s);
  ok('toggle visible', s.vis, s);
  // 2) клик → тёмная, сохранено
  await pg.click('#themeToggle'); await sleep(200); s = await theme();
  ok('click → dark', s.t === 'dark' && s.lbl.includes('Тёмная') && s.bg === 'rgb(18, 19, 26)', s);
  ok('saved in localStorage', await pg.evaluate(() => localStorage.getItem('hwThemeMode')) === 'dark');
  // 3) перезагрузка: выбор важнее Telegram light
  await pg.reload({waitUntil: 'domcontentloaded'}); await waitApp(); s = await theme();
  ok('reload keeps user choice over Telegram', s.t === 'dark', s);
  await pg.click('#themeToggle'); await sleep(200); s = await theme();
  ok('click → light (HTML palette #f5f6f7)', s.t === 'light' && s.bg === 'rgb(245, 246, 247)', s);

  // В Mini App вкладка «Сигналы» (paneScan) скрыта lean.js — для проверки показываем панель вручную
  await pg.evaluate(() => { document.getElementById('paneScan').style.display = ''; }); await sleep(100);
  const lowContrast = () => pg.evaluate(() => {
    const lum = c => { const m = c.match(/[\d.]+/g).map(Number); if (m.length > 3 && m[3] === 0) return null; const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }; return .2126 * f(m[0]) + .7152 * f(m[1]) + .0722 * f(m[2]); };
    const bgOf = e => { while (e) { const L = lum(getComputedStyle(e).backgroundColor); if (L != null) return L; e = e.parentElement; } return lum(getComputedStyle(document.body).backgroundColor); };
    return [...document.querySelectorAll('button')].filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0 && b.textContent.trim(); })
      .filter(b => { const a = lum(getComputedStyle(b).color), c = bgOf(b); const k = (Math.max(a, c) + .05) / (Math.min(a, c) + .05); return k < 2.5; }).map(b => b.textContent.trim().slice(0, 30));
  });
  ok('light: all visible buttons readable', (await lowContrast()).length === 0, await lowContrast());
  await pg.screenshot({path: SHOTS + '/light_journal.png'});
  await pg.click('#themeToggle'); await sleep(200);
  ok('dark: all visible buttons readable', (await lowContrast()).length === 0, await lowContrast());
  await pg.screenshot({path: SHOTS + '/dark_journal.png'});
  await pg.click('#themeToggle'); await sleep(200);
  // 4) ROWCAP: 300 строк + строка «Показать все N», клик → все строки
  const r = await pg.evaluate(() => {
    const out = {cap0: ROWCAP()};
    const tb = document.getElementById('tb'); tb.innerHTML = '';
    for (let i = 0; i < Math.min(350, ROWCAP()); i++) { const tr = document.createElement('tr'); tr.innerHTML = '<td>r' + i + '</td>'; tb.appendChild(tr); }
    scanMoreRow(tb, 350);
    out.rows = tb.querySelectorAll('tr').length; out.btn = (tb.querySelector('.rowsAllBtn') || {}).textContent || '';
    out.noBtnSmall = (() => { const t2 = document.createElement('tbody'); scanMoreRow(t2, 300); return t2.children.length === 0; })();
    window.__rs = 0; window.renderScan = () => { window.__rs++; };
    return out;
  });
  ok('cap 300 + «Показать все 350» row', r.cap0 === 300 && r.rows === 301 && r.btn.includes('Показать все 350'), r);
  ok('no «ещё» row when n ≤ 300', r.noBtnSmall, r);
  await pg.evaluate(() => { document.getElementById('resCard').style.display = ''; document.querySelector('#tb .rowsAllBtn').scrollIntoView({block: 'center'}); });
  const hit = await pg.evaluate(() => { const b = document.querySelector('#tb .rowsAllBtn'); const r = b.getBoundingClientRect(); const e = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2); return e === b ? 'ok' : (e ? e.outerHTML.slice(0, 120) : 'null'); });
  console.log('hit', hit);
  await pg.click('#tb .rowsAllBtn'); await sleep(100);
  const r2 = await pg.evaluate(() => ({all: ROWCAP(), rs: window.__rs}));
  ok('click «Показать все» → ROWS_ALL, re-render scan', r2.all === 1e9 && r2.rs === 1, r2);
  ok('renderScan/renderBt use ROWCAP()', await pg.evaluate(() => (document.documentElement.innerHTML.match(/slice\(0,ROWCAP\(\)\)/g) || []).length === 2 && (document.documentElement.innerHTML.match(/scanMoreRow\(tb,rows.length\)/g) || []).length === 2));

  // скриншоты: демонстрационные строки + «ещё»
  await pg.evaluate(() => {
    const tb = document.getElementById('tb'); tb.innerHTML = '';
    const mk = (i, d) => { const tr = document.createElement('tr'); tr.className = 'clickable'; tr.innerHTML = `<td class="muted">▸</td><td>👁</td><td>COIN${i}</td><td class="muted">17.10, 1${i}:00</td><td class="${d ? 'take' : 'stop'}">${d ? 'LONG' : 'SHORT'}</td><td class="muted">ЛП · prev high</td><td>1.234</td>`; tb.appendChild(tr); };
    for (let i = 0; i < 5; i++) mk(i, i % 2);
    const tr = document.createElement('tr'); tr.innerHTML = `<td colspan="20" class="muted" style="padding:10px">Показано 300 самых свежих из 412 сигналов (число и статистика выше — по всем). <button class="ghost rowsAllBtn" style="padding:3px 10px;font-size:12px">Показать все 412</button></td>`; tb.appendChild(tr);
    const rc = document.getElementById('resCard'); if (rc) rc.style.display = '';
    const sp = document.getElementById('paneScan'); if (sp) sp.style.display = '';
  });
  await pg.evaluate(() => window.scrollTo(0, 0)); await sleep(200);
  await pg.screenshot({path: SHOTS + '/light_top.png'});
  await pg.evaluate(() => document.getElementById('tb').closest('.card').scrollIntoView()); await sleep(200);
  await pg.screenshot({path: SHOTS + '/light_rows.png'});
  await pg.click('#themeToggle'); await sleep(200);
  await pg.evaluate(() => window.scrollTo(0, 0)); await sleep(200);
  await pg.screenshot({path: SHOTS + '/dark_top.png'});
  await pg.evaluate(() => document.getElementById('tb').closest('.card').scrollIntoView()); await sleep(200);
  await pg.screenshot({path: SHOTS + '/dark_rows.png'});
  ok('no page errors', errs.length === 0, errs);
  console.log('FAILS', fails); await br.close(); process.exit(fails ? 1 : 0);
})();
