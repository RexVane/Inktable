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
const { spawn, spawnSync } = require('child_process');
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

// ---- 数据目录：平台原生位置，可迁移 ----
const dataDirConfigPath = () => path.join(app.getPath('userData'), 'data-dir.json');

const legacyDataDir = () =>
  path.join(app.getPath('home'), 'Library', 'Application Support', 'Inktable');
const nativeDefaultDataDir = () => {
  if (process.platform === 'darwin') return legacyDataDir();
  // userData is `%APPDATA%/Inktable` on Windows and the XDG config location on
  // Linux. Keep DB/config/key material under one platform-native root.
  return app.getPath('userData');
};

function loadCustomDataDir() {
  try {
    const raw = JSON.parse(fs.readFileSync(dataDirConfigPath(), 'utf8'));
    if (raw && typeof raw.dir === 'string' && raw.dir) return raw.dir;
  } catch {}
  return null;
}

let customDataDir = loadCustomDataDir();
const defaultDataDir = () => {
  const nativeDir = nativeDefaultDataDir();
  const legacyDir = legacyDataDir();
  // Existing Windows installs used the macOS-shaped ~/Library path. Preserve
  // them until the user runs the normal data-location migration; new installs
  // use the platform-native root. Never silently strand an existing library.
  if (process.platform !== 'darwin'
      && fs.existsSync(path.join(legacyDir, 'library.db'))
      && !fs.existsSync(path.join(nativeDir, 'library.db'))) {
    return legacyDir;
  }
  return nativeDir;
};
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
  if (!safeStorage.isEncryptionAvailable()) {
    throw new Error('系统安全存储不可用，已拒绝在磁盘保存 API 密钥');
  }
  fs.mkdirSync(path.dirname(llmConfigPath()), { recursive: true });
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
  // 发布态只使用随应用分发的 PyInstaller 产物，避免误用开发机环境。
  const exeName = process.platform === 'win32' ? 'inktable-sidecar.exe' : 'inktable-sidecar';
  if (app.isPackaged) {
    const command = path.join(process.resourcesPath, 'sidecar', exeName);
    if (!fs.existsSync(command)) return null;
    return { command, args: [], cwd: path.dirname(command) };
  }

  // 开发态直接运行源码。__dirname = apps/desktop/electron，上溯 3 层到仓库根。
  const repoRoot = path.resolve(__dirname, '..', '..', '..');
  const apiRoot = path.join(repoRoot, 'services', 'api');
  const pythonName = process.platform === 'win32' ? 'python.exe' : 'python';
  const command = process.env.INKTABLE_PYTHON
    ? path.resolve(process.env.INKTABLE_PYTHON)
    : path.join(apiRoot, '.venv', process.platform === 'win32' ? 'Scripts' : 'bin', pythonName);
  if (!fs.existsSync(command)) return null;
  return { command, args: ['-u', '-m', 'app.entrypoint'], cwd: apiRoot };
}

function startSidecar() {
  return new Promise((resolve, reject) => {
    const launch = resolveSidecarPath();
    if (!launch) {
      const message = app.isPackaged
        ? '发布版 sidecar 未找到，请先构建 PyInstaller 产物'
        : '开发环境 Python 未找到，请先在 services/api 执行 uv sync';
      return reject(new Error(message));
    }

    const env = { ...process.env };
    if (customDataDir) env.INKTABLE_DATA_DIR = customDataDir;
    const proc = spawn(launch.command, launch.args, {
      cwd: launch.cwd,
      stdio: ['pipe', 'pipe', 'pipe'],
      env,
      windowsHide: process.platform === 'win32',
    });
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
      // 启动包含对整库的完整性检查，耗时随库体积增长（实测 1.17GB 库
      // 约 15s）。超时必须给足余量 —— 杀掉健康但繁忙的进程会造成
      // "后端全灭"假象。
      reject(new Error('sidecar 启动超时（60s）'));
    }, 60000);

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
  if (process.platform === 'win32') {
    // PyInstaller onefile 在 Windows 上是「引导进程 + Python 子进程」两层，
    // proc.kill() 只杀引导进程，Python 子进程会成为孤儿并继续持有
    // 数据库单实例锁（实测复现）。必须整棵进程树一起杀。
    try {
      spawnSync('taskkill', ['/pid', String(proc.pid), '/T', '/F'], {
        stdio: 'ignore',
        windowsHide: true,
      });
    } catch {}
    return;
  }
  try { proc.kill('SIGTERM'); } catch {}
  // 5 秒不退就强杀，避免残留进程占着端口和数据库锁
  setTimeout(() => {
    if (proc.exitCode !== null || proc.signalCode !== null) return;
    try { proc.kill('SIGKILL'); } catch {}
  }, 5000).unref();
}

