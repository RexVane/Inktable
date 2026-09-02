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

test('native OS interactions, file upload dialog, downloads and API client exist', () => {
  assert.match(app, /triggerNativeFileUpload/);
  assert.match(app, /triggerNativeFolderUpload/);
  assert.match(app, /triggerDownloadFile/);
  assert.match(app, /handleSendChat/);
  assert.match(app, /handleSaveAssistant/);
  assert.match(app, /openAddDatabaseModal/);
  assert.match(app, /const api = \{/);
  assert.match(app, /\/api\/v1\/session\/bootstrap/);
});

