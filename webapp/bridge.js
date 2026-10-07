/* bridge.js — Telegram Mini App ↔ backend templates sync
 * Works without Telegram SDK (browser testing via localStorage only).
 */
(function () {
  'use strict';

  var LS_KEY = 'hw_fbo_tpl';   // crypto; новый HTML: отдельные ключи на рынок (tplKey())
  var MARKETS = ['crypto', 'ru', 'algo'];
  function tplKeyFor(m) { return m === 'ru' ? 'hw_fbo_tpl_ru' : m === 'algo' ? 'hw_fbo_tpl_algo' : 'hw_fbo_tpl'; }
  function curMkt() {
    try { ensurePageHelpers(); } catch (e0) {}
    try { var m = window.__hwCurMkt ? window.__hwCurMkt() : 'crypto'; return MARKETS.indexOf(m) >= 0 ? m : 'crypto'; }
    catch (e) { return 'crypto'; }
  }
  var TG = (typeof window !== 'undefined' && window.Telegram && window.Telegram.WebApp)
    ? window.Telegram.WebApp
    : null;
  var IN_TG = !!(TG && TG.initData);

  function toast(msg) {
    var el = document.getElementById('tg-bridge-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'tg-bridge-toast';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.classList.remove('show'); }, 2800);
  }

  function setStatus(msg) {
    var s = document.getElementById('tg-bridge-status');
    if (s) s.textContent = msg || '';
  }

  function initDataHeader() {
    // Always re-read — TG captured at load may miss initData; SDK can fill later
    try {
      if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) {
        return String(window.Telegram.WebApp.initData);
      }
    } catch (e0) {}
    if (TG && TG.initData) return TG.initData;
    try {
      var q = new URLSearchParams(location.search);
      return q.get('initData') || '';
    } catch (e) {
      return '';
    }
  }

  function extractSyncToken() {
    try {
      var u = '';
      try { u = localStorage.getItem('hw_fbo_sync_url') || localStorage.getItem('hw_bot_sync_url') || ''; } catch (e1) {}
      if (!u) {
        var q = new URLSearchParams(location.search || '');
        u = q.get('sync') || q.get('sync_url') || '';
        var tok = q.get('token') || q.get('sync_token') || '';
        if (!u && tok) u = (location.origin || '') + '/sync/' + tok;
      }
      if (!u) return '';
      var m = String(u).match(/\/sync\/([A-Za-z0-9_-]+)/);
      return m ? m[1] : '';
    } catch (e) { return ''; }
  }

  function apiUrl(path) {
    var u = path;
    var id = initDataHeader();
    var tok = extractSyncToken();
    var q = new URLSearchParams(location.search);
    if (q.get('debug_user_id') && !id && !tok) {
      u += (u.indexOf('?') >= 0 ? '&' : '?') + 'debug_user_id=' + encodeURIComponent(q.get('debug_user_id'));
    } else if (id) {
      u += (u.indexOf('?') >= 0 ? '&' : '?') + 'initData=' + encodeURIComponent(id);
    } else if (tok) {
      u += (u.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(tok);
    }
    return u;
  }

  function apiHeaders() {
    var h = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
    var id = initDataHeader();
    if (id) h['X-Telegram-Init-Data'] = id;
    var tok = extractSyncToken();
    if (tok) h['X-Sync-Token'] = tok;
    return h;
  }

  async function api(method, path, body) {
    var opts = { method: method, headers: apiHeaders() };
    if (body !== undefined) opts.body = JSON.stringify(body);
    var res = await fetch(apiUrl(path), opts);
    var text = await res.text();
    var data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
    if (!res.ok) {
      var err = new Error((data && (data.error || data.detail)) || ('HTTP ' + res.status));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function readLocalTpl(m) {
    try { return JSON.parse(localStorage.getItem(tplKeyFor(m || curMkt())) || '{}') || {}; } catch (e) { return {}; }
  }

  function writeLocalTpl(obj, m) {
    try { localStorage.setItem(tplKeyFor(m || curMkt()), JSON.stringify(obj || {})); } catch (e) {}
  }

  /* Разовая миграция: старый общий hw_fbo_tpl содержал и крипту, и РФ (_market:'ru').
     Новый HTML хранит рынки раздельно — переносим РФ-шаблоны в hw_fbo_tpl_ru. */
  function migrateSharedTpl() {
    try {
      if (localStorage.getItem('hw_tpl_split_v1')) return;
      var all = JSON.parse(localStorage.getItem('hw_fbo_tpl') || '{}') || {};
      var ru = readLocalTpl('ru'), algo = readLocalTpl('algo'), cr = {};
      Object.keys(all).forEach(function (k) {
        var m = (all[k] && all[k]._market) || 'crypto';
        if (m === 'ru') ru[k] = all[k]; else if (m === 'algo') algo[k] = all[k]; else cr[k] = all[k];
      });
      writeLocalTpl(cr, 'crypto'); writeLocalTpl(ru, 'ru'); writeLocalTpl(algo, 'algo');
      localStorage.setItem('hw_tpl_split_v1', '1');
    } catch (e) {}
  }

  function unwrapTplEntry(entry) {
    if (entry && typeof entry === 'object' && entry.filters && typeof entry.filters === 'object') {
      var flat = Object.assign({}, entry.filters);
      if (entry.market && (flat._market === undefined || flat._market === null || flat._market === '')) {
        flat._market = entry.market;
      }
      return flat;
    }
    return entry;
  }

  function unwrapRemoteTemplates(remote) {
    var out = {};
    var src = remote || {};
    Object.keys(src).forEach(function (k) {
      out[k] = unwrapTplEntry(src[k]);
    });
    return out;
  }

  function mergeTpl(remote, m) {
    var local = readLocalTpl(m);
    var flatRemote = unwrapRemoteTemplates(remote || {});
    var out = Object.assign({}, local, flatRemote);
    writeLocalTpl(out, m);
    return out;
  }

  function tagMarket(o, m) {
    var out = {};
    Object.keys(o || {}).forEach(function (k) {
      var v = o[k];
      out[k] = (v && typeof v === 'object') ? Object.assign({}, v, { _market: m }) : v;
    });
    return out;
  }

  /* ---- Override loadTpl / saveTplStore after HTML inline script defines them ---- */
  function installHooks() {
    if (typeof window.loadTpl !== 'function' || typeof window.saveTplStore !== 'function') {
      return false;
    }
    if (window.__hwBridgeHooks) return true;
    window.__hwBridgeHooks = true;

    var _origLoad = window.loadTpl;
    var _origSave = window.saveTplStore;

    window.loadTpl = function () {
      return _origLoad();
    };

    window.saveTplStore = function (o) {
      _origSave(o);
      // Never push empty templates — would wipe server copy for this Telegram user
      var keys = o && typeof o === 'object' ? Object.keys(o) : [];
      if (!keys.length) {
        setStatus('шаблоны: пустой набор не отправляю на сервер');
        return;
      }
      // Fire-and-forget sync to backend — шаблоны текущего рынка (crypto | ru | algo)
      var m = curMkt();
      api('PUT', '/api/templates', { templates: tagMarket(o, m), market: m })
        .then(function () { setStatus('шаблоны сохранены на сервере'); })
        .catch(function (e) {
          setStatus('офлайн: только localStorage (' + (e.message || e) + ')');
        });
    };

    return true;
  }

  function waitHooks(cb) {
    if (installHooks()) { cb(); return; }
    var n = 0;
    var t = setInterval(function () {
      n++;
      if (installHooks() || n > 80) {
        clearInterval(t);
        cb();
      }
    }, 50);
  }

  async function syncFromServer() {
    migrateSharedTpl();
    try {
      var data = await api('GET', '/api/templates');
      var byM = (data && data.by_market) || null;
      if (!byM) {   // старый сервер: общий список, раскладываем по _market
        byM = { crypto: {}, ru: {}, algo: {} };
        var flat = unwrapRemoteTemplates((data && data.templates) || {});
        Object.keys(flat).forEach(function (k) {
          var mm = (flat[k] && flat[k]._market) || 'crypto';
          (byM[mm] = byM[mm] || {})[k] = flat[k];
        });
      }
      MARKETS.forEach(function (m) {
        var remote = byM[m] || {};
        var local = readLocalTpl(m);
        // Never wipe local with empty remote; push local up instead (per market)
        if (!Object.keys(remote).length && Object.keys(local).length) {
          api('PUT', '/api/templates', { templates: tagMarket(local, m), market: m })
            .catch(function (e) { console.warn('[bridge] push local tpl', m, e); });
          return;
        }
        if (Object.keys(remote).length) mergeTpl(remote, m);
      });
      // Never call saveTplStore({}) from sync
      if (typeof window.fillTplSelects === 'function') window.fillTplSelects();
      setStatus('шаблоны синхронизированы (крипта / РФ / алго — раздельно)');
    } catch (e) {
      setStatus('офлайн / без авторизации — localStorage');
      console.warn('[bridge] templates sync failed', e);
    }
  }

  function mapHtmlToBotFilter(html, name) {
    html = html || {};
    var strength = 3;
    if (html.str !== undefined && html.str !== 'auto' && html.str !== '') {
      var s = parseInt(html.str, 10);
      if (!isNaN(s)) strength = s;
    }
    var dist = 0.5;
    if (html.dist !== undefined && html.dist !== 'auto' && html.dist !== '') {
      var d = parseFloat(String(html.dist).replace(',', '.'));
      if (!isNaN(d)) dist = d;
    }
    var sides = ['LONG', 'SHORT'];
    var side = (html.side || 'auto').toString().toLowerCase();
    if (side === 'long') sides = ['LONG'];
    else if (side === 'short') sides = ['SHORT'];

    var bias = (html.bias || 'auto').toString().toLowerCase();
    var biasFilter = bias !== 'auto' && bias !== '' && bias !== 'off';

    return {
      desc: 'miniapp:' + (name || 'custom'),
      strength_min: strength,
      dist_atr_max: dist,
      sides: sides,
      bias_filter: !!biasFilter,
      html: html,
    };
  }

  async function pushFiltersToBot() {
    setStatus('отправляю фильтры боту…');
    try {
      var html = {};
      if (typeof window.currentTplObj === 'function') {
        html = window.currentTplObj('sc') || {};
      }
      // Prefer named active template if select has user tpl
      var name = null;
      var sel = document.querySelector('#sc_tpl');
      if (sel && sel.value && sel.value.indexOf('u:') === 0) {
        name = sel.value.slice(2);
      }
      var payload = { html: html };
      if (name) payload.name = name;
      // Also send mapped bot filter for clarity
      payload.filter = mapHtmlToBotFilter(html, name || 'sc');
      await api('PUT', '/api/signal-filter', payload);
      if (name) {
        try { await api('PUT', '/api/active-template', { name: name }); } catch (e2) {}
      }
      toast('Фильтры переданы боту');
      setStatus('бот использует текущие фильтры' + (name ? ' («' + name + '»)' : ''));
    } catch (e) {
      toast('Ошибка: ' + (e.message || e));
      setStatus('не удалось отправить боту');
    }
  }

  function injectBar() {
    if (!IN_TG && !(TG && typeof TG.ready === 'function')) {
      // Still show bar when opened as Mini App URL even without initData (debug)
      if (!/Telegram/i.test(navigator.userAgent) && !location.search.includes('debug_user_id')) {
        return;
      }
    }
    if (document.getElementById('tg-bridge-bar')) return;
    var bar = document.createElement('div');
    bar.id = 'tg-bridge-bar';
    bar.innerHTML =
      '<span id="tg-bridge-status"></span>';
    var wrap = document.querySelector('.wrap') || document.body;
    wrap.insertBefore(bar, wrap.firstChild);
    // «Шаблоны → боту» убрана: шаблоны уходят в бота синхронизацией (PUT /api/templates)
  }

  function bootTelegram() {
    if (!TG) return;
    try { TG.ready(); } catch (e) {}
    try { TG.expand(); } catch (e) {}
    try {
      if (TG.themeParams) {
        var bg = TG.themeParams.bg_color;
        var txt = TG.themeParams.text_color;
        if (bg) document.documentElement.style.setProperty('--bg', bg);
        if (txt) document.documentElement.style.setProperty('--fg', txt);
        if (TG.colorScheme === 'light') document.documentElement.setAttribute('data-theme', 'light');
        else if (TG.colorScheme === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
      }
      if (typeof TG.setHeaderColor === 'function') {
        try { TG.setHeaderColor('secondary_bg_color'); } catch (e2) {}
      }
    } catch (e) {}
  }


  /* ---- Mini App state: watch / backtest / signals (server = source of truth) ----
   * Templates sync above is untouched. Empty server wipe requires clear_* or matching updated_at.
   * Page-scope helpers (__hwSnapshotMiniappState / __hwApplyMiniappState) are injected so
   * `let BT` / `const MKT_STATE` in screener.html are reachable.
   */
  var STATE_LS_WATCH = 'hw_fbo_watch_all';
  var _stateMeta = { watch: 0, backtest: 0, signals: 0 };
  var _stateHydrated = false;
  var _hydrating = false;
  var _dirtyDuringHydrate = false;
  var _statePushTimer = null;
  var _pendingClear = { watch: false, backtest: false, signals: false };
  var _stateSyncing = false;

  function watchRowKey(x) {
    if (!x || typeof x !== 'object') return '';
    var m = x.mkt || x.market || 'crypto';
    return String(x.base || '') + '|' + String(x.t || '') + '|' + m;
  }

  function preferWatchField(a, b) {
    if (b !== undefined && b !== null && b !== '') return b;
    if (a !== undefined && a !== null && a !== '') return a;
    return (b !== undefined) ? b : a;
  }

  /** Merge two rows for the same key — keep sym/base/levels and live _now/_res from either side. */
  function mergeWatchRow(serverRow, localRow) {
    var a = serverRow || {};
    var b = localRow || {};
    var out = {};
    var keys = {};
    Object.keys(a).forEach(function (k) { keys[k] = 1; });
    Object.keys(b).forEach(function (k) { keys[k] = 1; });
    Object.keys(keys).forEach(function (k) {
      out[k] = preferWatchField(a[k], b[k]);
    });
    // Live mark / outcome: prefer whichever side still has them (local often fresher).
    if (b._now != null) out._now = b._now;
    else if (a._now != null) out._now = a._now;
    if (b._res) out._res = b._res;
    else if (a._res) out._res = a._res;
    // Identity fields: never drop sym/base if one side has them
    out.sym = preferWatchField(a.sym, b.sym);
    out.base = preferWatchField(a.base, b.base);
    out.e = preferWatchField(a.e, b.e);
    out.st = preferWatchField(a.st, b.st);
    out.tk = preferWatchField(a.tk, b.tk);
    out.mkt = preferWatchField(a.mkt || a.market, b.mkt || b.market) || 'crypto';
    out.market = out.mkt;
    // Derive Bybit-style sym from base when still missing (bot/cloud rows).
    if (!out.sym && out.base && out.mkt !== 'ru') {
      out.sym = /USDT$/i.test(String(out.base)) ? out.base : (out.base + 'USDT');
    }
    if (!out.base && out.sym) {
      if (out.mkt === 'ru') out.base = out.sym;
      else if (/-USDT-SWAP$/i.test(String(out.sym))) out.base = String(out.sym).split('-')[0];
      else if (/USDT$/i.test(String(out.sym))) out.base = String(out.sym).replace(/USDT$/i, '');
      else out.base = out.sym;
    }
    return out;
  }

  function mergeWatchLists(serverArr, localArr) {
    var map = {};
    var order = [];
    function ingest(arr, asLocal) {
      (arr || []).forEach(function (row) {
        if (!row || typeof row !== 'object') return;
        var k = watchRowKey(row);
        if (!k) return;
        if (!map[k]) {
          map[k] = row;
          order.push(k);
        } else {
          map[k] = asLocal ? mergeWatchRow(map[k], row) : mergeWatchRow(row, map[k]);
        }
      });
    }
    // Server first for presence, then local fills gaps (_now/_res/sym) and adds eye-clicks.
    ingest(serverArr, false);
    ingest(localArr, true);
    return order.map(function (k) { return map[k]; });
  }

  /* ---- Watch tombstones: {tombs:{key:deletedAtMs}, cleared_at:ms} ----
   * Explicit deletes / «Очистить всё» are recorded here and pushed to the server,
   * so union-merge (server∪local∪bot) never resurrects removed rows. A row survives
   * only if its `added` (fallback `t`) is newer than its tombstone and cleared_at. */
  var STATE_LS_TOMB = 'hw_fbo_watch_tomb';
  function readTomb() {
    try {
      var o = JSON.parse(localStorage.getItem(STATE_LS_TOMB) || '{}') || {};
      return { tombs: (o.tombs && typeof o.tombs === 'object') ? o.tombs : {}, cleared_at: +o.cleared_at || 0 };
    } catch (e) { return { tombs: {}, cleared_at: 0 }; }
  }
  function writeTomb(tb) {
    try { localStorage.setItem(STATE_LS_TOMB, JSON.stringify(tb)); } catch (e) {}
  }
  function mergeTomb(a, b) {
    var out = { tombs: {}, cleared_at: Math.max(+a.cleared_at || 0, +b.cleared_at || 0) };
    var minTs = Date.now() - 90 * 86400000;  // same TTL as server (_TOMB_TTL_MS)
    [a.tombs || {}, b.tombs || {}].forEach(function (t) {
      Object.keys(t).forEach(function (k) {
        var v = +t[k] || 0;
        if (v > out.cleared_at && v >= minTs && v > (out.tombs[k] || 0)) out.tombs[k] = v;
      });
    });
    return out;
  }
  function watchRowTime(x) { return +(x && (x.added || x.t)) || 0; }
  function watchAlive(x, tb) {
    var rt = watchRowTime(x);
    if (tb.cleared_at && rt <= tb.cleared_at) return false;
    var d = tb.tombs[watchRowKey(x)];
    return !(d && rt <= d);
  }
  function filterAlive(arr, tb) {
    return (arr || []).filter(function (x) { return x && typeof x === 'object' && watchAlive(x, tb); });
  }
  window.__hwWatchTomb = function (row) {
    var k = watchRowKey(row);
    if (!k) return;
    var tb = readTomb();
    tb.tombs[k] = Math.max(Date.now(), watchRowTime(row));
    writeTomb(tb);
  };
  window.__hwWatchClearedAll = function () {
    var tb = readTomb();
    var mx = Date.now();
    readLocalWatch().forEach(function (x) { mx = Math.max(mx, watchRowTime(x)); });
    tb.cleared_at = mx;
    tb.tombs = {};
    writeTomb(tb);
  };

  function readLocalWatch() {
    try { return JSON.parse(localStorage.getItem(STATE_LS_WATCH) || '[]'); } catch (e) { return []; }
  }

  function writeLocalWatch(arr) {
    try { localStorage.setItem(STATE_LS_WATCH, JSON.stringify(arr || [])); } catch (e) {}
  }

  function ensurePageHelpers() {
    if (document.getElementById('hw-miniapp-state-boot')) return;
    var s = document.createElement('script');
    s.id = 'hw-miniapp-state-boot';
    // Classic script shares top-level let/const with screener.html
    s.textContent = [
      'window.__hwSnapshotMiniappState = function () {',
      '  var bt = { crypto: [], ru: [], algo: [] }, sig = { crypto: [], ru: [], algo: [] };',
      '  try {',
      '    if (typeof MKT_STATE === "object" && MKT_STATE) {',
      '      if (typeof MKT === "string" && MKT_STATE[MKT]) {',
      '        MKT_STATE[MKT].BT = (typeof BT !== "undefined" && Array.isArray(BT)) ? BT : (MKT_STATE[MKT].BT || []);',
      '        MKT_STATE[MKT].LAST = (typeof LAST !== "undefined" && Array.isArray(LAST)) ? LAST : (MKT_STATE[MKT].LAST || []);',
      '      }',
      '      ["crypto","ru","algo"].forEach(function (m) {',
      '        var s = MKT_STATE[m] || {};',
      '        bt[m] = Array.isArray(s.BT) ? s.BT.slice() : [];',
      '        sig[m] = Array.isArray(s.LAST) ? s.LAST.slice() : [];',
      '      });',
      '    } else {',
      '      var m = (typeof MKT === "string" && (MKT === "ru" || MKT === "algo")) ? MKT : "crypto";',
      '      if (typeof BT !== "undefined" && Array.isArray(BT)) bt[m] = BT.slice();',
      '      if (typeof LAST !== "undefined" && Array.isArray(LAST)) sig[m] = LAST.slice();',
      '    }',
      '  } catch (e) { console.warn("[bridge] snapshot", e); }',
      '  return { backtest: bt, signals: sig };',
      '};',
      'window.__hwApplyMiniappState = function (data) {',
      '  if (!data) return;',
      '  try {',
      '    if (Array.isArray(data.watch)) {',
      '      try { localStorage.setItem("hw_fbo_watch_all", JSON.stringify(data.watch)); } catch (e0) {}',
      '    }',
      '    var bt = data.backtest || {}, sig = data.signals || {};',
      '    if (typeof MKT_STATE === "object" && MKT_STATE) {',
      '      ["crypto","ru","algo"].forEach(function (m) {',
      '        if (!MKT_STATE[m]) return;',
      '        if (Array.isArray(bt[m])) MKT_STATE[m].BT = bt[m];',
      '        if (Array.isArray(sig[m])) MKT_STATE[m].LAST = sig[m];',
      '      });',
      '      var m = (typeof MKT === "string") ? MKT : "crypto";',
      '      if (MKT_STATE[m]) { BT = MKT_STATE[m].BT || []; LAST = MKT_STATE[m].LAST || []; }',
      '    }',
      '    try { if (typeof renderWatch === "function") renderWatch(); } catch (e1) {}',
      '    try { if (typeof updCounts === "function") updCounts(); } catch (e2) {}',
      '    try { if (typeof renderBt === "function") renderBt(); } catch (e3) {}',
      '    try { if (typeof LAST !== "undefined" && LAST.length && typeof renderScan === "function") renderScan(); } catch (e4) {}',
      '  } catch (e) { console.warn("[bridge] apply", e); }',
      '};',
      'window.__hwOnWatchSave = function (w) {',
      '  if (typeof window.__hwScheduleStatePush === "function") window.__hwScheduleStatePush({ from: "watch" });',
      '};',
      'window.__hwMarkClearWatch = function () { window.__hwPendingClearWatch = true; try { if (window.__hwWatchClearedAll) window.__hwWatchClearedAll(); } catch (e) {} };',
      'window.__hwCurMkt = function () { return (typeof MKT === "string" && MKT) ? MKT : "crypto"; };',
      'window.__hwMarkClearBacktest = function () { window.__hwPendingClearBacktest = true; };'
    ].join('\n');
    (document.head || document.documentElement).appendChild(s);
  }

  function hydrateFromServer(data) {
    if (!data) return;
    _hydrating = true;
    var needSeed = false;
    try {
      if (data.updated_at) {
        _stateMeta.watch = data.updated_at.watch || 0;
        _stateMeta.backtest = data.updated_at.backtest || 0;
        _stateMeta.signals = data.updated_at.signals || 0;
      }
      ensurePageHelpers();

      var cleared = data.cleared || {};
      // Tombstones: server ∪ local; both lists filtered so deletes/clears never come back.
      var localTb = readTomb();
      var serverTb = { tombs: data.watch_tombstones || {}, cleared_at: +data.watch_cleared_at || 0 };
      var tb = mergeTomb(localTb, serverTb);
      writeTomb(tb);
      if (tb.cleared_at > serverTb.cleared_at) needSeed = true;
      Object.keys(tb.tombs).forEach(function (k) { if ((serverTb.tombs[k] || 0) < tb.tombs[k]) needSeed = true; });
      var serverWatch = filterAlive(Array.isArray(data.watch) ? data.watch : [], tb);
      var localWatch = filterAlive(readLocalWatch(), tb);
      // Legacy server tombstone (pre-20260928c): cleared & empty with no cleared_at → respect empty.
      if (cleared.watch && !serverWatch.length && !serverTb.cleared_at && !(Array.isArray(data.watch) && data.watch.length)) {
        localWatch = [];
      }
      // Union server∪local so eye-clicks during hydrate / Telegram 👁 + HTML are not lost.
      var merged = mergeWatchLists(serverWatch, localWatch);
      writeLocalWatch(merged);
      data = Object.assign({}, data, { watch: merged });
      if (merged.length > serverWatch.length) needSeed = true;

      // Backtest / signals: if server empty & not cleared, keep page memory / seed after apply probe
      var snap = null;
      try { snap = window.__hwSnapshotMiniappState && window.__hwSnapshotMiniappState(); } catch (e0) {}
      var bt = (data.backtest && typeof data.backtest === 'object') ? data.backtest : { crypto: [], ru: [] };
      var sig = (data.signals && typeof data.signals === 'object') ? data.signals : { crypto: [], ru: [] };
      var btEmpty = !(bt.crypto && bt.crypto.length) && !(bt.ru && bt.ru.length);
      var sigEmpty = !(sig.crypto && sig.crypto.length) && !(sig.ru && sig.ru.length);
      if (snap) {
        var locBtEmpty = !(snap.backtest.crypto && snap.backtest.crypto.length) && !(snap.backtest.ru && snap.backtest.ru.length);
        var locSigEmpty = !(snap.signals.crypto && snap.signals.crypto.length) && !(snap.signals.ru && snap.signals.ru.length);
        if (btEmpty && !locBtEmpty && !cleared.backtest) {
          data = Object.assign({}, data, { backtest: snap.backtest });
          needSeed = true;
        }
        if (sigEmpty && !locSigEmpty && !cleared.signals) {
          data = Object.assign({}, data, { signals: snap.signals });
          needSeed = true;
        }
      }

      if (typeof window.__hwApplyMiniappState === 'function') {
        window.__hwApplyMiniappState(data);
      }
      window.__hwLastState = data;
    } finally {
      _hydrating = false;
      _stateHydrated = true;
    }
    if (needSeed || _dirtyDuringHydrate) {
      _dirtyDuringHydrate = false;
      // Push local→server once so account gets existing device data / adds during hydrate
      scheduleStatePush();
    }
    // After cloud hydrate, refresh live «СЕЙЧАС» client-side (server stores rows, not tick prices).
    try {
      if (typeof window.refreshWatch === 'function') {
        setTimeout(function () {
          try { window.refreshWatch(); } catch (eR) { console.warn('[bridge] refreshWatch', eR); }
        }, 400);
      }
    } catch (eH) {}
  }

  function scheduleStatePush() {
    if (!_stateHydrated || _hydrating) return;
    clearTimeout(_statePushTimer);
    _statePushTimer = setTimeout(function () { _statePushTimer = null; pushStateToServer(); }, 700);
  }
  window.__hwScheduleStatePush = scheduleStatePush;
  function flushStatePush() {
    if (!_statePushTimer) return;
    clearTimeout(_statePushTimer);
    _statePushTimer = null;
    try { pushStateToServer(); } catch (e) {}
  }
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') flushStatePush();
  });
  window.addEventListener('pagehide', flushStatePush);

  async function pushStateToServer() {
    if (!_stateHydrated || _hydrating || _stateSyncing) return;
    _stateSyncing = true;
    try {
      ensurePageHelpers();
      var snap = (typeof window.__hwSnapshotMiniappState === 'function')
        ? window.__hwSnapshotMiniappState()
        : { backtest: { crypto: [], ru: [] }, signals: { crypto: [], ru: [] } };

      if (window.__hwPendingClearWatch) { _pendingClear.watch = true; window.__hwPendingClearWatch = false; }
      if (window.__hwPendingClearBacktest) { _pendingClear.backtest = true; window.__hwPendingClearBacktest = false; }

      var tbP = readTomb();
      var body = {
        watch: filterAlive(readLocalWatch(), tbP),
        watch_deleted: tbP.tombs,
        watch_cleared_at: tbP.cleared_at,
        watch_merge: true,
        backtest: snap.backtest,
        signals: snap.signals,
        updated_at: {
          watch: _stateMeta.watch,
          backtest: _stateMeta.backtest,
          signals: _stateMeta.signals
        },
        clear_watch: !!_pendingClear.watch,
        clear_backtest: !!_pendingClear.backtest,
        clear_signals: !!_pendingClear.signals
      };
      _pendingClear = { watch: false, backtest: false, signals: false };

      var data = await api('PUT', '/api/miniapp/state', body);
      if (data && data.updated_at) {
        _stateMeta.watch = data.updated_at.watch || _stateMeta.watch;
        _stateMeta.backtest = data.updated_at.backtest || _stateMeta.backtest;
        _stateMeta.signals = data.updated_at.signals || _stateMeta.signals;
      }
      if (data && data.rejected && data.rejected.length) {
        if (data.rejected.indexOf('watch') >= 0 && Array.isArray(data.watch)) {
          _hydrating = true;
          try {
            var rw = filterAlive(data.watch, readTomb());
            writeLocalWatch(rw);
            if (typeof window.__hwApplyMiniappState === 'function') {
              window.__hwApplyMiniappState({ watch: rw, backtest: data.backtest, signals: data.signals });
            }
          } finally { _hydrating = false; }
        }
        setStatus('сервер: защита от пустой перезаписи');
      } else {
        setStatus('отслеживаемые / бэктест сохранены в Telegram-аккаунте');
      }
      if (data && data.watch_tombstones) {
        writeTomb(mergeTomb(readTomb(), { tombs: data.watch_tombstones, cleared_at: +data.watch_cleared_at || 0 }));
      }
    } catch (e) {
      setStatus('офлайн: watch/бэктест локально (' + (e.message || e) + ')');
      console.warn('[bridge] state push failed', e);
    } finally {
      _stateSyncing = false;
    }
  }

  async function syncStateFromServer() {
    try {
      var data = await api('GET', '/api/miniapp/state');
      hydrateFromServer(data);
      var n = (data.watch && data.watch.length) || 0;
      var btN = 0;
      try {
        btN = ((data.backtest && data.backtest.crypto) || []).length
            + ((data.backtest && data.backtest.ru) || []).length;
      } catch (e0) {}
      setStatus('из аккаунта: отслеживаемых ' + n + ', бэктест ' + btN);
    } catch (e) {
      // Without auth do not mark hydrated — avoids empty PUT wiping nothing useful / retries
      if (e && e.status === 401) {
        _stateHydrated = false;
        console.warn('[bridge] state sync: no auth, local only');
      } else {
        // Network blip: allow later local-only saves to try again, but don't PUT empty on boot
        _stateHydrated = false;
        console.warn('[bridge] state sync failed', e);
      }
    }
  }

  function installStateHooks() {
    if (window.__hwStateHooks) return typeof window.saveWatch === 'function';
    if (typeof window.saveWatch !== 'function') return false;
    // MKT_STATE is declared late in HTML — wait until it exists in page helpers
    ensurePageHelpers();
    // Need MKT_STATE from page; helpers reference it at call time, so OK even if not yet declared
    // But apply/snapshot only work after MKT_STATE script ran:
    var ready = false;
    try {
      ready = typeof window.__hwSnapshotMiniappState === 'function';
    } catch (e) { ready = false; }
    if (!ready) return false;

    // Wait until screener declared MKT_STATE (near end of HTML)
    var mktReady = false;
    try {
      // Probe via snapshot — empty but defined means script ran past MKT_STATE
      var probe = document.querySelector('#mkt');
      mktReady = !!probe && typeof window.__hwApplyMiniappState === 'function';
    } catch (e2) {}
    if (!mktReady) return false;

    window.__hwStateHooks = true;

    var _origSave = window.saveWatch;
    window.saveWatch = function (w) {
      var tbS = readTomb();
      if (Array.isArray(w) && (tbS.cleared_at || Object.keys(tbS.tombs).length)) {
        w = filterAlive(w, tbS);
      }
      _origSave(w);
      if (_hydrating) {
        _dirtyDuringHydrate = true;
        return;
      }
      if (!_hydrating) scheduleStatePush();
    };

    // 👁 un-watch (row eye / scan eye) → tombstone, so server∪local merge can't resurrect it.
    // (Stale async saves, e.g. refreshWatch writing back its old snapshot, are dropped by the
    // tombstone filter in saveWatch above.)
    if (typeof window.toggleWatch === 'function' && !window.toggleWatch.__hwTomb) {
      var _origToggle = window.toggleWatch;
      window.toggleWatch = function (t) {
        try {
          var m = (t && t.mkt) || (typeof window.__hwCurMkt === 'function' ? window.__hwCurMkt() : null) || 'crypto';
          readLocalWatch().forEach(function (x) {
            if (x && t && x.base === t.base && x.t === t.t && (x.mkt || 'crypto') === m) window.__hwWatchTomb(x);
          });
        } catch (eT) {}
        return _origToggle.apply(this, arguments);
      };
      window.toggleWatch.__hwTomb = true;
    }

    if (typeof window.renderBt === 'function') {
      var _origBt = window.renderBt;
      window.renderBt = function () {
        var r = _origBt.apply(this, arguments);
        if (!_hydrating) scheduleStatePush();
        return r;
      };
    }
    if (typeof window.renderScan === 'function') {
      var _origSc = window.renderScan;
      window.renderScan = function () {
        var r = _origSc.apply(this, arguments);
        if (!_hydrating) scheduleStatePush();
        return r;
      };
    }

    var cw = document.getElementById('clearWatch');
    if (cw && !cw.__hwClearHook) {
      cw.__hwClearHook = true;
      cw.addEventListener('click', function () {
        // If user confirms, HTML handler calls saveWatch([]). Mark clear on next empty save.
        window.__hwPendingClearWatch = true;
        setTimeout(function () {
          // If confirm was cancelled, saveWatch([]) never ran — drop the flag
          // (saveWatch would have already consumed it via schedule; if still set and watch non-empty, clear flag)
          if (window.__hwPendingClearWatch && readLocalWatch().length > 0) {
            window.__hwPendingClearWatch = false;
          }
        }, 500);
      }, true);
    }
    var cb = document.getElementById('btClear');
    if (cb && !cb.__hwClearHook) {
      cb.__hwClearHook = true;
      cb.addEventListener('click', function () {
        window.__hwPendingClearBacktest = true;
        setTimeout(function () {
          try {
            var snap = window.__hwSnapshotMiniappState && window.__hwSnapshotMiniappState();
            var empty = snap && !(snap.backtest.crypto && snap.backtest.crypto.length)
              && !(snap.backtest.ru && snap.backtest.ru.length);
            if (window.__hwPendingClearBacktest && !empty) {
              window.__hwPendingClearBacktest = false;
            }
          } catch (e) {}
        }, 500);
      }, true);
    }
    return true;
  }

  function waitStateHooks(cb) {
    if (installStateHooks()) { cb(); return; }
    var n = 0;
    var t = setInterval(function () {
      n++;
      if (installStateHooks() || n > 120) {
        clearInterval(t);
        cb();
      }
    }, 50);
  }


  /** Save ?sync= / ?token= into localStorage (Mini App from bot). Never wipe existing. */
  function restoreSyncFromQuery() {
    try {
      var q = new URLSearchParams(location.search || '');
      var u = q.get('sync') || q.get('sync_url') || q.get('syncUrl') || '';
      var tok = q.get('token') || q.get('sync_token') || q.get('syncToken');
      if (!u && tok) {
        u = (location.origin || '') + '/sync/' + tok;
      }
      if (!u) return;
      try {
        localStorage.setItem('hw_fbo_sync_url', u);
        localStorage.setItem('hw_bot_sync_url', u);
      } catch (e) {}
      if (q.get('autosync') !== '0') window.__HW_AUTOSYNC = true;
    } catch (e) {}
  }

  function start() {
    restoreSyncFromQuery();
    bootTelegram();
    injectBar();
    waitHooks(function () {
      syncFromServer();
    });
    // Additive: watch / backtest / LAST — does not touch templates
    waitStateHooks(function () {
      syncStateFromServer();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
