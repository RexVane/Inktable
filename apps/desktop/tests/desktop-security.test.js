const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const desktopRoot = path.resolve(__dirname, '..');
const rendererPath = path.join(desktopRoot, 'renderer', 'index.html');
const mainPath = path.join(desktopRoot, 'electron', 'main.js');
const renderer = fs.readFileSync(rendererPath, 'utf8');
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

test('renderer content security policy forbids eval and limits API connections', () => {
  const policy = renderer.match(/http-equiv="Content-Security-Policy" content="([^"]+)"/);
  assert.ok(policy, 'renderer must declare a Content Security Policy');
  assert.match(policy[1], /default-src 'self'/);
  assert.match(policy[1], /connect-src http:\/\/127\.0\.0\.1:\*/);
  assert.match(policy[1], /object-src 'none'/);
  assert.doesNotMatch(policy[1], /unsafe-eval/);
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
});

test('development and packaged sidecar launch modes stay separated', () => {
  assert.match(main, /app\.isPackaged/);
  assert.match(main, /path\.join\(process\.resourcesPath, 'sidecar', exeName\)/);
  assert.match(main, /\.venv/);
  assert.match(main, /args: \['-u', '-m', 'app\.main'\]/);
  assert.match(main, /cwd: apiRoot/);
  assert.match(main, /spawn\(launch\.command, launch\.args, \{/);
  assert.match(main, /windowsHide: process\.platform === 'win32'/);
  assert.doesNotMatch(main, /services', 'api', 'dist'/);
});

test('clear action uses an explicit flag instead of an empty key', () => {
  assert.match(renderer, /llmSet\(\{ clear: true \}\)/);
});
