/**
 * Inktable 主进程 —— sidecar 生命周期 + 窗口管理。
 *
 * sidecar 契约（PLAN §6.2）：
 *   · 子进程绑定 127.0.0.1:0，内核分配端口
 *   · 启动后经 stdout 输出一行 JSON: {"port": N, "token": "..."}
 *   · 令牌经 stdin 传入，不走 argv（ps 对本机任意进程可见）
 *
 * 退出路径必须收敛：主进程崩溃不能留下孤儿 Python 进程占着端口。
 */

const { app, BrowserWindow, ipcMain, shell } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');

let mainWindow = null;
let sidecar = null;
let sidecarInfo = null; // { port, token }

const SESSION_TOKEN = crypto.randomBytes(32).toString('base64url');

function resolveSidecarPath() {
  // 打包后：Contents/Resources/sidecar/inktable-sidecar
  const packaged = path.join(process.resourcesPath || '', 'sidecar', 'inktable-sidecar');
  if (fs.existsSync(packaged)) return packaged;

  // 开发态：仓库内的 PyInstaller 产物
  // __dirname = apps/desktop/electron → 上溯 3 层到仓库根
  const dev = path.join(__dirname, '..', '..', '..', 'services', 'api', 'dist', 'inktable-sidecar');
  if (fs.existsSync(dev)) return dev;

  return null;
}

function startSidecar() {
  return new Promise((resolve, reject) => {
    const bin = resolveSidecarPath();
    if (!bin) return reject(new Error('sidecar 二进制未找到，先运行 pyinstaller sidecar.spec'));

    sidecar = spawn(bin, [], { stdio: ['pipe', 'pipe', 'pipe'] });

    const timer = setTimeout(() => reject(new Error('sidecar 启动超时（15s）')), 15000);
    let buffer = '';

    sidecar.stdout.on('data', (chunk) => {
      buffer += chunk.toString();
      const newline = buffer.indexOf('\n');
      if (newline === -1 || sidecarInfo) return;

      try {
        const info = JSON.parse(buffer.slice(0, newline));
        if (info.port) {
          sidecarInfo = { port: info.port, token: SESSION_TOKEN };
          clearTimeout(timer);
          resolve(sidecarInfo);
        }
      } catch {
        /* 非 JSON 行忽略 */
      }
    });

    sidecar.stderr.on('data', (d) => console.error('[sidecar]', d.toString().trim()));

    sidecar.on('exit', (code) => {
      console.error(`[sidecar] 退出，code=${code}`);
      sidecar = null;
      sidecarInfo = null;
    });

    sidecar.on('error', (err) => {
      clearTimeout(timer);
      reject(err);
    });

    // 令牌经 stdin 传入，避免出现在进程表里
    sidecar.stdin.write(JSON.stringify({ token: SESSION_TOKEN }) + '\n');
  });
}

function stopSidecar() {
  if (!sidecar) return;
  const proc = sidecar;
  sidecar = null;
  proc.kill('SIGTERM');
  // 5 秒不退就强杀，避免残留进程占着端口和数据库锁
  setTimeout(() => {
    try {
      proc.kill('SIGKILL');
    } catch {}
  }, 5000);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    titleBarStyle: 'hiddenInset',
    backgroundColor: '#faf9f7',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());

  // 外链走系统浏览器，不在应用内开窗
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

ipcMain.handle('sidecar:info', () => sidecarInfo);
ipcMain.handle('shell:reveal', (_e, filePath) => {
  if (typeof filePath === 'string' && filePath) shell.showItemInFolder(filePath);
});

app.whenReady().then(async () => {
  try {
    const info = await startSidecar();
    console.log(`[main] sidecar 就绪，端口 ${info.port}`);
  } catch (err) {
    console.error('[main] sidecar 启动失败:', err.message);
  }
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

// 所有退出路径都必须停掉 sidecar
app.on('before-quit', stopSidecar);
process.on('exit', stopSidecar);
process.on('SIGINT', () => app.quit());
process.on('SIGTERM', () => app.quit());
