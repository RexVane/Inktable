const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const desktopRoot = path.resolve(__dirname, '..');
const rendererPath = path.join(desktopRoot, 'renderer', 'index.html');
const libraryPath = path.join(desktopRoot, 'renderer', 'library.js');
const libraryCssPath = path.join(desktopRoot, 'renderer', 'library.css');
const mainPath = path.join(desktopRoot, 'electron', 'main.js');
const renderer = fs.readFileSync(rendererPath, 'utf8');
const library = fs.readFileSync(libraryPath, 'utf8');
const libraryCss = fs.readFileSync(libraryCssPath, 'utf8');
const rendererScripts = `${renderer}\n${library}`;
const main = fs.readFileSync(mainPath, 'utf8');

test('renderer HTML escaping covers text and attribute delimiters', () => {
  const match = renderer.match(/function esc\(s\) \{[\s\S]*?\n\}/);
  assert.ok(match, 'esc() must remain a self-contained helper');
  const context = {};
  const payload = `<img src="x" onerror='boom'>`;
  vm.runInNewContext(`${match[0]}; result = esc(${JSON.stringify(payload)});`, context);
  assert.equal(
    context.result,
    '&lt;img src=&quot;x&quot; onerror=&#39;boom&#39;&gt;',
  );
  assert.match(renderer, /function encodedData\(value\)/);
});

test('renderer content security policy forbids eval and network exfiltration', () => {
  const policy = renderer.match(/http-equiv="Content-Security-Policy" content="([^"]+)"/);
  assert.ok(policy, 'renderer must declare a Content Security Policy');
  assert.match(policy[1], /default-src 'self'/);
  // 渲染层不直连 sidecar（改走主进程代理）；唯一的连接出口是
  // ordodoc:// 授权文档协议 —— 即便脚本被注入，也无法把库内数据外传。
  assert.doesNotMatch(policy[1], /connect-src[^;]*127\.0\.0\.1/);
  assert.match(policy[1], /object-src 'none'/);
  assert.doesNotMatch(policy[1], /unsafe-eval/);
  assert.doesNotMatch(policy[1], /script-src[^;]*unsafe-inline/);
  assert.match(policy[1], /script-src 'self' 'sha256-/);
  // 原文查看器：唯一的连接出口是主进程 ordodoc:// 授权文档协议，
  // 依旧没有任何 http(s) / 本机端口出口。
  assert.match(policy[1], /connect-src ordodoc:/);
  assert.doesNotMatch(policy[1], /connect-src[^;]*(https?|ws|wss):/);
});

test('ordodoc scheme allows fetch from the file:// renderer', () => {
  // Chromium treats custom-scheme fetch from file:// as CORS. Without
  // corsEnabled the original viewer is a blank page with a console error.
  assert.match(main, /scheme: 'ordodoc'/);
  assert.match(main, /corsEnabled: true/);
  assert.match(main, /supportFetchAPI: true/);
});

test('core workbench controls expose keyboard and assistive semantics', () => {
  assert.match(renderer, /id="splitNav"[\s\S]*?tabindex="0"[\s\S]*?aria-valuenow="214"/);
  assert.match(renderer, /id="splitQa"[\s\S]*?tabindex="0"[\s\S]*?aria-valuenow="330"/);
  assert.match(renderer, /resizeWithKeyboard/);
  assert.match(renderer, /id="toast" role="status" aria-live="polite"/);
  assert.match(renderer, /id="sheet" role="dialog" aria-modal="true"/);
  assert.match(renderer, /sheetReturnFocus/);
  assert.match(renderer, /event\.key === 'Escape'/);
  assert.match(renderer, /aria-label="知识问答输入"/);
  assert.doesNotMatch(renderer, /快速简洁回答：不带引用/);
  assert.match(renderer, /保留确定性引用校验/);
  assert.match(fs.readFileSync(path.join(desktopRoot, 'renderer', 'workbench.css'), 'utf8'),
    /prefers-reduced-motion: reduce/);
});

