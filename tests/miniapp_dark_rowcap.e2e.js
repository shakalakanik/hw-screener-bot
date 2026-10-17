// Mini App = HTML 7.92 «тёмная» тема всегда (без переключателя, тема Telegram / localStorage игнорируются) + ROWCAP «Показать все».
const puppeteer = require(process.env.PPTR || '/workspace/scratch_hw/node_modules/puppeteer-core');
const B = 'http://127.0.0.1:8811';
const SHOTS = process.env.SHOTS || '/workspace/up40/shots';
let fails = 0; const ok = (n, c, x) => { if (!c) fails++; console.log((c ? 'OK   ' : 'FAIL ') + n + (c ? '' : ' ' + JSON.stringify(x || ''))); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const br = await puppeteer.launch({executablePath: '/usr/bin/google-chrome', headless: 'new', args: ['--no-sandbox']});
  const pg = await br.newPage();
  await pg.setViewport({width: 390, height: 844, isMobile: true, deviceScaleFactor: 2});
  await pg.setRequestInterception(true);
  pg.on('request', r => { const u = r.url(); (u.startsWith(B) || u.startsWith('data:')) ? r.continue() : r.abort(); });
  const errs = []; pg.on('pageerror', e => errs.push(String(e).slice(0, 300)));
  // Telegram в светлой теме со своими цветами — Mini App всё равно тёмная HTML 7.92
  await pg.evaluateOnNewDocument(() => { localStorage.setItem('hwThemeMode', 'light'); window.Telegram = {WebApp: {colorScheme: 'light', themeParams: {bg_color: '#ffffff', text_color: '#111111'}, initData: '', initDataUnsafe: {}, ready(){}, expand(){}, onEvent(){}, offEvent(){}, setHeaderColor(){}, setBackgroundColor(){}, BackButton: {show(){}, hide(){}, onClick(){}, offClick(){}}, MainButton: {show(){}, hide(){}, onClick(){}, offClick(){}, setText(){}}, HapticFeedback: {impactOccurred(){}, notificationOccurred(){}, selectionChanged(){}}}}; });
  await pg.goto(B + '/app?debug_user_id=42', {waitUntil: 'domcontentloaded'});
  await pg.waitForFunction(() => document.getElementById('hwMktBar'), {timeout: 20000}); await sleep(1500);
  const st = await pg.evaluate(() => { const cs = e => getComputedStyle(e); const t = document.querySelector('.tabs');
    return {theme: document.documentElement.getAttribute('data-theme'), bg: cs(document.body).backgroundColor, fg: cs(document.body).color,
      inlineBg: document.documentElement.style.getPropertyValue('--bg'), toggle: !!document.getElementById('themeToggle'),
      watchL: cs(document.querySelector('#tabWatch')).borderTopLeftRadius, testerR: cs(document.querySelector('#tabTester')).borderTopRightRadius, btR: cs(document.querySelector('#tabBt')).borderTopLeftRadius, jR: cs(document.querySelector('#tabJournal')).borderTopLeftRadius, rows: [...new Set(['tabWatch','tabBt','tabTester'].map(i => Math.round(document.getElementById(i).getBoundingClientRect().top)))].length, jRow: document.getElementById('tabJournal').getBoundingClientRect().top > document.getElementById('tabWatch').getBoundingClientRect().top,
      onBg: cs(document.querySelector('.tabs button.on')).backgroundColor, onFg: cs(document.querySelector('.tabs button.on')).color,
      mktOn: cs(document.querySelector('#hwMktBar button.on')).backgroundColor, mktRadius: cs(document.querySelector('#hwMktBar button')).borderTopLeftRadius}; });
  ok('no theme toggle', !st.toggle, st);
  ok('pure black bg / white text (HTML dark), Telegram light ignored', st.bg === 'rgb(0, 0, 0)' && st.fg === 'rgb(255, 255, 255)' && !st.theme && !st.inlineBg, st);
  ok('tabs = joined capsule (Отслеживаю·Шаблоны·Тестировщик, one row) + Журнал pill below', parseFloat(st.watchL) > 100 && parseFloat(st.testerR) > 100 && st.btR === '0px' && parseFloat(st.jR) > 100 && st.rows === 1 && st.jRow, st);
  ok('active tab white/black', st.onBg === 'rgb(255, 255, 255)' && st.onFg === 'rgb(0, 0, 0)', st);
  ok('market switcher pill, active = accent', st.mktOn === 'rgb(255, 255, 255)' && parseFloat(st.mktRadius) > 100, st);

  const lowContrast = () => pg.evaluate(() => {
    const lum = c => { const m = (c.match(/[\d.]+/g) || []).map(Number); if (m.length < 3 || (m.length > 3 && m[3] === 0)) return null; const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }; return .2126 * f(m[0]) + .7152 * f(m[1]) + .0722 * f(m[2]); };
    const bgOf = e => { while (e) { const L = lum(getComputedStyle(e).backgroundColor); if (L != null) return L; e = e.parentElement; } return 0; };
    const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
    return [...document.querySelectorAll('button,label,td,th,h1,h2,h3,.muted,select,input')].filter(e => vis(e) && (e.value || e.textContent || '').trim())
      .filter(e => { const a = lum(getComputedStyle(e).color), c = bgOf(e); return a != null && (Math.max(a, c) + .05) / (Math.min(a, c) + .05) < 2.5; })
      .map(e => e.tagName + ':' + (e.value || e.textContent).trim().slice(0, 25)).slice(0, 8);
  });
  const shot = async (name, label) => { const lc = await lowContrast(); ok(label + ': readable', lc.length === 0, lc); await pg.screenshot({path: SHOTS + '/' + name + '.png'}); };
  const tab = async id => { await pg.evaluate(id => { const b = document.getElementById(id); b.scrollIntoView({block: 'center', inline: 'center'}); b.click(); }, id); await sleep(700); await pg.evaluate(() => window.scrollTo(0, 0)); await sleep(200); };
  const mkt = async m => { await pg.evaluate(m => document.querySelector(`#hwMktBar button[data-m="${m}"]`).click(), m); await sleep(900); };

  await tab('tabJournal'); await shot('01_journal_crypto', 'Журнал (крипта)');
  await pg.evaluate(() => { const c = document.querySelector('#paneJournal .hw-journal-card'); if (c) c.scrollIntoView({block: 'start'}); window.scrollBy(0, -60); }); await sleep(300);
  await shot('02_journal_card', 'Журнал: карточка');
  await mkt('ru'); await pg.evaluate(() => window.scrollTo(0, 0)); await shot('03_journal_moex', 'Журнал (Мосбиржа)');
  await tab('tabWatch'); await shot('04_watch_moex', 'Отслеживаемые (Мосбиржа)');
  await tab('tabBt'); await shot('05_templates_moex_top', 'Шаблоны (Мосбиржа)');
  const seg = await pg.evaluate(() => { const s = [...document.querySelectorAll('.ruModeSeg')].find(e => e.getBoundingClientRect().height > 0); if (!s) return null; s.scrollIntoView({block: 'center'}); return [...s.querySelectorAll('button')].map(b => getComputedStyle(b).borderTopLeftRadius); });
  await sleep(300); if (seg) await shot('06_templates_moex_mode', 'Мосбиржа: режим');
  console.log('ruModeSeg visible:', JSON.stringify(seg));
  await mkt('crypto'); await tab('tabBt'); await shot('07_templates_crypto', 'Шаблоны (крипта)');
  await tab('tabTester'); await shot('08_tester', 'Тестировщик');
  // модалка сохранения шаблона
  const hasModal = await pg.evaluate(() => { const m = document.getElementById('tplSaveModal'); if (!m) return false; m.style.display = 'flex'; return true; });
  if (hasModal) { await sleep(300); await shot('09_modal_tpl', 'Модалка шаблона'); await pg.evaluate(() => { document.getElementById('tplSaveModal').style.display = 'none'; }); }
  await tab('tabWatch'); await shot('10_watch_crypto', 'Отслеживаемые (крипта)');

  // ROWCAP (HTML: renderScan/renderBt; в Mini App панель «Сигналы» скрыта — показываем для проверки)
  const r = await pg.evaluate(() => {
    document.getElementById('paneScan').style.display = ''; document.getElementById('resCard').style.display = '';
    const tb = document.getElementById('tb'); tb.innerHTML = '';
    for (let i = 0; i < ROWCAP(); i++) { const tr = document.createElement('tr'); tr.innerHTML = '<td>r' + i + '</td>'; tb.appendChild(tr); }
    scanMoreRow(tb, 350); const t2 = document.createElement('tbody'); scanMoreRow(t2, 300);
    window.__rs = 0; window.renderScan = () => { window.__rs++; };
    const b = tb.querySelector('.rowsAllBtn'); b.scrollIntoView({block: 'center'});
    return {cap: 300, rows: tb.children.length, btn: b.textContent, none300: t2.children.length === 0};
  });
  ok('ROWCAP 300 + «Показать все 350»', r.rows === 301 && r.btn.includes('Показать все 350') && r.none300, r);
  await pg.click('#tb .rowsAllBtn'); await sleep(100);
  ok('«Показать все» → all rows + re-render', await pg.evaluate(() => ROWCAP() === 1e9 && window.__rs === 1));
  ok('no page errors', errs.length === 0, errs);
  console.log('FAILS', fails); await br.close(); process.exit(fails ? 1 : 0);
})();
