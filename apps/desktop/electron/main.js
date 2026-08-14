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

const {
  app, BrowserWindow, dialog, globalShortcut, ipcMain, Menu,
  nativeImage, nativeTheme, safeStorage, shell, Tray,
} = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');
const { pathToFileURL } = require('url');

let mainWindow = null;
let sidecar = null;
let sidecarInfo = null; // { port, token }
let startupComplete = false;
let quitting = false;
let restartTimer = null;
let restartAttempts = 0;
let lastRestartError = '';
let sidecarStatusRevision = 0;
let lastSidecarStatus = { state: 'starting', revision: sidecarStatusRevision };

const SESSION_TOKEN = crypto.randomBytes(32).toString('base64url');
const hasSingleInstanceLock = app.requestSingleInstanceLock();

if (!hasSingleInstanceLock) {
  app.quit();
}

// ---- 数据目录：默认 ~/Library/Application Support/Inktable，可迁移 ----
const dataDirConfigPath = () => path.join(app.getPath('userData'), 'data-dir.json');

function loadCustomDataDir() {
  try {
    const raw = JSON.parse(fs.readFileSync(dataDirConfigPath(), 'utf8'));
    if (raw && typeof raw.dir === 'string' && raw.dir) return raw.dir;
  } catch {}
  return null;
}

let customDataDir = loadCustomDataDir();
const defaultDataDir = () =>
  path.join(app.getPath('home'), 'Library', 'Application Support', 'Inktable');
const currentDataDir = () => customDataDir || defaultDataDir();

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

    const env = { ...process.env };
    if (customDataDir) env.INKTABLE_DATA_DIR = customDataDir;
    const proc = spawn(bin, [], { stdio: ['pipe', 'pipe', 'pipe'], env });
    sidecar = proc;

    let settled = false;
    let buffer = '';
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      if (sidecar === proc) {
        sidecar = null;
        sidecarInfo = null;
      }
      terminateSidecarProcess(proc);
      reject(new Error('sidecar 启动超时（15s）'));
    }, 15000);

    const rejectStartup = (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (sidecar === proc) {
        sidecar = null;
        sidecarInfo = null;
      }
      terminateSidecarProcess(proc);
      reject(err);
    };

    proc.stdout.on('data', (chunk) => {
      buffer += chunk.toString();
      let newline;
      while (!settled && (newline = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;

        try {
          const info = JSON.parse(line);
          if (Number.isInteger(info.port) && info.port > 0 && info.port <= 65535) {
            settled = true;
            clearTimeout(timer);
            // 这里只代表端口已上报，不代表 /health 已通过。
            // 可用连接信息只能由 startHealthySidecar() 在健康检查后公开。
            resolve({ port: info.port, token: SESSION_TOKEN });
          }
        } catch {
          /* 非 JSON 行忽略，继续检查后续 stdout 行 */
        }
      }
    });

    proc.stderr.on('data', (d) => console.error('[sidecar]', d.toString().trim()));

    proc.on('exit', (code, signal) => {
      console.error(`[sidecar] 退出，code=${code}`);
      const wasCurrent = sidecar === proc;
      if (sidecar === proc) {
        sidecar = null;
        sidecarInfo = null;
      }
      if (!settled) {
        rejectStartup(new Error(`sidecar 在就绪前退出（code=${code}, signal=${signal || 'none'}）`));
      } else if (wasCurrent && !quitting) {
        publishSidecarStatus({ state: 'stopped', code, signal });
        scheduleSidecarRestart(new Error(
          `sidecar 退出（code=${code}, signal=${signal || 'none'}）`
        ));
      }
    });

    proc.on('error', rejectStartup);

    // 令牌与模型配置经 stdin 传入，避免出现在进程表里（§6.2/§6.3）
    const boot = { token: SESSION_TOKEN };
    const llm = loadLLMConfig();
    if (llm) boot.llm = llm;
    proc.stdin.write(JSON.stringify(boot) + '\n', (err) => {
      if (err) rejectStartup(err);
    });
  });
}

