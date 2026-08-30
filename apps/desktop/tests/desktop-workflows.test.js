const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const rendererPath = path.resolve(__dirname, '..', 'renderer', 'index.html');
const renderer = fs.readFileSync(rendererPath, 'utf8');
// 全部内联脚本，不只第一段：主题引导在 <head>、工作台在 </body> 之后，
// 只取首个匹配会让 9 万字符的主脚本失去语法校验。
const scripts = [...renderer.matchAll(/<script>([\s\S]*?)<\/script>/g)];

function sourceBetween(start, end) {
  const from = renderer.indexOf(start);
  const to = renderer.indexOf(end, from);
  assert.notEqual(from, -1, `missing source marker: ${start}`);
  assert.notEqual(to, -1, `missing source marker: ${end}`);
  return renderer.slice(from, to).trim();
}

test('settings dropdowns use a themed menu instead of the native OS popup', () => {
  const cssPath = path.resolve(__dirname, '..', 'renderer', 'workbench.css');
  const css = fs.readFileSync(cssPath, 'utf8');
  assert.match(renderer, /function mountThemedSelect\(/);
  assert.match(renderer, /closeThemedSelects\(/);
  assert.match(css, /\.tsel-menu\b/);
  assert.match(css, /\.tsel-btn\b/);
  assert.match(css, /background: var\(--surface\)/);
  assert.match(css, /var\(--shadow-pop\)/);
  assert.doesNotMatch(css, /select\s*\{[^}]*appearance:\s*none/);
});

test('every renderer inline script remains valid JavaScript', () => {
  assert.ok(scripts.length > 0, 'renderer must contain an inline script');
  for (const [i, match] of scripts.entries()) {
    assert.doesNotThrow(
      () => new vm.Script(match[1], { filename: `${rendererPath} [script ${i + 1}]` }),
      `inline script #${i + 1} is not valid JavaScript`,
    );
  }
});

test('file navigation delegates filters and pagination to the API', () => {
  assert.match(renderer, /params\.set\('source', activeSource\)/);
  assert.match(renderer, /params\.set\('ext', activeExt\)/);
  assert.match(renderer, /params\.set\('duplicate', 'true'\)/);
  assert.match(renderer, /offset: String\(append \? files\.length : 0\)/);
  assert.match(renderer, /filesTotal = Math\.max\(0, safeNumber\(d\.total\)\)/);
  assert.match(renderer, /load\(currentQuery, true\)/);
  assert.match(renderer, /id="loadMore"/);
  assert.doesNotMatch(renderer, /files = files\.filter\(function \(f\) \{ return f\.source_name === activeSource;/);
});

test('book search, indexing drain, and source preview stay wired end to end', () => {
  assert.match(renderer, /q: q, limit: 40, book_id: activeBook/);
  assert.match(renderer, /if \(version !== searchVersion\) return;/);
  assert.match(renderer, /while \(safeNumber\(st\.pending\) > 0\)/);
  assert.match(renderer, /await api\('\/index\/run', \{ method: 'POST'/);
  assert.match(renderer, /pendingAfter >= pendingBefore/);
  assert.match(renderer, /function runIndexing\([^)]*\)[\s\S]*?indexingPromise/);
  assert.match(renderer, /async function previewAndConfirmSource/);
  assert.match(renderer, /api\('\/sources\/preview'/);
  assert.match(renderer, /await previewAndConfirmSource\(s, hint\)/);
  assert.match(renderer, /await previewAndConfirmSource\(source, hint\)/);
  assert.match(renderer, /if \(!enabled\)[\s\S]*?return;/);
  assert.match(renderer, /function executeQuery\(q\) \{\s*clearTimeout\(timer\);\s*timer = null;/);
});

test('file list, evidence, and knowledge answer columns coexist without legacy modes', () => {
  const layout = sourceBetween('<div class="content-layout">', '<div class="toast"');
  assert.match(layout, /<section class="nav-panel"[^>]*aria-label="文件库导航"/);
  assert.match(layout, /<section class="results-panel"[^>]*aria-label="文件管理"/);
  assert.match(layout, /<aside class="qa-panel"[^>]*aria-label="知识问答"/);
  assert.match(layout, /id="nav"/);
  assert.match(layout, /id="rows"/);
  assert.match(layout, /id="evidence"/);
  assert.match(layout, /id="backToList"/);
  assert.match(layout, /id="answerRows"/);

  assert.doesNotMatch(renderer, /\b(?:activeView|setActiveView|workbenchNav|libraryNav)\b/);
  assert.doesNotMatch(renderer, /\b(?:queryMode|setQueryMode)\b|class="mode-button"|data-mode=/);
});

test('knowledge questions render only in the answer column', async () => {
  const askSource = sourceBetween('async function runAsk(', 'function refHtml(');
  assert.match(askSource, /getElementById\('answerRows'\)/);
  assert.doesNotMatch(askSource, /getElementById\('rows'\)/);

  const answerRows = {
    innerHTML: '',
    querySelectorAll() { return []; },
  };
  const filesRows = { innerHTML: 'file list stays visible' };
  const submit = { disabled: false };
  const context = {
    apiStream: async (requestPath, payload, onEvent) => {
      assert.equal(requestPath, '/ask/stream');
      assert.equal(JSON.stringify(payload), JSON.stringify(
        { question: '什么是银行家算法？', book_id: null, history: [], mode: 'deep' }));
      onEvent('chat.finalize', {
        status: 'answered', answer: '用于避免死锁 [C1]', mode: 'knowledge',
        retrieved: [], hedge: '', validation: {},
      });
      onEvent('chat.citations', { citations: [] });
    },
    document: {
      getElementById(id) {
        if (id === 'answerRows') return answerRows;
        if (id === 'rows') return filesRows;
        if (id === 'askSubmit') return submit;
        return null;
      },
    },
    showEvidence() {},
    sheetOpen() {},
    refHtml() { return ''; },
    setTimeout,
  };

  await vm.runInNewContext(`
    let activeBook = null;
    let llmInfo = { configured: true, available: true };
    let qaMode = 'deep';
    function esc(value) { return String(value); }
    ${askSource}
    runAsk('什么是银行家算法？')
  `, context);

  assert.match(answerRows.innerHTML, /用于避免死锁/);
  assert.equal(filesRows.innerHTML, 'file list stays visible');
});

test('clicking a file row switches the middle column to the detail page', async () => {
  // 从"中栏两页切换"注释块开始截取，才能带上 showListPage/showDetailPage 与文件查看器
  const evidenceSource = sourceBetween('let listTitle', 'async function api(');
  const rowsSource = sourceBetween('function renderRows(', '/* 右键归类');
  const escapingSource = sourceBetween('function esc(', 'function highlight(');
  const evidence = { innerHTML: '', style: {}, onscroll: null };
  const readerBody = {
    html: '',
    innerHTML: '',
    insertAdjacentHTML(_position, chunk) { this.html += chunk; },
  };
  const readerFoot = { textContent: '' };
  const fileSearch = { style: {} };
  const backButton = { style: {} };
  // .md 现在有原版式渲染，详情页会给出「原文 / 提取文本」切换并默认开原文。
  // 这三个元素真实渲染时就在详情 HTML 里，stub 也必须提供，否则 setView 会
  // 在 null 上设 onclick。
  const toggleButton = () => ({
    classList: { toggle() {} },
    onclick: null,
  });
  const vtOriginal = toggleButton();
  const vtText = toggleButton();
  const vtHint = { textContent: '' };
  const countEl = { style: {}, textContent: '' };
  const titleEl = { textContent: '' };
  const selectedClasses = new Set();
  const row = {
    dataset: {},
    classList: {
      add(name) { selectedClasses.add(name); },
      remove(name) { selectedClasses.delete(name); },
    },
  };
  const rows = {
    innerHTML: '',
    style: {},
    querySelectorAll(selector) {
      if (selector === '.row') return [row];
      if (selector === '.row.on') return [];
      return [];
    },
  };
  const context = {
    ICONS: {},
    fmtSize: () => '',
    fmtDate: () => '',
    api: async (requestPath) => {
      if (requestPath === '/files/7/detail') {
        return {
          file: { id: 7, name: '资源分配.md', path: '/docs/resource.md', ext: '.md' },
          document: {},
          sections: [{ section_path: '第一节', text: '详情兜底片段' }],
          truncated: false,
        };
      }
      if (requestPath.startsWith('/files/7/content')) {
        return {
          file_id: 7, total: 1, offset: 0, has_more: false,
          sections: [{ id: 1, section_path: '第一节', text: '正文片段' }],
        };
      }
      throw new Error(`unexpected API call: ${requestPath}`);
    },
    document: {
      getElementById(id) {
        if (id === 'rows') return rows;
        if (id === 'evidence') return evidence;
        if (id === 'readerBody') return readerBody;
        if (id === 'readerFoot') return readerFoot;
        if (id === 'fileSearch') return fileSearch;
        if (id === 'backToList') return backButton;
        if (id === 'count') return countEl;
        if (id === 'resultsTitle') return titleEl;
        if (id === 'vtOriginal') return vtOriginal;
        if (id === 'vtText') return vtText;
        if (id === 'vtHint') return vtHint;
        return null;
      },
      // 原文视图要内嵌 vendor 脚本。这里让它停在"永不 settle"的加载态：
      // 本例要验的是切到提取文本后全文照旧从 /content 渲染，不是 md 渲染本身
      // （那由 viewer-formats.test.js 覆盖）。
      querySelector: () => null,
      createElement: () => ({ dataset: {}, style: {} }),
      head: { appendChild() {} },
    },
    setTimeout,
    window: { ordo: { revealInFinder() {} } },
  };

  const result = await vm.runInNewContext(`
    let files = [{ id: 7, name: '资源分配.md', path: '/docs/resource.md', ext: '.md', state: 'ready' }];
    let filesTotal = 1, selectedEvidence = null, detailVersion = 0;
    let currentQuery = '', activeDuplicate = false, activeRecent = null;
    ${escapingSource}
    ${evidenceSource}
    ${rowsSource}
    renderRows();
    rowClick = document.getElementById('rows').querySelectorAll('.row')[0].onclick;
    (async () => {
      rowClick();
      await new Promise(function (resolve) { setTimeout(resolve, 0); });
      await new Promise(function (resolve) { setTimeout(resolve, 0); });
      var evidenceHtml = document.getElementById('evidence').innerHTML;
      // 默认落在原文视图（.md 可原版式渲染）；再切「提取文本」验证全文通道
      var defaultBody = document.getElementById('readerBody').innerHTML;
      document.getElementById('vtText').onclick();
      await new Promise(function (resolve) { setTimeout(resolve, 0); });
      await new Promise(function (resolve) { setTimeout(resolve, 0); });
      return { evidenceHtml: evidenceHtml, defaultBody: defaultBody };
    })()
  `, context);

  assert.match(result.evidenceHtml, /资源分配\.md/);
  assert.match(result.evidenceHtml, /\/docs\/resource\.md/);
  // .md 可原版式渲染，所以详情页给出视图切换并默认进原文
  assert.match(result.evidenceHtml, /id="vtOriginal"/);
  assert.match(result.defaultBody, /original-loading/);
  // 切到「提取文本」后，全文仍来自 /content 分页接口
  assert.match(readerBody.html, /正文片段/);
  assert.match(readerFoot.textContent, /全文完/);
  assert.equal(selectedClasses.has('on'), true);
  // 页面切换：列表隐藏、详情显示、返回键出现、标题换成文件名
  assert.equal(rows.style.display, 'none');
  assert.equal(evidence.style.display, '');
  assert.equal(backButton.style.display, '');
  assert.equal(fileSearch.style.display, 'none');
  assert.equal(titleEl.textContent, '资源分配.md');
});

test('boot loads the default file list when the library has files', async () => {
  const bootSource = sourceBetween('async function boot(', 'function requestApplicationResume(');
  assert.doesNotMatch(bootSource, /\bactiveView\b/);

  const setup = { style: { display: '' } };
  const query = { value: '  ' };
  const calls = [];
  const context = {
    api: async (requestPath) => {
      calls.push(`api:${requestPath}`);
      if (requestPath === '/stats') return { files: 3 };
      if (requestPath === '/settings/llm') return { configured: false };
      if (requestPath === '/reports/weekly') return { generated: false };
      throw new Error(`unexpected API call: ${requestPath}`);
    },
    refreshCats: async () => calls.push('refreshCats'),
    renderNav: () => calls.push('renderNav'),
    updateWorkbenchChrome: () => calls.push('updateWorkbenchChrome'),
    updateModelState: (status) => calls.push(`updateModelState:${status.configured}`),
    load: async (value) => calls.push(`load:${value}`),
    executeQuery: async () => calls.push('executeQuery'),
    renderKnowledgeEmpty: () => calls.push('renderKnowledgeEmpty'),
    runAutoExtClassify: async () => calls.push('runAutoExtClassify'),
    scheduleModelAutoCheck: () => calls.push('scheduleModelAutoCheck'),
    document: {
      getElementById(id) {
        if (id === 'setup') return setup;
        if (id === 'q') return query;
        return null;
      },
    },
  };

  const result = await vm.runInNewContext(`
    let stats = null, sources = [], llmInfo = { configured: false };
    ${bootSource}
    boot()
  `, context);

  assert.equal(result.hasFiles, true);
  assert.equal(setup.style.display, 'none');
  assert.equal(calls.includes('load:'), true);
  assert.equal(calls.includes('runAutoExtClassify'), true);
  assert.equal(calls.includes('scheduleModelAutoCheck'), true);
  assert.equal(calls.includes('executeQuery'), false);
  assert.equal(calls.includes('renderKnowledgeEmpty'), false);
});

test('load sends the active scope and appends the next page', async () => {
  const loadSource = sourceBetween('async function load(', '/* 全文搜索');
  const safeNumberSource = renderer.match(/function safeNumber\(value\) \{[\s\S]*?\n\}/)[0];
  const requests = [];
  let renders = 0;
  const count = { textContent: '' };
  const context = {
    URLSearchParams,
    api: async (requestPath) => {
      requests.push(requestPath);
      const offset = Number(new URL(`http://local${requestPath}`).searchParams.get('offset'));
      return offset === 0
        ? { total: 4, files: [{ id: 1 }, { id: 2 }] }
        : { total: 4, files: [{ id: 3 }, { id: 4 }] };
    },
    document: { getElementById: () => count },
    renderRows: () => { renders += 1; },
    setListTitle: () => {},
  };

  const result = await vm.runInNewContext(`
    let files = [], filesTotal = 0, currentQuery = '', loadVersion = 0, searchVersion = 0;
    let activeCategory = null, activeBook = 9, activeSource = '微信 & QQ';
    let activeExt = '.pdf', activeDir = '/Users/demo/Documents/报告', activeDuplicate = true;
    let activeRecent = null;
    const PAGE_SIZE = 300;
    ${safeNumberSource}
    ${loadSource}
    (async () => {
      await load('合同');
      await load('合同', true);
      return { files, filesTotal, currentQuery };
    })()
  `, context);

  assert.deepEqual(Array.from(result.files, (f) => f.id), [1, 2, 3, 4]);
  assert.equal(result.filesTotal, 4);
  assert.equal(result.currentQuery, '合同');
  assert.equal(count.textContent, '4 个文件');
  assert.equal(renders, 2);

  const first = new URL(`http://local${requests[0]}`).searchParams;
  const second = new URL(`http://local${requests[1]}`).searchParams;
  assert.equal(first.get('source'), '微信 & QQ');
  assert.equal(first.get('ext'), '.pdf');
  assert.equal(first.get('dir'), '/Users/demo/Documents/报告');
  assert.equal(first.get('duplicate'), 'true');
  assert.equal(first.get('book_id'), '9');
  assert.equal(first.get('q'), '合同');
  assert.equal(first.get('offset'), '0');
  assert.equal(second.get('offset'), '2');
});

test('source preview exposes truncated scale before confirmation', async () => {
  const previewSource = sourceBetween(
    'async function previewAndConfirmSource(',
    "document.getElementById('btnEnable')",
  );
  const safeNumberSource = renderer.match(/function safeNumber\(value\) \{[\s\S]*?\n\}/)[0];
  const hint = { textContent: '' };
  let prompt = '';
  const context = {
    api: async (requestPath, options) => {
      assert.equal(requestPath, '/sources/preview');
      assert.deepEqual(JSON.parse(options.body), { path: '/large' });
      return {
        total_files: 50000,
        will_index: 1200,
        ignored_by_ext: 300,
        excluded_dirs: 7,
        truncated: true,
      };
    },
    confirm: (message) => { prompt = message; return true; },
  };

  const confirmed = await vm.runInNewContext(`
    ${safeNumberSource}
    ${previewSource}
    previewAndConfirmSource({ name: '大目录', path: '/large' }, hint)
  `, { ...context, hint });

  assert.equal(confirmed, true);
  assert.match(prompt, /预计文件：至少 50000/);
  assert.match(prompt, /可收录文件：1200/);
  assert.match(prompt, /排除目录：7/);
});

test('sidecar lifecycle is visible and initial sidecar info counts as ready', () => {
  assert.match(renderer, /id="sidecarStatus"/);
  assert.match(renderer, /window\.ordo\.onSidecar\(handleSidecarStatus\)/);

  const statusSource = sourceBetween('function setSidecarStatus(', 'let toastTimer;');
  const safeNumberSource = renderer.match(/function safeNumber\(value\) \{[\s\S]*?\n\}/)[0];
  const status = {
    className: '',
    title: '',
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
  };
  const label = { textContent: '' };
  const context = {
    window: { ordo: null },
    document: {
      getElementById(id) {
        if (id === 'sidecarStatus') return status;
        if (id === 'sidecarStatusText') return label;
        return null;
      },
    },
  };
  vm.runInNewContext(`${safeNumberSource}\n${statusSource}`, context);

  const cases = [
    [{ port: 51000 }, 'sidecar-status ready', '服务已连接'],
    [{ state: 'ready' }, 'sidecar-status ready', '服务已连接'],
    [{ state: 'restarting', attempt: 2 }, 'sidecar-status restarting', '正在重启（第 2 次）…'],
    [{ state: 'stopped' }, 'sidecar-status stopped', '服务已停止'],
    [{ state: 'failed', error: '健康检查失败' }, 'sidecar-status failed', '服务不可用'],
  ];
  for (const [info, className, text] of cases) {
    context.setSidecarStatus(info);
    assert.equal(status.className, className);
    assert.equal(label.textContent, text);
  }
  assert.equal(status.title, '健康检查失败');
  assert.equal(status.attributes['aria-label'], '健康检查失败');
});

test('drawer toggles collapse each side column and persist independently of width', () => {
  // 顶栏两端各有一个开关，收起后按钮仍在原处 —— 若把开关放进栏内，
  // 收起之后就没有可点的地方把它请回来。
  assert.match(renderer, /id="toggleNav"[\s\S]{0,200}aria-controls="navPanel"/);
  assert.match(renderer, /id="toggleQa"[\s\S]{0,200}aria-controls="qaPanel"/);
  assert.match(renderer, /<section class="nav-panel" id="navPanel"/);
  assert.match(renderer, /<aside class="qa-panel" id="qaPanel"/);

  // 收起的栏连同它的 splitter 一起归零：留着 5px 拖拽条会在贴边处留下
  // 一道摸不到来源的缝。
  assert.match(renderer, /\(navOpen \? navWidth \+ 'px 5px' : '0px 0px'\)/);
  assert.match(renderer, /\(qaOpen \? '5px ' \+ qaWidth \+ 'px' : '0px 0px'\)/);

  // 开合与宽度分开存：收起再展开要回到用户拖出来的宽度，不是默认值。
  assert.match(renderer, /nav: navWidth, qa: qaWidth, navOpen: navOpen, qaOpen: qaOpen/);
  assert.match(renderer, /if \(saved\.navOpen === false\) navOpen = false/);
  assert.match(renderer, /if \(saved\.qaOpen === false\) qaOpen = false/);

  // 0 宽列里若还留着可聚焦控件，Tab 会跳进看不见的地方。
  const css = fs.readFileSync(
    path.resolve(__dirname, '..', 'renderer', 'workbench.css'), 'utf8');
  assert.match(css, /\.nav-panel\.is-collapsed,\s*\n\.qa-panel\.is-collapsed \{\s*\n\s*visibility: hidden;/);
  // 拖 splitter 时必须关掉列宽过渡，否则每帧都在改列宽会拖出滞后感。
  assert.match(css, /body\.resizing \.content-layout \{\s*\n\s*transition: none;/);
});

test('overlays are one flat panel surface with a real radius', () => {
  const css = fs.readFileSync(
    path.resolve(__dirname, '..', 'renderer', 'workbench.css'), 'utf8');

  // 浮层用 --panel（深色下比内容层亮），不是 --surface（=内容卡色）。
  // 用内容卡色会让设置面板与中栏同色，"浮起来"读不出来。
  assert.match(css, /\.sheet-box \{[^}]*background: var\(--panel\)/);
  assert.match(css, /\.sheet-box \{[^}]*border-radius: var\(--radius-card\)/);
  assert.match(css, /\.setup \.body \{[^}]*background: var\(--panel\)/);
  assert.match(css, /\.setup \.foot \{[^}]*background: var\(--panel\)/);

  // 页头/页脚/侧导航都不许再各自上底色 —— 一个浮层里第二、第三种灰
  // 就是"颜色不一致"的来源。
  assert.doesNotMatch(css, /\.sheet-foot \{[^}]*background:/);
  assert.doesNotMatch(css, /\.sheet-head \{[^}]*background:/);
  assert.match(css, /\.set-nav \{[^}]*background: transparent/);

  // 遮罩走主题 veil：硬编码黑幕在浅色主题下会把整页压成灰。
  assert.match(css, /\.sheet,\s*\n\.setup \{[\s\S]{0,220}background: var\(--veil\)/);
  assert.doesNotMatch(css, /background: rgba\(0, 0, 0, 0\.35\)/);

  // 浮层内部重绑表面语义，后代四十处规则不必逐条改。
  assert.match(css, /\.sheet-box,\s*\n\.setup \.body \{\s*\n\s*--surface: var\(--inset-surface\)/);
});
