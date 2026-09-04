'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const app = read('src/App.jsx');
const shell = read('src/components/AppShell.jsx');
const coreViews = read('src/views/CoreViews.jsx');
const chat = read('src/views/ChatView.jsx');
const assistants = read('src/views/AssistantsView.jsx');
const indexing = read('src/views/IndexingView.jsx');
const css = read('app.css');
const html = read('index.html');
const pkg = JSON.parse(read('package.json'));

test('React is the only frontend framework and Vite entrypoint', () => {
  assert.equal(pkg.dependencies.react, '^18.3.1');
  assert.equal(pkg.dependencies['react-dom'], '^18.3.1');
  assert.equal(pkg.dependencies['react-router-dom'], '^6.30.1');
  assert.equal(pkg.dependencies.vue, undefined);
  assert.equal(pkg.dependencies.pinia, undefined);
  assert.equal(pkg.devDependencies['@vitejs/plugin-vue'], undefined);
  assert.match(html, /id="root"/);
  assert.match(html, /src="\/src\/main\.jsx"/);
  assert.doesNotMatch(html, /app\.js|vue/i);
});

test('all product routes remain available in React Router', () => {
  for (const route of [
    '/home',
    '/knowledge/registry',
    '/knowledge/datasets',
    '/knowledge/parsing',
    '/knowledge/index',
    '/knowledge/slices',
    '/knowledge/config',
    '/apps/chat',
    '/apps/assistants',
    '/qaflow/:stage?',
    '/settings/:tab'
  ]) {
    assert.ok(app.includes(route), 'missing route ' + route);
  }
  for (const stage of ['parse', 'embed', 'route', 'recall', 'fuse', 'rerank', 'prompt', 'answer']) {
    assert.ok(coreViews.includes(stage), 'missing QA stage ' + stage);
  }
});

test('visible navigation and page content are preserved', () => {
  for (const text of [
    'Local Knowledge Engine',
    '数据登记',
    '构建知识索引',
    '问答流程诊断',
    '智能问答',
    '智能助手',
    '系统设置'
  ]) assert.ok(shell.includes(text), 'missing shell text ' + text);

  for (const text of ['工作台总览', '2,783 篇', '数据解析流水线', '知识库管理']) {
    assert.ok(coreViews.includes(text), 'missing core page text ' + text);
  }
  assert.ok(chat.includes('如何为企业网站安装产品问答助手？'));
  assert.ok(assistants.includes('请求趋势 (近 7 天)'));
  assert.ok(indexing.includes('HNSW 层次图索引已就绪'));
  assert.ok(indexing.includes('BM25 倒排索引与混合检索调优'));
});

test('visual system and responsive layout contracts are retained', () => {
  for (const token of [
    '--green',
    '--line',
    '--accent',
    '--shell',
    '--card-bg',
    '[data-theme="nebula"]',
    '.workspace-layout-indexing',
    '.consistency-view',
    '.index-stepper-track',
    '@media (prefers-reduced-motion: reduce)',
    '@media (max-width: 760px)',
    ':focus-visible'
  ]) assert.ok(css.includes(token), 'missing visual token ' + token);
});

test('production build contains React bundle, styles and favicon', () => {
  const dist = path.join(root, 'dist');
  const builtHtml = fs.readFileSync(path.join(dist, 'index.html'), 'utf8');
  assert.match(builtHtml, /assets\/index-[^"]+\.js/);
  assert.match(builtHtml, /assets\/index-[^"]+\.css/);
  assert.ok(fs.readdirSync(path.join(dist, 'assets')).some(file => /^icon-.*\.png$/.test(file)));
});

test('no Vue source files remain', () => {
  const walk = directory => fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const full = path.join(directory, entry.name);
    return entry.isDirectory() ? walk(full) : [full];
  });
  assert.deepEqual(walk(path.join(root, 'src')).filter(file => file.endsWith('.vue')), []);
});