function publishSidecarStatus(info) {
  lastSidecarStatus = { ...info, revision: ++sidecarStatusRevision };
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send('sidecar:status', lastSidecarStatus);
}

async function startHealthySidecar() {
  const info = await startSidecar();
  try {
    const response = await fetch(`http://127.0.0.1:${info.port}/health`, {
      headers: { Authorization: `Bearer ${info.token}` },
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error(`health HTTP ${response.status}`);
    const health = await response.json();
    if (health.status !== 'ok') {
      const failed = Object.entries(health.checks || {})
        .filter(([, check]) => check && check.ok === false)
        .map(([name]) => name);
      throw new Error(`sidecar 健康检查未通过${failed.length ? `：${failed.join('、')}` : ''}`);
    }
    sidecarInfo = info;
    publishSidecarStatus({ state: 'ready', health });
    return info;
  } catch (err) {
    stopSidecar();
    throw err;
  }
}

function scheduleSidecarRestart(error = null) {
  if (error) lastRestartError = String(error.message || error);
  if (quitting || restartTimer || restartAttempts >= 3) {
    if (!quitting && restartAttempts >= 3) {
      publishSidecarStatus({
        state: 'failed',
        error: lastRestartError || 'sidecar 多次重启失败',
      });
    }
    return;
  }
  const attempt = ++restartAttempts;
  const delay = Math.min(1000 * (2 ** (attempt - 1)), 10000);
  publishSidecarStatus({ state: 'restarting', attempt, delay });
  restartTimer = setTimeout(async () => {
    restartTimer = null;
    try {
      await startHealthySidecar();
      restartAttempts = 0;
      lastRestartError = '';
      console.log('[main] sidecar 已恢复');
    } catch (err) {
      console.error('[main] sidecar 重启失败:', err.message);
      scheduleSidecarRestart(err);
    }
  }, delay);
  restartTimer.unref();
}

function terminateSidecarProcess(proc) {
  if (!proc || proc.exitCode !== null || proc.signalCode !== null) return;
  try { proc.kill('SIGTERM'); } catch {}
  // 5 秒不退就强杀，避免残留进程占着端口和数据库锁
  setTimeout(() => {
    if (proc.exitCode !== null || proc.signalCode !== null) return;
    try { proc.kill('SIGKILL'); } catch {}
  }, 5000).unref();
}

function stopSidecar() {
  if (!sidecar) return;
  const proc = sidecar;
  sidecar = null;
  sidecarInfo = null;
  terminateSidecarProcess(proc);
}

function stopSidecarAndWait(timeoutMs = 6000) {
  // 数据迁移前必须确认进程真的退了 —— 数据库文件被占用时移动会损坏
  return new Promise((resolve) => {
    const proc = sidecar;
    if (!proc || proc.exitCode !== null || proc.signalCode !== null) {
      sidecar = null;
      sidecarInfo = null;
      return resolve();
    }
    sidecar = null;
    sidecarInfo = null;
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    proc.once('exit', finish);
    terminateSidecarProcess(proc);
    setTimeout(finish, timeoutMs).unref();
  });
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

  const rendererPath = path.join(__dirname, '..', 'renderer', 'index.html');
  const rendererUrl = pathToFileURL(rendererPath).toString();

  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (url !== rendererUrl) event.preventDefault();
  });

  mainWindow.loadFile(rendererPath);
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('closed', () => { mainWindow = null; });

  // 外链走系统浏览器，不在应用内开窗
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const protocol = new URL(url).protocol;
      if (protocol === 'http:' || protocol === 'https:') {
        shell.openExternal(url).catch((err) => {
          console.error('[main] 外链打开失败:', err.message);
        });
      }
    } catch {}
    return { action: 'deny' };
  });
}

