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
  nativeImage, nativeTheme, protocol, safeStorage, shell, Tray,
} = require('electron');
const { spawn, spawnSync } = require('child_process');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');
const { pathToFileURL } = require('url');
const { trashFileById } = require('./file-operations');
const { prepareSlotConfig } = require('./model-config');
const { copyDataDirectory, pathsOverlap, writeJsonAtomic } = require('./data-migration');

// ---- inkdoc:// 自定义协议：原文查看器的文件字节通道 ----
// 渲染层 connect-src 'none'，不能自己发请求；PDF.js / docx-preview 需要
// 用 fetch 拿文件字节与字体资源。inkdoc 只做两件事，且都先过授权：
//   inkdoc://file/{file_id}/{name}  → 转发 sidecar /files/{id}/raw（库内登记路径，
//                                     Range 透传，大 PDF 懒加载）
//   inkdoc://app/vendor/{path}      → 提供应用内嵌的 pdfjs cmaps/standard_fonts
// 密钥只在主进程，渲染层拿不到 sidecar 端口与令牌。
protocol.registerSchemesAsPrivileged([
  { scheme: 'inkdoc',
    privileges: { standard: true, stream: true, supportFetchAPI: true } },
]);

const VENDOR_ROOT = path.resolve(__dirname, '..', 'renderer', 'vendor');

async function handleInkdocRequest(request) {
  let url;
  try {
    url = new URL(request.url);
  } catch {
    return new Response('bad request', { status: 400 });
  }
  if (!sidecarInfo) {
    return new Response('sidecar 未就绪',
      { status: 503, headers: { 'Access-Control-Allow-Origin': '*' } });
  }

  if (url.host === 'file') {
    // inkdoc://file/{file_id}/{name} —— id 必须是纯数字，其余由 sidecar 校验
    const match = url.pathname.match(/^\/(\d+)(?:\/.*)?$/);
    if (!match) return new Response('not found', { status: 404 });
    const headers = { Authorization: `Bearer ${sidecarInfo.token}` };
    const range = request.headers.get('range');
    if (range) headers.Range = range;
    const upstream = await fetch(
      `http://127.0.0.1:${sidecarInfo.port}/files/${match[1]}/raw`, { headers });
    const respHeaders = new Headers();
    for (const h of ['content-type', 'content-length', 'content-range',
      'accept-ranges', 'etag', 'last-modified']) {
      const value = upstream.headers.get(h);
      if (value) respHeaders.set(h, value);
    }
    respHeaders.set('Cache-Control', 'no-store');
    // file:// 页面 origin 是 null：没有 ACAO 头 fetch 一律 "Failed to fetch"
    respHeaders.set('Access-Control-Allow-Origin', '*');
    return new Response(upstream.body,
      { status: upstream.status, headers: respHeaders });
  }

  if (url.host === 'app') {
    // inkdoc://app/vendor/{path} —— 只读应用内嵌 vendor 目录，拒绝任何穿越
    const match = url.pathname.match(/^\/vendor\/(.+)$/);
    if (!match) return new Response('not found', { status: 404 });
    const target = path.resolve(VENDOR_ROOT, decodeURIComponent(match[1]));
    if (!target.startsWith(VENDOR_ROOT + path.sep)) {
      return new Response('forbidden', { status: 403 });
    }
    try {
      const stat = fs.statSync(target);
      if (!stat.isFile()) throw new Error('not a file');
      const ext = path.extname(target).toLowerCase();
      const types = {
        '.bcmap': 'application/octet-stream', '.pfb': 'application/octet-stream',
        '.ttf': 'font/ttf', '.mjs': 'text/javascript', '.js': 'text/javascript',
      };
      return new Response(fs.readFileSync(target), {
        headers: { 'Content-Type': types[ext] || 'application/octet-stream',
                   'Cache-Control': 'no-store',
                   'Access-Control-Allow-Origin': '*' },
      });
    } catch {
      return new Response('not found', { status: 404 });
    }
  }

  return new Response('not found', { status: 404 });
}

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
  // userData is the fixed control root. New libraries live in a child directory
  // so moving data can never move data-dir.json or llm.enc themselves.
  return path.join(app.getPath('userData'), 'data');
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
  const controlRoot = app.getPath('userData');
  if (fs.existsSync(path.join(controlRoot, 'library.db'))
      && !fs.existsSync(path.join(nativeDir, 'library.db'))) {
    return controlRoot;
  }
  if (fs.existsSync(path.join(legacyDir, 'library.db'))
      && !fs.existsSync(path.join(nativeDir, 'library.db'))) {
    return legacyDir;
  }
  return nativeDir;
};
const currentDataDir = () => customDataDir || defaultDataDir();

