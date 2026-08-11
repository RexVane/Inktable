// 暴露给渲染进程的受控桥（contextIsolation 开启）
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('inktable', {
  getSidecarInfo: () => ipcRenderer.invoke('sidecar:info'),
  revealInFinder: (filePath) => ipcRenderer.invoke('shell:reveal', filePath),
  pickDirectory: () => ipcRenderer.invoke('dialog:pickDirectory'),
  onSidecar: (cb) => {
    ipcRenderer.on('sidecar:status', (_e, info) => cb(info));
    ipcRenderer.invoke('sidecar:info').then(cb);
  },
});