function stopSidecar() {
  abortAllApiStreams();
  if (!sidecar) return;
  const proc = sidecar;
  sidecar = null;
  sidecarInfo = null;
  terminateSidecarProcess(proc);
}

function stopSidecarAndWait(timeoutMs = 6000) {
  abortAllApiStreams();
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

// Windows 原生窗口控件（最小化/最大化/关闭）画在系统叠加层上，不受 CSS
// 管辖。它必须与 .topbar 的外壳色**逐值一致**，否则右上角会出现一块颜色
// 割裂的白条 —— 顶栏融进外壳的整体感当场破掉。
//
// 主题有七套（Nebula #222630、Coal #161616、Linen #f2f2ec …），一对
// 「深色/浅色」硬编码值表达不了，所以颜色由渲染层从 CSS 的 --shell /
// --ink 读出后传进来：**CSS 始终是唯一颜色来源**，加主题不必改这里。
const OVERLAY_FALLBACK = { color: '#222630', symbolColor: '#e2e8f0' };
let overlayColors = { ...OVERLAY_FALLBACK };

// 渲染层是不可信边界，颜色字符串必须先验形再交给 Electron。
const HEX_COLOR = /^#[0-9a-fA-F]{6}$/;

function updateTitleBarOverlay() {
  if (process.platform !== 'win32' || !mainWindow || mainWindow.isDestroyed()) return;
  try {
    mainWindow.setTitleBarOverlay({ ...overlayColors, height: 48 });
  } catch { /* 旧版 Electron 或非叠加窗口：忽略 */ }
}

function setOverlayColors(color, symbolColor) {
  if (!HEX_COLOR.test(String(color)) || !HEX_COLOR.test(String(symbolColor))) return false;
  overlayColors = { color: String(color), symbolColor: String(symbolColor) };
  updateTitleBarOverlay();
  return true;
}

nativeTheme.on('updated', updateTitleBarOverlay);

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    // 与 macOS 的 hiddenInset 观感对齐：Windows 用「窗口控件叠加」——
    // 隐藏系统标题栏，把最小化/最大化/关闭按钮叠进应用顶栏（高度 48
    // 与 .topbar 一致）；按钮配色由 updateTitleBarOverlay 跟随主题。
    // Linux 无叠加能力，保持系统原生标题栏。
    ...(process.platform === 'darwin'
      ? { titleBarStyle: 'hiddenInset' }
      : process.platform === 'win32'
        ? {
            titleBarStyle: 'hidden',
            // 首帧就用默认主题（Nebula）的外壳色。渲染层就绪后会按实际生效
            // 的主题覆写；这里若留纯白，启动瞬间右上角会闪一块白条。
            titleBarOverlay: { ...OVERLAY_FALLBACK, height: 48 },
          }
        : {}),
    backgroundColor: '#faf9f7',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  updateTitleBarOverlay();

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

// 只回连接就绪信号，绝不把 bearer token 交给渲染进程。渲染层的所有
// sidecar 调用都改走下面的主进程代理，令牌仅存在于主进程内存里 ——
// 即便 CSP 被绕过、渲染层被注入脚本，也拿不到令牌直接打 sidecar。
ipcMain.handle('sidecar:info', () => (sidecarInfo ? { port: sidecarInfo.port } : null));
ipcMain.handle('sidecar:get-status', () => lastSidecarStatus);

const API_ROUTE_RULES = [
  ['GET', /^\/(?:health|categories|books|stats|sources|watch\/status|index\/status|settings\/(?:ocr|qa|llm)|integrations\/ccswitch|reports\/weekly|journal|journal\/related)(?:\?[^#]*)?$/],
  ['GET', /^\/files(?:\?[^#]*)?$/],
  // 文件树是 /files/tree（两段），不是 /files/{id}/tree；正文读取带分页查询参数。
  // file_id 是整数主键，收窄成 [0-9]+ 而非宽松的 [^/]+。
  ['GET', /^\/files\/tree(?:\?[^#]*)?$/],
  ['GET', /^\/files\/[0-9]+\/(?:detail|content)(?:\?[^#]*)?$/],
  // AI Library 只放行当前 UI 需要的精确路由和参数形状；不要用
  // /library/.* 或任意查询串这种宽白名单。
  ['GET', /^\/library\/items(?:\?limit=[0-9]+&offset=[0-9]+(?:&status=(?:pending|running|ready|failed|stale))?)?$/],
  ['GET', /^\/library\/items\/[0-9]+$/],
  ['GET', /^\/library\/(?:stats|enrichment\/status|relations\/status)$/],
  ['POST', /^\/library\/sync$/],
  ['POST', /^\/library\/enrich(?:\?limit=[0-9]+)?$/],
  ['POST', /^\/library\/relations\/rebuild(?:\?limit=[0-9]+&top_k=[0-9]+&min_score=-?[0-9]+(?:\.[0-9]+)?&chunks_per_item=[0-9]+)?$/],
  ['POST', /^\/(?:settings\/llm\/test|ask(?:\/stream)?|files\/(?:remove|classify)|books\/add|classify\/auto_ext|search|sources\/(?:discover|discover_deep|preview|enable|disable|remove|add|auto_preserve|preserve_all)|watch\/start|index\/(?:run|embed_backfill|retry_scanned)|settings\/(?:ocr|qa)|journal\/remove)$/],
];
const MAX_API_BODY_BYTES = 1024 * 1024;
const MAX_STREAM_PAYLOAD_BYTES = 256 * 1024;
const MAX_STREAM_FRAME_BYTES = 1024 * 1024;
const MAX_STREAM_TOTAL_BYTES = 16 * 1024 * 1024;
const MAX_STREAMS_PER_SENDER = 4;

function isSafeApiPath(p) {
  if (typeof p !== 'string' || !p || p.length > 4096 || p[0] !== '/' ||
      p[1] === '/' || /[\s\x00-\x1f\\#]/.test(p) || p.includes('://')) return false;
  let parsed;
  try { parsed = new URL(p, 'http://sidecar.invalid'); } catch { return false; }
  if (parsed.origin !== 'http://sidecar.invalid') return false;
  let decoded;
  try { decoded = decodeURIComponent(parsed.pathname); } catch { return false; }
  return decoded === parsed.pathname && !decoded.includes('\\') &&
    !decoded.split('/').some((part) => part === '.' || part === '..');
}

function isAllowedApiRequest(method, p) {
  return isSafeApiPath(p) && API_ROUTE_RULES.some(([m, rule]) => m === method && rule.test(p));
}

function byteLength(value) { return Buffer.byteLength(value, 'utf8'); }

function abortAllApiStreams() {
  for (const entry of activeApiStreams.values()) entry.controller.abort();
}


// 主进程受控 HTTP 代理：渲染层只给出相对 path / method / 已序列化 body，
// 令牌由主进程附加。非 GET/POST 一律拒绝。
ipcMain.handle('api:request', async (_e, req) => {
  if (!sidecarInfo) return { ok: false, status: 0, error: 'sidecar 未就绪' };
  const reqPath = req && req.path;
  const method = (req && req.method ? String(req.method) : 'GET').toUpperCase();
  if (!isAllowedApiRequest(method, reqPath)) return { ok: false, status: 0, error: 'route not allowed' };
  const body = req && typeof req.body === 'string' ? req.body : undefined;
  if (method === 'POST' && body !== undefined && byteLength(body) > MAX_API_BODY_BYTES) {
    return { ok: false, status: 0, error: 'request body too large' };
  }
  try {
    const response = await fetch(`http://127.0.0.1:${sidecarInfo.port}${reqPath}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${sidecarInfo.token}`,
      },
      body: method === 'POST' ? (body ?? '{}') : undefined,
    });
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    return { ok: response.ok, status: response.status, data };
  } catch (err) {
    return { ok: false, status: 0, error: String((err && err.message) || err) };
  }
});

// SSE 流式代理：主进程读取 SSE、就地解析成 {event, data}，逐条转发给
// 发起窗口的私有频道。令牌不出主进程，渲染层只收到已解析事件。
const activeApiStreams = new Map();

ipcMain.handle('api:stream-start', (evt, req) => {
  if (!sidecarInfo) throw new Error('sidecar 未就绪');
  const id = req && req.id;
  const reqPath = req && req.path;
  if (typeof id !== 'string' || !id || !isAllowedApiRequest('POST', reqPath)) {
    throw new Error('invalid stream request');
  }
  if (id.length > 128 || !/^[A-Za-z0-9_-]+$/.test(id)) {
    throw new Error('invalid stream id');
  }
  const sender = evt.sender;
  const streamKey = `${sender.id}:${id}`;
  if (activeApiStreams.has(streamKey)) throw new Error('stream already active');
  if ([...activeApiStreams.values()].filter((entry) => entry.sender === sender).length >= MAX_STREAMS_PER_SENDER) {
    throw new Error('too many active streams');
  }

  const controller = new AbortController();
  const payload = req && typeof req.payload === 'object' && req.payload ? req.payload : {};
  let serializedPayload;
  try { serializedPayload = JSON.stringify(payload); } catch { throw new Error('invalid stream payload'); }
  if (byteLength(serializedPayload) > MAX_STREAM_PAYLOAD_BYTES) throw new Error('stream payload too large');
  let cleaned = false;

  // A stream belongs to the renderer that opened it. Closing/reloading that
  // renderer must abort the upstream fetch; otherwise the sidecar worker keeps
  // generating after nobody can receive the IPC events.
  const onSenderGone = () => controller.abort();
  const onSenderNavigation = (_event, _url, isInPlace, isMainFrame) => {
    if (isMainFrame && !isInPlace) onSenderGone();
  };
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    sender.removeListener('destroyed', onSenderGone);
    sender.removeListener('render-process-gone', onSenderGone);
    sender.removeListener('did-start-navigation', onSenderNavigation);
    if (activeApiStreams.get(streamKey)?.controller === controller) {
      activeApiStreams.delete(streamKey);
    }
  };
  const entry = { controller, sender, cleanup };
  activeApiStreams.set(streamKey, entry);
  const channel = `api:stream:${id}`;
  sender.once('destroyed', onSenderGone);
  sender.once('render-process-gone', onSenderGone);
  sender.on('did-start-navigation', onSenderNavigation);

  const emit = (msg) => {
    if (sender.isDestroyed()) {
      onSenderGone();
      return;
    }
    try {
      sender.send(channel, msg);
    } catch {
      onSenderGone();
    }
  };

  (async () => {
    try {
      const response = await fetch(`http://127.0.0.1:${sidecarInfo.port}${reqPath}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${sidecarInfo.token}`,
        },
        body: serializedPayload,
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        emit({ type: 'error', message: `HTTP ${response.status}` });
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let streamedBytes = 0;
      for (;;) {
        const chunk = await reader.read();
        // 一个坏掉/恶意的 sidecar 响应不能把主进程内存吃光：单帧和总量都设上限，
        // 超限即中止本流（abort 会让下面的 catch 走 end 分支）。
        streamedBytes += chunk.value ? chunk.value.byteLength : 0;
        if (streamedBytes > MAX_STREAM_TOTAL_BYTES) {
          emit({ type: 'error', message: 'stream response too large' });
          controller.abort();
          return;
        }
        buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
        if (byteLength(buffer) > MAX_STREAM_FRAME_BYTES) {
          emit({ type: 'error', message: 'stream frame too large' });
          controller.abort();
          return;
        }
        let split;
        while ((split = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          let event = 'message';
          const data = [];
          block.split(/\r?\n/).forEach((line) => {
            if (line.startsWith('event:')) event = line.slice(6).trim();
            else if (line.startsWith('data:')) data.push(line.slice(5).trim());
          });
          if (data.length) {
            let parsed = null;
            try { parsed = JSON.parse(data.join('\n')); } catch { parsed = null; }
            // 只转发解析成功的事件对象；坏帧丢弃而不是把 null 塞给渲染层处理器。
            if (parsed !== null) emit({ type: 'event', event, data: parsed });
          }
        }
        if (chunk.done) break;
      }
      emit({ type: 'end' });
    } catch (err) {
      if (controller.signal.aborted) emit({ type: 'end' });
      else emit({ type: 'error', message: String((err && err.message) || err) });
    } finally {
      cleanup();
    }
  })();
  return { started: true };
});

ipcMain.handle('api:stream-abort', (evt, id) => {
  const entry = activeApiStreams.get(`${evt.sender.id}:${id}`);
  // A compromised renderer must not be able to cancel another renderer's stream.
  if (entry && entry.sender === evt.sender) entry.controller.abort();
  return true;
});

async function authorizeShellPath(filePath, action) {
  if (typeof filePath !== 'string' || !filePath || filePath.length > 4096) return false;
  if (!sidecarInfo) return false;
  try {
    const response = await fetch(
      `http://127.0.0.1:${sidecarInfo.port}/files/authorize_path`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${sidecarInfo.token}`,
        },
        body: JSON.stringify({ path: filePath, action }),
      },
    );
    if (!response.ok) return false;
    const result = await response.json();
    return result.authorized === true;
  } catch {
    return false;
  }
}

ipcMain.handle('shell:reveal', async (_e, filePath) => {
  if (!await authorizeShellPath(filePath, 'reveal')) return false;
  shell.showItemInFolder(filePath);
  return true;
});

ipcMain.handle('shell:open', async (_e, filePath) => {
  // 用系统默认应用打开文件（文件查看器对不可解析格式的兜底）
  if (!await authorizeShellPath(filePath, 'open')) return '路径未获授权';
  return shell.openPath(filePath);
});

ipcMain.handle('shell:trash', async (_e, filePath) => {
  // 删除 = 移到废纸篓（可恢复），绝不直接 unlink。路径必须是 sidecar
  // 数据库中的 exact source path；renderer 不能自行指定任意磁盘目标。
  if (!await authorizeShellPath(filePath, 'trash')) {
    return { ok: false, error: '路径未获授权' };
  }
  try {
    await shell.trashItem(filePath);
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

ipcMain.handle('theme:set', (_e, req) => {
  // 兼容旧签名（只传 mode 字符串）；新签名带上当前主题的实际叠加色。
  const mode = req && typeof req === 'object' ? req.mode : req;
  nativeTheme.themeSource = ['light', 'dark', 'system'].includes(mode) ? mode : 'system';
  if (req && typeof req === 'object') {
    setOverlayColors(req.color, req.symbolColor);
  } else {
    // 没带颜色（旧渲染层）：退回按深浅取 Nebula / Silver 的外壳色，
    // 至少不会露出与任何主题都不匹配的纯白。
    const dark = nativeTheme.shouldUseDarkColors;
    setOverlayColors(dark ? '#222630' : '#f3f4f6', dark ? '#e2e8f0' : '#334155');
  }
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
  const encryption_available = safeStorage.isEncryptionAvailable();
  return cfg
    ? { endpoint: cfg.endpoint || '', model: cfg.model || '', has_key: !!cfg.api_key,
        encryption_available }
    : { endpoint: '', model: '', has_key: false, encryption_available };
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