// ---- 模型配置：safeStorage 加密落盘（§6.3），密钥绝不进渲染进程 ----
// v2 起按槽位存储：{ slots: { qa, library, embedding } }，各槽位独立。
const llmConfigPath = () => path.join(app.getPath('userData'), 'llm.enc');

function loadLLMConfig() {
  try {
    const blob = fs.readFileSync(llmConfigPath());
    const parsed = JSON.parse(safeStorage.decryptString(blob));
    const slots = (parsed && parsed.slots) || {};
    // v1 平铺格式（{endpoint, api_key, model}）迁移为 qa 槽位
    const qa = slots.qa
      || (parsed && !parsed.slots && (parsed.endpoint || parsed.model) ? parsed : null);
    return {
      qa: qa || null,
      library: slots.library || null,
      embedding: slots.embedding || null,
    };
  } catch { return null; }
}

function saveLLMConfig(slots) {
  if (!slots.qa && !slots.library && !slots.embedding) {
    // 三个槽位全空 = 没有任何密钥值得落盘，直接删文件
    try { fs.unlinkSync(llmConfigPath()); } catch (err) {
      if (err.code !== 'ENOENT') throw err;
    }
    return;
  }
  if (!safeStorage.isEncryptionAvailable()) {
    throw new Error('系统安全存储不可用，已拒绝在磁盘保存 API 密钥');
  }
  fs.mkdirSync(path.dirname(llmConfigPath()), { recursive: true });
  fs.writeFileSync(llmConfigPath(), safeStorage.encryptString(JSON.stringify({ slots })));
}

function slotShape(cfg) {
  return cfg
    ? { provider: cfg.provider || 'openai', endpoint: cfg.endpoint || '',
        model: cfg.model || '', has_key: !!cfg.api_key }
    : { provider: null, endpoint: '', model: '', has_key: false };
}

async function pushLLMToSidecar(cfg, slot) {
  if (!sidecarInfo) throw new Error('sidecar 未就绪，模型配置未保存');
  try {
    const response = await fetch(`http://127.0.0.1:${sidecarInfo.port}/settings/models`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
                 Authorization: `Bearer ${sidecarInfo.token}` },
      body: JSON.stringify(cfg
        ? { slot, provider: cfg.provider || 'openai', endpoint: cfg.endpoint || '',
            api_key: cfg.api_key || '', model: cfg.model || '' }
        : { slot, clear: true }),
    });
    if (!response.ok) {
      let detail = '';
      try { detail = String((await response.json()).detail || ''); } catch {}
      throw new Error(detail || `sidecar 拒绝模型配置（HTTP ${response.status}）`);
    }
    return await response.json();
  } catch (e) {
    console.error('[main] 模型配置推送失败:', e.message);
    throw e;
  }
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
    env.INKTABLE_DATA_DIR = currentDataDir();
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
    const slots = loadLLMConfig();
    if (slots) {
      boot.llm = {};
      for (const [slot, cfg] of Object.entries(slots)) {
        if (cfg) boot.llm[slot] = cfg;
      }
    }
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

