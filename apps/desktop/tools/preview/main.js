/**
 * UI 预览截图工具：加载真实 renderer + 模拟后端数据，产出各状态截图。
 * 用法：npx electron tools/preview/main.js [输出目录，默认 /tmp/inktable-ui]
 */
const { app, BrowserWindow, protocol, nativeTheme } = require('electron');
const fs = require('fs');
const path = require('path');
const mock = require('./mock-data');

const outDir = process.argv[2] || '/tmp/inktable-ui';
fs.mkdirSync(outDir, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

app.whenReady().then(async () => {
  protocol.handle('http', (request) => {
    const url = new URL(request.url);
    const body = mock.route(url.pathname, request.method, url.searchParams);
    return new Response(JSON.stringify(body), {
      headers: { 'Content-Type': 'application/json' },
    });
  });

  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });

  await win.loadFile(path.join(__dirname, '..', '..', 'renderer', 'index.html'));

  // 等待文件列表渲染完成
  await waitFor(win, "document.querySelectorAll('.row').length > 3");

  // 选中一个文件 → 中栏出现详情
  await win.webContents.executeJavaScript(
    "document.querySelectorAll('.row')[1].click(); true", true);
  await waitFor(win, "!!document.querySelector('.file-detail')");

  // 右栏发起提问 → 渲染带引用的回答
  await win.webContents.executeJavaScript(`
    document.getElementById('askQ').value = '封阳台每平米多少钱？什么时候验收？';
    runAsk('封阳台每平米多少钱？什么时候验收？'); true`, true);
  await waitFor(win, "!!document.querySelector('.ans .refs')");
  await shot(win, '1-light-workbench');

  // 搜索状态
  await win.webContents.executeJavaScript(`
    document.getElementById('q').value = '封阳台';
    doSearch('封阳台'); true`, true);
  await waitFor(win, "document.querySelectorAll('.hit').length >= 2");
  await win.webContents.executeJavaScript(
    "document.querySelectorAll('.hit')[0].click(); true", true);
  await shot(win, '2-light-search');

  // 设置面板
  await win.webContents.executeJavaScript("sheetOpen(true); true", true);
  await waitFor(win, "!!document.querySelector('.srow')");
  await shot(win, '3-light-settings');
  await win.webContents.executeJavaScript("sheetOpen(false); true", true);

  // 深色模式
  nativeTheme.themeSource = 'dark';
  await sleep(500);
  await shot(win, '4-dark-workbench');
  nativeTheme.themeSource = 'light';

  // 最小窗口（960x640）紧凑布局
  win.setSize(960, 640);
  await sleep(400);
  await shot(win, '5-light-compact');

  app.quit();
}).catch((err) => {
  console.error('预览失败:', err);
  app.exit(1);
});
