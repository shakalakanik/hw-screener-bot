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
    // Новый HTML: одна таблица #wtb на текущий рынок (crypto / ru / algo) — переключаем рынок сигнала
    try {
      var mk = document.getElementById('mkt');
      if (L.fm && mk && mk.value !== L.fm) { mk.value = L.fm; mk.dispatchEvent(new Event('change')); }
    } catch (eM) {}
    var sels = ['#wtb'].concat(L.fm === 'ru' ? ['#wtb_ru', '#wtb_crypto'] : ['#wtb_crypto', '#wtb_ru']);
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
    var m = String(sp || '').match(/^j_(c|m|a)(?:_(\d+))?$/i);
    if (!m) return null;
    var c = m[1].toLowerCase();
    return {
      market: c === 'm' ? 'ru' : 'crypto',   // 16.10: «Крипта (Алго)» убрана
      id: m[2] ? (parseInt(m[2], 10) || 0) : 0
    };
  }

  function parseJournalTarget() {
    var market = '', id = 0, fromPath = false;
    var m = (location.pathname || '').match(/\/journal\/(crypto|moex|ru|algo)(?:\/(\d+))?/i);
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
      // 👁 «Отслеживаемые» в правом верхнем углу карточки журнала (штатный .eye: .35 / .on = 1)
      '#paneJournal .hw-journal-card{position:relative}' +
      '#paneJournal .hw-journal-card .hw-j-title{padding-right:40px}' +
      '#paneJournal .hw-j-eye{position:absolute;top:6px;right:6px;width:36px;height:36px;display:flex;align-items:center;' +
      'justify-content:center;font-size:18px;line-height:1;border-radius:8px;-webkit-tap-highlight-color:transparent;z-index:1}' +
      '#paneJournal .hw-j-eye.on{background:rgba(76,154,255,.18)}' +
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

  /* ---- 👁 на карточке журнала → штатный toggleWatch() скринера ----
   * Та же запись, что кладёт бот (storage.watchlist_item_html_shape): base/sym/t/d/lv/e/st/tk…
   * toggleWatch (обёрнут bridge.js: tombstone при снятии) пишет в hw_fbo_watch_all с mkt=MKT
   * и через __hwOnWatchSave синхронизирует с сервером (per Telegram-аккаунт). */
  var jWatchItems = {};
  function journalWatchItem(row) {
    var c = row.card || {};
    var m = row.market === 'ru' ? 'ru' : 'crypto';
    var tick = String(row.ticker || c.ticker || '');
    var base = m === 'ru' ? tick : tick.replace(/USDT$/i, '');
    if (!base) return null;
    var ts = parseInt(c.signal_ts || row.signal_ts || 0, 10);
    if (!ts) return null;
    if (ts < 1e12) ts *= 1000;   // секунды → мс
    // HTML 7.58: сигнал с середины часа — в карточке signal_ts = bar_ts − 30 мин, в движке t = bar_ts и _src = 30
    var src30 = !!c.src30 && c.bar_ts;
    if (src30) { ts = parseInt(c.bar_ts, 10); if (ts < 1e12) ts *= 1000; }
    var side = String(c.side || '').toUpperCase() === 'SHORT' ? 'SHORT' : 'LONG';
    var n = function (v) { var x = Number(v); return isNaN(x) ? 0 : x; };
    var e = n(c.last != null ? c.last : c.entry), st = n(c.stop);
    return {
      mkt: m, market: m, type: c.strategy || (m === 'algo' ? 'algo' : 'brk'),
      base: base, sym: m === 'ru' ? base : base + 'USDT', t: ts,
      d: side === 'LONG' ? 1 : 0, side: side,
      lv: n(c.level), k: c.kind || '', p: n(c.prob), e: e, st: st, tk: n(c.take),
      rp: e ? Math.abs(e - st) / Math.abs(e) : 0,
      strength: c.strength != null ? c.strength : 3, crosses: 0, vol_mult: 1,
      score: (c.score != null && c.score !== '' && !isNaN(Number(c.score))) ? Number(c.score) : null,
      _src: src30 ? 30 : undefined, sw: c.sw === 2 ? 2 : undefined,
      swm: (c.swm === 0 || c.swm === 1) ? c.swm : undefined
    };
  }
  function journalIsWatched(t) {
    try { return typeof window.isWatched === 'function' && !!window.isWatched(t); } catch (e) { return false; }
  }
  function journalEyeHtml(row) {
    var t = journalWatchItem(row);
    if (!t) return '';
    jWatchItems[row.id] = t;
    var on = journalIsWatched(t);
    return '<span class="eye hw-j-eye' + (on ? ' on' : '') + '" role="button" tabindex="0" aria-pressed="' + on + '"' +
      ' aria-label="Отслеживать" data-jid="' + jEsc(row.id) + '" data-base="' + jEsc(t.base) + '" data-t="' + t.t + '">👁</span>';
  }
  function onJournalEye(ev) {
    var eye = ev.target && ev.target.closest ? ev.target.closest('.hw-j-eye') : null;
    if (!eye) return;
    if (ev.type === 'keydown' && ev.key !== 'Enter' && ev.key !== ' ') return;
    ev.preventDefault();
    ev.stopPropagation();
    var t = jWatchItems[eye.getAttribute('data-jid')];
    if (!t || typeof window.toggleWatch !== 'function') return;
    // toggleWatch пишет в раздел текущего рынка — журнал показывает только его; на всякий случай сверяем
    var cm = typeof window.__hwCurMkt === 'function' ? window.__hwCurMkt() : curMarket();
    if (cm !== t.mkt) return;
    var item = {};
    for (var k in t) item[k] = t[k];
    if (item.score == null) {
      try { item.score = typeof window.signalScore === 'function' ? window.signalScore(item) : null; } catch (e) { item.score = null; }
      if (item.score == null || isNaN(Number(item.score))) item.score = 0;
    }
    try { window.toggleWatch(item); } catch (e2) { return; }
    var on = journalIsWatched(t);
    eye.classList.toggle('on', on);
    eye.setAttribute('aria-pressed', String(on));
    try { if (typeof window.syncEyeButtons === 'function') window.syncEyeButtons(); } catch (e3) {}
  }

  /* ---- Статус сделки в журнале (HTML 7.18/7.46 sigStatus): активна ±R / тейк / стоп / безубыток / закрыта по времени.
   * Пересчёт по свежим часовым свечам — тем же движком 7.71, что «Отслеживаю» (halfBarAfter, beROf, holdOfT, ruFriCut).
   * Результат кэшируется на устройстве (hw_jstatus); «🔄 Обновить» пересчитывает раздел текущего рынка. */
  var JST_KEY = 'hw_jstatus', jstCache = null, jstBusy = false, jstAutoDone = {};
  function jstLoad() { if (jstCache) return jstCache; try { jstCache = JSON.parse(localStorage.getItem(JST_KEY) || '{}') || {}; } catch (e) { jstCache = {}; } return jstCache; }
  function jstSave() { try { localStorage.setItem(JST_KEY, JSON.stringify(jstCache || {})); } catch (e) {} }
  function jStatusGet(id) { return jstLoad()[String(id)] || null; }
  function jR(r) { return (r >= 0 ? '+' : '') + Number(r).toFixed(2) + 'R'; }
  function jStatusHtml(st) {
    if (!st) return '<span class="muted">—</span>';
    if (st.nodata) return '<span class="muted">нет свечей</span>';
    if (st.o === 'take') return '<span class="take">тейк</span>' + (st.r != null ? ' <span class="muted" style="font-size:11px">' + jR(st.r) + '</span>' : '');
    if (st.o === 'stop') return '<span class="stop">стоп</span> <span class="muted" style="font-size:11px">−1.00R</span>';
    if (st.o === 'be') return '<span class="muted">безубыток</span> <span class="muted" style="font-size:11px">0.00R</span>';
    if (st.o === 'expired') return '<span class="muted">закрыта по времени</span>' + (st.r != null ? ' <span class="muted" style="font-size:11px">' + jR(st.r) + '</span>' : '');
    if (st.r != null) return '<span class="' + (st.r >= 0 ? 'take' : 'stop') + '">' + jR(st.r) + '</span> <span class="muted" style="font-size:11px">активна' + (st.be ? ' · БУ' : '') + '</span>';
    return '<span class="muted">активна</span>';
  }
  function jStatusPaint(root) {
    var els = (root || document).querySelectorAll('.hw-j-status');
    for (var i = 0; i < els.length; i++) els[i].innerHTML = jStatusHtml(jStatusGet(els[i].getAttribute('data-jid')));
  }
  function jExFor(m) {
    /* global MOEX, Bybit, EX */
    if (m === 'ru') return (typeof MOEX !== 'undefined') ? MOEX : null;
    try { if (typeof EX !== 'undefined' && EX && (typeof MOEX === 'undefined' || EX !== MOEX)) return EX; } catch (e) {}
    return (typeof Bybit !== 'undefined') ? Bybit : null;
  }
  async function jEvalTrade(t) {
    var ex = jExFor(t.mkt);
    if (!ex || !t.e || !t.st || !t.tk) return null;
    var need = Math.max(400, Math.ceil((Date.now() - t.t) / 3600000) + 50);
    var h1 = await ex.kline(t.sym, '1h', need);
    var half = [];
    try { if (typeof halfBarAfter === 'function') half = await halfBarAfter(ex, t); } catch (e) { half = []; }
    var after = half.concat(h1.filter(function (b) { return b.ts > t.t; }));
    if (!h1.length || h1[0].ts > t.t) return { nodata: true };
    var risk = Math.abs(t.e - t.st); if (!(risk > 0)) return null;
    var beR = typeof beROf === 'function' ? beROf(t) : 1.5;
    var beLvl = t.d ? t.e + beR * risk : t.e - beR * risk;
    var fri = typeof ruFriCut === 'function' ? ruFriCut(t) : null;
    var hold = typeof holdOfT === 'function' ? holdOfT(t) : 168;
    var cur = t.st, moved = false, lc = null, res = null, r = null;
    var rLc = function () { return lc == null ? 0 : (t.d ? lc - t.e : t.e - lc) / risk; };
    for (var i = 0; i < after.length; i++) {
      var b = after[i];
      if (fri != null && b.ts >= fri) { res = 'expired'; r = rLc(); break; }
      if ((b.ts - t.t) / 3600000 > hold) { res = 'expired'; r = rLc(); break; }
      var hs = t.d ? b.l <= cur : b.h >= cur, ht = t.d ? b.h >= t.tk : b.l <= t.tk;
      if (hs) { res = moved ? 'be' : 'stop'; r = moved ? 0 : -1; break; }
      if (ht) { res = 'take'; r = Math.abs(t.tk - t.e) / risk; break; }
      if (beR > 0 && !moved && (t.d ? b.h >= beLvl : b.l <= beLvl)) { cur = t.e; moved = true; }
      lc = b.c;
    }
    if (!res && fri != null && after.length && after[after.length - 1].ts + 3600000 >= fri) { res = 'expired'; r = rLc(); }
    if (!res) { var p = h1.length ? h1[h1.length - 1].c : null; r = p != null ? (t.d ? p - t.e : t.e - p) / risk : null; }
    return { o: res || 'open', r: r, be: moved, at: Date.now() };
  }
  async function jStatusRun(rows, onlyMissing) {
    if (jstBusy || !rows || !rows.length) return;
    jstBusy = true;
    var cache = jstLoad(), list = [];
    for (var i = 0; i < rows.length; i++) {
      var st = cache[String(rows[i].id)];
      // 17.10: как «Отслеживаемые» — пересчитываются только открытые (активна / без статуса / активна с БУ);
      // завершённые (тейк / стоп / безубыток / по времени) сохраняют статус, свечи по ним не качаются.
      if (st && st.o && st.o !== 'open' && !st.nodata) continue;
      if (onlyMissing && st && !st.nodata && (Date.now() - (st.at || 0)) < 15 * 60000) continue;   // авто: свежепересчитанные открытые не трогаем
      var t = journalWatchItem(rows[i]); if (t) list.push([rows[i].id, t]);
    }
    var prog = document.querySelector('.hw-j-prog'), btn = document.querySelector('.hw-j-refresh');
    if (btn) btn.disabled = true;
    var done = 0, idx = 0;
    async function worker() {
      while (idx < list.length) {
        var it = list[idx++];
        try { var st = await jEvalTrade(it[1]); if (st) cache[String(it[0])] = st; } catch (e) {}
        done++; if (prog) prog.textContent = done + '/' + list.length;
      }
    }
    try { await Promise.all([worker(), worker(), worker(), worker()]); } finally {
      jstSave(); jstBusy = false;
      if (btn) btn.disabled = false;
      if (prog) prog.textContent = list.length ? 'обновлено' : '';
      jStatusPaint(document.getElementById('journalBody'));
    }
  }
  function jStatusAuto(rows) {
    var cur = curMarket();
    var body = document.getElementById('journalBody');
    if (body && !body.__hwJstArmed) {
      body.__hwJstArmed = true;
      body.addEventListener('click', function (ev) {
        var b = ev.target && ev.target.closest ? ev.target.closest('.hw-j-refresh') : null;
        if (!b) return;
        ev.preventDefault();
        var d = lastJournalData || {}; jStatusRun(d[curMarket()] || [], false);
      });
    }
    if (jstAutoDone[cur]) return;
    jstAutoDone[cur] = true;
    setTimeout(function () { jStatusRun(rows, true); }, 300);   // только сделки без статуса или открытые (старше 15 мин)
  }

  /* HTML 7.37: режим Мосбиржи «Интрадей/Свинг» — настройка пользователя из бота (⚙️ Фильтр → Мосбиржа) */
  function syncRuMode() {
    fetch(journalAuthUrl('/api/me'), { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.ru_mode) return;
        var m = d.ru_mode === 'swing' ? 'swing' : 'intraday';
        try { localStorage.setItem('hw_ru_mode', m); } catch (e) {}
        /* global RU_MODE */
        try { if (typeof RU_MODE !== 'undefined' && RU_MODE !== m) { RU_MODE = m; if (typeof renderScan === 'function' && typeof MKT !== 'undefined' && MKT === 'ru') renderScan(); } } catch (e) {}
      }).catch(function () {});
  }
  setTimeout(syncRuMode, 500);

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
    var strat = c.strategy === 'brk' ? '📈 Пробой' : c.strategy === 'algo' ? ('⚡ Алго' + (c.algo_combo ? ': ' + jEsc(c.algo_combo) : '')) : '🔻 Ложный пробой';
    var prob = (c.strategy === 'fbo' && c.prob != null && c.prob !== '')
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
      journalEyeHtml(row) +
      '<div class="hw-j-title">' + ((row.is_best || c.is_best) ? '⭐ ' : '') + emoji + ' ' + jEsc(row.ticker || c.ticker || '') + ' — ' + jEsc(side || '—') + '</div>' +
      '<div class="tradeInfo">' +
        '<div><span>рынок</span> <b>' + mkt + ' · ' + strat + '</b></div>' +
        tpl +
        '<div><span>уровень</span> <b>' + jFmt(level) + '</b></div>' +
        '<div><span>вход</span> <b>' + jFmt(entry) + '</b></div>' +
        '<div><span>стоп-лосс</span> <b class="stop">' + jFmt(stop) + '  (−' + risk.toFixed(1) + '%)</b></div>' +
        '<div><span>тейк-профит</span> <b class="take">' + jFmt(take) + '  (+' + tp.toFixed(1) + '%)</b></div>' +
        '<div><span>оценка</span> ' + score + '</div>' +
        '<div><span>статус</span> <b class="hw-j-status" data-jid="' + jEsc(row.id) + '">' + jStatusHtml(jStatusGet(row.id)) + '</b></div>' +
        // signal_ts = открытие H1-бара (у 30-минутного сигнала — bar_ts − 30 мин); показываем момент входа (+1ч), как HTML sigEnd(t)
        '<div><span>сигнал</span> <b>' + jEsc(jMsk((Number(c.signal_ts || row.signal_ts) || 0) && (Number(c.signal_ts || row.signal_ts) + 3600000))) + '</b></div>' +
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
    var btn = (rows && rows.length) ? ' <button type="button" class="hw-j-refresh" style="margin-left:8px;padding:4px 10px;font-size:13px">🔄 Обновить</button>' +
      '<span class="hw-j-prog muted" style="margin-left:6px;font-size:12px"></span>' : '';
    return '<div class="card" id="journalSec-' + market + '"><div class="sect"><span>' + title + '</span>' + btn + '</div>' + inner + '</div>';
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

  // Прокрутка к сделке: ждём, пока вкладка журнала видима и #jcard-<id> отрисован,
  // центрируем, подсвечиваем и докручиваем, пока грузятся графики (они сдвигают layout).
  var focusTimer = null, focusDone = '';
  function focusJournalCard() {
    var t = parseJournalTarget();
    if (!t) return;
    var key = t.market + ':' + t.id;
    if (focusDone === key) {   // перерисовка журнала: только вернуть подсветку
      var c0 = t.id ? document.getElementById('jcard-' + t.id) : null;
      if (c0) c0.classList.add('hw-focus');
      return;
    }
    if (focusTimer) clearInterval(focusTimer);
    var t0 = Date.now(), hits = 0, userMoved = false;
    var stop = function () { userMoved = true; };
    ['touchstart', 'wheel'].forEach(function (ev) { window.addEventListener(ev, stop, { once: true, passive: true }); });
    focusTimer = setInterval(function () {
      var pane = document.getElementById('paneJournal');
      var visible = pane && pane.style.display !== 'none' && pane.offsetParent !== null;
      var card = t.id ? document.getElementById('jcard-' + t.id) : null;
      var el = card || document.getElementById('journalSec-' + t.market);
      if (userMoved || Date.now() - t0 > 8000 || (el && visible && hits >= 6)) {
        clearInterval(focusTimer); focusTimer = null;
        if (el) focusDone = key;
        return;
      }
      if (!el || !visible) return;
      if (card && !card.classList.contains('hw-focus')) {
        document.querySelectorAll('#paneJournal .hw-journal-card.hw-focus').forEach(function (c) { c.classList.remove('hw-focus'); });
        card.classList.add('hw-focus');
      }
      try { el.scrollIntoView({ block: card ? 'center' : 'start', behavior: hits ? 'auto' : 'smooth' }); }
      catch (e) { try { el.scrollIntoView(); } catch (e2) {} }
      hits++;
    }, 250);
  }

  var journalSeq = 0;
  var lastJournalData = null;

  /* ---- Переключатель рынка вверху: Крипта | Мосбиржа | Крипта (Алго) ----
   * Использует штатный #mkt / applyMarket() скринера (per-market state: шаблоны, бэктест,
   * тестер, скан, «Отслеживаю»). Журнал фильтруется здесь. */
  var MKT_LABELS = [['crypto', 'Крипта'], ['ru', 'Мосбиржа']];   // 16.10: «Крипта (Алго)» убрана
  function curMarket() {
    var mk = document.getElementById('mkt');
    var v = mk && mk.value;
    return v === 'ru' ? 'ru' : 'crypto';
  }
  function setMarket(m) {
    m = (m === 'moex' || m === 'ru') ? 'ru' : 'crypto';
    var mk = document.getElementById('mkt');
    if (mk && mk.value !== m) {
      mk.value = m;
      try { mk.dispatchEvent(new Event('change')); } catch (e) {}
    }
    syncMarketBar();
  }
  function syncMarketBar() {
    var cur = curMarket();
    var bs = document.querySelectorAll('#hwMktBar button');
    for (var i = 0; i < bs.length; i++) bs[i].classList.toggle('on', bs[i].getAttribute('data-m') === cur);
  }
  function onMarketChanged() {
    syncMarketBar();
    var pane = document.getElementById('paneJournal');
    if (pane && pane.style.display !== 'none' && lastJournalData) renderJournal(lastJournalData);
  }
  function installMarketBar() {
    if (document.getElementById('hwMktBar')) return;
    var wrap = document.querySelector('.wrap') || document.body;
    var st = document.createElement('style');
    st.id = 'hw-mktbar-css';
    st.textContent =
      '#hwMktBar{position:sticky;top:0;z-index:10000;box-shadow:0 2px 6px rgba(0,0,0,.18);display:flex;gap:4px;padding:6px 0;margin:0 0 8px;' +
      'background:var(--bg,#0f1115)}' +
      '#hwMktBar button{flex:1 1 0;min-width:0;padding:9px 4px;font-size:14px;font-weight:600;white-space:nowrap;' +
      'overflow:hidden;text-overflow:ellipsis;border-radius:8px;border:1px solid rgba(127,127,127,.35);' +
      'background:transparent;color:inherit;cursor:pointer}' +
      '#hwMktBar button.on{background:#4c9aff;border-color:#4c9aff;color:#fff}' +
      '@media (max-width:380px){#hwMktBar button{font-size:12.5px;padding:8px 2px}}';
    document.head.appendChild(st);
    var bar = document.createElement('div');
    bar.id = 'hwMktBar';
    bar.setAttribute('role', 'tablist');
    bar.innerHTML = MKT_LABELS.map(function (x) {
      return '<button type="button" data-m="' + x[0] + '">' + x[1] + '</button>';
    }).join('');
    wrap.insertBefore(bar, wrap.firstChild);
    bar.addEventListener('click', function (ev) {
      var b = ev.target && ev.target.closest ? ev.target.closest('button[data-m]') : null;
      if (b) setMarket(b.getAttribute('data-m'));
    });
    // старый селектор «Рынок» в панели скана — скрыт, остаётся источником правды
    var mk = document.getElementById('mkt');
    if (mk) {
      var f = mk.closest ? mk.closest('.field') : null;
      if (f) f.style.display = 'none';
      mk.addEventListener('change', onMarketChanged);
    }
    syncMarketBar();
  }
  // Deep link (журнал / «Отслеживаю») переключает рынок ПЕРВЫМ — поверх запомненного
  var targetMarketDone = false;
  function applyTargetMarket() {
    if (targetMarketDone) return;
    var t = parseJournalTarget();
    if (t && t.market) { targetMarketDone = true; setMarket(t.market); return; }
    var L = null;
    try { L = parseDeepLink(); } catch (e) {}
    if (L && L.fm) { targetMarketDone = true; setMarket(L.fm); }
  }
  function renderJournal(data) {
    var body = document.getElementById('journalBody');
    if (!body) return;
    body.dataset.loaded = '1';
    var t = parseJournalTarget();
    var crypto = (data && data.crypto) || [];
    var ru = (data && data.ru) || [];
    lastJournalData = data || {};
    var secs = { crypto: ['Крипта', crypto], ru: ['Мосбиржа', ru] };
    // Только раздел выбранного вверху рынка (deep link уже переключил рынок на свой)
    var cur = curMarket();
    jWatchItems = {};
    var html = journalSectionHtml(cur, secs[cur][0], secs[cur][1]);
    body.innerHTML = html;
    jStatusPaint(body);
    jStatusAuto(secs[cur][1]);
    if (!body.__hwEyeArmed) {
      body.__hwEyeArmed = true;
      body.addEventListener('click', onJournalEye);
      body.addEventListener('keydown', onJournalEye);
    }
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
      applyTargetMarket();
      var pane = document.getElementById('paneJournal');
      var tab = document.getElementById('tabJournal');
      if (!pane || !tab || pane.style.display === 'none' || !tab.classList.contains('on')) {
        showJournalPane();
      }
    }, 200);
  }

  /* ⭐ The Best (HTML 6.93): в таблице «Сигналы» помечаем строки, которые отобрал бы конвейер The best
     (bestPipeline движка) на том же окне. Только отметка — фильтр/кнопки не меняем. */
  function nativeBestStar() { /* global nameCell, STAR_CUR */ return typeof nameCell === 'function' && typeof STAR_CUR === 'object'; }
  function installBestStar() {
    if (nativeBestStar()) return;   // движок 7.0x сам ставит ★ (= отобран конвейером The best) — не дублируем
    if (typeof window.renderScan !== 'function' || window.renderScan.__hwBest) return;
    var orig = window.renderScan;
    var wrapped = function () {
      var r = orig.apply(this, arguments);
      try { markBestRows(); } catch (e) {}
      return r;
    };
    wrapped.__hwBest = true;
    window.renderScan = wrapped;
  }
  function markBestRows() {
    /* global LAST, MKT, bestPipeline, windowHours */
    if (nativeBestStar()) return;
    if (typeof bestPipeline !== 'function' || typeof LAST === 'undefined' || !LAST || typeof MKT === 'undefined' || MKT !== 'crypto') return;
    var cutoff = Date.now() - windowHours() * 3600000;
    var K = {};
    bestPipeline(LAST.filter(function (s) { return s.t >= cutoff; })).forEach(function (t) { K[t.base + '|' + t.t] = 1; });
    document.querySelectorAll('#tb tr').forEach(function (tr) {
      var eye = tr.querySelector('.eye[data-base]');
      if (!eye) return;
      var key = eye.getAttribute('data-base') + '|' + eye.getAttribute('data-t');
      var td = tr.children[2];
      if (!td || !K[key] || td.getAttribute('data-hw-best')) return;
      td.setAttribute('data-hw-best', '1');
      td.textContent = '⭐ ' + td.textContent;
    });
  }

  /* 💾 «Сохранить» (скачать HTML со всеми данными) в Mini App не нужна: прячем и глушим клик
     (capture на document — срабатывает раньше обработчика HTML). Шаблоны/отслеживаемые не трогаем. */
  function killSaveFileBtn() {
    var b = document.getElementById('btnSaveFile');
    if (b) { b.style.display = 'none'; b.disabled = true; b.setAttribute('aria-hidden', 'true'); }
  }
  document.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target.closest('#btnSaveFile') : null;
    if (t) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);

  /* «Обновить всё» в «Отслеживаемых» (bugfix 13.10): hwRefreshSignal движка пересчитывал не все признаки
     (нет approach_atr / d1_streak …) → signalScore падал на старую оценку. Теперь после него берём сигнал
     scanSymbol на том же часе/уровне и копируем ВСЕ его признаки (уровень, накопление, ЛП, v5/v6),
     оценку — ровно как в списке сигналов (signalScore до recompute), флаг The best — как bestPipeline. */
  var HW_TRADE_KEYS = { type: 1, base: 1, sym: 1, t: 1, d: 1, side: 1, lv: 1, k: 1, e: 1, st: 1, tk: 1, rp: 1,
    fwd: 1, o: 1, _i: 1, mkt: 1, added: 1, score: 1, p: 1 };
  function hwSyncFromScan(t, d1, h1) {
    /* global scanSymbol, lastClosed, atr, approachAtr, d1Streak, signalScore, isBestFBO, bestPoke, BEST_POKE, MKT, HW_FEAT_KEYS */
    // только крипта: у MOEX признаки индекса (IMOEX) и ruCluster считаются по всему списку — их даёт сам движок
    if (typeof scanSymbol !== 'function' || MKT !== 'crypto') return false;
    if (t._src === 30) return false;   // 7.58: сигнал с середины часа — признаки уже посчитаны на его момент
    var h1c = lastClosed(h1), idx = -1;
    for (var i = 0; i < h1c.length; i++) if (h1c[i].ts === t.t) { idx = i; break; }
    if (idx < 60) return false;
    var ty = t.type || 'fbo', m = null;
    try {
      // окно скана — с начала UTC-дня сигнала: дневное состояние (ATR/уровни) как у скана, покрывающего весь день
      var D = 86400000, ds = Math.floor(t.t / D) * D, i0 = idx;
      while (i0 > 0 && h1c[i0 - 1].ts >= ds) i0--;
      var sig = scanSymbol(t.base, t.sym, d1, h1, { noNight: false, scanBars: h1c.length - i0 + 1, raw: true })
        .filter(function (s) { return (s.type || 'fbo') === ty && s.t === t.t; })
        .sort(function (a, b) { return Math.abs(a.lv - t.lv) - Math.abs(b.lv - t.lv); });
      if (sig[0] && Math.abs(sig[0].lv / t.lv - 1) <= 0.01) m = sig[0];
    } catch (e) {}
    if (m) {
      Object.keys(m).forEach(function (k) { if (!HW_TRADE_KEYS[k]) t[k] = m[k]; });
      t.p = m.p;
      t.score = signalScore(m);                       // как в «Сигналах»: оценка до recompute
    } else if (ty === 'fbo') {
      var h1cut = h1c.slice(0, idx + 1);
      t.approach_atr = approachAtr(h1cut, atr(h1cut.slice(-60)));
      t.d1_streak = d1Streak(lastClosed(d1), h1c[idx].ts);
      var tmp = {}; Object.keys(t).forEach(function (k) { tmp[k] = t[k]; }); delete tmp.st;   // stop как в скане
      t.score = signalScore(tmp);
    }
    if (ty === 'fbo' && MKT === 'crypto' && typeof isBestFBO === 'function') {
      var pr = bestPoke(t);
      t.is_best = !!(isBestFBO(t) && !((pr || 0) > BEST_POKE));
    }
    t._algo = 'v6-6.93';
    return true;
  }
  function installWatchRefreshFix() {
    if (typeof window.hwRefreshSignal !== 'function' || window.hwRefreshSignal.__hwFix) return;
    try { if (typeof HW_FEAT_KEYS !== 'undefined') ['approach_atr', 'd1_streak', 'is_best', 'acc', 'touched8', 'strength', 'crosses'].forEach(function (k) { if (HW_FEAT_KEYS.indexOf(k) < 0) HW_FEAT_KEYS.push(k); }); } catch (e) {}
    var orig = window.hwRefreshSignal;
    var fixed = async function (t, d1, h1) {
      var r = await orig.apply(this, arguments);
      try { if (hwSyncFromScan(t, d1, h1)) r = true; } catch (e) { console.warn('[lean] watch sync', e); }
      return r;
    };
    fixed.__hwFix = true;
    window.hwRefreshSignal = fixed;
    window.__hwSyncFromScan = hwSyncFromScan;
  }
  /* Одноразовый ремонт: позиции, пересчитанные старой версией «Обновить всё» (нет метки _algo) — полный
     пересчёт новой. Список/шаблоны не удаляются: refreshWatch только обновляет поля и сохраняет. */
  function repairWatchOnce() {
    try {
      /* global loadWatch, refreshWatch */
      if (typeof MKT === 'undefined' || MKT !== 'crypto' || typeof refreshWatch !== 'function') return;
      if (localStorage.getItem('hw_watch_fix_v6') === '1') return;
      var w = loadWatch();
      var need = w.some(function (x) { return (x.type || 'fbo') === 'fbo' && x._algo !== 'v6-6.93'; });
      localStorage.setItem('hw_watch_fix_v6', '1');
      if (need) refreshWatch(true);
    } catch (e) {}
  }

  function start() {
    var n = 0;
    var t = setInterval(function () {
      n++;
      if (ready() || n > 100) {
        clearInterval(t);
        if (!ready()) { console.warn('[lean] screener DOM not ready, giving up'); return; }
        installConcHooks();
        killSaveFileBtn();
        // автообновление HTML в Mini App выключено (скан только по кнопке / вручную включённой галочке)
        try { var au = document.getElementById('autoScan'); if (au && !au.__hwTouched) au.checked = false; } catch (e) {}
        try { installBestStar(); markBestRows(); } catch (e) {}
        try { installWatchRefreshFix(); setTimeout(repairWatchOnce, 8000); } catch (e) {}
        hideBacktestRunUI();
        hideOtherTabs();
        renameTemplatesTab();
        installJournalUI();
        installMarketBar();
        applyTargetMarket();
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

/* 17.10: формат символа под источник. Журнал/«Отслеживаю» хранят крипту как BTCUSDT (Bybit), а при откате источника
 * на OKX нужен BTC-USDT-SWAP (и наоборот: позиции, добавленные из скана на OKX, на Bybit). Оборачиваем методы свечей. */
function hwSymFor(exName, sym) {
  var s = String(sym || '');
  if (exName === 'OKX') { var m = /^([A-Z0-9]+)USDT$/i.exec(s); return m ? m[1].toUpperCase() + '-USDT-SWAP' : s; }
  if (exName === 'Bybit') { var k = /^([A-Z0-9]+)-USDT-SWAP$/i.exec(s); return k ? k[1].toUpperCase() + 'USDT' : s; }
  return s;
}
(function () {
  function wrap(ex, name) {
    if (!ex || ex.__hwSymWrapped) return;
    ex.__hwSymWrapped = true;
    ['kline', 'k30', 'm5'].forEach(function (fn) {
      var orig = ex[fn]; if (typeof orig !== 'function') return;
      ex[fn] = function (sym) { var a = Array.prototype.slice.call(arguments); a[0] = hwSymFor(name, sym); return orig.apply(this, a); };
    });
  }
  function install() {
    /* global OKX, Bybit */
    try { if (typeof OKX !== 'undefined') wrap(OKX, 'OKX'); } catch (e) {}
    try { if (typeof Bybit !== 'undefined') wrap(Bybit, 'Bybit'); } catch (e) {}
  }
  if (typeof document === 'undefined') return;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install); else install();
})();
if (typeof module !== 'undefined' && module.exports) module.exports = { hwSymFor: hwSymFor };
