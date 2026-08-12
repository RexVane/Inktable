// 预览工具的桥接桩：与正式 preload 暴露相同的 API 形状，数据全为演示值。
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('inktable', {
  getSidecarInfo: async () => ({ port: 45999, token: 'preview-token' }),
  getSidecarStatus: async () => ({ state: 'ready', revision: 1 }),
  revealInFinder: async () => {},
  pickDirectory: async () => null,
  llmGet: async () => ({ endpoint: 'https://api.example.com/v1', model: 'qwen3-max', has_key: true }),
  llmSet: async () => ({ endpoint: 'https://api.example.com/v1', model: 'qwen3-max', has_key: true }),
  onSidecar: (cb) => {
    if (typeof cb === 'function') setTimeout(() => cb({ state: 'ready', revision: 1 }), 0);
    return () => {};
  },
});
