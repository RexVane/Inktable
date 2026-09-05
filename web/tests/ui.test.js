'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'app.css'), 'utf8');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');

const pageIds = [
  'home', 'knowledge/config', 'knowledge/datasets', 'knowledge/registry', 'knowledge/parsing', 'knowledge/index',
  'qaflow/parse', 'qaflow/embed', 'qaflow/route', 'qaflow/recall', 'qaflow/fuse', 'qaflow/rerank', 'qaflow/prompt', 'qaflow/answer',
  'apps/chat', 'apps/assistants', 'settings/general', 'settings/models', 'settings/storage', 'settings/version'
];

async function mountNavigation(hash = '', initialStorage = {}) {
  const storage = new Map(Object.entries(initialStorage));
  const listeners = new Map();
  const makeElement = () => {
    let html = '';
    const classes = new Set();
    const attributes = new Map();
    return {
      dataset: {}, style: {}, children: [], textContent: '',
      get innerHTML() { return html; },
      set innerHTML(value) { html = value; this.children = []; },
      appendChild(child) { this.children.push(child); },
      setAttribute(name, value) { attributes.set(name, String(value)); },
      getAttribute(name) { return attributes.get(name) ?? null; },
      classList: {
        add: name => classes.add(name),
        remove: name => classes.delete(name),
        contains: name => classes.has(name),
        toggle(name, enabled) {
          if (enabled ?? !classes.has(name)) { classes.add(name); return true; }
          classes.delete(name);
          return false;
        }
      }
    };
  };
  const ids = new Map(['app', 'sidebar', 'drawer', 'searchBtn', 'newChat', 'workspaceBtn', 'body', 'pageTitle', 'pageDesc', 'actions']
    .map(id => [id, makeElement()]));
  const groups = ['knowledge', 'qaflow', 'apps', 'settings'].map(rail => ({ ...makeElement(), dataset: { rail } }));
  const parents = groups.map(group => ({ ...makeElement(), dataset: { group: group.dataset.rail } }));
  const pages = pageIds.map(page => ({ ...makeElement(), dataset: { page } }));
  const appElement = ids.get('app');
  let shellHtml = '';
  let shellRenders = 0;
  Object.defineProperty(appElement, 'innerHTML', {
    get: () => shellHtml,
    set(value) {
      shellHtml = value;
      shellRenders++;
      for (const id of ['body', 'pageTitle', 'pageDesc', 'actions']) ids.set(id, makeElement());
    }
  });
  const document = {
    getElementById: id => ids.get(id),
    querySelectorAll: selector => ({ '[data-rail]': groups, '[data-group]': parents, '[data-page]': pages }[selector] || []),
    createElement: makeElement,
    addEventListener() {}
  };
  const window = {
    OrdoApi: { createClient: () => ({ connected: false, bootstrapSession: async () => null }) },
    location: { hash },
    addEventListener(event, callback) { listeners.set(event, callback); }
  };
  vm.runInNewContext(app, {
    window, document, URLSearchParams, setTimeout, clearTimeout,
    console: { warn() {}, error: console.error },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)) }
  }, { filename: 'app.js' });
  await new Promise(resolve => setImmediate(resolve));
  return {
    window, document, storage, parents, groups, pages,
    get shellRenders() { return shellRenders; },
    async dispatchHashChange() {
      listeners.get('hashchange')();
      await new Promise(resolve => setImmediate(resolve));
    }
  };
}

test('web entrypoint uses external static assets and local product title', () => {
  assert.match(html, /app\.css/);
  assert.match(html, /app\.js/);
  assert.match(html, /Ordo · 本地知识工作台/);
  assert.match(html, /rel="icon"/);
  assert.match(html, /href="\.\/icon\.png"/);
  assert.match(html, /theme-init\.js/);
  assert.ok(fs.statSync(path.join(root, 'icon.png')).size > 1024, 'favicon asset should be present');
  assert.ok(fs.statSync(path.join(root, 'theme-init.js')).size > 100, 'theme bootstrap should be present');
  assert.doesNotMatch(html, /<script(?![^>]*src=)[^>]*>[\s\S]*<\/script>/);
});

test('all planned 22 mockup pages and routes remain available with 8 QA stages', () => {
  for (const id of pageIds) {
    assert.ok(app.includes(id), `missing route ${id}`);
  }
  assert.match(app, /const flowRoutes = \['parse', 'embed', 'route', 'recall', 'fuse', 'rerank', 'prompt', 'answer'\]/);
  assert.match(app, /问题解析/);
  assert.match(app, /问题向量化/);
  assert.match(app, /检索路由/);
  assert.match(app, /多路召回/);
  assert.match(app, /结果融合/);
  assert.match(app, /重排/);
  assert.match(app, /构建提示词/);
  assert.match(app, /回答生成/);
});

