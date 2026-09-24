/* bridge.js — Telegram Mini App ↔ backend templates sync
 * Works without Telegram SDK (browser testing via localStorage only).
 */
(function () {
  'use strict';

  var LS_KEY = 'hw_fbo_tpl';
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
    if (TG && TG.initData) return TG.initData;
    try {
      var q = new URLSearchParams(location.search);
      return q.get('initData') || '';
    } catch (e) {
      return '';
    }
  }

  function apiUrl(path) {
    var u = path;
    var id = initDataHeader();
    var q = new URLSearchParams(location.search);
    if (q.get('debug_user_id') && !id) {
      u += (u.indexOf('?') >= 0 ? '&' : '?') + 'debug_user_id=' + encodeURIComponent(q.get('debug_user_id'));
    } else if (id) {
      u += (u.indexOf('?') >= 0 ? '&' : '?') + 'initData=' + encodeURIComponent(id);
    }
    return u;
  }

  function apiHeaders() {
    var h = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
    var id = initDataHeader();
    if (id) h['X-Telegram-Init-Data'] = id;
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

  function readLocalTpl() {
    try { return JSON.parse(localStorage.getItem(LS_KEY) || '{}'); } catch (e) { return {}; }
  }

  function writeLocalTpl(obj) {
    try { localStorage.setItem(LS_KEY, JSON.stringify(obj || {})); } catch (e) {}
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

  function mergeTpl(remote) {
    var local = readLocalTpl();
    var flatRemote = unwrapRemoteTemplates(remote || {});
    var out = Object.assign({}, local, flatRemote);
    writeLocalTpl(out);
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
      // Fire-and-forget sync to backend
      api('PUT', '/api/templates', { templates: o })
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
    try {
      var data = await api('GET', '/api/templates');
      var remoteRaw = (data && data.templates) || {};
      var remote = unwrapRemoteTemplates(remoteRaw);
      var local = readLocalTpl();
      var remoteKeys = Object.keys(remote);
      var localKeys = Object.keys(local || {});

      // Never wipe local with empty remote; push local up instead
      if (!remoteKeys.length && localKeys.length) {
        setStatus('сервер пуст — храню локальные, отправляю на сервер');
        api('PUT', '/api/templates', { templates: local })
          .then(function () { setStatus('локальные шаблоны залиты на сервер'); })
          .catch(function (e) {
            setStatus('сервер пуст, локальные сохранены (' + (e.message || e) + ')');
          });
        if (typeof window.fillTplSelects === 'function') window.fillTplSelects();
        return;
      }

      if (remoteKeys.length) {
        mergeTpl(remote);
      }
      // Never call saveTplStore({}) from sync
      if (typeof window.fillTplSelects === 'function') window.fillTplSelects();
      if (data && data.active) {
        var act = data.active;
        var label = Array.isArray(act)
          ? act.join(', ')
          : (act.names ? (act.names || []).join(', ') : String(act));
        setStatus(label ? ('активный для бота: ' + label) : 'шаблоны синхронизированы');
      } else {
        setStatus('шаблоны синхронизированы');
      }
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
      '<button type="button" id="tg-bridge-to-bot">Шаблоны → боту</button>' +
      '<span id="tg-bridge-status"></span>';
    var wrap = document.querySelector('.wrap') || document.body;
    wrap.insertBefore(bar, wrap.firstChild);
    document.getElementById('tg-bridge-to-bot').addEventListener('click', function () {
      pushFiltersToBot();
    });
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
  var _statePushTimer = null;
  var _pendingClear = { watch: false, backtest: false, signals: false };
  var _stateSyncing = false;

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
      '  var bt = { crypto: [], ru: [] }, sig = { crypto: [], ru: [] };',
      '  try {',
      '    if (typeof MKT_STATE === "object" && MKT_STATE) {',
      '      if (typeof MKT === "string" && MKT_STATE[MKT]) {',
      '        MKT_STATE[MKT].BT = (typeof BT !== "undefined" && Array.isArray(BT)) ? BT : (MKT_STATE[MKT].BT || []);',
      '        MKT_STATE[MKT].LAST = (typeof LAST !== "undefined" && Array.isArray(LAST)) ? LAST : (MKT_STATE[MKT].LAST || []);',
      '      }',
      '      ["crypto","ru"].forEach(function (m) {',
      '        var s = MKT_STATE[m] || {};',
      '        bt[m] = Array.isArray(s.BT) ? s.BT.slice() : [];',
      '        sig[m] = Array.isArray(s.LAST) ? s.LAST.slice() : [];',
      '      });',
      '    } else {',
      '      var m = (typeof MKT === "string" && MKT === "ru") ? "ru" : "crypto";',
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
      '      ["crypto","ru"].forEach(function (m) {',
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
      'window.__hwMarkClearWatch = function () { window.__hwPendingClearWatch = true; };',
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

      var serverWatch = Array.isArray(data.watch) ? data.watch : [];
      var localWatch = readLocalWatch();
      var cleared = data.cleared || {};
      // First deploy / empty server: keep local cache and seed server (do not wipe local).
      // If user explicitly cleared on server (tombstone), respect empty.
      if (!serverWatch.length && localWatch.length && !cleared.watch) {
        data = Object.assign({}, data, { watch: localWatch });
        needSeed = true;
      } else {
        writeLocalWatch(serverWatch);
      }

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
    if (needSeed) {
      // Push local→server once so account gets existing device data
      scheduleStatePush();
    }
  }

  function scheduleStatePush() {
    if (!_stateHydrated || _hydrating) return;
    clearTimeout(_statePushTimer);
    _statePushTimer = setTimeout(pushStateToServer, 700);
  }
  window.__hwScheduleStatePush = scheduleStatePush;

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

      var body = {
        watch: readLocalWatch(),
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
            writeLocalWatch(data.watch);
            if (typeof window.__hwApplyMiniappState === 'function') {
              window.__hwApplyMiniappState({ watch: data.watch, backtest: data.backtest, signals: data.signals });
            }
          } finally { _hydrating = false; }
        }
        setStatus('сервер: защита от пустой перезаписи');
      } else {
        setStatus('отслеживаемые / бэктест сохранены в Telegram-аккаунте');
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
      _origSave(w);
      if (!_hydrating) scheduleStatePush();
    };

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
