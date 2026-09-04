// Ordo API Client for React
// 会话模型：bootstrap() 获取 csrfToken（HttpOnly cookie 由后端下发），
// 之后所有非 GET 请求自动携带 x-ordo-csrf；401 时自动重新 bootstrap 并重试一次。
const BASE_URL = '';

let csrfToken = null;
let bootstrapPromise = null;

export async function bootstrap() {
  if (!bootstrapPromise) {
    bootstrapPromise = request('/api/v1/session/bootstrap', { method: 'GET' })
      .then((data) => {
        csrfToken = data?.csrfToken || null;
        return data;
      })
      .catch((err) => {
        bootstrapPromise = null;
        throw err;
      });
  }
  return bootstrapPromise;
}

export function hasSession() {
  return Boolean(csrfToken);
}

export class ApiError extends Error {
  constructor(status, code, message, details = null) {
    super(message || code || 'Request failed');
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function rawRequest(path, options = {}) {
  const url = `${BASE_URL}${path}`;
  const headers = {
    'Accept': 'application/json',
    ...(options.headers || {})
  };

  let body = options.body;
  if (body && typeof body === 'object' && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  if (options.method && options.method !== 'GET' && csrfToken) {
    headers['x-ordo-csrf'] = csrfToken;
  }

  let res;
  try {
    res = await fetch(url, { ...options, body, headers });
  } catch (err) {
    throw new ApiError(0, 'NETWORK_ERROR', err.message || '网络通信异常，请确认本地服务已启动');
  }

  const isJson = res.headers.get('content-type')?.includes('application/json');
  const data = isJson ? await res.json() : await res.text();

  if (!res.ok) {
    const errData = (typeof data === 'object' && data !== null) ? (data.error || data) : {};
    throw new ApiError(res.status, errData.code || ('HTTP_' + res.status), errData.message || res.statusText, errData.details);
  }

  return (data && typeof data === 'object' && 'data' in data) ? data.data : data;
}

async function request(path, options = {}) {
  const method = options.method || 'GET';
  if (!csrfToken && path !== '/api/v1/session/bootstrap') {
    await bootstrap();
  }
  try {
    return await rawRequest(path, options);
  } catch (err) {
    // 401 自愈：会话过期/丢失时重新 bootstrap 后重试一次
    if (err instanceof ApiError && err.status === 401 && path !== '/api/v1/session/bootstrap') {
      bootstrapPromise = null;
      await bootstrap();
      return rawRequest(path, options);
    }
    throw err;
  }
}

// multipart 上传：body 传 FormData，由后端 @fastify/multipart 解析
export function upload(path, formData, options = {}) {
  return request(path, { ...options, method: 'POST', body: formData });
}

// SSE 流式消费（conversations/{id}/messages 等 text/event-stream 接口）
// 事件协议：event: stage|token|done，data 为 JSON
export async function streamSSE(path, { body = {}, onStage, onToken, onDone, signal } = {}) {
  if (!csrfToken) await bootstrap();
  let res;
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      method: 'POST',
      headers: {
        'Accept': 'text/event-stream',
        'Content-Type': 'application/json',
        'x-ordo-csrf': csrfToken
      },
      body: JSON.stringify(body),
      signal
    });
  } catch (err) {
    throw new ApiError(0, 'NETWORK_ERROR', err.message || '网络通信异常，请确认本地服务已启动');
  }
  if (!res.ok) {
    const payload = await res.json().catch(() => ({}));
    const errData = payload.error || payload;
    throw new ApiError(res.status, errData.code || ('HTTP_' + res.status), errData.message || res.statusText, errData.details);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let event = 'message';
      const dataLines = [];
      for (const line of chunk.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;
      let payload;
      try { payload = JSON.parse(dataLines.join('\n')); } catch { payload = dataLines.join('\n'); }
      if (event === 'stage' && onStage) onStage(payload);
      else if (event === 'token' && onToken) onToken(payload);
      else if (event === 'done' && onDone) onDone(payload);
    }
  }
}