test('modals and interactive states exist for new chat and global search', () => {
  assert.match(app, /openNewChatModal/);
  assert.match(app, /openSearchModal/);
  assert.match(app, /kb-picker-grid/);
  assert.match(app, /search-palette/);
  assert.match(app, /Ctrl\+K/);
});

test('topbar notification and user controls are removed without changing notification preferences', () => {
  assert.doesNotMatch(app, /bell-btn|unread-dot|user-avatar|topbar-actions|topbar-spacer|toggleNotificationsPopover|ordoNotificationsPopover/);
  assert.doesNotMatch(css, /\.(?:bell-btn|unread-dot|user-avatar|topbar-actions|topbar-spacer)\b/);
  for (const setting of ['notifyOnMessage', 'notifyOnTask', 'notifyOnUpdate']) {
    assert.ok(app.includes(`id="sw_${setting}"`), `notification preference ${setting} must remain available`);
  }
});

test('parent menus and sidebar drawer preserve the current route, page nodes, and unsaved input', async () => {
  const ui = await mountNavigation('#/apps/chat');
  const body = ui.document.getElementById('body');
  const title = ui.document.getElementById('pageTitle');
  const textarea = ui.document.createElement('textarea');
  textarea.value = 'Unsaved question';
  body.appendChild(textarea);
  const bodyHtml = body.innerHTML;
  const shellRenders = ui.shellRenders;

  for (const parent of ui.parents) {
    parent.onclick();
    assert.equal(ui.window.ordoState.open, parent.dataset.group);
    assert.equal(parent.getAttribute('aria-expanded'), 'true');
    assert.ok(ui.groups.find(group => group.dataset.rail === parent.dataset.group).classList.contains('is-open'));
    parent.onclick();
    assert.equal(ui.window.ordoState.open, '');
    assert.equal(parent.getAttribute('aria-expanded'), 'false');
  }
  const drawer = ui.document.getElementById('drawer');
  drawer.onclick();
  assert.equal(ui.window.ordoState.collapsed, true);
  assert.equal(drawer.getAttribute('aria-expanded'), 'false');
  drawer.onclick();
  assert.equal(ui.window.ordoState.collapsed, false);
  assert.equal(drawer.getAttribute('aria-expanded'), 'true');

  assert.equal(ui.window.location.hash, '#/apps/chat');
  assert.equal(ui.window.ordoState.page, 'apps/chat');
  assert.equal(ui.shellRenders, shellRenders);
  assert.equal(ui.document.getElementById('body'), body);
  assert.equal(ui.document.getElementById('pageTitle'), title);
  assert.equal(body.innerHTML, bodyHtml);
  assert.equal(body.children[0], textarea);
  assert.equal(textarea.value, 'Unsaved question');
});

test('a parent menu expands a collapsed sidebar without navigating or replacing the page', async () => {
  const ui = await mountNavigation('#/knowledge/registry', { 'ordo.sidebarCollapsed': 'true', 'ordo.openRail': 'knowledge' });
  const body = ui.document.getElementById('body');
  const shellRenders = ui.shellRenders;
  ui.parents.find(parent => parent.dataset.group === 'knowledge').onclick();
  assert.equal(ui.window.ordoState.collapsed, false);
  assert.equal(ui.window.ordoState.open, 'knowledge');
  assert.equal(ui.storage.get('ordo.sidebarCollapsed'), 'false');
  assert.equal(ui.document.getElementById('sidebar').classList.contains('collapsed'), false);
  assert.equal(ui.window.location.hash, '#/knowledge/registry');
  assert.equal(ui.window.ordoState.page, 'knowledge/registry');
  assert.equal(ui.document.getElementById('body'), body);
  assert.equal(ui.shellRenders, shellRenders);
});

