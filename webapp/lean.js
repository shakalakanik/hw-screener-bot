/* lean.js — облегчённый режим Mini App: «Отслеживаю», «Журнал сигналов», «Шаблоны».
 * Ничего не удаляет из screener.html — только прячет лишнее и переиспользует
 * существующий механизм вкладок/шаблонов (низкий риск сломать бэктест-движок,
 * который использует тот же код).
 *
 * «Шаблоны» — это вкладка «Бэктест», лишённая самого прогона бэктеста: в ней
 * полный набор фильтров отбора + выбор стратегии + «Одновременно в рынке,
 * макс», конструктор шаблонов (сохранить/переименовать/удалить), синхронизация
 * с ботом через /api/templates (уже работает, не трогаем).
 *
 * Оставлено «как backtest-only», НЕ переносится в шаблон и не влияет на бота:
 * «Мин. сделок/нед для перебора», «Перебрать комбинации», «Проверка на
 * переобучение» — это метрики по множеству исторических сделок сразу, у
 * одного живого сигнала их не бывает.
 *
 * «Одновременно в рынке, макс» (bt_conc) — ЖИВОЕ ограничение: сохраняется в
 * шаблон как _conc и проверяется ботом перед отправкой (screener.py).
 */
(function () {
  'use strict';

  function hideEl(el) { if (el) el.style.display = 'none'; }
  function hideField(id) {
    var el = document.getElementById(id);
    if (!el) return;
    var wrap = el.closest ? el.closest('.field') : null;
    hideEl(wrap || el);
  }
  function hideById(id) { hideEl(document.getElementById(id)); }

  function ready() {
    return !!(document.getElementById('tabScan') && document.getElementById('tabBt')
      && document.getElementById('tabWatch') && document.getElementById('paneBt')
      && typeof window.currentTplObj === 'function' && typeof window.applyTpl === 'function');
  }

  /* ---- patch currentTplObj / applyTpl: сохранить и восстановить _conc для bt ---- */
  function installConcHooks() {
    if (window.__hwConcHooks) return;
    window.__hwConcHooks = true;

    var _origCur = window.currentTplObj;
    window.currentTplObj = function (pfx) {
      var o = _origCur(pfx);
      if (pfx === 'bt') {
        var concEl = document.getElementById('bt_conc');
        if (concEl) {
          var n = parseInt(concEl.value, 10);
          o._conc = isNaN(n) ? 0 : n;
        }
      }
      return o;
    };

    var _origApply = window.applyTpl;
    window.applyTpl = function (pfx, val) {
      _origApply(pfx, val);
      if (pfx === 'bt' && val) {
        var src = val.indexOf('b:') === 0
          ? null // встроенный шаблон — сброс _conc в 0 (без ограничения)
          : (window.loadTpl ? window.loadTpl()[val.slice(2)] : null);
        var concEl = document.getElementById('bt_conc');
        if (concEl) concEl.value = (src && src._conc != null) ? src._conc : 0;
      }
    };
  }

  function hideBacktestRunUI() {
    // Вводная заметка «Сюда попадают сделки, найденные скринером…»
    var paneBt = document.getElementById('paneBt');
    if (paneBt) {
      var note = paneBt.querySelector('.card .note');
      hideEl(note);
    }
    // Кнопки запуска/поиска/переобучения — не нужны, шаблон просто сохраняем
    ['btRecalc', 'btSplit', 'btSearch', 'btBack', 'btCopyScan', 'btClear'].forEach(hideById);
    // Backtest-only поля, не имеющие смысла для одного живого сигнала
    hideField('bt_minwk');
    hideField('bt_daymax');
    hideField('bt_nostreak');
    hideField('bt_budget');
    // Результаты бэктеста (таблица сделок) — весь второй .card внутри paneBt
    var btkpi = document.getElementById('btkpi');
    if (btkpi) {
      var resultsCard = btkpi.closest('.card');
      hideEl(resultsCard);
    }
  }

  function hideOtherTabs() {
    // «Сигналы» (живой скан из браузера) и «Состояние рынка» — больше не нужны:
    // бот сканирует сам на сервере. Кнопки прячем, панели тоже — TABS-массив
    // в исходном скрипте продолжает существовать и работать, просто эти два
    // пункта больше не кликабельны.
    hideById('tabScan');
    hideById('tabRegime');
    var paneScan = document.getElementById('paneScan');
    var paneRegime = document.getElementById('paneRegime');
    hideEl(paneScan);
    hideEl(paneRegime);
  }

  function renameTemplatesTab() {
    var tabBt = document.getElementById('tabBt');
    if (tabBt) {
      // Оставляем <span id="btCnt"> (его обновляет исходный скрипт), только прячем счётчик
      var first = tabBt.firstChild;
      if (first && first.nodeType === 3) first.textContent = 'Шаблоны ';
      var cnt = document.getElementById('btCnt');
      if (cnt) cnt.style.display = 'none';
    }
    var sect = document.querySelector('#paneBt .sect span');
    if (sect) sect.textContent = 'Все фильтры отбора — «авто» значит фильтр выключен';
  }

  /* Deep link из бота → «Отслеживаю» на конкретной записи.
     Источники (первый найденный):
       ?tab=watch&focus=BASE&ft=<signal_ts_ms>&fm=crypto|ru   (bot.py _app_url_with_sync)
       #tab=watch&focus=…                                      (то же в hash)
       Telegram start_param / ?tgWebAppStartParam=:  watch | w_BASE_TS_MKT | watch-BASE-TS-MKT
     Строки рендерятся асинхронно (облачный hydrate + refreshWatch перерисовывают tbody),
     поэтому ждём строку до ~20 с и повторно подсвечиваем её после каждой перерисовки. */
  function pathWantsWatch() {
    return /\/watch\/?$/.test(location.pathname || '') || /\/app\/watch\b/.test(location.pathname || '');
  }

  function showWatchPane() {
    var panes = ['paneScan', 'paneWatch', 'paneBt', 'paneRegime', 'paneJournal'];
    var tabs = ['tabScan', 'tabWatch', 'tabBt', 'tabRegime', 'tabJournal'];
    var pane = document.getElementById('paneWatch');
    var tab = document.getElementById('tabWatch');
    if (!pane || !tab) return false;
    panes.forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.style.display = id === 'paneWatch' ? '' : 'none';
    });
    tabs.forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.classList.toggle('on', id === 'tabWatch');
    });
    try { if (window.renderWatch) window.renderWatch(); } catch (e) {}
    return true;
  }

  function parseDeepLink() {
    var out = { tab: '', base: '', ft: 0, fm: '' };
    if (pathWantsWatch()) out.tab = 'watch';
    function take(q) {
      if (!q) return;
      if (!out.tab && q.get('tab')) out.tab = q.get('tab');
      if (!out.base && q.get('focus')) out.base = q.get('focus');
      if (!out.ft && q.get('ft')) out.ft = parseInt(q.get('ft'), 10) || 0;
      if (!out.fm && q.get('fm')) out.fm = q.get('fm');
    }
    try { take(new URLSearchParams(location.search || '')); } catch (e) {}
    try { take(new URLSearchParams((location.hash || '').replace(/^#/, ''))); } catch (e) {}
    var sp = '';
    try { sp = (window.Telegram && Telegram.WebApp && Telegram.WebApp.initDataUnsafe
      && Telegram.WebApp.initDataUnsafe.start_param) || ''; } catch (e) {}
    if (!sp) { try { sp = new URLSearchParams(location.search || '').get('tgWebAppStartParam') || ''; } catch (e) {} }
    if (sp) {
      var parts = String(sp).split(/__|[_-]/);
      var head = (parts[0] || '').toLowerCase();
      if (head === 'w' || head === 'watch' || head === 'focus') {
        if (!out.tab) out.tab = 'watch';
        if (!out.base && parts[1]) out.base = parts[1];
        if (!out.ft && parts[2]) out.ft = parseInt(parts[2], 10) || 0;
        if (!out.fm && parts[3]) out.fm = parts[3];
      }
    }
    out.fm = (/^(ru|moex|мосбиржа)$/i.test(out.fm)) ? 'ru' : (out.fm ? 'crypto' : '');
    return (out.tab === 'watch' || out.base) ? out : null;
  }

  function injectFocusCss() {
    if (document.getElementById('hw-focus-css')) return;
    var st = document.createElement('style');
    st.id = 'hw-focus-css';
    st.textContent = '#paneWatch tr.hw-focus{background-color:rgba(76,154,255,.16)}' +
      '#paneWatch tr.hw-focus{outline:2px solid #4c9aff;outline-offset:-2px}';
    document.head.appendChild(st);
  }

  function focusFromLink() {
    if (pathWantsJournal()) return;
    var L = parseDeepLink();
    if (!L) return;
    injectFocusCss();
    var tabWatch = document.getElementById('tabWatch');
    var paneWatch = document.getElementById('paneWatch');
    var userLeft = false, userTouched = false;
    ['tabBt', 'tabScan', 'tabRegime'].forEach(function (id) {
      var b = document.getElementById(id);
      if (b) b.addEventListener('click', function (ev) { if (ev.isTrusted) userLeft = true; });
    });
    ['touchstart', 'wheel', 'keydown'].forEach(function (evn) {
      window.addEventListener(evn, function () { userTouched = true; }, { passive: true, once: true });
    });
    function ensureWatchTab() {
      if (userLeft || !tabWatch || !paneWatch) return;
      // click может не сработать, если слушатель ещё не повешен — ставим вкладку напрямую
      if (paneWatch.style.display === 'none' || !tabWatch.classList.contains('on')) {
        showWatchPane();
        try { tabWatch.click(); } catch (e) {}
      }
    }
    ensureWatchTab();
    if (!L.base) return;
    var want = String(L.base).toUpperCase().replace(/USDT$/, '');
    var sels = L.fm === 'ru' ? ['#wtb_ru', '#wtb_crypto'] : (L.fm === 'crypto' ? ['#wtb_crypto', '#wtb_ru'] : ['#wtb_crypto', '#wtb_ru']);
    function rowBase(tr) {
      var b = tr.getAttribute('data-base');
      if (!b && tr.children.length > 2) b = (tr.children[2].textContent || '').trim();
      return String(b || '').toUpperCase().replace(/USDT$/, '');
    }
    function findRow(loose) {
      var best = null, bestD = Infinity;
      for (var s = 0; s < sels.length; s++) {
        var rows = document.querySelectorAll(sels[s] + ' tr.clickable');
        for (var i = 0; i < rows.length; i++) {
          if (rowBase(rows[i]) !== want) continue;
          var t = parseInt(rows[i].getAttribute('data-t') || '0', 10);
          var d = (L.ft && t) ? Math.abs(t - L.ft) : (L.ft ? Infinity : 0);
          if (d <= 120000) return rows[i];          // точное совпадение по времени сигнала
          if (s === 0 && d < bestD) { best = rows[i]; bestD = d; }
        }
      }
      return loose ? best : null;                    // после ~5 c — ближайшая по времени запись тикера
    }
    var focused = null, firstAt = 0, t0 = Date.now();
    function apply(tr, first) {
      document.querySelectorAll('#paneWatch tr.hw-focus').forEach(function (r) { if (r !== tr) r.classList.remove('hw-focus'); });
      tr.classList.add('hw-focus');
      focused = tr;
      var detailOpen = tr.nextSibling && tr.nextSibling.classList && tr.nextSibling.classList.contains('detail');
      var recent = Date.now() - firstAt < 12000;
      if (first || (recent && !userTouched)) {
        if (!detailOpen) { try { tr.click(); } catch (e) {} }
        try { tr.scrollIntoView({ block: 'center', behavior: first ? 'smooth' : 'auto' }); } catch (e) { tr.scrollIntoView(); }
      }
    }
    var timer = setInterval(function () {
      var el = Date.now() - t0;
      if (userLeft || el > 25000) { clearInterval(timer); return; }
      ensureWatchTab();
      if (focused && document.contains(focused)) return;   // строка жива — ничего не делаем
      var tr = findRow(el > 5000);
      if (!tr) return;
      var first = !firstAt;
      if (first) firstAt = Date.now();
      apply(tr, first);
    }, 250);
    window.__hwFocusDeepLink = L;
  }


  /* Журнал сигналов. Путь /app/journal/crypto|moex[/id] — query Telegram может выкинуть. */
  function startParamRaw() {
    var sp = '';
    try {
      sp = (window.Telegram && Telegram.WebApp && Telegram.WebApp.initDataUnsafe
        && Telegram.WebApp.initDataUnsafe.start_param) || '';
    } catch (e0) {}
    if (!sp) {
      try { sp = new URLSearchParams(location.search || '').get('tgWebAppStartParam') || ''; } catch (e1) {}
    }
    if (!sp) {
      try { sp = new URLSearchParams((location.hash || '').replace(/^#/, '')).get('tgWebAppStartParam') || ''; } catch (e2) {}
    }
    return String(sp || '');
  }

  function journalFromStartParam(sp) {
    // j_c / j_c_12 — крипта, j_m / j_m_12 — Мосбиржа. Так открывает кнопка t.me?startapp=
    var m = String(sp || '').match(/^j_(c|m)(?:_(\d+))?$/i);
    if (!m) return null;
    return {
      market: m[1].toLowerCase() === 'm' ? 'ru' : 'crypto',
      id: m[2] ? (parseInt(m[2], 10) || 0) : 0
    };
  }

  function parseJournalTarget() {
    var market = '', id = 0, fromPath = false;
    var m = (location.pathname || '').match(/\/journal\/(crypto|moex|ru)(?:\/(\d+))?/i);
    if (m) {
      fromPath = true;
      market = /^(moex|ru)$/i.test(m[1]) ? 'ru' : 'crypto';
      id = m[2] ? (parseInt(m[2], 10) || 0) : 0;
    }
    if (!fromPath) {
      var fromStart = journalFromStartParam(startParamRaw());
      if (fromStart) return fromStart;
      try {
        var q = new URLSearchParams(location.search || '');
        if ((q.get('tab') || '') === 'journal') {
          var fm = (q.get('fm') || '').toLowerCase();
          market = (fm === 'ru' || fm === 'moex') ? 'ru' : 'crypto';
          id = parseInt(q.get('jid') || '0', 10) || 0;
        }
      } catch (e) {}
    }
    return market ? { market: market, id: id } : null;
  }

  function pathWantsJournal() { return !!parseJournalTarget(); }

  function injectJournalCss() {
    if (document.getElementById('hw-journal-css')) return;
    var st = document.createElement('style');
    st.id = 'hw-journal-css';
    st.textContent = '#paneJournal .hw-jchart{width:100%;height:auto;display:block;background:#12131a;border-radius:4px;min-height:72px}' +
      '#paneJournal .hw-journal-card.hw-focus{outline:2px solid #4c9aff;outline-offset:-2px;background:rgba(76,154,255,.10)}' +
      '#paneJournal .hw-j-title{font-size:16px;font-weight:700;margin:0 0 6px}' +
      '#tabJournal{white-space:nowrap;flex:1 0 100% !important;order:5}' +
      '.tabs{flex-wrap:wrap !important;overflow:visible !important}';
    document.head.appendChild(st);
  }

  function journalAuthSuffix() {
    var id = '';
    try { id = (window.Telegram && Telegram.WebApp && Telegram.WebApp.initData) || ''; } catch (e0) {}
    if (id) return 'initData=' + encodeURIComponent(id);
    var tok = '';
    try {
      var ls = localStorage.getItem('hw_fbo_sync_url') || localStorage.getItem('hw_bot_sync_url') || '';
      var mm = String(ls).match(/\/sync\/([A-Za-z0-9_-]+)/);
      if (mm) tok = mm[1];
    } catch (e1) {}
    if (!tok) {
      try {
        var q = new URLSearchParams(location.search || '');
        tok = q.get('token') || '';
        if (!tok) {
          var sync = q.get('sync') || '';
          var m2 = String(sync).match(/\/sync\/([A-Za-z0-9_-]+)/);
          if (m2) tok = m2[1];
        }
        if (tok) return 'token=' + encodeURIComponent(tok);
        var dbg = q.get('debug_user_id');
        if (dbg) return 'debug_user_id=' + encodeURIComponent(dbg);
      } catch (e2) {}
    } else {
      return 'token=' + encodeURIComponent(tok);
    }
    return '';
  }

  function journalAuthUrl(path) {
    var s = journalAuthSuffix();
    if (!s) return path;
    return path + (path.indexOf('?') >= 0 ? '&' : '?') + s;
  }

  function jEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (ch) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch];
    });
  }
  function jFmt(v) {
    if (v == null || v === '' || isNaN(Number(v))) return '—';
    var n = Number(v), a = Math.abs(n);
    if (a >= 1000) return n.toFixed(1);
    if (a >= 1) return n.toFixed(3);
    if (a >= 0.01) return n.toFixed(5);
    return n.toPrecision(4);
  }
  function jMsk(ms) {
    if (!ms) return '—';
    try {
      return new Date(Number(ms)).toLocaleString('ru-RU', {
        timeZone: 'Europe/Moscow', day: '2-digit', month: '2-digit',
        hour: '2-digit', minute: '2-digit'
      }) + ' МСК';
    } catch (e) { return '—'; }
  }
  function jStars(n) {
    var k = parseInt(n, 10);
    if (isNaN(k)) k = 0;
    if (k < 0) k = 0;
    if (k > 5) k = 5;
    var s = '';
    for (var i = 0; i < 5; i++) s += (i < k) ? '★' : '☆';
    return s;
  }

  function journalCardHtml(row) {
    var c = row.card || {};
    var side = String(c.side || '');
    var emoji = side === 'SHORT' ? '🔴' : '🟢';
    var entry = (c.last != null ? c.last : c.entry);
    var stop = c.stop, take = c.take, level = c.level;
    var eN = Number(entry), sN = Number(stop), tN = Number(take);
    var risk = (eN && !isNaN(eN) && !isNaN(sN)) ? Math.abs(eN - sN) / Math.abs(eN) * 100 : 0;
    var tp = (eN && !isNaN(eN) && !isNaN(tN)) ? Math.abs(tN - eN) / Math.abs(eN) * 100 : 0;
    var dist = (c.dist_atr != null && !isNaN(Number(c.dist_atr))) ? Number(c.dist_atr) * 100 : null;
    var strat = c.strategy === 'brk' ? '📈 Пробой' : '🔻 Ложный пробой';
    var prob = (c.strategy !== 'brk' && c.prob != null && c.prob !== '')
      ? '  (модель p=' + Number(c.prob).toFixed(2) + ')' : '';
    var mkt = row.market === 'ru' ? '🇷🇺 Мосбиржа' : '🌐 Крипта';
    var tpl = c.matched_template ? '<div><span>шаблон</span> <b>' + jEsc(c.matched_template) + '</b></div>' : '';
    var score = (c.score != null && c.score !== '')
      ? ('⭐ Оценка: <b>' + Number(c.score).toFixed(1) + '</b> / 10')
      : ('💪 Сила: <b>' + jStars(c.strength) + '</b>');
    var biasMap = { up: '↑ up', down: '↓ down', flat: '→ flat' };
    var bias = biasMap[c.d1_bias] || '→ flat';
    var charts = row.charts || {};
    function box(title, src, extra) {
      var cls = 'chartBox' + (extra ? ' ' + extra : '');
      if (!src) return '<div class="' + cls + '"><h4>' + title + '</h4><div class="note">Скриншот недоступен</div></div>';
      var full = journalAuthUrl(src);
      return '<div class="' + cls + '"><h4>' + title + '</h4><img class="hw-jchart" alt="' + jEsc(title) + '" loading="lazy" data-src="' + jEsc(full) + '"></div>';
    }
    return '<article class="card hw-journal-card" id="jcard-' + row.id + '" data-id="' + row.id + '" data-market="' + jEsc(row.market) + '">' +
      '<div class="hw-j-title">' + emoji + ' ' + jEsc(row.ticker || c.ticker || '') + ' — ' + jEsc(side || '—') + '</div>' +
      '<div class="tradeInfo">' +
        '<div><span>рынок</span> <b>' + mkt + ' · ' + strat + jEsc(prob) + '</b></div>' +
        tpl +
        '<div><span>уровень</span> <b>' + jFmt(level) + (c.kind ? ' [' + jEsc(c.kind) + ']' : '') + '</b></div>' +
        '<div><span>вход</span> <b>' + jFmt(entry) + (dist == null ? '' : '  (расст. ' + dist.toFixed(0) + '% ATR)') + '</b></div>' +
        '<div><span>стоп-лосс</span> <b class="stop">' + jFmt(stop) + '  (−' + risk.toFixed(1) + '%)</b></div>' +
        '<div><span>тейк-профит</span> <b class="take">' + jFmt(take) + '  (+' + tp.toFixed(1) + '%)</b></div>' +
        '<div><span>оценка</span> ' + score + '</div>' +
        '<div><span>тренд D1</span> <b>' + jEsc(bias) + '</b></div>' +
        '<div><span>сигнал</span> <b>' + jEsc(jMsk(c.signal_ts || row.signal_ts)) + '</b></div>' +
      '</div>' +
      '<div class="charts">' +
        box('Дневной (1D)', charts.d1) +
        box('Часовой (1H)', charts.h1) +
        box('5-минутный (5M)', charts.m5, 'c3') +
      '</div>' +
      '<div class="legend">' +
        '<span><i style="background:#ffd54f"></i>уровень</span>' +
        '<span><i style="background:#b0b6c8"></i>вход</span>' +
        '<span><i style="background:#ef5350"></i>стоп</span>' +
        '<span><i style="background:#26a69a"></i>тейк</span>' +
        '<span><i style="background:#7aa2f7"></i>бар сигнала</span>' +
      '</div></article>';
  }

  function journalSectionHtml(market, title, rows) {
    var inner = '';
    if (!rows || !rows.length) {
      inner = '<div class="note">Пока нет сигналов, отправленных в чат.</div>';
    } else {
      for (var i = 0; i < rows.length; i++) inner += journalCardHtml(rows[i]);
    }
    return '<div class="card" id="journalSec-' + market + '"><div class="sect"><span>' + title + '</span></div>' + inner + '</div>';
  }

  function armJournalCharts(root) {
    var imgs = root.querySelectorAll('img.hw-jchart');
    for (var i = 0; i < imgs.length; i++) {
      (function (img) {
        var tries = 0;
        var base = img.getAttribute('data-src') || '';
        img.addEventListener('error', function () {
          tries++;
          if (tries > 8) {
            var note = document.createElement('div');
            note.className = 'note';
            note.textContent = 'Скриншот недоступен';
            if (img.parentNode) img.replaceWith(note);
            return;
          }
          setTimeout(function () {
            if (!img.isConnected) return;
            var join = base.indexOf('?') >= 0 ? '&' : '?';
            img.src = base + join + 'r=' + tries;
          }, 1600);
        });
        img.src = base;
      })(imgs[i]);
    }
  }

  function focusJournalCard() {
    var t = parseJournalTarget();
    if (!t) return;
    var card = t.id ? document.getElementById('jcard-' + t.id) : null;
    if (card) card.classList.add('hw-focus');
    var el = card || document.getElementById('journalSec-' + t.market);
    if (el && el.scrollIntoView) {
      try { el.scrollIntoView({ block: 'start', behavior: 'smooth' }); }
      catch (e) { try { el.scrollIntoView(); } catch (e2) {} }
    }
  }

  var journalSeq = 0;
  function renderJournal(data) {
    var body = document.getElementById('journalBody');
    if (!body) return;
    body.dataset.loaded = '1';
    var t = parseJournalTarget();
    var crypto = (data && data.crypto) || [];
    var ru = (data && data.ru) || [];
    var html;
    if (t && t.market === 'ru') {
      html = journalSectionHtml('ru', 'Мосбиржа', ru) + journalSectionHtml('crypto', 'Крипта', crypto);
    } else {
      html = journalSectionHtml('crypto', 'Крипта', crypto) + journalSectionHtml('ru', 'Мосбиржа', ru);
    }
    body.innerHTML = html;
    armJournalCharts(body);
    focusJournalCard();
  }

  function loadJournal() {
    var seq = ++journalSeq;
    var body = document.getElementById('journalBody');
    if (body && !body.dataset.loaded) body.innerHTML = '<div class="note">Загрузка…</div>';
    fetch(journalAuthUrl('/api/journal'), { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        if (seq !== journalSeq) return;
        renderJournal(data || {});
      })
      .catch(function () {
        if (seq !== journalSeq) return;
        if (body) body.innerHTML = '<div class="card"><div class="note">Не удалось загрузить журнал. Откройте Mini App из Telegram ещё раз.</div></div>';
      });
  }

  var journalUserLeft = false;

  function hideJournalChrome() {
    var pane = document.getElementById('paneJournal');
    var tab = document.getElementById('tabJournal');
    if (pane) pane.style.display = 'none';
    if (tab) tab.classList.remove('on');
  }

  function showJournalPane() {
    if (!document.getElementById('paneJournal')) installJournalUI();
    var panes = ['paneScan', 'paneWatch', 'paneBt', 'paneRegime', 'paneJournal'];
    var tabs = ['tabScan', 'tabWatch', 'tabBt', 'tabRegime', 'tabJournal'];
    panes.forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.style.display = id === 'paneJournal' ? '' : 'none';
    });
    tabs.forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.classList.toggle('on', id === 'tabJournal');
    });
    loadJournal();
  }

  function installJournalUI() {
    if (document.getElementById('tabJournal')) return;
    var tabsEl = document.querySelector('.tabs');
    var watch = document.getElementById('tabWatch');
    if (!tabsEl || !watch) return;
    injectJournalCss();
    var btn = document.createElement('button');
    btn.id = 'tabJournal';
    btn.type = 'button';
    btn.textContent = 'Журнал сигналов';
    if (watch.nextSibling) tabsEl.insertBefore(btn, watch.nextSibling);
    else tabsEl.appendChild(btn);
    var pane = document.createElement('div');
    pane.id = 'paneJournal';
    pane.style.display = 'none';
    pane.innerHTML = '<div class="card"><div class="note">Здесь только сигналы, которые бот отправил в Telegram: та же карточка (вход, стоп-лосс, тейк-профит и остальные поля) и три графика — D1, H1 и 5m. Ручной скан Mini App без сообщения в чат в журнал не пишется. Сначала новые.</div></div><div id="journalBody"></div>';
    var anchor = document.getElementById('paneWatch');
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(pane, anchor.nextSibling);
    else (document.querySelector('.wrap') || document.body).appendChild(pane);
    document.addEventListener('click', function (ev) {
      var node = ev.target;
      if (!node || !node.closest) return;
      if (!node.closest('#tabJournal')) return;
      ev.preventDefault();
      ev.stopPropagation();
      journalUserLeft = false;
      showJournalPane();
    }, true);
    ['tabScan', 'tabWatch', 'tabBt', 'tabRegime'].forEach(function (id) {
      var b = document.getElementById(id);
      if (!b) return;
      b.addEventListener('click', function (ev) {
        if (ev.isTrusted) journalUserLeft = true;
        hideJournalChrome();
      });
    });
  }

  function selectDefaultTab() {
    // «Посмотреть сигнал» → /app/journal/crypto|moex (путь, не query).
    if (pathWantsJournal()) {
      showJournalPane();
      return;
    }
    // Старые карточки всё ещё открывают /app/watch.
    if (pathWantsWatch() || (parseDeepLink() && parseDeepLink().tab === 'watch')) {
      showWatchPane();
      return;
    }
    var tabWatch = document.getElementById('tabWatch');
    if (tabWatch) tabWatch.click();
  }

  function enforceJournalOpen() {
    // start_param иногда появляется на кадр позже, а «Отслеживаю» успевает перебить вкладку.
    var t0 = Date.now();
    var timer = setInterval(function () {
      if (journalUserLeft || Date.now() - t0 > 4000) { clearInterval(timer); return; }
      if (!parseJournalTarget()) return;
      var pane = document.getElementById('paneJournal');
      var tab = document.getElementById('tabJournal');
      if (!pane || !tab || pane.style.display === 'none' || !tab.classList.contains('on')) {
        showJournalPane();
      }
    }, 200);
  }

  function start() {
    var n = 0;
    var t = setInterval(function () {
      n++;
      if (ready() || n > 100) {
        clearInterval(t);
        if (!ready()) { console.warn('[lean] screener DOM not ready, giving up'); return; }
        installConcHooks();
        hideBacktestRunUI();
        hideOtherTabs();
        renameTemplatesTab();
        installJournalUI();
        selectDefaultTab();
        focusFromLink();
        enforceJournalOpen();
        try { if (window.Telegram && Telegram.WebApp && Telegram.WebApp.expand) Telegram.WebApp.expand(); } catch (e) {}
      }
    }, 50);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