ipcMain.handle('sidecar:info', () => sidecarInfo);
ipcMain.handle('sidecar:get-status', () => lastSidecarStatus);
ipcMain.handle('shell:reveal', (_e, filePath) => {
  if (typeof filePath === 'string' && filePath) shell.showItemInFolder(filePath);
});

ipcMain.handle('shell:open', (_e, filePath) => {
  // 用系统默认应用打开文件（文件查看器对不可解析格式的兜底）
  if (typeof filePath !== 'string' || !filePath) return '';
  return shell.openPath(filePath);
});

ipcMain.handle('shell:trash', async (_e, filePath) => {
  // 删除 = 移到废纸篓（可恢复），绝不直接 unlink
  if (typeof filePath !== 'string' || !filePath) {
    return { ok: false, error: '路径无效' };
  }
  try {
    await shell.trashItem(filePath);
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

ipcMain.handle('theme:set', (_e, mode) => {
  nativeTheme.themeSource = ['light', 'dark', 'system'].includes(mode) ? mode : 'system';
});

ipcMain.handle('data:get', () => ({
  dir: currentDataDir(),
  isDefault: !customDataDir,
}));

ipcMain.handle('data:change', async () => {
  const picked = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory', 'createDirectory'],
    title: '选择新的数据目录（将在其下创建 Inktable 文件夹）',
    buttonLabel: '迁移到这里',
  });
  if (picked.canceled || !picked.filePaths.length) return { ok: false, canceled: true };

  const oldDir = currentDataDir();
  const target = path.join(picked.filePaths[0], 'Inktable');
  if (target === oldDir) return { ok: false, error: '与当前位置相同' };
  if ((target + path.sep).startsWith(oldDir + path.sep)) {
    return { ok: false, error: '不能选在当前数据目录内部' };
  }
  if (fs.existsSync(target) && fs.readdirSync(target).length > 0) {
    return { ok: false, error: '目标已存在且非空：' + target };
  }

  // 停库 → 搬目录 → 记配置 → 用新目录重启。搬运期间界面显示"正在重启"。
  publishSidecarStatus({ state: 'restarting', attempt: 0 });
  await stopSidecarAndWait();
  try {
    if (fs.existsSync(oldDir)) {
      fs.mkdirSync(path.dirname(target), { recursive: true });
      try {
        fs.renameSync(oldDir, target);
      } catch {
        // 跨卷移动：复制后删除
        fs.cpSync(oldDir, target, { recursive: true });
        fs.rmSync(oldDir, { recursive: true, force: true });
      }
      // 单实例锁是进程级的陈旧状态，不随迁移带走
      try { fs.rmSync(path.join(target, 'inktable.lock'), { force: true }); } catch {}
    } else {
      fs.mkdirSync(target, { recursive: true });
    }
    customDataDir = target;
    fs.writeFileSync(dataDirConfigPath(), JSON.stringify({ dir: target }));
  } catch (err) {
    console.error('[main] 数据迁移失败:', err.message);
    try { await startHealthySidecar(); } catch (e2) { scheduleSidecarRestart(e2); }
    return { ok: false, error: '迁移失败：' + err.message };
  }

  try {
    await startHealthySidecar();
    restartAttempts = 0;
    lastRestartError = '';
    // 保全副本的绝对路径记录在库里，迁移后要把前缀改过来
    try {
      await fetch(`http://127.0.0.1:${sidecarInfo.port}/system/rebase_preserved`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${sidecarInfo.token}`,
        },
        body: JSON.stringify({ old_prefix: oldDir, new_prefix: target }),
      });
    } catch (e) { console.error('[main] 保全路径重写失败:', e.message); }
    return { ok: true, dir: target };
  } catch (err) {
    scheduleSidecarRestart(err);
    return { ok: false, error: '数据已迁移，但服务重启失败：' + err.message, dir: target };
  }
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
  if (!incoming || typeof incoming !== 'object') {
    throw new TypeError('LLM 配置格式无效');
  }
  if (incoming.clear === true) {
    try { fs.unlinkSync(llmConfigPath()); } catch (err) {
      if (err.code !== 'ENOENT') throw err;
    }
    await pushLLMToSidecar(null);
    return { endpoint: '', model: '', has_key: false };
  }

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

if (hasSingleInstanceLock) app.on('second-instance', () => {
  if (!mainWindow || mainWindow.isDestroyed()) {
    if (app.isReady() && startupComplete) createWindow();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  if (!mainWindow.isVisible()) mainWindow.show();
  mainWindow.focus();
});

// ---- 全局快捷键 + 菜单栏常驻：⌥Space 随手呼出搜索（Spotlight 式）----
let tray = null;

function summonSearch() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    if (app.isReady() && startupComplete) createWindow();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  app.focus({ steal: true });
  mainWindow.focus();
  mainWindow.webContents.send('focus-search');
}

function setupGlobalEntry() {
  let accelerator = '';
  try {
    if (globalShortcut.register('Alt+Space', summonSearch)) {
      accelerator = '⌥Space';
    } else if (globalShortcut.register('CommandOrControl+Shift+K', summonSearch)) {
      accelerator = '⌘⇧K';   // ⌥Space 被别的应用占了就退而求其次
    }
  } catch (err) {
    console.error('[main] 全局快捷键注册失败:', err.message);
  }

  try {
    // 菜单栏图标需 ~16pt 模板图；系统命名图标是 Touch Bar 尺寸，必须缩放，
    // 否则会显示成一个特别大的放大镜。用 1x/2x 两档 PNG 作为模板图。
    const icon = nativeImage.createFromNamedImage('NSTouchBarSearchTemplate')
      .resize({ width: 16, height: 16 });
    icon.setTemplateImage(true);
    tray = new Tray(icon);
    tray.setToolTip('Inktable — 个人知识库');
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: accelerator ? `打开搜索（${accelerator}）` : '打开搜索', click: summonSearch },
      { type: 'separator' },
      { label: '退出 Inktable', click: () => app.quit() },
    ]));
    tray.on('click', summonSearch);
  } catch (err) {
    console.error('[main] 菜单栏图标创建失败:', err.message);
  }
  if (accelerator) console.log(`[main] 全局快捷键已注册：${accelerator}`);
}

if (hasSingleInstanceLock) app.whenReady().then(async () => {
  // 开发态（electron .）Dock 显示的是 Electron 默认图标，这里换成品牌图标；
  // 打包版由 electron-builder 从 build/icon.icns 注入，无需此步
  if (process.platform === 'darwin' && !app.isPackaged) {
    try {
      app.dock.setIcon(nativeImage.createFromPath(
        path.join(__dirname, '..', 'build', 'icon-1024.png')));
    } catch { /* 图标缺失不影响启动 */ }
  }
  publishSidecarStatus({ state: 'starting' });
  try {
    const info = await startHealthySidecar();
    restartAttempts = 0;
    lastRestartError = '';
    console.log(`[main] sidecar 就绪，端口 ${info.port}`);
  } catch (err) {
    console.error('[main] sidecar 启动失败:', err.message);
    // 首次启动失败也走与运行期崩溃相同的 3 次退避重试。
    scheduleSidecarRestart(err);
  }
  startupComplete = true;
  createWindow();
  setupGlobalEntry();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

// 所有退出路径都必须停掉 sidecar
app.on('before-quit', () => {
  quitting = true;
  try { globalShortcut.unregisterAll(); } catch {}
  if (restartTimer) {
    clearTimeout(restartTimer);
    restartTimer = null;
  }
  stopSidecar();
});
process.on('exit', () => {
  quitting = true;
  stopSidecar();
});
process.on('SIGINT', () => app.quit());
process.on('SIGTERM', () => app.quit());
