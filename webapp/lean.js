/* lean.js — облегчённый режим Mini App поверх исходного HW FBO screener.html.
 * Ничего не удаляет из самого файла: прячет вкладки «Сигналы»(live-run часть)/
 * «Отслеживаю»/«Состояние рынка», превращает «Сигналы» в «Шаблоны» (оставляя
 * только конструктор шаблонов) и добавляет новую вкладку «Статистика» —
 * история сигналов, реально отправленных в Telegram, с авто-отслеживанием
 * исхода (тейк/стоп), которое считает бот на сервере (screener.check_signal_outcomes).
 */
(function () {
  'use strict';

  function initDataHeader() {
    try {
      if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) {
        return String(window.Telegram.WebApp.initData);
      }
    } catch (e) {}
    try {
      var q = new URLSearchParams(location.search);
      return q.get('initData') || '';
    } catch (e2) { return ''; }
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

  async function apiGet(path) {
    var headers = { 'Accept': 'application/json' };
    var id = initDataHeader();
    if (id) headers['X-Telegram-Init-Data'] = id;
    var tok = extractSyncToken();
    if (tok) headers['X-Sync-Token'] = tok;
    var res = await fetch(apiUrl(path), { method: 'GET', headers: headers });
    var text = await res.text();
    var data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
    if (!res.ok) {
      var err = new Error((data && (data.error || data.detail)) || ('HTTP ' + res.status));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function hideField(id) {
    var el = document.getElementById(id);
    if (!el) return;
    var wrap = el.closest ? el.closest('.field') : null;
    (wrap || el).style.display = 'none';
  }

  function hideById(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  }

  var STRAT_LABEL = { brk: '📈 Пробой', fbo: '🔻 Ложный пробой' };
  var MARKET_LABEL = { crypto: '🌐 Крипта', ru: '🇷🇺 MOEX' };
  var STATUS_LABEL = {
    open: '⏳ открыт', win: '🟢 тейк', loss: '🔴 стоп', expired: '⚪ истёк',
  };

  function fmtNum(v, d) {
    if (v === null || v === undefined || v === '') return '—';
    var n = Number(v);
    if (!isFinite(n)) return '—';
    return n.toFixed(d === undefined ? 4 : d);
  }

  function fmtDate(ts) {
    if (!ts) return '—';
    try {
      var d = new Date(Number(ts));
      return d.toLocaleString('ru-RU', {
        day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
      });
    } catch (e) { return '—'; }
  }

  function buildStatsPane() {
    var pane = document.createElement('div');
    pane.id = 'paneStats';
    pane.style.display = 'none';
    pane.innerHTML =
      '<div class="card">' +
        '<div class="row">' +
          '<div class="field"><label>Рынок</label>' +
            '<select id="st_mkt"><option value="">Все</option><option value="crypto">Крипта</option><option value="ru">MOEX</option></select></div>' +
          '<div class="field"><label>Стратегия</label>' +
            '<select id="st_strat"><option value="">Все</option><option value="brk">Пробой</option><option value="fbo">Ложный пробой</option></select></div>' +
          '<div class="field"><label>Статус</label>' +
            '<select id="st_status"><option value="">Все</option><option value="open">Открыт</option><option value="win">Тейк</option><option value="loss">Стоп</option><option value="expired">Истёк</option></select></div>' +
          '<div class="field"><label>Шаблон</label><select id="st_tpl"><option value="">Все</option></select></div>' +
          '<div class="field"><button id="st_refresh">Обновить</button></div>' +
        '</div>' +
        '<div id="st_summary" class="regimeRow" style="margin-top:10px"></div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="scroll"><table><thead><tr>' +
          '<th>Тикер</th><th>Рынок</th><th>Стратегия</th><th>Шаблон</th><th>Сторона</th>' +
          '<th>Вход</th><th>Стоп</th><th>Тейк</th><th>Результат</th><th>Сигнал</th><th>Решено</th>' +
        '</tr></thead><tbody id="st_rows"></tbody></table></div>' +
        '<div class="note" id="st_empty" style="display:none;margin-top:8px">Пока нет отправленных сигналов по этому фильтру.</div>' +
      '</div>';
    return pane;
  }

  function renderSummary(stats) {
    var box = document.getElementById('st_summary');
    if (!box) return;
    var tot = { win: 0, loss: 0, open: 0, expired: 0 };
    (stats || []).forEach(function (s) {
      tot.win += s.win || 0; tot.loss += s.loss || 0;
      tot.open += s.open || 0; tot.expired += s.expired || 0;
    });
    var decided = tot.win + tot.loss;
    var winrate = decided ? Math.round((tot.win / decided) * 100) : null;
    box.innerHTML =
      '<div>Тейк<b style="color:#3fb950">' + tot.win + '</b></div>' +
      '<div>Стоп<b style="color:#f85149">' + tot.loss + '</b></div>' +
      '<div>Открыто<b>' + tot.open + '</b></div>' +
      '<div>Истекло<b>' + tot.expired + '</b></div>' +
      '<div>Винрейт<b>' + (winrate === null ? '—' : winrate + '%') + '</b></div>';
  }

  function renderTplFilter(rows) {
    var sel = document.getElementById('st_tpl');
    if (!sel) return;
    var cur = sel.value;
    var names = Array.from(new Set(rows.map(function (r) { return r.template; }).filter(Boolean)));
    sel.innerHTML = '<option value="">Все</option>' +
      names.map(function (n) { return '<option value="' + n.replace(/"/g, '&quot;') + '">' + n + '</option>'; }).join('');
    if (names.indexOf(cur) >= 0) sel.value = cur;
  }

  function renderRows(rows) {
    var tb = document.getElementById('st_rows');
    var empty = document.getElementById('st_empty');
    if (!tb) return;
    if (!rows.length) {
      tb.innerHTML = '';
      if (empty) empty.style.display = '';
      return;
    }
    if (empty) empty.style.display = 'none';
    tb.innerHTML = rows.map(function (r) {
      var sideColor = r.side === 'LONG' ? '#3fb950' : '#f85149';
      return '<tr>' +
        '<td><b>' + (r.ticker || '') + '</b></td>' +
        '<td>' + (MARKET_LABEL[r.market] || r.market) + '</td>' +
        '<td>' + (STRAT_LABEL[r.strategy] || r.strategy) + '</td>' +
        '<td>' + (r.template || '—') + '</td>' +
        '<td style="color:' + sideColor + '">' + r.side + '</td>' +
        '<td>' + fmtNum(r.entry) + '</td>' +
        '<td>' + fmtNum(r.stop) + '</td>' +
        '<td>' + fmtNum(r.take) + '</td>' +
        '<td>' + (STATUS_LABEL[r.status] || r.status) + '</td>' +
        '<td>' + fmtDate(r.signal_ts) + '</td>' +
        '<td>' + fmtDate(r.outcome_ts) + '</td>' +
        '</tr>';
    }).join('');
  }

  var _statsLoaded = false;

  async function loadStats() {
    try {
      var mkt = document.getElementById('st_mkt').value;
      var strat = document.getElementById('st_strat').value;
      var status = document.getElementById('st_status').value;
      var tpl = document.getElementById('st_tpl').value;
      var qs = [];
      if (mkt) qs.push('market=' + encodeURIComponent(mkt));
      if (strat) qs.push('strategy=' + encodeURIComponent(strat));
      if (status) qs.push('status=' + encodeURIComponent(status));
      if (tpl) qs.push('template=' + encodeURIComponent(tpl));
      qs.push('limit=300');
      var data = await apiGet('/api/miniapp/stats' + (qs.length ? '?' + qs.join('&') : ''));
      var rows = (data && data.rows) || [];
      if (!tpl) renderTplFilter(rows);
      renderRows(rows);
      renderSummary((data && data.stats) || []);
      _statsLoaded = true;
    } catch (e) {
      var tb = document.getElementById('st_rows');
      if (tb) tb.innerHTML = '';
      var empty = document.getElementById('st_empty');
      if (empty) { empty.style.display = ''; empty.textContent = 'Не удалось загрузить статистику: ' + (e.message || e); }
      console.warn('[lean] stats load failed', e);
    }
  }

  function setupStatsTab() {
    var tabs = document.querySelector('.tabs');
    var scanTab = document.getElementById('tabScan');
    var btTab = document.getElementById('tabBt');
    var scanPane = document.getElementById('paneScan');
    var btPane = document.getElementById('paneBt');
    if (!tabs || !scanTab || !btTab || !scanPane || !btPane) return false;

    // Переименовать «Сигналы» → «Шаблоны» (внутри уже прибраны live-run поля)
    scanTab.textContent = 'Шаблоны';

    var statsTab = document.createElement('button');
    statsTab.id = 'tabStats';
    statsTab.textContent = 'Статистика';
    tabs.appendChild(statsTab);

    var statsPane = buildStatsPane();
    scanPane.parentNode.appendChild(statsPane);

    function showStats() {
      scanPane.style.display = 'none';
      btPane.style.display = 'none';
      var w = document.getElementById('paneWatch'); if (w) w.style.display = 'none';
      var r = document.getElementById('paneRegime'); if (r) r.style.display = 'none';
      statsPane.style.display = '';
      scanTab.classList.remove('on');
      btTab.classList.remove('on');
      statsTab.classList.add('on');
      loadStats();
    }
    function hideStats() {
      statsPane.style.display = 'none';
      statsTab.classList.remove('on');
    }

    statsTab.addEventListener('click', showStats);
    scanTab.addEventListener('click', hideStats);
    btTab.addEventListener('click', hideStats);

    ['st_mkt', 'st_strat', 'st_status', 'st_tpl'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener('change', loadStats);
    });
    var refreshBtn = document.getElementById('st_refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', loadStats);

    return true;
  }

  function slimDownScanTab() {
    // Живой скан из браузера больше не нужен — оставляем только конструктор
    // шаблонов (рынок/стратегия/фильтры/кнопки шаблонов), прячем остальное.
    hideById('fldSrcRu');
    hideById('fldSrc');
    hideField('minVol');
    hideField('nInst');
    hideField('win');
    hideField('winUnit');
    hideById('fldNight');
    hideById('fldStocks');
    hideField('run');
    hideById('addWatch');
    hideById('addBt');
    hideById('status');
    hideById('prog');

    // Вкладки «Отслеживаю» и «Состояние рынка» убраны из UI полностью.
    hideById('tabWatch');
    hideById('tabRegime');
    var pw = document.getElementById('paneWatch'); if (pw) pw.style.display = 'none';
    var pr = document.getElementById('paneRegime'); if (pr) pr.style.display = 'none';
  }

  function ready() {
    return !!(document.getElementById('tabScan') && document.getElementById('tabBt')
      && document.getElementById('paneScan') && typeof window.el === 'function');
  }

  function start() {
    var n = 0;
    var t = setInterval(function () {
      n++;
      if (ready() || n > 100) {
        clearInterval(t);
        if (!ready()) { console.warn('[lean] screener DOM not ready, giving up'); return; }
        slimDownScanTab();
        setupStatsTab();
      }
    }, 50);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