export const api = {
  // Session
  bootstrap,

  // System & Health
  getHealth: () => request('/api/v1/health'),
  getVersion: () => request('/api/v1/version'),
  getDiagnostics: () => request('/api/v1/diagnostics'),

  // Knowledge Bases & Datasets
  getKnowledgeBases: () => request('/api/v1/knowledge-bases'),
  getDatasets: (kbId) => request(`/api/v1/knowledge-bases/${kbId}/datasets`),
  createDataset: (kbId, payload) => request(`/api/v1/knowledge-bases/${kbId}/datasets`, { method: 'POST', body: payload }),
  getDocuments: (datasetId, params = {}) => {
    const q = new URLSearchParams(params).toString();
    return request(`/api/v1/datasets/${datasetId}/documents${q ? '?' + q : ''}`);
  },

  // Indexing & Chunks
  getIndexingPipeline: (datasetId) => request(`/api/v1/datasets/${datasetId}/indexing/pipeline`),
  getIndexingStats: (datasetId) => request(`/api/v1/datasets/${datasetId}/indexing/stats`),
  getChapters: (datasetId) => request(`/api/v1/datasets/${datasetId}/chapters`),
  getChunks: (datasetId, params = {}) => {
    const q = new URLSearchParams(params).toString();
    return request(`/api/v1/datasets/${datasetId}/chunks${q ? '?' + q : ''}`);
  },
  getChunk: (chunkId) => request(`/api/v1/chunks/${chunkId}`),
  getChunkLineage: (chunkId) => request(`/api/v1/chunks/${chunkId}/lineage`),
  editChunk: (chunkId, payload) => request(`/api/v1/chunks/${chunkId}/revisions`, { method: 'POST', body: payload }),
  vectorizeChunk: (chunkId) => request(`/api/v1/chunks/${chunkId}/vectorize`, { method: 'POST' }),
  splitChunk: (chunkId, payload) => request(`/api/v1/chunks/${chunkId}/split`, { method: 'POST', body: payload }),
  mergeChunks: (payload) => request('/api/v1/chunks/merge', { method: 'POST', body: payload }),
  toggleDisableChunk: (chunkId, excluded) => request(`/api/v1/chunks/${chunkId}/toggle-disable`, { method: 'POST', body: { excluded } }),
  batchVectorizePending: (datasetId) => request(`/api/v1/datasets/${datasetId}/indexing/vectorize-pending`, { method: 'POST' }),
  rebuildHnswIndex: (datasetId) => request(`/api/v1/datasets/${datasetId}/indexing/rebuild-hnsw`, { method: 'POST' }),
  optimizeVectorIndex: (datasetId) => request(`/api/v1/datasets/${datasetId}/indexing/optimize-index`, { method: 'POST' }),
  rebuildBm25Index: (datasetId) => request(`/api/v1/datasets/${datasetId}/indexing/rebuild-bm25`, { method: 'POST' }),
  setHybridWeights: (datasetId, payload) => request(`/api/v1/datasets/${datasetId}/indexing/hybrid-weights`, { method: 'PUT', body: payload }),
  getReleases: (datasetId) => request(`/api/v1/datasets/${datasetId}/releases`),
  buildRelease: (datasetId, payload = {}) => request(`/api/v1/datasets/${datasetId}/releases`, { method: 'POST', body: payload }),
  searchRelease: (releaseId, query) => request(`/api/v1/releases/${releaseId}/search`, { method: 'POST', body: { query } }),

  // Parsing & Tasks
  getTasks: (params = {}) => {
    const q = new URLSearchParams(params).toString();
    return request(`/api/v1/tasks${q ? '?' + q : ''}`);
  },
  retryTask: (taskId) => request(`/api/v1/tasks/${taskId}/retry`, { method: 'POST' }),
  pauseTask: (taskId) => request(`/api/v1/tasks/${taskId}/pause`, { method: 'POST' }),
  cancelTask: (taskId) => request(`/api/v1/tasks/${taskId}/cancel`, { method: 'POST' })
};
