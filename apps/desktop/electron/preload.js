// 暴露给渲染进程的受控桥（contextIsolation 开启）
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('inktable', {
  getSidecarInfo: () => ipcRenderer.invoke('sidecar:info'),
  getSidecarStatus: () => ipcRenderer.invoke('sidecar:get-status'),
  revealInFinder: (filePath) => ipcRenderer.invoke('shell:reveal', filePath),
  pickDirectory: () => ipcRenderer.invoke('dialog:pickDirectory'),
  llmGet: () => ipcRenderer.invoke('llm:get'),
  llmSet: (cfg) => ipcRenderer.invoke('llm:set', cfg),
  onSidecar: (cb) => {
    if (typeof cb !== 'function') return () => {};
    const listener = (_e, info) => cb(info);
    ipcRenderer.on('sidecar:status', listener);
    // 状态由主进程缓存，因此窗口创建前发生的 restarting/failed 也不会丢。
    ipcRenderer.invoke('sidecar:get-status').then(cb);
    return () => ipcRenderer.removeListener('sidecar:status', listener);
  },
});
