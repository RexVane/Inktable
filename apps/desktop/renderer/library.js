/* Ordo AI Library renderer module.
 *
 * Loaded as a same-origin external script by preload. It never receives the
 * sidecar token and can only use the controlled main-process API proxy exposed
 * on window.ordo. All API-originated text is written through textContent.
 */
(function () {
  'use strict';

  var PAGE = 100;
  var state = {
    active: false,
    items: [],
    total: 0,
    offset: 0,
    filter: '',
    categoryId: null,     // 左栏树选中的分类
    tagId: null,          // 左栏树选中的标签
    taxonomy: null,
    treeItems: [],        // /library/tree 全量叶子
    enriching: false,     // sidecar 正在排水（用户点的或半小时扫描）
    enrichDone: 0,        // 本轮已整理篇数
    enrichFailed: 0,
    enrichStop: false,    // 再点一次按钮 = 停止
    enrichRunId: null,
    enrichMode: null,     // user | idle
    enrichPollTimer: null,
    enrichWasRunning: false,
    treeOpen: (function () {
      try { return JSON.parse(localStorage.getItem('libraryTreeOpen') || '{}') || {}; }
      catch (e) { return {}; }
    })(),
    detailItemId: null,
    stats: null,
    relations: null,
    enrichment: null,
    detail: null,
    busy: false,
    refreshVersion: 0,
    busyVersion: 0,
    enteredOnce: false,
  };

  function node(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  function button(text, cls, onClick) {
    var el = node('button', cls || 'library-action', text);
    el.type = 'button';
    if (onClick) el.addEventListener('click', onClick);
    return el;
  }

  function number(value) {
    var n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }

  function statusLabel(status) {
    return {
      pending: '待整理', running: '整理中', ready: '已整理',
      failed: '整理失败', stale: '需更新',
    }[status] || String(status || '待整理');
  }

  function formatPercent(value) {
    return Math.round(Math.max(0, Math.min(1, number(value))) * 100) + '%';
  }

  function showToast(message) {
    if (typeof window.toast === 'function') return window.toast(message);
    var target = document.getElementById('toast');
    if (!target) return;
    target.textContent = message;
    target.classList.add('show');
    window.setTimeout(function () { target.classList.remove('show'); }, 2800);
  }

  async function api(path, method) {
    var result = await window.ordo.apiRequest({ path: path, method: method || 'GET' });
    if (!result) throw new Error('本地服务未就绪');
    if (!result.ok) {
      var detail = result.data && result.data.detail;
      throw new Error(detail || result.error || ('HTTP ' + (result.status || '?')));
    }
    return result.data || {};
  }

  function resultPanel() { return document.querySelector('.results-panel'); }
  function surface() { return document.getElementById('librarySurface'); }
  function modeButtons() {
    return {
      files: document.getElementById('libraryModeFiles'),
      library: document.getElementById('libraryModeLibrary'),
    };
  }

  function ensureSurface() {
    if (surface()) return surface();
    var panel = resultPanel();
    if (!panel) return null;
    var el = node('div', 'library-surface');
    el.id = 'librarySurface';
    el.hidden = true;
    panel.appendChild(el);
    return el;
  }

  function ensureModeSwitch() {
    var head = document.querySelector('#navPanel > .panel-head');
    if (!head || document.getElementById('libraryModeSwitch')) return;
    // 这一栏只放「文件 / 知识馆」两个档。原先还加一个"浏览"标题，
    // 但那两个按钮本身已经说清了这是什么，标题只是又占一行、又多一层措辞。
    var title = head.querySelector('.panel-title');
    if (title) title.remove();

    var wrap = node('div', 'library-mode-switch');
    wrap.id = 'libraryModeSwitch';
    wrap.setAttribute('role', 'tablist');
    wrap.setAttribute('aria-label', '浏览模式');
    var files = button('文件', 'on', leaveLibrary);
    files.id = 'libraryModeFiles';
    files.setAttribute('role', 'tab');
    files.setAttribute('aria-selected', 'true');
    var library = button('知识馆', '', enterLibrary);
    library.id = 'libraryModeLibrary';
    library.setAttribute('role', 'tab');
    library.setAttribute('aria-selected', 'false');
    wrap.append(files, library);
    head.appendChild(wrap);
  }

  function setMode(active) {
    state.active = active;
    var buttons = modeButtons();
    if (buttons.files) {
      buttons.files.classList.toggle('on', !active);
      buttons.files.setAttribute('aria-selected', active ? 'false' : 'true');
    }
    if (buttons.library) {
      buttons.library.classList.toggle('on', active);
      buttons.library.setAttribute('aria-selected', active ? 'true' : 'false');
    }
  }

  function hideFileSurface() {
    var fileSearch = document.getElementById('fileSearch');
    var rows = document.getElementById('rows');
    var evidence = document.getElementById('evidence');
    var back = document.getElementById('backToList');
    if (fileSearch) fileSearch.style.display = 'none';
    if (rows) rows.style.display = 'none';
    if (evidence) evidence.style.display = 'none';
    if (back) back.style.display = 'none';
  }

  function setChrome(title, count) {
    var top = document.getElementById('topbarScope');
    var resultsTitle = document.getElementById('resultsTitle');
    var counter = document.getElementById('count');
    if (top) top.textContent = '知识馆';
    if (resultsTitle) resultsTitle.textContent = title || '知识馆';
    if (counter) {
      counter.style.display = '';
      counter.textContent = count || '';
    }
  }

  async function enterLibrary() {
    if (state.active) return;
    setMode(true);
    state.detail = null;
    if (typeof window.showListPage === 'function') window.showListPage();
    hideFileSurface();
    var el = ensureSurface();
    if (!el) return;
    el.hidden = false;
    var hasCache = !!(state.stats || state.items.length);
    if (hasCache) {
      setChrome('知识馆', state.total + ' 个知识条目');
      if (!el.querySelector('.library-shell')) renderOverview();
    } else {
      setChrome('知识馆', '正在读取…');
      renderLoading('正在整理你的知识馆入口…');
    }

    try {
      // Deterministic metadata sync only. No LLM call is triggered here.
      if (!state.enteredOnce) {
        await api('/library/sync', 'POST');
        state.enteredOnce = true;
        await refreshLibrary(true);
      } else if (!hasCache) {
        await refreshLibrary(true);
      }
    } catch (err) {
      if (hasCache) showToast('知识馆刷新失败：' + err.message);
      else renderError('知识馆加载失败：' + err.message);
    }
  }

  function leaveLibrary() {
    if (!state.active) return;
    setMode(false);
    state.detail = null;
    // 左栏树留在 DOM 里，CSS 会藏起来；下次进来不必再拼一遍。
    var el = surface();
    if (el) el.hidden = true;
    if (typeof window.showListPage === 'function') window.showListPage();
    if (typeof window.updateWorkbenchChrome === 'function') window.updateWorkbenchChrome();
    if (typeof window.restoreFileList === 'function') window.restoreFileList();
    else {
      var q = document.getElementById('q');
      if (typeof window.load === 'function') window.load(q ? q.value.trim() : '');
    }
  }

  /* 左栏知识馆树：分类按 parent_id 嵌套、可展开收起，
     分类下挂知识条目叶子（点击直接打开知识卡片），标签仍为平铺过滤。 */
  function persistTreeOpen() {
    try { localStorage.setItem('libraryTreeOpen', JSON.stringify(state.treeOpen)); }
    catch (e) { /* 存不下就算了，只影响展开记忆 */ }
  }

  function chevronEl(open) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'ln-chev' + (open ? ' open' : ''));
    svg.setAttribute('viewBox', '0 0 10 10');
    svg.setAttribute('aria-hidden', 'true');
    var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M3 1.6 L7.2 5 L3 8.4');
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '1.7');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(path);
    return svg;
  }

  function itemsUnder(categoryId) {
    return state.treeItems.filter(function (it) {
      return it.category_id === categoryId;
    });
  }

  function paintLibraryNavOn() {
    var tree = document.getElementById('libraryNav');
    if (!tree) return;
    tree.querySelectorAll('.ln-item').forEach(function (row) {
      var on = false;
      if (row.dataset.nav === 'all') on = !state.categoryId && !state.tagId;
      else if (row.dataset.cat === '-1') on = state.categoryId === -1;
      else if (row.dataset.cat) on = String(state.categoryId) === row.dataset.cat;
      else if (row.dataset.tag) on = state.tagId === +row.dataset.tag && !state.categoryId;
      else if (row.dataset.leaf) on = state.detailItemId === +row.dataset.leaf;
      row.classList.toggle('on', on);
    });
    var allCount = tree.querySelector('[data-nav="all"] .ln-count');
    if (allCount) allCount.textContent = String(state.total);
  }

  function selectLibraryFilter(nextCategoryId, nextTagId) {
    state.categoryId = nextCategoryId;
    state.tagId = nextTagId;
    state.detail = null;
    state.detailItemId = null;
    paintLibraryNavOn();
    refreshLibrary(true, { light: true });
  }

  function makeLeaf(item, depth) {
    var isActive = state.detailItemId === item.id;
    var row = node('button', 'ln-item ln-leaf' + (isActive ? ' on' : ''),
                   item.title || '未命名条目');
    row.dataset.leaf = String(item.id);
    row.style.paddingLeft = (9 + depth * 13) + 'px';
    row.title = item.title || '';
    row.addEventListener('click', function () {
      state.detailItemId = item.id;
      paintLibraryNavOn();
      openDetail(item.id);
    });
    return row;
  }

  function makeCategoryRow(cat, depth) {
    var open = !!state.treeOpen[cat.id];
    var row = node('button', 'ln-item ln-dir' + (state.categoryId === cat.id ? ' on' : ''));
    row.dataset.cat = String(cat.id);
    row.style.paddingLeft = (9 + depth * 13) + 'px';
    var label = node('span', 'ln-label', cat.name || '未命名分类');
    row.appendChild(chevronEl(open));
    row.appendChild(label);
    row.appendChild(node('span', 'ln-count', String(cat.count || 0)));
    row.addEventListener('click', function (event) {
      if (event.target.closest('.ln-chev')) {
        state.treeOpen[cat.id] = !open;
        persistTreeOpen();
        renderLibraryNav();
        return;
      }
      var next = state.categoryId === cat.id ? null : cat.id;
      if (next !== null && !state.treeOpen[cat.id]) {
        state.treeOpen[cat.id] = true;
        persistTreeOpen();
        state.categoryId = next;
        state.tagId = null;
        state.detail = null;
        state.detailItemId = null;
        renderLibraryNav();
        refreshLibrary(true, { light: true });
        return;
      }
      selectLibraryFilter(next, null);
    });
    return row;
  }

  function appendCategory(tree, cat, depth, childrenMap) {
    tree.appendChild(makeCategoryRow(cat, depth));
    if (!state.treeOpen[cat.id]) return;
    (childrenMap[cat.id] || []).forEach(function (child) {
      appendCategory(tree, child, depth + 1, childrenMap);
    });
    itemsUnder(cat.id).forEach(function (item, index) {
      if (index >= 50) return;   // 单类叶子太多时截断，用中栏列表看全
      tree.appendChild(makeLeaf(item, depth + 1));
    });
  }

  function renderLibraryNav() {
    var panel = document.getElementById('navPanel');
    if (!panel) return;
    var old = document.getElementById('libraryNav');
    if (old) old.remove();
    if (!state.taxonomy) return;
    var tree = node('div', 'library-nav');
    tree.id = 'libraryNav';

    var all = node('button', 'ln-item' + (!state.categoryId && !state.tagId ? ' on' : ''),
                   '全部条目');
    all.dataset.nav = 'all';
    all.appendChild(node('span', 'ln-count', String(state.total)));
    all.addEventListener('click', function () {
      selectLibraryFilter(null, null);
    });
    tree.appendChild(all);

    var categories = (state.taxonomy.categories || []);
    if (categories.length) {
      tree.appendChild(node('div', 'ln-head', '分类'));
      var childrenMap = {};
      var roots = [];
      categories.forEach(function (cat) {
        if (cat.parent_id && categories.some(function (c) { return c.id === cat.parent_id; })) {
          (childrenMap[cat.parent_id] = childrenMap[cat.parent_id] || []).push(cat);
        } else {
          roots.push(cat);
        }
      });
      roots.forEach(function (root) { appendCategory(tree, root, 0, childrenMap); });

      var uncatItems = itemsUnder(null);
      if (uncatItems.length || state.taxonomy.uncategorized > 0) {
        var uncatOpen = !!state.treeOpen.uncategorized;
        var uncat = node('button', 'ln-item ln-dir' + (state.categoryId === -1 ? ' on' : ''));
        uncat.dataset.cat = '-1';
        uncat.appendChild(chevronEl(uncatOpen));
        uncat.appendChild(node('span', 'ln-label', '未分类'));
        uncat.appendChild(node('span', 'ln-count', String(state.taxonomy.uncategorized)));
        uncat.addEventListener('click', function (event) {
          if (event.target.closest('.ln-chev')) {
            state.treeOpen.uncategorized = !uncatOpen;
            persistTreeOpen();
            renderLibraryNav();
            return;
          }
          var next = state.categoryId === -1 ? null : -1;
          if (next === -1 && !state.treeOpen.uncategorized) {
            state.treeOpen.uncategorized = true;
            persistTreeOpen();
            state.categoryId = -1;
            state.tagId = null;
            renderLibraryNav();
            refreshLibrary(true, { light: true });
            return;
          }
          selectLibraryFilter(next, null);
        });
        tree.appendChild(uncat);
        if (uncatOpen) {
          uncatItems.slice(0, 50).forEach(function (item) {
            tree.appendChild(makeLeaf(item, 1));
          });
        }
      }
    }

    var tags = (state.taxonomy.tags || []);
    if (tags.length) {
      tree.appendChild(node('div', 'ln-head', '标签'));
      tags.slice(0, 30).forEach(function (tag) {
        var isActive = state.tagId === tag.id && !state.categoryId;
        var row = node('button', 'ln-item' + (isActive ? ' on' : ''), tag.name || '未命名标签');
        row.dataset.tag = String(tag.id);
        row.appendChild(node('span', 'ln-count', String(tag.count || 0)));
        row.addEventListener('click', function () {
          selectLibraryFilter(null, isActive ? null : tag.id);
        });
        tree.appendChild(row);
      });
    }

    panel.appendChild(tree);
    renderFilterChip();
  }

  function activeFilterLabel() {
    if (state.categoryId != null) {
      if (state.categoryId === -1) return '未分类';
      var cats = (state.taxonomy && state.taxonomy.categories) || [];
      for (var i = 0; i < cats.length; i++) {
        if (cats[i].id === state.categoryId) return cats[i].name;
      }
      return '分类';
    }
    if (state.tagId != null) {
      var tags = (state.taxonomy && state.taxonomy.tags) || [];
      for (var j = 0; j < tags.length; j++) {
        if (tags[j].id === state.tagId) return tags[j].name;
      }
      return '标签';
    }
    return null;
  }

  function renderFilterChip() {
    var old = document.getElementById('libraryFilterChip');
    if (old) old.remove();
    var label = activeFilterLabel();
    if (!label) return;
    var chip = node('button', 'library-action library-filter-chip', '✕ ' + label);
    chip.id = 'libraryFilterChip';
    chip.title = '清除左栏筛选';
    chip.addEventListener('click', function () {
      selectLibraryFilter(null, null);
    });
    var bar = document.querySelector('.library-toolbar');
    if (bar) bar.insertBefore(chip, bar.firstChild);
  }

  function renderLoading(text) {
    var root = surface();
    if (!root) return;
    root.replaceChildren();
    var shell = node('div', 'library-shell');
    shell.appendChild(node('div', 'library-empty', text || '加载中…'));
    root.appendChild(shell);
  }

  function renderError(text) {
    var root = surface();
    if (!root) return;
    root.replaceChildren();
    var shell = node('div', 'library-shell');
    shell.appendChild(node('div', 'library-note error', text));
    shell.appendChild(button('重试', 'library-action', function () { refreshLibrary(true); }));
    root.appendChild(shell);
    setChrome('知识馆', '加载失败');
  }

  async function refreshLibrary(resetItems, opts) {
    if (!state.active) return;
    opts = opts || {};
    var light = !!opts.light;
    var requestVersion = ++state.refreshVersion;
    var categoryId = state.categoryId;
    var tagId = state.tagId;
    var offset = resetItems ? 0 : state.items.length;
    var itemsUrl = '/library/items?limit=' + PAGE + '&offset=' + offset;
    if (categoryId) itemsUrl += '&category_id=' + categoryId;
    if (tagId) itemsUrl += '&tag_id=' + tagId;
    if (light) {
      try {
        var lightPage = await api(itemsUrl);
        if (!state.active || requestVersion !== state.refreshVersion) return;
        state.items = resetItems ? (lightPage.items || []) : state.items.concat(lightPage.items || []);
        state.total = number(lightPage.total);
        state.offset = state.items.length;
        state.detail = null;
        paintLibraryNavOn();
        renderOverview();
      } catch (err) {
        if (requestVersion === state.refreshVersion) {
          showToast('筛选失败：' + err.message);
        }
      }
      return;
    }
    state.busy = true;
    state.busyVersion = requestVersion;
    try {
      var results = await Promise.all([
        api('/library/stats'),
        api('/library/relations/status'),
        api('/library/enrichment/status'),
        api(itemsUrl),
        api('/library/taxonomy'),
        api('/library/tree'),
      ]);
      if (!state.active || requestVersion !== state.refreshVersion) return;
      state.stats = results[0];
      state.relations = results[1];
      state.enrichment = results[2];
      applyWorkerStatus(results[2], { toast: false });
      var page = results[3];
      state.taxonomy = results[4];
      state.treeItems = results[5] && results[5].items || [];
      renderLibraryNav();
      state.items = resetItems ? (page.items || []) : state.items.concat(page.items || []);
      state.total = number(page.total);
      state.offset = state.items.length;
      state.detail = null;
      renderOverview();
    } finally {
      if (state.busyVersion === requestVersion) state.busy = false;
    }
  }

  function statCard(value, label) {
    var card = node('div', 'library-stat');
    card.appendChild(node('div', 'library-stat-value', value));
    card.appendChild(node('div', 'library-stat-label', label));
    return card;
  }

  function stopEnrichPoll() {
    if (!state.enrichPollTimer) return;
    clearInterval(state.enrichPollTimer);
    state.enrichPollTimer = null;
  }

  function startEnrichPoll() {
    if (state.enrichPollTimer) return;
    state.enrichPollTimer = setInterval(function () {
      pollEnrichmentWorker();
    }, 2500);
  }

  function applyWorkerStatus(payload, opts) {
    opts = opts || {};
    var worker = (payload && payload.worker) || {};
    var running = !!worker.running;
    var was = state.enrichWasRunning;
    state.enriching = running;
    state.enrichRunId = worker.run_id || null;
    state.enrichDone = number(worker.ready);
    state.enrichFailed = number(worker.failed);
    state.enrichMode = worker.mode || null;
    state.enrichStop = !!worker.stopping;
    if (running) startEnrichPoll();
    else stopEnrichPoll();
    if (opts.toast && was && !running) {
      var failed = state.enrichFailed;
      var idle = worker.last_mode === 'idle';
      if (worker.error) {
        showToast(worker.error);
      } else if (state.enrichStop) {
        showToast('已停止：完成 ' + state.enrichDone + ' 篇' +
          (failed ? '，失败 ' + failed + ' 篇' : ''));
      } else if (state.enrichDone || failed) {
        showToast((idle ? '后台整理完成 ' : '整理完成 ') +
          state.enrichDone + ' 篇' +
          (failed ? '，失败 ' + failed + ' 篇（需点击“重试失败项”）' : ''));
      } else if (!idle) {
        showToast('没有需要整理的条目');
      }
      if (state.active) refreshLibrary(true);
    }
    state.enrichWasRunning = running;
    var btn = document.getElementById('libraryEnrichBtn');
    if (btn) {
      btn.textContent = enrichButtonLabel();
      btn.disabled = !!worker.stopping;
    }
  }

  async function pollEnrichmentWorker() {
    try {
      var st = await api('/library/enrichment/status');
      state.enrichment = st;
      applyWorkerStatus(st, { toast: true });
    } catch (err) {
      /* sidecar 重启时下一次轮询会接上 */
    }
  }

  async function stopEnrichmentPass() {
    state.enrichStop = true;
    try {
      var snap = await api('/library/enrichment/drain/cancel', 'POST');
      applyWorkerStatus({ worker: snap }, { toast: false });
      showToast('将在当前批次完成后停止');
    } catch (err) {
      showToast('停止请求失败：' + err.message);
    }
  }

  async function runEnrichmentPass(retryFailed) {
    if (state.enriching) {
      await stopEnrichmentPass();
      return;
    }
    try {
      var snap = await api(
        retryFailed ? '/library/enrichment/drain?retry_failed=true'
          : '/library/enrichment/drain',
        'POST');
      applyWorkerStatus({ worker: snap }, { toast: false });
      if (!snap.running && !state.enriching) {
        showToast(retryFailed ? '没有需要重试的失败项' : '没有需要整理的条目');
      } else {
        showToast(retryFailed ? '开始重试失败项' : '开始整理全部待办条目');
        startEnrichPoll();
      }
      if (state.active) renderOverview();
    } catch (err) {
      showToast('整理失败：' + err.message);
    }
  }

  function enrichButtonLabel() {
    if (state.enrichStop && state.enriching) return '正在停止…';
    if (state.enriching) {
      var prefix = state.enrichMode === 'idle' ? '后台整理中… 已完 ' : '整理中… 已完 ';
      return prefix + state.enrichDone + ' 篇（点击停止）';
    }
    return 'AI 整理全部';
  }

  function actionToolbar(shell) {
    var bar = node('div', 'library-toolbar');
    var search = node('input', 'library-search');
    search.type = 'search';
    search.id = 'librarySearch';
    search.placeholder = '筛选已加载的知识条目…';
    search.value = state.filter;
    search.addEventListener('input', function () {
      state.filter = search.value.trim().toLowerCase();
      renderItemGrid();
    });
    bar.appendChild(search);

    var sync = button('同步馆藏', 'library-action', async function () {
      await runAction(sync, '同步中…', async function () {
        await api('/library/sync', 'POST');
        await refreshLibrary(true);
        showToast('知识馆已同步');
      });
    });
    bar.appendChild(sync);

    // 点一次交给 sidecar 排水到空；进行中再点一次会停止认领下一批。
    var enrich = button(
      enrichButtonLabel(),
      'library-action primary', async function () {
        await runEnrichmentPass(false);
      });
    enrich.id = 'libraryEnrichBtn';
    bar.appendChild(enrich);

    if (!state.enriching && number((state.stats || {}).by_status &&
        state.stats.by_status.failed)) {
      var retryFailed = button('重试失败项', 'library-action', async function () {
        await runEnrichmentPass(true);
      });
      bar.appendChild(retryFailed);
    }

    var rebuild = button('重建相关资料', 'library-action', async function () {
      await runAction(rebuild, '计算中…', async function () {
        var result = await api('/library/relations/rebuild', 'POST');
        showToast('已生成 ' + number(result.relations) + ' 条相关资料关系');
        await refreshLibrary(true);
      });
    });
    bar.appendChild(rebuild);
    shell.appendChild(bar);
  }

  async function runAction(control, busyText, fn) {
    if (state.busy) return;
    state.busy = true;
    var previous = control.textContent;
    control.disabled = true;
    control.textContent = busyText;
    try {
      await fn();
    } catch (err) {
      showToast('操作失败：' + err.message);
    } finally {
      state.busy = false;
      control.disabled = false;
      control.textContent = previous;
    }
  }

  function renderOverview() {
    if (!state.active) return;
    var root = surface();
    root.replaceChildren();
    var shell = node('div', 'library-shell');
    actionToolbar(shell);

    var stats = state.stats || {};
    var by = stats.by_status || {};
    var relations = state.relations || {};
    var statusGrid = node('div', 'library-status-grid');
    statusGrid.append(
      statCard(number(stats.total), '馆藏知识条目'),
      statCard(number(by.ready), '用户摘要已完成'),
      statCard(number(stats.tagged), '已有主题标签'),
      statCard(number(relations.relations), '有效相关关系')
    );
    shell.appendChild(statusGrid);

    if (state.enrichment && state.enrichment.available === false) {
      shell.appendChild(node('div', 'library-note warn',
        'AI 整理暂不可用：需要本机 Ollama 的 ' +
        String(state.enrichment.model || 'qwen3:8b') + '。搜索、向量检索和知识浏览不受影响。'));
    } else {
      shell.appendChild(node('div', 'library-note',
        '点「AI 整理全部」会把当前待办一次做完；之后每半小时自动扫一遍未整理条目，慢慢补。失败项仍需点「重试失败项」。'));
    }
    if (relations.stale_relations) {
      shell.appendChild(node('div', 'library-note warn',
        number(relations.stale_relations) + ' 条相关关系已因文档更新失效，点击“重建相关资料”即可刷新。'));
    } else if (relations.total_visible >= 2 && relations.needs_rebuild) {
      shell.appendChild(node('div', 'library-note',
        '知识条目已经就绪，但还没有构建文档关系。重建会复用现有 bge-m3 向量，不会重新上传或复制文件。'));
    }

    var head = node('div', 'library-section-head');
    head.appendChild(node('div', 'library-section-title', '馆藏'));
    head.appendChild(node('div', 'library-section-meta',
      '已加载 ' + state.items.length + ' / ' + state.total));
    shell.appendChild(head);
    var grid = node('div', 'library-grid');
    grid.id = 'libraryGrid';
    shell.appendChild(grid);
    var more = node('div', 'library-more');
    more.id = 'libraryMore';
    shell.appendChild(more);
    root.appendChild(shell);
    renderItemGrid();
    setChrome('知识馆', state.total + ' 个知识条目');
  }

  function matches(item) {
    if (!state.filter) return true;
    return [item.title, item.summary, item.category_name, item.item_type, item.language]
      .some(function (value) { return String(value || '').toLowerCase().includes(state.filter); });
  }

  function renderItemGrid() {
    var grid = document.getElementById('libraryGrid');
    var more = document.getElementById('libraryMore');
    if (!grid || !more) return;
    grid.replaceChildren();
    more.replaceChildren();
    var filtered = state.items.filter(matches);
    if (!filtered.length) {
      grid.appendChild(node('div', 'library-empty',
        state.filter ? '已加载条目中没有匹配项' : '知识馆还没有条目'));
    } else {
      filtered.forEach(function (item) { grid.appendChild(itemCard(item)); });
    }
    if (!state.filter && state.items.length < state.total) {
      more.appendChild(button('加载更多', 'library-action', async function () {
        try { await refreshLibrary(false); } catch (err) { showToast('加载失败：' + err.message); }
      }));
    }
  }

  function itemCard(item) {
    var card = node('article', 'library-card');
    card.tabIndex = 0;
    card.dataset.libraryItem = String(item.id);
    var head = node('div', 'library-card-head');
    head.appendChild(node('span', 'library-kind', item.item_type || 'doc'));
    head.appendChild(node('div', 'library-card-title', item.title || '未命名资料'));
    head.appendChild(node('span', 'library-badge ' + (item.enrichment_status || ''),
      statusLabel(item.enrichment_status)));
    card.appendChild(head);
    card.appendChild(node('div', 'library-card-summary',
      item.summary || (item.enrichment_status === 'running'
        ? '本地模型正在整理这份资料…'
        : '尚未生成知识卡片摘要；检索专用摘要不会在这里展示。')));
    var meta = node('div', 'library-card-meta');
    if (item.category_name) meta.appendChild(node('span', '', item.category_name));
    if (item.language) meta.appendChild(node('span', '', item.language));
    meta.appendChild(node('span', '', number(item.source_file_count) + ' 个来源位置'));
    card.appendChild(meta);
    card.addEventListener('click', function () { openDetail(item.id); });
    card.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openDetail(item.id);
      }
    });
    return card;
  }

  async function openDetail(itemId) {
    if (!state.active) return;
    state.detailItemId = itemId;
    renderLoading('正在读取知识卡片…');
    try {
      state.detail = await api('/library/items/' + encodeURIComponent(itemId));
      renderDetail();
    } catch (err) {
      renderError('知识卡片加载失败：' + err.message);
    }
  }

  function renderDetail() {
    var item = state.detail;
    if (!item || !state.active) return renderOverview();
    var root = surface();
    root.replaceChildren();
    var shell = node('div', 'library-shell');

    var head = node('div', 'library-detail-head');
    var back = button('← 返回', 'library-action library-back', function () {
      state.detail = null;
      renderOverview();
    });
    head.appendChild(back);
    var titleWrap = node('div', 'library-source-main');
    titleWrap.appendChild(node('div', 'library-detail-title', item.title || '未命名资料'));
    var meta = [item.item_type, item.category_name, item.language,
      statusLabel(item.enrichment_status)].filter(Boolean).join(' · ');
    titleWrap.appendChild(node('div', 'library-detail-meta', meta));
    head.appendChild(titleWrap);
    shell.appendChild(head);

    var summaryHead = node('div', 'library-section-head');
    summaryHead.appendChild(node('div', 'library-section-title', '知识卡片摘要'));
    summaryHead.appendChild(node('div', 'library-section-meta',
      '面向用户 · 与检索摘要分离' +
      (item.enrichment_model ? ' · ' + item.enrichment_model : '')));
    shell.appendChild(summaryHead);
    shell.appendChild(node('div', 'library-summary-box', item.summary ||
      (item.enrichment_status === 'stale'
        ? '原知识摘要已因文档内容更新而过期，请重新执行 AI 整理。'
        : '这份资料还没有生成知识摘要。')));

    var tags = Array.isArray(item.tags) ? item.tags : [];
    var tagHead = node('div', 'library-section-head');
    tagHead.appendChild(node('div', 'library-section-title', '主题标签'));
    tagHead.appendChild(node('div', 'library-section-meta', tags.length + ' 个'));
    shell.appendChild(tagHead);
    var tagRow = node('div', 'library-tag-row');
    if (tags.length) tags.forEach(function (tag) {
      tagRow.appendChild(node('span', 'library-badge', tag.name));
    });
    else tagRow.appendChild(node('span', 'library-section-meta', '暂未标注'));
    shell.appendChild(tagRow);

    var sources = Array.isArray(item.source_files) ? item.source_files : [];
    var sourceHead = node('div', 'library-section-head');
    sourceHead.appendChild(node('div', 'library-section-title', '真实文件来源'));
    sourceHead.appendChild(node('div', 'library-section-meta', sources.length + ' 个位置'));
    shell.appendChild(sourceHead);
    if (!sources.length) {
      shell.appendChild(node('div', 'library-note', '当前没有可见的原始文件位置。'));
    } else {
      sources.forEach(function (source) { shell.appendChild(sourceRow(source)); });
    }

    var related = Array.isArray(item.related) ? item.related : [];
    var relatedHead = node('div', 'library-section-head');
    relatedHead.appendChild(node('div', 'library-section-title', '相关资料'));
    relatedHead.appendChild(node('div', 'library-section-meta', related.length + ' 条'));
    shell.appendChild(relatedHead);
    if (!related.length) {
      shell.appendChild(node('div', 'library-note',
        '暂时没有有效关系。关系只在互为 Top-K 且相似度达到门槛时建立。'));
    } else {
      related.forEach(function (rel) { shell.appendChild(relatedRow(rel)); });
    }

    root.appendChild(shell);
    setChrome(item.title || '知识卡片', '知识卡片');
  }

  function sourceRow(source) {
    var row = node('div', 'library-source');
    var path = source.state === 'missing' && source.preserved_path
      ? source.preserved_path : (source.path || source.preserved_path || '');
    var main = node('div', 'library-source-main');
    main.appendChild(node('div', 'library-source-name', source.name || '未命名文件'));
    main.appendChild(node('div', 'library-source-path', path));
    row.appendChild(main);
    var actions = node('div', 'library-source-actions');
    if (path) {
      actions.appendChild(button('打开', 'library-mini', function () {
        if (window.ordo.openPath) window.ordo.openPath(path);
      }));
      actions.appendChild(button('定位', 'library-mini', function () {
        window.ordo.revealInFinder(path);
      }));
    }
    row.appendChild(actions);
    return row;
  }

  function relatedRow(rel) {
    var row = node('div', 'library-related');
    row.tabIndex = 0;
    var main = node('div', 'library-related-main');
    main.appendChild(node('div', 'library-related-name', rel.title || '相关资料'));
    main.appendChild(node('div', 'library-related-summary', rel.summary || '点击打开知识卡片'));
    row.appendChild(main);
    if (rel.score !== null && rel.score !== undefined) {
      row.appendChild(node('div', 'library-score', formatPercent(rel.score)));
    }
    row.addEventListener('click', function () { openDetail(rel.id); });
    row.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openDetail(rel.id);
      }
    });
    return row;
  }

  function installKeyboardBoundary() {
    document.addEventListener('keydown', function (event) {
      if (!state.active) return;
      var sheet = document.getElementById('sheet');
      if (sheet && sheet.classList.contains('show')) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'f') {
        event.preventDefault();
        event.stopImmediatePropagation();
        var search = document.getElementById('librarySearch');
        if (search) { search.focus(); search.select(); }
      } else if (event.key === 'Escape' && state.detail) {
        event.preventDefault();
        event.stopImmediatePropagation();
        state.detail = null;
        renderOverview();
      }
    }, true);
  }

  function boot() {
    if (!window.ordo || !window.ordo.apiRequest) return;
    ensureModeSwitch();
    ensureSurface();
    installKeyboardBoundary();
    window.OrdoLibrary = {
      enter: enterLibrary,
      leave: leaveLibrary,
      refresh: function () { return refreshLibrary(true); },
      isActive: function () { return state.active; },
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
