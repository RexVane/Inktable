/**
 * 边界状态截图：空库首启向导 / 模型未配置 / 低置信 hedge / sidecar 故障。
 * 用法：npx electron tools/preview/edge-states.js [输出目录]
 */
const { app, BrowserWindow, protocol } = require('electron');
const fs = require('fs');
const path = require('path');
const mock = require('./mock-data');

const outDir = process.argv[2] || '/tmp/ordo-ui-after';
fs.mkdirSync(outDir, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let mode = 'empty';

const DISCOVERED = { sources: [
  { name: '微信文件', path: '/Users/demo/xwechat_files', kind: 'wechat', discovered_by: 'auto', volatile: true, doc_count: 1210 },
  { name: '下载', path: '/Users/demo/Downloads', kind: 'folder', discovered_by: 'auto', volatile: false, doc_count: 863 },
  { name: 'QQ 文件', path: '/Users/demo/Documents/Tencent Files', kind: 'qq', discovered_by: 'auto', volatile: true, doc_count: 397 },
  { name: 'Safari 下载', path: '/Users/demo/Downloads/Safari', kind: 'browser', discovered_by: 'auto', volatile: false, doc_count: 0 },
] };

function route(pathname, method, params) {
  if (mode === 'empty') {
    if (pathname === '/stats') return { files: 0, deduped: 0, by_source: [], by_ext: [] };
    if (pathname === '/sources/discover') return DISCOVERED;
    if (pathname === '/settings/llm') return { configured: false };
  }
  if (mode === 'degraded') {
    if (pathname === '/settings/llm') return { configured: false };
    if (pathname === '/search') {
      const base = mock.route(pathname, method, params);
      return { ...base, hedge: '文件库里关于「梅瓶」的内容很少，以下结果可能不完全对应你的问题' };
    }
  }
  return mock.route(pathname, method, params);
}

async function waitFor(win, expr, timeout = 8000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const ok = await win.webContents.executeJavaScript(expr, true).catch(() => false);
    if (ok) return true;
    await sleep(120);
  }
  throw new Error('waitFor 超时: ' + expr);
}

async function shot(win, name) {
  await sleep(350);
  const image = await win.webContents.capturePage();
  fs.writeFileSync(path.join(outDir, name + '.png'), image.toPNG());
  console.log('已保存', name + '.png');
}

function createWindow() {
  return new BrowserWindow({
    width: 1280, height: 820, show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true, nodeIntegration: false, sandbox: true,
      backgroundThrottling: false,
    },
  });
}

// 场景间会短暂出现零窗口状态，阻止 Electron 默认退出
app.on('window-all-closed', () => {});

app.whenReady().then(async () => {
  protocol.handle('http', (request) => {
    const url = new URL(request.url);
    return new Response(JSON.stringify(route(url.pathname, request.method, url.searchParams)), {
      headers: { 'Content-Type': 'application/json' },
    });
  });

  // 1. 空库首启：来源发现向导
  mode = 'empty';
  const win1 = createWindow();
  await win1.loadFile(path.join(__dirname, '..', '..', 'renderer', 'index.html'));
  await waitFor(win1, "document.getElementById('setup').style.display === 'flex' && document.querySelectorAll('.src').length >= 3");
  await shot(win1, '7-light-setup');
  win1.destroy();

  // 2. 降级态：模型未配置 + 低置信 hedge 搜索
  mode = 'degraded';
  const win2 = createWindow();
  await win2.loadFile(path.join(__dirname, '..', '..', 'renderer', 'index.html'));
  await waitFor(win2, "document.querySelectorAll('.row').length > 3");
  await win2.webContents.executeJavaScript(`
    document.getElementById('q').value = '梅瓶';
    doSearch('梅瓶'); true`, true);
  await waitFor(win2, "document.querySelectorAll('.hit').length >= 2");
  await shot(win2, '8-light-degraded-hedge');

  // 3. sidecar 故障徽标
  await win2.webContents.executeJavaScript(
    "handleSidecarStatus({ state: 'failed', error: '本地服务启动失败', revision: 99 }); true", true);
  await sleep(300);
  await shot(win2, '9-light-sidecar-failed');

  app.quit();
}).catch((err) => {
  console.error('边界状态预览失败:', err);
  app.exit(1);
});
