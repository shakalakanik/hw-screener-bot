/* lean.js — облегчённый режим Mini App: только «Отслеживаю» и «Шаблоны».
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
    var panes = ['paneScan', 'paneWatch', 'paneBt', 'paneRegime'];
    var tabs = ['tabScan', 'tabWatch', 'tabBt', 'tabRegime'];
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

  function selectDefaultTab() {
    // С кнопки «Посмотреть сигнал» путь /app/watch — всегда эта вкладка.
    if (pathWantsWatch() || (parseDeepLink() && parseDeepLink().tab === 'watch')) {
      showWatchPane();
      return;
    }
    var tabWatch = document.getElementById('tabWatch');
    if (tabWatch) tabWatch.click();
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
        selectDefaultTab();
        focusFromLink();
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
