/* Ordo browser API client. No DOM access and no rendering side effects. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.OrdoApi = api;
})(typeof globalThis === 'undefined' ? this : globalThis, function () {
  'use strict';

  // Signatures: path IDs first, then query (GET) or payload (writes), then
  // request options. The catalog includes planned contracts; a wrapper does
  // not imply that the running backend implements the endpoint.
  const definitions = {
    bootstrapSession: ['GET', '/session/bootstrap'],
    getDashboard: ['GET', '/dashboard'],
    getHealth: ['GET', '/health'],
    getVersion: ['GET', '/version'],
    getDiagnostics: ['GET', '/diagnostics'],
    getOpenApi: ['GET', '/openapi.json'],
    search: ['GET', '/search'],
    getKnowledgeBases: ['GET', '/knowledge-bases'],
    createKnowledgeBase: ['POST', '/knowledge-bases'],
    getKnowledgeBase: ['GET', '/knowledge-bases/:kbId'],
    updateKnowledgeBase: ['PATCH', '/knowledge-bases/:kbId'],
    deleteKnowledgeBase: ['DELETE', '/knowledge-bases/:kbId'],
    getKnowledgeBaseImpact: ['GET', '/knowledge-bases/:kbId/impact'],
    getIndexProfiles: ['GET', '/knowledge-bases/:kbId/index-profiles'],
    createIndexProfile: ['POST', '/knowledge-bases/:kbId/index-profiles'],
    getIndexProfile: ['GET', '/index-profiles/:profileId'],
    updateIndexProfile: ['PATCH', '/index-profiles/:profileId'],
    deleteIndexProfile: ['DELETE', '/index-profiles/:profileId'],
    setDefaultIndexProfile: ['POST', '/index-profiles/:profileId/default'],
    getDatasets: ['GET', '/knowledge-bases/:kbId/datasets'],
    createDataset: ['POST', '/knowledge-bases/:kbId/datasets'],
    getDataset: ['GET', '/datasets/:datasetId'],
    updateDataset: ['PATCH', '/datasets/:datasetId'],
    deleteDataset: ['DELETE', '/datasets/:datasetId'],
    getSources: ['GET', '/datasets/:datasetId/sources'],
    createSource: ['POST', '/datasets/:datasetId/sources'],
    uploadDocument: ['POST', '/datasets/:datasetId/files'],
    uploadArchive: ['POST', '/datasets/:datasetId/archives'],
    directoryPreview: ['POST', '/datasets/:datasetId/directory/preview'],
    directoryImport: ['POST', '/datasets/:datasetId/directory/import'],
    getDocuments: ['GET', '/datasets/:datasetId/documents'],
    getDocument: ['GET', '/documents/:documentId'],
    deleteDocument: ['DELETE', '/documents/:documentId'],
    getArtifact: ['GET', '/artifacts/:artifactId/:kind'],
    getChunks: ['GET', '/datasets/:datasetId/chunks'],
    getIndexingStats: ['GET', '/datasets/:datasetId/indexing/stats'],
    getIndexingPipeline: ['GET', '/datasets/:datasetId/indexing/pipeline'],
    getChapters: ['GET', '/datasets/:datasetId/chapters'],
    getChunk: ['GET', '/chunks/:chunkId'],
    getChunkLineage: ['GET', '/chunks/:chunkId/lineage'],
    editChunk: ['POST', '/chunks/:chunkId/revisions'],
    vectorizeChunk: ['POST', '/chunks/:chunkId/vectorize'],
    toggleChunkDisabled: ['POST', '/chunks/:chunkId/toggle-disable'],
    restoreChunk: ['POST', '/chunks/:chunkId/restore'],
    getChunkDiff: ['GET', '/chunks/:chunkId/diff'],
    splitChunk: ['POST', '/chunks/:chunkId/split'],
    mergeChunks: ['POST', '/chunks/merge'],
    vectorizePending: ['POST', '/datasets/:datasetId/indexing/vectorize-pending'],
    rebuildHnswIndex: ['POST', '/datasets/:datasetId/indexing/rebuild-hnsw'],
    optimizeVectorIndex: ['POST', '/datasets/:datasetId/indexing/optimize-index'],
    rebuildBm25Index: ['POST', '/datasets/:datasetId/indexing/rebuild-bm25'],
    setHybridWeights: ['PUT', '/datasets/:datasetId/indexing/hybrid-weights'],
    getReleases: ['GET', '/datasets/:datasetId/releases'],
    buildRelease: ['POST', '/datasets/:datasetId/releases'],
    getRelease: ['GET', '/releases/:releaseId'],
    activateRelease: ['POST', '/releases/:releaseId/activate'],
    rollbackRelease: ['POST', '/releases/:releaseId/rollback'],
    getReleaseImpact: ['GET', '/releases/:releaseId/impact'],
    searchRelease: ['POST', '/releases/:releaseId/search'],
    getTasks: ['GET', '/tasks'],
    getTask: ['GET', '/tasks/:taskId'],
    cancelTask: ['POST', '/tasks/:taskId/cancel'],
    pauseTask: ['POST', '/tasks/:taskId/pause'],
    resumeTask: ['POST', '/tasks/:taskId/resume'],
    retryTask: ['POST', '/tasks/:taskId/retry'],
    waitTask: ['GET', '/tasks/:taskId/wait'],
    getConnectors: ['GET', '/connectors'],
    createConnector: ['POST', '/connectors'],
    getConnector: ['GET', '/connectors/:connectorId'],
    testConnector: ['POST', '/connectors/:connectorId/test'],
    getConnectorSchema: ['GET', '/connectors/:connectorId/schema'],
    getQueryTemplates: ['GET', '/connectors/:connectorId/templates'],
    createQueryTemplate: ['POST', '/connectors/:connectorId/templates'],
    executeQueryTemplate: ['POST', '/query-templates/:templateId/execute'],
    snapshotQueryTemplate: ['POST', '/query-templates/:templateId/snapshot'],
    getOntologies: ['GET', '/knowledge-bases/:kbId/ontologies'],
    createOntology: ['POST', '/knowledge-bases/:kbId/ontologies'],
    publishOntology: ['POST', '/ontologies/:ontologyId/publish'],
    getGraph: ['GET', '/datasets/:datasetId/graph'],
    getGraphEntities: ['GET', '/datasets/:datasetId/graph/entities'],
    createGraphEntity: ['POST', '/datasets/:datasetId/graph/entities'],
    createGraphRelation: ['POST', '/datasets/:datasetId/graph/relations'],
    getConversations: ['GET', '/conversations'],
    createConversation: ['POST', '/conversations'],
    getConversation: ['GET', '/conversations/:conversationId'],
    deleteConversation: ['DELETE', '/conversations/:conversationId'],
    sendMessage: ['POST', '/conversations/:conversationId/messages'],
    sendFeedback: ['POST', '/messages/:messageId/feedback'],
    openCitation: ['GET', '/citations/:citationId'],
    getTraces: ['GET', '/traces'],
    getTrace: ['GET', '/traces/:traceId'],
    getTracePipeline: ['GET', '/traces/:traceId/pipeline'],
    replayTrace: ['POST', '/traces/:traceId/replay'],
    compareTraces: ['GET', '/traces/:traceId/compare/:otherId'],
    getTraceRouteStage: ['GET', '/traces/:traceId/stages/route'],
    getTraceRecallStage: ['GET', '/traces/:traceId/stages/recall'],
    filterRecall: ['POST', '/traces/:traceId/stages/recall/filter'],
    retryRecallChannel: ['POST', '/traces/:traceId/stages/recall/retry-channel'],
    getRecallChunk: ['GET', '/traces/:traceId/stages/recall/chunks/:chunkId'],
    exportRecall: ['GET', '/traces/:traceId/stages/recall/export'],
    getTraceFusionStage: ['GET', '/traces/:traceId/stages/fusion'],
    updateFusionWeights: ['PUT', '/traces/:traceId/stages/fusion/weights'],
    resetFusionWeights: ['POST', '/traces/:traceId/stages/fusion/reset-weights'],
    getFusionCalculation: ['GET', '/traces/:traceId/stages/fusion/calculation/:candidateId'],
    getFusionCandidates: ['GET', '/traces/:traceId/stages/fusion/candidates'],
    getFusionChunk: ['GET', '/traces/:traceId/stages/fusion/chunks/:candidateId'],
    getFusionLogs: ['GET', '/traces/:traceId/stages/fusion/logs'],
    exportFusion: ['GET', '/traces/:traceId/stages/fusion/export'],
    rerunFusionStage: ['POST', '/traces/:traceId/stages/fusion/rerun'],
    getTraceRerankStage: ['GET', '/traces/:traceId/stages/rerank'],
    getRerankChunk: ['GET', '/traces/:traceId/stages/rerank/chunks/:chunkId'],
    updateRerankConfig: ['PUT', '/traces/:traceId/stages/rerank/config'],
    compareRerank: ['POST', '/traces/:traceId/stages/rerank/compare'],
    getRerankLogs: ['GET', '/traces/:traceId/stages/rerank/logs'],
    getModels: ['GET', '/models'],
    createModel: ['POST', '/models'],
    getModel: ['GET', '/models/:modelId'],
    patchModel: ['PATCH', '/models/:modelId'],
    deleteModel: ['DELETE', '/models/:modelId'],
    testModel: ['POST', '/models/:modelId/test'],
    getSettings: ['GET', '/settings'],
    updateSetting: ['PUT', '/settings/:key'],
    getFeatureFlags: ['GET', '/feature-flags'],
    putFeatureFlag: ['PUT', '/feature-flags/:key'],
    getWikiPages: ['GET', '/wiki'],
    createWikiPage: ['POST', '/wiki'],
    getWikiPage: ['GET', '/wiki/:pageId'],
    reviseWikiPage: ['POST', '/wiki/:pageId'],
    wikiFromMessage: ['POST', '/wiki/from-message/:messageId'],
    getAssistants: ['GET', '/assistants'],
    createAssistant: ['POST', '/assistants'],
    getAssistant: ['GET', '/assistants/:assistantId'],
    updateAssistant: ['PATCH', '/assistants/:assistantId'],
    publishAssistant: ['POST', '/assistants/:assistantId/publish'],
    pauseAssistant: ['POST', '/assistants/:assistantId/pause'],
    getAssistantClients: ['GET', '/assistants/:assistantId/clients'],
    createAssistantClient: ['POST', '/assistants/:assistantId/clients'],
    rotateWidgetClient: ['POST', '/widget-clients/:clientId/rotate'],
    revokeWidgetClient: ['DELETE', '/widget-clients/:clientId'],
    issueWidgetToken: ['POST', '/public/widget/token'],
    createWidgetSession: ['POST', '/public/widget/sessions'],
    sendWidgetMessage: ['POST', '/public/widget/sessions/:sessionId/messages'],
    requestWidgetHandoff: ['POST', '/public/widget/sessions/:sessionId/handoff'],
    deleteWidgetSession: ['DELETE', '/public/widget/sessions/:sessionId'],
    getHandoffs: ['GET', '/handoffs'],
    updateHandoff: ['PATCH', '/handoffs/:handoffId'],
    getBackups: ['GET', '/backups'],
    createBackup: ['POST', '/backups'],
    restoreBackup: ['POST', '/backups/:backupId/restore'],
    getAudit: ['GET', '/audit'],
    verifyAudit: ['GET', '/audit/verify'],

    // Page-specific contracts from planning/前端API清单. These are kept
    // separate from the existing dataset-scoped /documents and /sources APIs.
    getAllDatasets: ['GET', '/datasets'],
    createUnscopedDataset: ['POST', '/datasets'],
    getRegisteredSources: ['GET', '/sources'],
    getSourceTree: ['GET', '/sources/tree'],
    uploadFile: ['POST', '/files'],
    assignSourceDataset: ['PATCH', '/sources/:sourceId/dataset'],
    getRecentSources: ['GET', '/sources/recent'],
    getSourceActivities: ['GET', '/sources/recent-activities'],
    getSourceAttentionItems: ['GET', '/sources/attention-items'],
    testConnectorConfig: ['POST', '/connectors/test'],
    deleteSource: ['DELETE', '/sources/:sourceId'],
    getDatasetTree: ['GET', '/datasets/:datasetId/tree'],
    getDatasetFiles: ['GET', '/datasets/:datasetId/files'],
    inspectFile: ['GET', '/files/:fileId/inspect'],
    createDatasetFolder: ['POST', '/datasets/:datasetId/folders'],
    deleteDatasetFile: ['DELETE', '/datasets/:datasetId/files/:fileId'],
    batchDeleteDatasetFiles: ['POST', '/datasets/:datasetId/files/batch-delete'],
    moveDatasetFile: ['PATCH', '/datasets/:datasetId/files/:fileId/move'],
    batchMoveDatasetFiles: ['POST', '/datasets/:datasetId/files/batch-move'],
    getParsingProfiles: ['GET', '/parsing/profiles'],
    startParsing: ['POST', '/parsing/start'],
    pauseParsing: ['POST', '/parsing/pause'],
    resumeParsing: ['POST', '/parsing/resume'],
    retryFailedParsing: ['POST', '/parsing/retry-failed'],
    getParsingPipelineStats: ['GET', '/parsing/pipeline-stats'],
    getParsingTasks: ['GET', '/parsing/tasks'],
    clearPendingParsingTasks: ['POST', '/parsing/tasks/clear-pending'],
    getParsingSettings: ['GET', '/parsing/settings'],
    updateParsingSettings: ['PATCH', '/parsing/settings'],
    exportParsingLogs: ['GET', '/parsing/logs/export'],
    getDocumentPreviewPages: ['GET', '/documents/:documentId/preview/pages'],
    getDocumentPage: ['GET', '/documents/:documentId/pages/:pageNum'],
    getDocumentPageInspect: ['GET', '/documents/:documentId/pages/:pageNum/inspect'],
    getDocumentPageDiff: ['GET', '/documents/:documentId/pages/:pageNum/diff'],
    getSystemResources: ['GET', '/system/resources'],
    getTraceParseStage: ['GET', '/traces/:traceId/stages/parse'],
    quickParse: ['POST', '/traces/quick-parse'],
    getConversationContext: ['GET', '/conversations/:conversationId/context'],
    reparseTrace: ['POST', '/traces/:traceId/stages/parse/reparse'],
    updateTraceParse: ['PUT', '/traces/:traceId/stages/parse'],
    getTraceParseLogs: ['GET', '/traces/:traceId/stages/parse/logs'],
    classifyTraceIntent: ['POST', '/traces/:traceId/stages/parse/classify-intent'],
    extractTraceEntities: ['POST', '/traces/:traceId/stages/parse/extract-entities'],
    updateTraceKeywords: ['PATCH', '/traces/:traceId/stages/parse/keywords'],
    updateNormalizedQuery: ['PATCH', '/traces/:traceId/stages/parse/normalized-query'],
    rewriteTraceQuery: ['POST', '/traces/:traceId/stages/parse/rewrite-query'],
    updateRewrittenQuery: ['PATCH', '/traces/:traceId/stages/parse/rewritten-query'],
    updateTraceFilters: ['PATCH', '/traces/:traceId/stages/parse/filters'],
    updateTraceRawJson: ['PUT', '/traces/:traceId/stages/parse/raw-json'],
    getTraceEmbedStage: ['GET', '/traces/:traceId/stages/embed'],
    recomputeEmbedding: ['POST', '/traces/:traceId/stages/embed/recompute'],
    getEmbeddingScatter: ['GET', '/traces/:traceId/stages/embed/scatter'],
    compareEmbeddingModels: ['POST', '/traces/:traceId/stages/embed/compare-models'],
    getEmbeddingVector: ['GET', '/traces/:traceId/stages/embed/vector'],
    getEmbeddingLogs: ['GET', '/traces/:traceId/stages/embed/logs'],
    updateTraceRoute: ['PUT', '/traces/:traceId/stages/route'],
    simulateTraceRoute: ['POST', '/traces/:traceId/stages/route/simulate'],
    getTraceRouteIndexes: ['GET', '/traces/:traceId/stages/route/indexes'],
    getTraceRouteIntent: ['GET', '/traces/:traceId/stages/route/intent'],
    getTraceRouteLogs: ['GET', '/traces/:traceId/stages/route/logs'],
    getRecallChannel: ['GET', '/traces/:traceId/stages/recall/channels/:channelId'],
    getTracePromptStage: ['GET', '/traces/:traceId/stages/prompt'],
    updateTracePrompt: ['PUT', '/traces/:traceId/stages/prompt'],
    getPromptVersions: ['GET', '/traces/:traceId/stages/prompt/versions'],
    maskPrompt: ['POST', '/traces/:traceId/stages/prompt/mask'],
    scanPromptSensitiveData: ['GET', '/traces/:traceId/stages/prompt/sensitive-scan'],
    getTraceGenerationStage: ['GET', '/traces/:traceId/stages/generation'],
    regenerateAnswer: ['POST', '/traces/:traceId/stages/generation/regenerate'],
    sendTraceFeedback: ['POST', '/traces/:traceId/feedback'],
    saveTraceQa: ['POST', '/traces/:traceId/save-qa'],
    getTraceWaterfall: ['GET', '/traces/:traceId/waterfall'],
    replayAll: ['POST', '/traces/:traceId/replay-all'],
    deleteAssistant: ['DELETE', '/assistants/:assistantId']
  };

  const operations = Object.freeze(Object.fromEntries(Object.entries(definitions).map(([name, [method, path]]) =>
    [name, Object.freeze({ method, path: '/api/v1' + path })])));
  // The page 15 legacy URL has an existing canonical backend equivalent.
  const aliases = Object.freeze({ 'POST /api/v1/messages/:id/wiki': 'wikiFromMessage' });
  const unwrap = value => value && Object.prototype.hasOwnProperty.call(value, 'data') ? value.data : value;
  const objectOr = (value, key, fallback) => value && typeof value === 'object' ? value : { [key]: value ?? fallback };

  class ApiError extends Error {
    constructor(message, details = {}) {
      super(message);
      this.name = 'ApiError';
      Object.assign(this, details);
    }
  }

  function queryString(params = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params || {})) {
      if (value === undefined || value === null || value === '') continue;
      for (const item of Array.isArray(value) ? value : [value]) {
        query.append(key, typeof item === 'object' ? JSON.stringify(item) : String(item));
      }
    }
    return query.toString();
  }

  async function readEvents(response, onEvent, signal) {
    if (!response.body?.getReader) throw new ApiError('响应不支持事件流', { code: 'STREAM_UNAVAILABLE' });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '', event = 'message', data = [], result = null, completed = false;
    const dispatch = () => {
      if (!data.length) { event = 'message'; return; }
      const text = data.join('\n');
      let value;
      try { value = JSON.parse(text); } catch { value = text; }
      if (event === 'error') throw new ApiError(value?.error?.message || value?.message || '问答事件流失败', { code: value?.error?.code || value?.code || 'STREAM_ERROR' });
      if (event === 'done') { result = value; completed = true; }
      onEvent?.(event, value);
      event = 'message'; data = [];
    };
    const line = value => {
      if (value === '') return dispatch();
      if (value.startsWith('event:')) event = value.slice(6).trim() || 'message';
      if (value.startsWith('data:')) data.push(value.slice(5).replace(/^ /, ''));
    };
    const abort = () => { reader.cancel().catch(() => {}); };
    signal?.addEventListener('abort', abort, { once: true });
    try {
      while (true) {
        if (signal?.aborted) throw new ApiError('请求已取消', { code: 'REQUEST_ABORTED' });
        const { value, done } = await reader.read();
        if (signal?.aborted) throw new ApiError('请求已取消', { code: 'REQUEST_ABORTED' });
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
        let match;
        while ((match = /\r\n|\n|\r(?!$)/.exec(buffer))) {
          line(buffer.slice(0, match.index));
          buffer = buffer.slice(match.index + match[0].length);
          if (completed) break;
        }
        if (completed) break;
        if (done) break;
      }
      if (!completed) {
        if (buffer) line(buffer.replace(/\r$/, ''));
        dispatch();
      }
      if (!completed) throw new ApiError('事件流在完成前中断', { code: 'STREAM_INCOMPLETE' });
      return result;
    } finally {
      signal?.removeEventListener('abort', abort);
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  function createClient({ fetch: fetchImpl = (...args) => globalThis.fetch(...args), baseUrl = '', throwOnError = false } = {}) {
    const client = {
      csrfToken: null, workspaceId: null, connected: false, lastError: null,
      async request(url, options = {}) {
        const { query, responseType = 'json', public: isPublic = false, token, idempotencyKey,
          onEvent, envelope, throwOnError: shouldThrow = throwOnError, skipRefresh = false, ...init } = options;
        try {
          if (!url.startsWith('/api/v1/')) throw new ApiError('API 路径必须位于 /api/v1/', { code: 'API_PATH_INVALID' });
          const qs = queryString(query);
          const target = baseUrl.replace(/\/$/, '') + url + (qs ? (url.includes('?') ? '&' : '?') + qs : '');
          const method = String(init.method || 'GET').toUpperCase();
          const headers = new Headers(init.headers || {});
          const form = typeof FormData !== 'undefined' && init.body instanceof FormData;
          if (init.body !== undefined && init.body !== null && !form && typeof init.body === 'object' && !(init.body instanceof Blob) && !(init.body instanceof ArrayBuffer) && !ArrayBuffer.isView(init.body)) {
            init.body = JSON.stringify(init.body);
          }
          if (form) headers.delete('Content-Type');
          else if (init.body !== undefined && init.body !== null && typeof init.body === 'string' && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
          if (!isPublic && this.csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(method)) headers.set('x-ordo-csrf', this.csrfToken);
          if (isPublic) headers.delete('x-ordo-csrf');
          if (token) headers.set('Authorization', 'Bearer ' + token);
          if (idempotencyKey) headers.set('Idempotency-Key', idempotencyKey);
          if (responseType === 'stream') headers.set('Accept', 'text/event-stream');
          const response = await fetchImpl(target, { ...init, method, headers, credentials: isPublic ? 'omit' : 'same-origin' });
          if (!response.ok) {
            const payload = await response.json().catch(() => null);
            const detail = payload?.error || {};
            if (!isPublic && !skipRefresh && ['SESSION_REQUIRED', 'CSRF_INVALID'].includes(detail.code) && [401, 403].includes(response.status)) {
              const session = await this.bootstrapSession();
              if (session) return this.request(url, { ...options, skipRefresh: true });
            }
            throw new ApiError(detail.message || `HTTP ${response.status}`, { ...detail, status: response.status, code: detail.code || 'HTTP_ERROR', url, method });
          }
          let payload;
          if (response.status === 204) payload = null;
          else if (responseType === 'stream') {
            if (!response.headers.get('Content-Type')?.includes('text/event-stream')) throw new ApiError('服务未返回 SSE 事件流', { code: 'STREAM_CONTENT_TYPE_INVALID' });
            payload = await readEvents(response, onEvent, init.signal);
          }
          else if (responseType === 'text') payload = await response.text();
          else if (responseType === 'blob') payload = await response.blob();
          else payload = await response.json();
          this.lastError = null;
          return payload;
        } catch (error) {
          const failure = error instanceof ApiError ? error : new ApiError(error.message || '请求失败', { status: 0, code: error.name === 'AbortError' ? 'REQUEST_ABORTED' : 'NETWORK_ERROR', url });
          this.lastError = { status: failure.status || 0, code: failure.code, message: failure.message, requestId: failure.requestId, details: failure.details, url };
          if (shouldThrow) throw failure;
          return null;
        }
      },
      async call(name, args = [], options = {}) {
        const operation = operations[name];
        if (!operation) throw new ApiError(`未知 API 方法: ${name}`, { code: 'API_METHOD_UNKNOWN' });
        let index = 0;
        const url = operation.path.replace(/:([A-Za-z]\w*)/g, (_, key) => {
          const value = args[index++];
          if (value === null || value === undefined || value === '' || typeof value === 'object') throw new ApiError(`${key} 为必填路径参数`, { code: 'API_PARAMETER_REQUIRED' });
          return encodeURIComponent(String(value)).replace(/[!'()*]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());
        });
        const input = args[index] ?? {};
        const requestOptions = { ...(args[index + 1] || {}), ...options };
        const responseType = requestOptions.responseType || (name === 'getArtifact' && args[1] === 'markdown' ? 'text' : 'json');
        const payload = await this.request(url, { ...requestOptions, method: operation.method, responseType,
          public: operation.path.startsWith('/api/v1/public/'),
          ...(operation.method === 'GET' ? { query: input } : { body: input }) });
        return requestOptions.envelope || responseType !== 'json' ? payload : unwrap(payload);
      }
    };
    for (const name of Object.keys(operations)) client[name] = function (...args) { return this.call(name, args); };

    // Preserve the workbench's positional signatures while accepting complete
    // request objects, so optional fields are no longer silently discarded.
    Object.assign(client, {
      async bootstrapSession(params = {}, options = {}) {
        if (this._sessionPromise) return this._sessionPromise;
        this._sessionPromise = (async () => {
          const session = await this.call('bootstrapSession', [params, options], { skipRefresh: true });
          this.connected = Boolean(session?.csrfToken);
          if (this.connected) { this.csrfToken = session.csrfToken; this.workspaceId = session.workspaceId; }
          return session;
        })();
        try { return await this._sessionPromise; } finally { this._sessionPromise = null; }
      },
      search(value, params = {}, options = {}) { return this.call('search', [{ ...objectOr(value, 'q', ''), ...params }, options]); },
      createConversation(value, kbId, datasetId, options = {}) {
        return this.call('createConversation', [typeof value === 'object' ? value : { title: value, knowledgeBaseId: kbId, ...(datasetId ? { datasetId } : {}) }, typeof value === 'object' ? kbId : options]);
      },
      sendMessage(id, value, options = {}) { return this.call('sendMessage', [id, objectOr(value, 'question', ''), options]); },
      sendMessageStream(id, value, onEvent, options = {}) { return this.call('sendMessage', [id, objectOr(value, 'question', ''), options], { responseType: 'stream', onEvent }); },
      regenerateAnswerStream(id, payload, onEvent, options = {}) { return this.call('regenerateAnswer', [id, payload, options], { responseType: 'stream', onEvent }); },
      createBackup(value = 'manual', options = {}) { return this.call('createBackup', [objectOr(value, 'label', 'manual'), options]); },
      restoreBackup(id, value = {}, options = {}) { return this.call('restoreBackup', [id, objectOr(value, 'targetRoot'), options]); },
      waitTask(id, value = 20000, options = {}) { return this.call('waitTask', [id, objectOr(value, 'timeoutMs', 20000), options]); },
      getChunkDiff(id, value = {}, options = {}) { return this.call('getChunkDiff', [id, objectOr(value, 'against'), options]); },
      putFeatureFlag(key, value, options = {}) { return this.call('putFeatureFlag', [key, objectOr(value, 'enabled', false), options]); },
      retryRecallChannel(id, value = {}, options = {}) { return this.call('retryRecallChannel', [id, objectOr(value, 'channelId'), options]); },
      exportRecall(id, value = 'json', options = {}) { return this.call('exportRecall', [id, objectOr(value, 'format', 'json'), options]); },
      exportFusion(id, value = 'json', options = {}) { return this.call('exportFusion', [id, objectOr(value, 'format', 'json'), options]); },
      directoryPreview(id, value, options = {}) { return this.call('directoryPreview', [id, objectOr(value, 'directory'), options]); },
      directoryImport(id, value, options = {}) { return this.call('directoryImport', [id, objectOr(value, 'directory'), options]); },
      async createAssistantClient(id, payload = {}, options = {}) {
        const { origins, ...rest } = payload;
        const result = await this.call('createAssistantClient', [id, { ...rest, allowedOrigins: payload.allowedOrigins ?? origins ?? [] }, options]);
        if (!result || options.envelope) return result;
        const { clientSecret, ...record } = result;
        return { ...result, client: record };
      },
      async getAssistantClients(id, params = {}, options = {}) {
        const result = await this.call('getAssistantClients', [id, params, options]);
        return Array.isArray(result) ? result.map(record => ({ ...record, origins: record.allowedOrigins ?? record.origins ?? [] })) : result;
      },
      uploadDocument(id, file, sourceId, options = {}) {
        const form = new FormData();
        if (sourceId) form.append('sourceId', sourceId);
        form.append('file', file);
        return this.call('uploadDocument', [id, form, options]);
      },
      uploadArchive(id, file, options = {}) {
        const form = new FormData(); form.append('file', file);
        return this.call('uploadArchive', [id, form, options]);
      },
      uploadFile(file, fields = {}, options = {}) {
        const form = new FormData();
        for (const [key, value] of Object.entries(fields)) if (value !== undefined && value !== null) form.append(key, typeof value === 'object' ? JSON.stringify(value) : value);
        form.append('file', file);
        return this.call('uploadFile', [form, options]);
      },
      getArtifactMarkdown(id, options = {}) { return this.call('getArtifact', [id, 'markdown', {}, options], { responseType: 'text' }); },
      getArtifactDocument(id, options = {}) { return this.getArtifact(id, 'document', {}, options); },
      getArtifactManifest(id, options = {}) { return this.getArtifact(id, 'manifest', {}, options); },
      getArtifactQuality(id, options = {}) { return this.getArtifact(id, 'quality', {}, options); },
      formatLocator(locator = {}) {
        locator = locator.locator_json || locator.locator || locator.source_locator || locator;
        if (typeof locator === 'string') { try { locator = JSON.parse(locator); } catch { return locator; } }
        if (!locator || typeof locator !== 'object') return '';
        if (locator.page) return `P.${locator.page}`;
        if (locator.slide) return `S.${locator.slide}`;
        if (locator.sheet) return `Sheet ${locator.sheet}`;
        if (locator.line || locator.start) return `L.${locator.line || locator.start}`;
        return '';
      }
    });
    for (const name of ['getDocuments', 'getChunks', 'getTasks', 'getConversations', 'getTraces', 'getFusionCandidates', 'getAudit', 'getDatasetFiles', 'getRegisteredSources', 'getParsingTasks']) {
      client[name + 'Page'] = function (...args) { return this.call(name, args, { envelope: true }); };
    }
    client.saveMessageWiki = function (...args) { return this.wikiFromMessage(...args); };
    return client;
  }

  return { createClient, operations, aliases, ApiError, queryString };
});
