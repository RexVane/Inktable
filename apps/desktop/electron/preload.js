// 暴露给渲染进程的受控桥（contextIsolation 开启）
const { contextBridge, ipcRenderer, webFrame } = require('electron');

contextBridge.exposeInMainWorld('inktable', {
  // 渲染层按平台微调布局（macOS 红绿灯留白等）
  platform: process.platform,
  // 只回 { port }（就绪信号），不含令牌。所有 sidecar 调用走下面的代理。
  getSidecarInfo: () => ipcRenderer.invoke('sidecar:info'),
  getSidecarStatus: () => ipcRenderer.invoke('sidecar:get-status'),
  // 受控 HTTP 代理：渲染层给相对 path / method / 已序列化 body，
  // 令牌由主进程附加，永不进入渲染进程。
  apiRequest: (req) => ipcRenderer.invoke('api:request', req),
  // SSE 流式代理：主进程解析事件并经私有频道逐条回传。
  apiStream: (path, payload, onEvent) => {
    const id = 's' + Date.now().toString(36) + Math.random().toString(36).slice(2);
    const channel = 'api:stream:' + id;
    return new Promise((resolve, reject) => {
      const listener = (_e, msg) => {
        if (!msg) return;
        if (msg.type === 'event') {
          if (typeof onEvent === 'function') {
            try { onEvent(msg.event, msg.data); } catch (err) {
              ipcRenderer.removeListener(channel, listener);
              ipcRenderer.invoke('api:stream-abort', id);
              reject(err);
            }
          }
        } else if (msg.type === 'end') {
          ipcRenderer.removeListener(channel, listener);
          resolve();
        } else if (msg.type === 'error') {
          ipcRenderer.removeListener(channel, listener);
          reject(new Error(msg.message || 'stream failed'));
        }
      };
      ipcRenderer.on(channel, listener);
      ipcRenderer.invoke('api:stream-start', { id, path, payload }).catch((err) => {
        ipcRenderer.removeListener(channel, listener);
        reject(err);
      });
    });
  },
  revealInFinder: (filePath) => ipcRenderer.invoke('shell:reveal', filePath),
  openPath: (filePath) => ipcRenderer.invoke('shell:open', filePath),
  trashItem: (filePath) => ipcRenderer.invoke('shell:trash', filePath),
  setZoom: (factor) => {
    const f = Number(factor);
    webFrame.setZoomFactor(Number.isFinite(f) && f >= 0.5 && f <= 2 ? f : 1);
  },
  // mode 决定原生 chrome（右键菜单、原生滚动条）的深浅；color/symbolColor
  // 是当前主题从 CSS 读出的外壳色与墨色，用于 Windows 窗口控件叠加层。
  setTheme: (req) => ipcRenderer.invoke('theme:set', req),
  dataGet: () => ipcRenderer.invoke('data:get'),
  dataChange: () => ipcRenderer.invoke('data:change'),
  pickDirectory: () => ipcRenderer.invoke('dialog:pickDirectory'),
  llmGet: () => ipcRenderer.invoke('llm:get'),
  llmSet: (cfg) => ipcRenderer.invoke('llm:set', cfg),
  onFocusSearch: (cb) => {
    if (typeof cb !== 'function') return () => {};
    const listener = () => cb();
    ipcRenderer.on('focus-search', listener);
    return () => ipcRenderer.removeListener('focus-search', listener);
  },
  onSidecar: (cb) => {
    if (typeof cb !== 'function') return () => {};
    const listener = (_e, info) => cb(info);
    ipcRenderer.on('sidecar:status', listener);
    // 状态由主进程缓存，因此窗口创建前发生的 restarting/failed 也不会丢。
    ipcRenderer.invoke('sidecar:get-status').then(cb);
    return () => ipcRenderer.removeListener('sidecar:status', listener);
  },
});

// AI Library 是普通 renderer 模块，不属于特权 bridge。preload 只负责把同源
// 静态资源挂进页面：library.js 仍运行在页面世界，只能调用上面已经受控的
// window.inktable API；它拿不到 ipcRenderer，更拿不到 sidecar bearer 令牌。
// 这样不用去改 9 万字符的 inline 工作台脚本，也不会改变现有 CSP hash。
window.addEventListener('DOMContentLoaded', () => {
  if (!document.querySelector('link[data-inktable-library]')) {
    const style = document.createElement('link');
    style.rel = 'stylesheet';
    style.href = './library.css';
    style.dataset.inktableLibrary = 'style';
    document.head.appendChild(style);
  }
  if (!document.querySelector('script[data-inktable-library]')) {
    const script = document.createElement('script');
    script.src = './library.js';
    script.async = false;
    script.dataset.inktableLibrary = 'script';
    document.body.appendChild(script);
  }
}, { once: true });