function stopSidecarAndWait(timeoutMs = 7000) {
  abortAllApiStreams();
  // 数据迁移前必须确认进程真的退了 —— 数据库文件被占用时移动会损坏
  return new Promise((resolve, reject) => {
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
    setTimeout(() => {
      if (done) return;
      done = true;
      reject(new Error('sidecar 未在迁移时限内退出，已中止数据迁移'));
    }, timeoutMs).unref();
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
  ['GET', /^\/(?:health|categories|books|stats|sources|watch\/status|index\/status|settings\/(?:ocr|qa|llm|models)|integrations\/ccswitch|reports\/weekly|journal|journal\/related)(?:\?[^#]*)?$/],
  ['GET', /^\/files(?:\?[^#]*)?$/],
  // 文件树是 /files/tree（两段），不是 /files/{id}/tree；正文读取带分页查询参数。
  // file_id 是整数主键，收窄成 [0-9]+ 而非宽松的 [^/]+。
  ['GET', /^\/files\/tree(?:\?[^#]*)?$/],
  ['GET', /^\/files\/[0-9]+\/(?:detail|content)(?:\?[^#]*)?$/],
  // AI Library 只放行当前 UI 需要的精确路由和参数形状；不要用
  // /library/.* 或任意查询串这种宽白名单。
  ['GET', /^\/library\/items(?:\?limit=[0-9]+&offset=[0-9]+(?:&status=(?:pending|running|ready|failed|stale))?(?:&category_id=-?[0-9]+)?(?:&tag_id=[0-9]+)?)?$/],
  ['GET', /^\/library\/items\/[0-9]+$/],
  ['GET', /^\/library\/(?:stats|enrichment\/status|relations\/status|taxonomy|tree)$/],
  ['GET', /^\/library\/enrichment\/runs\/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/],
  ['GET', /^\/files\/[0-9]+\/raw$/],
  ['POST', /^\/library\/sync$/],
  ['POST', /^\/library\/enrich(?:\?limit=[0-9]+)?$/],
  ['POST', /^\/library\/enrichment\/runs(?:\?retry_failed=true)?$/],
  ['POST', /^\/library\/enrichment\/runs\/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\/(?:cancel|step\?limit=[0-9]+)$/],
  ['POST', /^\/library\/relations\/rebuild(?:\?limit=[0-9]+&top_k=[0-9]+&min_score=-?[0-9]+(?:\.[0-9]+)?&chunks_per_item=[0-9]+)?$/],
  ['POST', /^\/(?:settings\/llm\/test|settings\/models(?:\/test|\/list)?|ask(?:\/stream)?|files\/(?:remove|classify)|books\/add|classify\/auto_ext|search|sources\/(?:discover|discover_deep|preview|enable|disable|remove|add|auto_preserve|preserve_all)|watch\/start|index\/(?:run|embed_backfill|retry_scanned)|settings\/(?:ocr|qa)|journal\/remove)$/],
];
const MAX_API_BODY_BYTES = 1024 * 1024;
const MAX_STREAM_PAYLOAD_BYTES = 256 * 1024;
const MAX_STREAM_FRAME_BYTES = 1024 * 1024;
const MAX_STREAM_TOTAL_BYTES = 16 * 1024 * 1024;
const MAX_STREAMS_PER_SENDER = 4;
const MAX_RAW_FALLBACK_BYTES = 16 * 1024 * 1024;

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

async function readResponseBufferLimited(response, limit) {
  const declared = Number(response.headers.get('content-length') || 0);
  if (Number.isFinite(declared) && declared > limit) {
    throw new Error(`file exceeds ${Math.round(limit / 1024 / 1024)} MB IPC fallback limit`);
  }
  if (!response.body || typeof response.body.getReader !== 'function') {
    const buffer = Buffer.from(await response.arrayBuffer());
    if (buffer.length > limit) throw new Error('file exceeds IPC fallback limit');
    return buffer;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const chunk = await reader.read();
    if (chunk.done) break;
    total += chunk.value.byteLength;
    if (total > limit) {
      await reader.cancel('IPC fallback limit exceeded');
      throw new Error('file exceeds IPC fallback limit');
    }
    chunks.push(Buffer.from(chunk.value));
  }
  return Buffer.concat(chunks, total);
}

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
  // 原文查看器保底通道：inkdoc:// 不可用时以 base64 过 IPC 取文件字节
  const rawMatch = method === 'GET' ? reqPath.match(/^\/files\/([0-9]+)\/raw$/) : null;
  if (rawMatch) {
    try {
      const upstream = await fetch(
        `http://127.0.0.1:${sidecarInfo.port}/files/${rawMatch[1]}/raw`,
        { headers: { Authorization: `Bearer ${sidecarInfo.token}` } });
      if (!upstream.ok) {
        return { ok: false, status: upstream.status, error: 'HTTP ' + upstream.status };
      }
      const buf = await readResponseBufferLimited(upstream, MAX_RAW_FALLBACK_BYTES);
      return { ok: true, status: upstream.status,
               b64: buf.toString('base64'),
               contentType: upstream.headers.get('content-type') || 'application/octet-stream' };
    } catch (err) {
      return { ok: false, status: 0, error: String((err && err.message) || err) };
    }
  }
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

async function privilegedSidecarRequest(requestPath, method, payload) {
  if (!sidecarInfo) return { ok: false, error: 'sidecar 未就绪' };
  try {
    const response = await fetch(`http://127.0.0.1:${sidecarInfo.port}${requestPath}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${sidecarInfo.token}`,
      },
      body: method === 'POST' ? JSON.stringify(payload || {}) : undefined,
    });
    let data = null;
    try { data = await response.json(); } catch {}
    return { ok: response.ok, status: response.status, data,
             error: response.ok ? '' : `HTTP ${response.status}` };
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err) };
  }
}

ipcMain.handle('file:trash-by-id', async (_e, fileId) => {
  return trashFileById(fileId, {
    sidecarRequest: privilegedSidecarRequest,
    trashItem: (filePath) => shell.trashItem(filePath),
    confirm: async (target) => {
      const kinds = [target.source ? '原文件' : '', target.preserved ? '保全副本' : '']
        .filter(Boolean).join('和');
      const answer = await dialog.showMessageBox(mainWindow, {
        type: 'warning',
        buttons: ['取消', '移入废纸篓'],
        defaultId: 0,
        cancelId: 0,
        message: `把“${target.name}”移入废纸篓？`,
        detail: `${kinds}将由系统移入废纸篓；全部成功后才会清理库内记录。`,
      });
      return answer.response === 1;
    },
  });
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
  if (pathsOverlap(oldDir, target)) return { ok: false, error: '新旧数据目录不能互相包含' };
  if (fs.existsSync(target)) return { ok: false, error: '目标已存在：' + target };

  // 先在旧库运行完整检查；失败时不停止服务、不复制任何数据。
  const integrity = await privilegedSidecarRequest('/db/integrity_check', 'POST', {});
  if (!integrity.ok || !integrity.data || integrity.data.ok !== true) {
    return { ok: false, error: '当前数据库完整性检查未通过，已取消迁移' };
  }

  // 检查 → 确认停库 → 暂存复制与逐文件 SHA-256 → 原子落目标 →
  // 新库健康检查/rebase → 最后才原子写目录指针。旧目录始终保留作回滚。
  publishSidecarStatus({ state: 'restarting', attempt: 0 });
  try {
    await stopSidecarAndWait();
  } catch (err) {
    try { await startHealthySidecar(); } catch (restartErr) { scheduleSidecarRestart(restartErr); }
    return { ok: false, error: err.message };
  }
  const previousCustomDir = customDataDir;
  let copied;
  try {
    copied = copyDataDirectory(oldDir, target, app.getPath('userData'));
    customDataDir = target;
  } catch (err) {
    customDataDir = previousCustomDir;
    console.error('[main] 数据迁移失败:', err.message);
    try { await startHealthySidecar(); } catch (e2) { scheduleSidecarRestart(e2); }
    return { ok: false, error: '迁移失败：' + err.message };
  }

  try {
    await startHealthySidecar();
    restartAttempts = 0;
    lastRestartError = '';
    const rebased = await privilegedSidecarRequest('/system/rebase_preserved', 'POST', {
      old_prefix: oldDir,
      new_prefix: target,
    });
    if (!rebased.ok) throw new Error('保全路径重写失败：' + rebased.error);
    writeJsonAtomic(dataDirConfigPath(), { dir: target });
    return { ok: true, dir: target, files: copied.files, bytes: copied.bytes,
             old_dir_retained: oldDir };
  } catch (err) {
    console.error('[main] 新数据目录验证失败，回滚：', err.message);
    try { await stopSidecarAndWait(); } catch {}
    customDataDir = previousCustomDir;
    try {
      await startHealthySidecar();
      restartAttempts = 0;
      lastRestartError = '';
    } catch (rollbackErr) {
      scheduleSidecarRestart(rollbackErr);
      return { ok: false, error: '新目录失败且旧目录重启失败：' + rollbackErr.message,
               rollback: false, candidate_dir: target };
    }
    return { ok: false, error: '新目录验证失败，已恢复旧目录：' + err.message,
             rollback: true, candidate_dir: target };
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
  const slots = loadLLMConfig() || { qa: null, library: null, embedding: null };
  return {
    slots: {
      qa: slotShape(slots.qa),
      library: slotShape(slots.library),
      embedding: slotShape(slots.embedding),
    },
    encryption_available: safeStorage.isEncryptionAvailable(),
  };
});

ipcMain.handle('llm:set', async (_e, incoming) => {
  if (!incoming || typeof incoming !== 'object') {
    throw new TypeError('模型配置格式无效');
  }
  const slot = ['qa', 'library', 'embedding'].includes(incoming.slot)
    ? incoming.slot : 'qa';
  const prev = loadLLMConfig() || { qa: null, library: null, embedding: null };
  if (incoming.clear === true) {
    const oldSlot = prev[slot];
    await pushLLMToSidecar(null, slot);
    prev[slot] = null;
    try {
      saveLLMConfig(prev);
    } catch (err) {
      if (oldSlot) await pushLLMToSidecar(oldSlot, slot);
      throw err;
    }
    return slotShape(null);
  }
  const prevSlot = prev[slot] || {};
  const prepared = prepareSlotConfig(incoming, prevSlot, slot);
  const cfg = prepared.config;
  // sidecar 是配置校验的最终裁决者。只有它接受以后才加密落盘；本地
  // 持久化失败则把运行时配置回滚到旧槽位，避免内存/磁盘悄悄分叉。
  await pushLLMToSidecar(cfg, slot);
  prev[slot] = cfg;
  try {
    saveLLMConfig(prev);
  } catch (err) {
    await pushLLMToSidecar(prevSlot && prevSlot.endpoint ? prevSlot : null, slot);
    throw err;
  }
  return slotShape(cfg);
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
  // inkdoc:// 的注册必须先于任何窗口加载（见文件头部注释的授权模型）
  protocol.handle('inkdoc', handleInkdocRequest);

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
