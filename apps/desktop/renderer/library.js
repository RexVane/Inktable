/* Inktable AI Library renderer module.
 *
 * Loaded as a same-origin external script by preload. It never receives the
 * sidecar token and can only use the controlled main-process API proxy exposed
 * on window.inktable. All API-originated text is written through textContent.
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
    stats: null,
    relations: null,
    enrichment: null,
    detail: null,
    busy: false,
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
    var result = await window.inktable.apiRequest({ path: path, method: method || 'GET' });
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
    var title = head.querySelector('.panel-title');
    if (title) title.textContent = '浏览';

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
    if (top) top.textContent = 'AI 图书馆';
    if (resultsTitle) resultsTitle.textContent = title || '知识馆';
    if (counter) {
      counter.style.display = '';
      counter.textContent = count || '';
    }
  }

  async function enterLibrary() {
    if (state.active || state.busy) return;
    setMode(true);
    state.detail = null;
    if (typeof window.showListPage === 'function') window.showListPage();
    hideFileSurface();
    var el = ensureSurface();
    if (!el) return;
    el.hidden = false;
    setChrome('知识馆', '正在读取…');
    renderLoading('正在整理你的知识馆入口…');

    try {
      // Deterministic metadata sync only. No LLM call is triggered here.
      if (!state.enteredOnce) {
        await api('/library/sync', 'POST');
        state.enteredOnce = true;
      }
      await refreshLibrary(true);
    } catch (err) {
      renderError('知识馆加载失败：' + err.message);
    }
  }

  function leaveLibrary() {
    if (!state.active) return;
    setMode(false);
    state.detail = null;
    var el = surface();
    if (el) el.hidden = true;
    if (typeof window.showListPage === 'function') window.showListPage();
    if (typeof window.updateWorkbenchChrome === 'function') window.updateWorkbenchChrome();
    var q = document.getElementById('q');
    if (typeof window.load === 'function') window.load(q ? q.value.trim() : '');
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

  async function refreshLibrary(resetItems) {
    if (!state.active) return;
    state.busy = true;
    try {
      var offset = resetItems ? 0 : state.items.length;
      var results = await Promise.all([
        api('/library/stats'),
        api('/library/relations/status'),
        api('/library/enrichment/status'),
        api('/library/items?limit=' + PAGE + '&offset=' + offset),
      ]);
      state.stats = results[0];
      state.relations = results[1];
      state.enrichment = results[2];
      var page = results[3];
      state.items = resetItems ? (page.items || []) : state.items.concat(page.items || []);
      state.total = number(page.total);
      state.offset = state.items.length;
      state.detail = null;
      renderOverview();
    } finally {
      state.busy = false;
    }
  }

  function statCard(value, label) {
    var card = node('div', 'library-stat');
    card.appendChild(node('div', 'library-stat-value', value));
    card.appendChild(node('div', 'library-stat-label', label));
    return card;
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

    var enrich = button('AI 整理 3 篇', 'library-action primary', async function () {
      await runAction(enrich, '整理中…', async function () {
        var result = await api('/library/enrich?limit=3', 'POST');
        if (result.available === false) {
          showToast(result.error || '本地整理模型不可用');
          await refreshLibrary(true);
          return;
        }
        showToast('已整理 ' + number(result.ready) + ' 篇' +
          (number(result.stale) ? '，' + number(result.stale) + ' 篇需重试' : ''));
        await refreshLibrary(true);
      });
    });
    bar.appendChild(enrich);

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
        if (window.inktable.openPath) window.inktable.openPath(path);
      }));
      actions.appendChild(button('定位', 'library-mini', function () {
        window.inktable.revealInFinder(path);
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
    if (!window.inktable || !window.inktable.apiRequest) return;
    ensureModeSwitch();
    ensureSurface();
    installKeyboardBoundary();
    window.InktableLibrary = {
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
