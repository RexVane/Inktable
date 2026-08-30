const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const desktopRoot = path.resolve(__dirname, '..');
const main = fs.readFileSync(path.join(desktopRoot, 'electron', 'main.js'), 'utf8');
const preload = fs.readFileSync(path.join(desktopRoot, 'electron', 'preload.js'), 'utf8');
const renderer = fs.readFileSync(path.join(desktopRoot, 'renderer', 'index.html'), 'utf8');
const library = fs.readFileSync(path.join(desktopRoot, 'renderer', 'library.js'), 'utf8');
const libraryCss = fs.readFileSync(path.join(desktopRoot, 'renderer', 'library.css'), 'utf8');

function loadRouteGuard() {
  const rules = main.match(/const API_ROUTE_RULES = \[[\s\S]*?\n\];/);
  const safe = main.match(/function isSafeApiPath\(p\) \{[\s\S]*?\n\}/);
  const allowed = main.match(/function isAllowedApiRequest\(method, p\) \{[\s\S]*?\n\}/);
  assert.ok(rules && safe && allowed, 'desktop route guard must stay extractable');
  const context = { URL, decodeURIComponent };
  vm.runInNewContext(`${rules[0]}\n${safe[0]}\n${allowed[0]}`, context);
  return context;
}

test('preload loads Library only as same-origin renderer assets', () => {
  assert.match(preload, /style\.href = '\.\/library\.css'/);
  assert.match(preload, /script\.src = '\.\/library\.js'/);
  assert.match(preload, /DOMContentLoaded/);
  assert.doesNotMatch(preload, /https?:\/\//);
  assert.doesNotMatch(preload, /\btoken\b/i);

  const policy = renderer.match(/http-equiv="Content-Security-Policy" content="([^"]+)"/);
  assert.ok(policy);
  assert.match(policy[1], /script-src 'self'/);
  assert.match(policy[1], /style-src 'self'/);
  // 唯一连接出口是主进程 inkdoc:// 授权文档协议（原文查看器）
  assert.match(policy[1], /connect-src inkdoc:/);
});

test('Library renderer has no privileged or direct network capability', () => {
  assert.doesNotMatch(library, /ipcRenderer/);
  assert.doesNotMatch(library, /contextBridge/);
  assert.doesNotMatch(library, /Bearer/);
  assert.doesNotMatch(library, /127\.0\.0\.1/);
  assert.doesNotMatch(library, /\bfetch\s*\(/);
  assert.match(library, /window\.inktable\.apiRequest/);
  assert.match(library, /window\.inktable\.revealInFinder/);
});

test('Library API-originated strings never flow through innerHTML', () => {
  // The module builds elements and assigns textContent instead of interpolating
  // model/database strings into HTML. Keeping this global prohibition is cheap
  // and makes future additions fail closed.
  assert.doesNotMatch(library, /\.innerHTML\s*=/);
  assert.doesNotMatch(library, /insertAdjacentHTML/);
  assert.match(library, /el\.textContent = String\(text\)/);
  assert.doesNotMatch(library, /document\.write/);
});

test('Library JavaScript parses as a standalone classic renderer script', () => {
  assert.doesNotThrow(() => new vm.Script(library, { filename: 'library.js' }));
});

test('Library routes used by the renderer are admitted by the exact proxy allowlist', () => {
  const guard = loadRouteGuard();
  const routes = [
    ['GET', '/library/stats'],
    ['GET', '/library/relations/status'],
    ['GET', '/library/enrichment/status'],
    ['GET', '/library/items?limit=100&offset=0'],
    ['GET', '/library/items/123'],
    ['POST', '/library/sync'],
    ['POST', '/library/enrich?limit=3'],
    ['POST', '/library/enrichment/drain'],
    ['POST', '/library/enrichment/drain?retry_failed=true'],
    ['POST', '/library/enrichment/drain/cancel'],
    ['POST', '/library/enrichment/runs'],
    ['POST', '/library/enrichment/runs?retry_failed=true'],
    ['POST', '/library/enrichment/runs/123e4567-e89b-12d3-a456-426614174000/step?limit=20'],
    ['POST', '/library/enrichment/runs/123e4567-e89b-12d3-a456-426614174000/cancel'],
    ['POST', '/library/relations/rebuild'],
  ];
  for (const [method, route] of routes) {
    assert.equal(guard.isAllowedApiRequest(method, route), true,
      `${method} ${route} must be admitted`);
  }

  for (const [method, route] of [
    ['GET', '/library/items/nope'],
    ['GET', '/library/relations/rebuild'],
    ['POST', '/library/relations/status'],
    ['PUT', '/library/sync'],
    ['POST', '/library/admin'],
    ['GET', '/library/../settings/llm'],
  ]) {
    assert.equal(guard.isAllowedApiRequest(method, route), false,
      `${method} ${route} must stay blocked`);
  }
});

test('Library maintenance actions remain explicit local UI actions', () => {
  assert.match(library, /AI 整理全部/);
  assert.match(library, /\/library\/enrichment\/drain/);
  assert.match(library, /\/library\/enrichment\/drain\/cancel/);
  assert.match(library, /重试失败项/);
  assert.match(library, /重建相关资料/);
  assert.match(library, /\/library\/relations\/rebuild/);
  assert.match(library, /Deterministic metadata sync only/);
  assert.doesNotMatch(library, /settings\/llm/);
});

test('Library styles reuse workbench theme tokens instead of defining a new palette', () => {
  assert.match(libraryCss, /var\(--surface-3\)/);
  assert.match(libraryCss, /var\(--text\)/);
  assert.match(libraryCss, /var\(--accent-area\)/);
  assert.doesNotMatch(libraryCss, /#[0-9a-fA-F]{6}/);
});
