'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'app.css'), 'utf8');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');

const pageIds = [
  'home', 'knowledge/config', 'knowledge/datasets', 'knowledge/registry', 'knowledge/parsing', 'knowledge/index',
  'qaflow/parse', 'qaflow/embed', 'qaflow/route', 'qaflow/recall', 'qaflow/fuse', 'qaflow/rerank', 'qaflow/prompt', 'qaflow/answer',
  'apps/chat', 'apps/assistants', 'settings/general', 'settings/models', 'settings/storage', 'settings/version'
];

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
    global.window = { location: { hash: '#/' + page }, addEventListener: () => {} };
    let htmlOutput = '';
    global.document = {
      getElementById: (id) => ({
        set innerHTML(val) { if (id === 'body') htmlOutput = val; },
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
    assert.ok(htmlOutput.length > 500, `Page ${page} failed to render or returned empty content (${htmlOutput.length} chars)`);
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
});

test('stage 1 accessibility and responsive interaction contracts are wired', () => {
  assert.match(app, /class="workspace-title"/);
  assert.match(app, /handleWorkspaceSelect/);
  assert.match(app, /mobile-open/);
  assert.match(app, /drawer-backdrop/);
  assert.match(app, /state\.mobileOpen = false/);
  assert.match(app, /aria-modal/);
  assert.match(app, /aria-labelledby/);
  assert.match(app, /focusOverlayFirst/);
  assert.match(app, /e\.key === 'Tab'/);
  assert.match(app, /const restoreFocus = \(\) =>/);
  assert.match(app, /restoreFocus\(\);\s*requestAnimationFrame\(restoreFocus\)/);
  assert.match(app, /setAttribute\('role', 'tablist'\)/);
  assert.match(app, /setAttribute\('role', 'progressbar'\)/);
  assert.match(css, /drawer-backdrop\.is-visible/);
  assert.match(css, /overflow-wrap: anywhere/);
});

test('online management views use real entities, task outcomes, routes and honest fallbacks', () => {
  assert.match(app, /async getAssistant\(id\)/);
  assert.match(app, /async getDataset\(dsId\)/);
  assert.match(app, /async getDiagnostics\(\)/);
  assert.match(app, /getWidgetBundleStatus/);
  assert.match(app, /fetch\('\/widget\.js', \{ method: 'HEAD'/);
  assert.match(app, /暂未接入/);
  assert.match(app, /waitTaskTerminal/);
  assert.match(app, /const result = await api\.waitTaskTerminal\(task\.id/);
  assert.match(app, /readRouteParams/);
  for (const param of ['kb', 'dataset', 'chunk', 'document', 'conversation']) {
    assert.match(app, new RegExp(`routeParams\\?\\.${param}`), `missing route query parameter ${param}`);
  }
  assert.match(app, /ordo\.notificationRead/);
  assert.match(app, /taskTerminalStatuses\.has\(task\.status\)/);
  assert.match(app, /handleDeleteConversation/);
  assert.match(app, /state\.chatConversations = \(state\.chatConversations \|\| \[\]\)\.filter/);
  assert.match(app, /state\.chatMessages = \[\];/);
  assert.match(app, /诊断导出需要连接服务端/);
  assert.match(app, /无法生成嵌入代码|无法生成嵌入/);
  assert.doesNotMatch(app, /if \(!diag\)[\\s\\S]{0,500}triggerDownloadFile\([^;]*mock/i);
});