test('every inline script is CSP-hash declared, with no stale hashes', () => {
  const policy = renderer.match(/http-equiv="Content-Security-Policy" content="([^"]+)"/);
  assert.ok(policy, 'renderer must declare a Content Security Policy');
  const scriptSrc = policy[1].match(/script-src([^;]*)/);
  assert.ok(scriptSrc, 'policy must carry a script-src directive');
  const declared = [...scriptSrc[1].matchAll(/'(sha256-[^']+)'/g)].map((m) => m[1]);

  const scripts = [...renderer.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  assert.ok(scripts.length > 0, 'renderer must contain its inline boot script');

  // HTML parsing normalizes CRLF/CR to LF before CSP hashes inline script text.
  // Hashing the raw Windows file bytes leaves the static shell visible while
  // Chromium silently blocks all boot code at runtime.
  const actual = scripts.map((m) => 'sha256-' + crypto.createHash('sha256')
    .update(m[1].replace(/\r\n?/g, '\n'), 'utf8')
    .digest('base64'));

  // Compare as sets, in both directions. One-directional checks let a real
  // defect through: the theme bootstrap runs in <head> before first paint and
  // the workbench boots after </body>, so an un-declared second script would
  // be silently blocked while the first still hashes fine — the app would
  // render, just with the wrong theme and no explanation.
  for (const [i, hash] of actual.entries()) {
    assert.ok(declared.includes(hash),
      `inline script #${i + 1} is not declared in script-src (computed ${hash})`);
  }
  for (const hash of declared) {
    assert.ok(actual.includes(hash),
      `script-src declares ${hash}, which no inline script produces`);
  }
});

