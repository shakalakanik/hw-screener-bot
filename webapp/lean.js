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
      && typeof window.el === 'function' && typeof window.currentTplObj === 'function');
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
    if (tabBt) tabBt.innerHTML = 'Шаблоны';
    var sect = document.querySelector('#paneBt .sect span');
    if (sect) sect.textContent = 'Все фильтры отбора — «авто» значит фильтр выключен';
  }

  function selectDefaultTab() {
    // По умолчанию открываем «Отслеживаю» — это то, что смотрят чаще всего.
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
      }
    }, 50);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
