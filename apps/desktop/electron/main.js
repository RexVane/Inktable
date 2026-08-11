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

const { app, BrowserWindow, dialog, ipcMain, safeStorage, shell } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');

let mainWindow = null;
let sidecar = null;
let sidecarInfo = null; // { port, token }

const SESSION_TOKEN = crypto.randomBytes(32).toString('base64url');

// ---- LLM 配置：safeStorage 加密落盘（§6.3），密钥绝不进渲染进程 ----
const llmConfigPath = () => path.join(app.getPath('userData'), 'llm.enc');

function loadLLMConfig() {
  try {
    const blob = fs.readFileSync(llmConfigPath());
    return JSON.parse(safeStorage.decryptString(blob));
  } catch { return null; }
}

function saveLLMConfig(cfg) {
  fs.writeFileSync(llmConfigPath(), safeStorage.encryptString(JSON.stringify(cfg)));
}

async function pushLLMToSidecar(cfg) {
  if (!sidecarInfo) return;
  try {
    await fetch(`http://127.0.0.1:${sidecarInfo.port}/settings/llm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
                 Authorization: `Bearer ${sidecarInfo.token}` },
      body: JSON.stringify(cfg || { endpoint: '', api_key: '', model: '' }),
    });
  } catch (e) { console.error('[main] LLM 配置推送失败:', e.message); }
}

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

    // 令牌与模型配置经 stdin 传入，避免出现在进程表里（§6.2/§6.3）
    const boot = { token: SESSION_TOKEN };
    const llm = loadLLMConfig();
    if (llm) boot.llm = llm;
    sidecar.stdin.write(JSON.stringify(boot) + '\n');
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

// 目录选择走原生对话框：这是 macOS 上让用户授予目录访问权的正规途径，
// 用户手动选过的目录，沙盒会记住授权（§6.4 TCC）。
ipcMain.handle('dialog:pickDirectory', async () => {
  const r = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory', 'createDirectory'],
    title: '选择要索引的目录',
    buttonLabel: '添加',
  });
  return r.canceled || !r.filePaths.length ? null : r.filePaths[0];
});

ipcMain.handle('llm:get', () => {
  const cfg = loadLLMConfig();
  return cfg
    ? { endpoint: cfg.endpoint || '', model: cfg.model || '', has_key: !!cfg.api_key }
    : { endpoint: '', model: '', has_key: false };
});

ipcMain.handle('llm:set', async (_e, incoming) => {
  const prev = loadLLMConfig() || {};
  const cfg = {
    endpoint: String(incoming.endpoint || '').trim(),
    // 留空 = 保留已存的密钥（改端点/模型不必重输密钥）
    api_key: String(incoming.api_key || '').trim() || prev.api_key || '',
    model: String(incoming.model || '').trim(),
  };
  if (!cfg.endpoint && !cfg.api_key && !cfg.model) {
    try { fs.unlinkSync(llmConfigPath()); } catch {}
    await pushLLMToSidecar(null);
    return { endpoint: '', model: '', has_key: false };
  }
  saveLLMConfig(cfg);
  await pushLLMToSidecar(cfg);
  return { endpoint: cfg.endpoint, model: cfg.model, has_key: !!cfg.api_key };
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
