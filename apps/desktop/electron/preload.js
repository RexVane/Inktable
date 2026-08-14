// 暴露给渲染进程的受控桥（contextIsolation 开启）
const { contextBridge, ipcRenderer, webFrame } = require('electron');

contextBridge.exposeInMainWorld('inktable', {
  getSidecarInfo: () => ipcRenderer.invoke('sidecar:info'),
  getSidecarStatus: () => ipcRenderer.invoke('sidecar:get-status'),
  revealInFinder: (filePath) => ipcRenderer.invoke('shell:reveal', filePath),
  openPath: (filePath) => ipcRenderer.invoke('shell:open', filePath),
  trashItem: (filePath) => ipcRenderer.invoke('shell:trash', filePath),
  setZoom: (factor) => {
    const f = Number(factor);
    webFrame.setZoomFactor(Number.isFinite(f) && f >= 0.5 && f <= 2 ? f : 1);
  },
  setTheme: (mode) => ipcRenderer.invoke('theme:set', mode),
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