test('renderer never receives the sidecar bearer token', () => {
  const preloadPath = path.join(desktopRoot, 'electron', 'preload.js');
  const preload = fs.readFileSync(preloadPath, 'utf8');
  // 令牌只存在于主进程：sidecar:info 只回 { port }，无 token 字段。
  assert.match(main, /ipcMain\.handle\('sidecar:info', \(\) => \(sidecarInfo \? \{ port: sidecarInfo\.port \} : null\)\)/);
  // 渲染层与 preload 都不得再拼 Authorization / 读取 token。
  assert.doesNotMatch(rendererScripts, /Bearer/);
  assert.doesNotMatch(rendererScripts, /info\.token/);
  assert.doesNotMatch(preload, /token/);
  // 所有 sidecar 访问改走受控主进程代理。
  assert.match(preload, /apiRequest: \(req\) => ipcRenderer\.invoke\('api:request', req\)/);
  assert.match(preload, /api:stream-start/);
  assert.match(renderer, /window\.ordo\.apiRequest\(/);
  assert.match(renderer, /window\.ordo\.apiStream\(/);
  assert.doesNotMatch(rendererScripts, /fetch\('http:\/\/127\.0\.0\.1/);
});

// 把 main.js 里的纯路由校验函数搬进沙箱真实执行。
// 静态字符串断言只能证明"代码里写了白名单"，不能证明白名单是对的 ——
// 实测中它曾同时放过越权路径为 0、却误拦了 /files/tree 与 /files/{id}/content，
// 导致文件树和全文查看器静默失效，而当时全部静态断言都是绿的。
function loadRouteGuard() {
  const rules = main.match(/const API_ROUTE_RULES = \[[\s\S]*?\n\];/);
  const safe = main.match(/function isSafeApiPath\(p\) \{[\s\S]*?\n\}/);
  const allowed = main.match(/function isAllowedApiRequest\(method, p\) \{[\s\S]*?\n\}/);
  assert.ok(rules && safe && allowed, 'route guard must stay extractable as pure functions');
  const context = { URL, decodeURIComponent };
  vm.runInNewContext(`${rules[0]}\n${safe[0]}\n${allowed[0]}`, context);
  return context;
}

// 渲染层真实调用点。新增 api()/apiStream() 调用而忘记同步白名单时，本测试会失败。
function rendererRequests() {
  const found = new Map();
  for (const m of rendererScripts.matchAll(
    /\bapi\('([^']+)'(?:\s*,\s*(?:\{[^}]*?method:\s*'(\w+)'|'(\w+)'))?/g)) {
    found.set(m[1], (m[2] || m[3] || 'GET').toUpperCase());
  }
  for (const m of rendererScripts.matchAll(/apiStream\('([^']+)'/g)) found.set(m[1], 'POST');
  const literal = [...found].filter(([p]) =>
    !p.includes('+') && !p.endsWith('/') && !p.endsWith('?') && !p.endsWith('='));
  // 运行期拼接出来的路径用代表性实例覆盖，确保两个独立 renderer
  // 模块的调用都被主进程白名单接住。
  return [
    ...literal,
    ['/files/123/detail', 'GET'],
    ['/files/123/content?offset=0&limit=60', 'GET'],
    ['/files?limit=50&offset=0', 'GET'],
    ['/files/tree', 'GET'],
    ['/files/tree?dir=%2Ftmp', 'GET'],
    ['/reports/weekly?force=true', 'GET'],
    ['/library/items?limit=100&offset=0', 'GET'],
    ['/library/items/123', 'GET'],
    ['/library/stats', 'GET'],
    ['/library/enrichment/status', 'GET'],
    ['/library/relations/status', 'GET'],
    ['/library/sync', 'POST'],
    ['/library/enrich?limit=3', 'POST'],
    ['/library/enrichment/drain', 'POST'],
    ['/library/enrichment/drain?retry_failed=true', 'POST'],
    ['/library/enrichment/drain/cancel', 'POST'],
    ['/library/enrichment/runs', 'POST'],
    ['/library/enrichment/runs?retry_failed=true', 'POST'],
    ['/library/enrichment/runs/123e4567-e89b-12d3-a456-426614174000/step?limit=20', 'POST'],
    ['/library/enrichment/runs/123e4567-e89b-12d3-a456-426614174000/cancel', 'POST'],
    ['/library/relations/rebuild?limit=1000&top_k=8&min_score=0.6&chunks_per_item=16', 'POST'],
  ];
}

test('proxy allowlist admits every route the renderer actually calls', () => {
  const guard = loadRouteGuard();
  const requests = rendererRequests();
  assert.ok(requests.length > 30, 'renderer request extraction must not silently go empty');
  const blocked = requests.filter(([p, method]) => !guard.isAllowedApiRequest(method, p));
  assert.deepEqual(blocked, [], `allowlist would break live renderer calls: ${JSON.stringify(blocked)}`);
});

test('proxy allowlist rejects traversal, absolute, and unlisted routes', () => {
  const guard = loadRouteGuard();
  const attacks = [
    ['GET', '/../../etc/passwd'],
    ['GET', '//evil.example/x'],
    ['GET', 'http://evil.example/x'],
    ['GET', '/files/%2e%2e/secret'],
    ['GET', '/files/1/../../x'],
    ['GET', '/files/abc/content'],
    ['GET', '/admin'],
    ['PUT', '/stats'],
    ['DELETE', '/files'],
    ['GET', '/library/items/abc'],
    ['GET', '/library/admin'],
    ['POST', '/library/items'],
    ['DELETE', '/library/sync'],
    ['GET', '/library/relations/rebuild'],
    // 密钥必须走主进程 safeStorage，渲染层不得直接投递明文密钥
    ['POST', '/settings/llm'],
    // 仅主进程在数据迁移时自行调用
    ['POST', '/system/rebase_preserved'],
    ['POST', '/db/backup'],
  ];
  const leaked = attacks.filter(([method, p]) => guard.isAllowedApiRequest(method, p));
  assert.deepEqual(leaked, [], `allowlist leaked privileged routes: ${JSON.stringify(leaked)}`);
});

test('library proxy routes are admitted by exact method and resource shape', () => {
  const guard = loadRouteGuard();
  const admitted = [
    ['GET', '/library/items'],
    ['GET', '/library/items?limit=36&offset=0&status=ready'],
    ['GET', '/library/items/42'],
    ['GET', '/library/stats'],
    ['GET', '/library/enrichment/status'],
    ['GET', '/library/relations/status'],
    ['POST', '/library/sync'],
    ['POST', '/library/enrich?limit=3'],
    ['POST', '/library/enrichment/drain'],
    ['POST', '/library/enrichment/drain?retry_failed=true'],
    ['POST', '/library/enrichment/drain/cancel'],
    ['POST', '/library/enrichment/runs'],
    ['POST', '/library/enrichment/runs?retry_failed=true'],
    ['GET', '/library/enrichment/runs/123e4567-e89b-12d3-a456-426614174000'],
    ['POST', '/library/enrichment/runs/123e4567-e89b-12d3-a456-426614174000/step?limit=20'],
    ['POST', '/library/enrichment/runs/123e4567-e89b-12d3-a456-426614174000/cancel'],
    ['POST', '/library/relations/rebuild?limit=1000&top_k=8&min_score=0.6&chunks_per_item=16'],
  ];
  for (const [method, requestPath] of admitted) {
    assert.equal(guard.isAllowedApiRequest(method, requestPath), true,
      `${method} ${requestPath} should be admitted`);
  }

  const rejected = [
    ['POST', '/library/items'],
    ['PUT', '/library/sync'],
    ['GET', '/library/sync'],
    ['POST', '/library/stats'],
    ['GET', '/library/items/42/relations'],
    ['GET', '/library/items/not-a-number'],
    ['GET', '/library/items/42?include=private'],
    ['GET', '/library/items?limit=36&offset=0&include=private'],
    ['GET', '/library/items?offset=0&limit=36'],
    ['POST', '/library/sync?force=true'],
    ['POST', '/library/enrich?limit=3&provider=cloud'],
    ['POST', '/library/enrichment/runs?retry_failed=false'],
    ['POST', '/library/enrichment/runs/not-a-uuid/cancel'],
    ['POST', '/library/enrichment/runs/123e4567-e89b-12d3-a456-426614174000/step?limit=20&provider=cloud'],
    ['POST', '/library/relations/rebuild?limit=1000'],
    ['POST', '/library/relations/rebuild/force'],
    ['DELETE', '/library/items/42'],
  ];
  for (const [method, requestPath] of rejected) {
    assert.equal(guard.isAllowedApiRequest(method, requestPath), false,
      `${method} ${requestPath} should remain blocked`);
  }
});

test('main process api proxy validates paths and attaches the token itself', () => {
  assert.match(main, /function isSafeApiPath\(p\)/);
  assert.match(main, /ipcMain\.handle\('api:request'/);
  assert.match(main, /ipcMain\.handle\('api:stream-start'/);
  // 代理必须校验相对路径并只允许 GET/POST。
  assert.match(main, /function isAllowedApiRequest\(method, p\)/);
  // 代理必须走显式路由白名单，并对请求体/流负载设上限。
  assert.match(main, /const API_ROUTE_RULES = \[/);
  assert.match(main, /ask\(\?:\\\/stream\)\?/);
  assert.match(main, /MAX_API_BODY_BYTES/);
  assert.match(main, /MAX_STREAM_PAYLOAD_BYTES/);
  assert.match(main, /MAX_STREAM_FRAME_BYTES/);
  assert.match(main, /MAX_STREAM_TOTAL_BYTES/);
  assert.match(main, /MAX_STREAMS_PER_SENDER/);
  // 流按发起窗口隔离：跨窗口不能互相取消，sidecar 停止时全部中止。
  assert.match(main, /const streamKey = `\$\{sender\.id\}:\$\{id\}`/);
  assert.match(main, /entry\.sender === evt\.sender/);
  assert.match(main, /function abortAllApiStreams\(\)/);
  assert.match(main, /!isAllowedApiRequest\(method, reqPath\)/);
  // 令牌由主进程附加。
  assert.match(main, /Authorization: `Bearer \$\{sidecarInfo\.token\}`/);
});

test('API-controlled renderer fields are escaped before HTML insertion', () => {
  assert.match(renderer, /'<span class="fname">' \+ esc\(f\.name\)/);
  assert.match(renderer, /esc\(f\.source_name \|\| ''\)/);
  // 文件树节点：目录路径与文件名都来自库内登记数据，必须编码后进 DOM
  assert.match(renderer, /data-dir="' \+ encodedData\(dirPath\)/);
  assert.match(renderer, /data-fname="' \+ encodedData\(f\.name\)/);
  assert.match(renderer, /'<span class="label">' \+ esc\(name\)/);
  assert.match(renderer, /'<span class="label">' \+ esc\(f\.name\)/);
  assert.match(renderer, /data-path="' \+ encodedData\(openPath\)/);
  assert.doesNotMatch(renderer, /'<span class="fname">' \+ f\.name/);
  assert.doesNotMatch(renderer, /\+ \(f\.source_name \|\| ''\) \+/);
  assert.doesNotMatch(renderer, /\+ s\.name \+/);
  assert.doesNotMatch(renderer, /\+ s\.path\.replace/);
  assert.doesNotMatch(renderer, /\+ s\.file_count \+/);
  assert.doesNotMatch(renderer, /\+ c\.tag \+/);
});

test('library UI keeps the filesystem read-only and treats cards as derived data', () => {
  // 提示横幅已按用户要求移除：知识馆模式不再显示派生层说明
  assert.doesNotMatch(libraryCss, /原始文件不会被移动、复制或改名/);
  assert.match(library, /真实文件来源/);
  assert.match(library, /window\.ordo\.revealInFinder\(path\)/);
  assert.match(library, /item\.summary/);
  assert.match(library, /检索专用摘要不会在这里展示/);
  assert.match(library, /面向用户 · 与检索摘要分离/);
  assert.doesNotMatch(library, /item\.abstract|retrieval_abstract/);
  assert.doesNotMatch(library, /trashItem|rename|moveFile|copyFile|unlink|removeSource/);
});

test('main process has single-instance, key-clear, and timeout cleanup guards', () => {
  assert.match(main, /app\.requestSingleInstanceLock\(\)/);
  assert.match(main, /app\.on\(['"]second-instance['"]/);
  assert.match(main, /incoming\.clear === true/);
  assert.match(main, /fs\.unlinkSync\(llmConfigPath\(\)\)/);
  assert.match(main, /sidecar 启动超时（60s）/);
  assert.match(main, /terminateSidecarProcess\(proc\)/);
  assert.match(main, /spawnSync\(['"]taskkill['"][\s\S]*?['"]\/T['"][\s\S]*?windowsHide: true/);
  assert.doesNotMatch(main, /spawn\(['"]taskkill['"]/);
  assert.match(main, /const rejectStartup = \(err\) => \{[\s\S]*?terminateSidecarProcess\(proc\)/);
  assert.match(main, /webContents\.on\(['"]will-navigate['"]/);
  assert.match(main, /protocol === 'http:' \|\| protocol === 'https:'/);
  assert.match(main, /startHealthySidecar/);
  assert.match(main, /\/health/);
  assert.match(main, /scheduleSidecarRestart/);
  assert.match(main, /sidecar:status/);
  assert.match(main, /safeStorage\.isEncryptionAvailable\(\)/);
  assert.match(main, /authorizeShellPath/);
  assert.match(main, /\/files\/authorize_path/);
  assert.match(main, /ipcMain\.handle\('file:trash-by-id'/);
  assert.match(main, /trashFileById/);
  assert.doesNotMatch(main, /ipcMain\.handle\('shell:trash'/);
  assert.doesNotMatch(rendererScripts, /trashItem\(/);
  assert.match(main, /env\.ORDO_DATA_DIR = currentDataDir\(\)/);
  assert.match(main, /copyDataDirectory/);
  assert.match(main, /\/db\/integrity_check/);
  assert.match(main, /writeJsonAtomic\(dataDirConfigPath\(\)/);
  assert.doesNotMatch(main, /fs\.rmSync\(oldDir/);
});

test('development and packaged sidecar launch modes stay separated', () => {
  assert.match(main, /app\.isPackaged/);
  assert.match(main, /path\.join\(process\.resourcesPath, 'sidecar', exeName\)/);
  assert.match(main, /\.venv/);
  assert.match(main, /args: \['-u', '-m', 'app\.entrypoint'\]/);
  assert.match(main, /cwd: apiRoot/);
  assert.match(main, /spawn\(launch\.command, launch\.args, \{/);
  assert.match(main, /windowsHide: process\.platform === 'win32'/);
  assert.doesNotMatch(main, /services', 'api', 'dist'/);
});

test('clear action uses an explicit flag instead of an empty key', () => {
  assert.match(renderer, /llmSet\([^,]+, \{ clear: true \}\)/);
});

test('window control overlay is theme-driven and renderer colors are validated', () => {
  // Windows 的窗口控件画在系统叠加层上，CSS 管不到它。七套主题各有自己的
  // 外壳色，所以颜色必须由渲染层从 CSS token 读出后传入 —— 一对硬编码的
  // 深/浅值会让右上角出现与当前主题不匹配的色块。
  assert.match(main, /setTitleBarOverlay/);
  assert.doesNotMatch(main, /color: dark \? '#202226' : '#ffffff'/);

  // 颜色来自渲染层（不可信边界），交给 Electron 前必须验形。
  assert.match(main, /HEX_COLOR\s*=\s*\/\^#\[0-9a-fA-F\]\{6\}\$\//);
  assert.match(main, /if \(!HEX_COLOR\.test\(String\(color\)\) \|\| !HEX_COLOR\.test\(String\(symbolColor\)\)\) return false/);

  // 首帧兜底不能是纯白：那与七套主题里的任何一套都不匹配。
  assert.match(main, /OVERLAY_FALLBACK\s*=\s*\{ color: '#222630'/);
  assert.doesNotMatch(main, /titleBarOverlay: \{ color: '#ffffff'/);

  // 渲染层把当前生效主题的 --shell / --ink 传过去。
  assert.match(renderer, /color: hexFromCss\(cs\.getPropertyValue\('--shell'\)\)/);
  assert.match(renderer, /symbolColor: hexFromCss\(cs\.getPropertyValue\('--ink'\)\)/);
});

test('journal routes are allowlisted and remain read-mostly', () => {
  const guard = loadRouteGuard();
  // 调查日志的三个入口必须能走通，否则界面上的「历史」是死按钮
  for (const [method, p] of [
    ['GET', '/journal'],
    ['GET', '/journal?q=%E9%94%81&limit=30'],
    ['GET', '/journal/related?question=x'],
    ['POST', '/journal/remove'],
  ]) {
    assert.ok(guard.isAllowedApiRequest(method, p),
      `${method} ${p} 应被放行`);
  }
  // 不存在的动词/路径不能因为前缀相同就被放行
  for (const [method, p] of [
    ['POST', '/journal'],
    ['GET', '/journal/remove'],
    ['DELETE', '/journal'],
    ['GET', '/journal/../settings/llm'],
  ]) {
    assert.ok(!guard.isAllowedApiRequest(method, p),
      `${method} ${p} 不应被放行`);
  }
});