test('the initial root opens home, explicit deep links survive, and only leaf items navigate', async () => {
  for (const hash of ['', '#', '#/', '#/?source=bookmark']) {
    const ui = await mountNavigation(hash, { 'ordo.openRail': 'qaflow', 'ordo.page': 'qaflow/answer' });
    assert.equal(ui.window.ordoState.page, 'home', `root hash ${hash} must open home`);
  }
  const ui = await mountNavigation('#/settings/models');
  assert.equal(ui.window.ordoState.page, 'settings/models');
  ui.pages.find(page => page.dataset.page === 'knowledge/datasets').onclick();
  assert.equal(ui.window.location.hash, '#/knowledge/datasets');
  await ui.dispatchHashChange();
  assert.equal(ui.window.ordoState.page, 'knowledge/datasets');
  assert.equal(ui.window.ordoState.open, 'knowledge');
  ui.pages.find(page => page.dataset.page === 'home').onclick();
  assert.equal(ui.window.location.hash, '#/home');
  await ui.dispatchHashChange();
  assert.equal(ui.window.ordoState.page, 'home');
  assert.match(ui.document.getElementById('body').innerHTML, /正在连接 Ordo|无法连接 Ordo 服务/);
});

test('visual system includes stable responsive, design tokens and layout primitives', () => {
  for (const token of [
    '--green', '--line', '--accent', '--shell', '--card-bg', '--on-accent',
    '[data-theme="nebula"]', '[data-theme="linen"]', '.action-cards-grid',
    '.pipeline-nodes-card', '.diff-container', '.consistency-view',
    '@media (prefers-reduced-motion: reduce)', '@media (max-width: 760px)', ':focus-visible'
  ]) {
    assert.ok(css.includes(token), `missing visual token ${token}`);
  }
  assert.equal((css.match(/@media \(max-width: 760px\)/g) || []).length, 1, 'mobile rules should share 760px breakpoint');
});

test('every page route executes without error and produces full non-empty HTML', async () => {
  for (const page of pageIds) {
    global.window = { OrdoApi: require('../api'), location: { hash: '#/' + page }, addEventListener: () => {} };
    let htmlOutput = '';
    let shellOutput = '';
    global.document = {
      getElementById: (id) => ({
        set innerHTML(val) {
          if (id === 'body') htmlOutput = val;
          if (id === 'app') shellOutput = val;
        },
        get innerHTML() { return htmlOutput; },
        set textContent(val) {},
        style: {},
        appendChild: () => {},
        classList: { add: () => {}, remove: () => {} }
      }),
      querySelectorAll: () => [],
      addEventListener: () => {}
    };
    global.localStorage = { getItem: () => null, setItem: () => {} };

    eval(app);
    await new Promise(r => setTimeout(r, 10));
    assert.match(htmlOutput, /正在连接 Ordo|无法连接 Ordo 服务/, `Page ${page} must show connection state without a backend`);
    assert.doesNotMatch(htmlOutput, /页面加载出错|演示模式|用户手册_产品A/);
    assert.match(shellOutput, /class="topbar"/);
    assert.doesNotMatch(shellOutput, /bell-btn|unread-dot|user-avatar|topbar-actions|topbar-spacer|title="通知"|title="当前用户"/);
    assert.equal(global.window.toggleNotificationsPopover, undefined);
  }
});

test('all P1, P2 and P3 wired endpoints, handlers and security contracts are verified in app.js', () => {
  // P1 Core Workbench Wiring
  assert.match(app, /handleBatchDeleteDocs/);
  assert.match(app, /handleDeleteSingleDoc/);
  assert.match(app, /handleDirectoryImportPrompt/);
  assert.match(app, /handleExecuteDirImport/);
  assert.match(app, /handleSearchRelease/);
  assert.match(app, /handleSwitchChatKb/);
  assert.match(app, /local-hash-v1/);

  // P2 Settings, Assistants & Storage
  assert.match(app, /handleResetGeneralSettings/);
  assert.match(app, /handleCancelGeneralSettings/);
  assert.match(app, /handleRestoreStorageBackup/);
  assert.match(app, /handleToggleFeatureFlag/);
  assert.match(app, /getAssistantClients/);
  assert.match(app, /createAssistantClient/);
  assert.match(app, /rotateWidgetClient/);
  assert.match(app, /openCreateWidgetClientModal/);
  assert.match(app, /handleRotateWidgetClient/);

  // P3 Experience & Debounce
  assert.match(app, /searchDebounceTimer/);
  assert.match(app, /home-bottom-grid/);
  assert.match(app, /home-bottom-card/);

  // Stage 5 & 6 Fusion and Rerank Wiring
  assert.match(app, /getTraceFusionStage/);
  assert.match(app, /getTraceRerankStage/);
  assert.match(app, /handleApplyRerankThreshold/);
  assert.match(app, /openRerankCompareModal/);
});
