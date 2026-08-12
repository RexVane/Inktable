// 快速检查：深色模式下主按钮的计算样式
const { app, BrowserWindow, protocol, nativeTheme } = require('electron');
const path = require('path');
const mock = require('./mock-data');

app.whenReady().then(async () => {
  protocol.handle('http', (request) => {
    const url = new URL(request.url);
    return new Response(JSON.stringify(mock.route(url.pathname, request.method, url.searchParams)), {
      headers: { 'Content-Type': 'application/json' },
    });
  });
  nativeTheme.themeSource = 'dark';
  const win = new BrowserWindow({
    width: 1280, height: 820, show: false,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true },
  });
  await win.loadFile(path.join(__dirname, '..', '..', 'renderer', 'index.html'));
  await new Promise((r) => setTimeout(r, 1200));
  const result = await win.webContents.executeJavaScript(`
    (() => {
      const s = getComputedStyle(document.getElementById('askSubmit'));
      const root = getComputedStyle(document.documentElement);
      return {
        dark: matchMedia('(prefers-color-scheme: dark)').matches,
        color: s.color, bg: s.backgroundColor, disabled: document.getElementById('askSubmit').disabled,
        onAccent: root.getPropertyValue('--on-accent'),
      };
    })()`, true);
  console.log(JSON.stringify(result, null, 2));
  const image = await win.webContents.capturePage({ x: 924, y: 520, width: 356, height: 300 });
  require('fs').writeFileSync('/tmp/inktable-ui-after/dark-composer-crop.png', image.toPNG());
  console.log('已保存 dark-composer-crop.png');
  app.quit();
});
