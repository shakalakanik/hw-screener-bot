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

  function mergeTpl(remote) {
    var local = readLocalTpl();
    var out = Object.assign({}, local, remote || {});
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
      // Fire-and-forget sync to backend
      api('PUT', '/api/templates', { templates: o || {} })
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
      var remote = (data && data.templates) || {};
      mergeTpl(remote);
      if (typeof window.fillTplSelects === 'function') window.fillTplSelects();
      if (data && data.active) {
        setStatus('активный для бота: ' + data.active);
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

  function start() {
    bootTelegram();
    injectBar();
    waitHooks(function () {
      syncFromServer();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
