// 截图工具专用的最小假桥。只为让渲染层的启动路径跑通到「空态」,
// 不连 sidecar、不碰令牌 —— 与真 preload 无关,不要在产品路径引用。
const { contextBridge } = require('electron');

const fail = async () => ({ ok: false, status: 0, error: 'theme-shots: no sidecar' });

contextBridge.exposeInMainWorld('inktable', {
  platform: process.platform,
  getSidecarInfo: async () => null,
  getSidecarStatus: async () => ({ state: 'starting' }),
  apiRequest: fail,
  apiStream: async () => { throw new Error('theme-shots: no sidecar'); },
  revealInFinder: async () => false,
  openPath: async () => '',
  trashItem: async () => ({ ok: false }),
  setZoom: () => {},
  setTheme: () => {},
});
