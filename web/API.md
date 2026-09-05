# 前端 API 调用层

入口为 `web/api.js`，使用原生 Fetch，不需要依赖或构建。`index.html` 在 `app.js` 前加载此模块，现有工作台通过 `window.ordoApi` 使用同一个客户端。页面模板、渲染逻辑、CSS 和主题文件没有因本次接口补充而调整。

## 覆盖范围与后端边界

- `planning/前端API清单/` 的逐页契约与总览经去重后有 **203 个方法/路径组合**，全部已有客户端封装或等价路径映射。
- 客户端目录提供 **224 个接口操作**，全部已由 Python/FastAPI 后端注册，另保留 Wiki 兼容地址。
- 原先缺失的 **73 个操作**已接入 Python 处理器。未配置 OCR、额外向量模型等外部能力时返回明确的能力不可用错误；本地算法与重放方式详见 [后端能力说明](../serverpy/README.md)。调用失败返回 `null` 并写入 `lastError`，或按选项抛出 `ApiError`。
- 清单中的 `POST /api/v1/messages/:id/wiki` 使用 `saveMessageWiki()` / `wikiFromMessage()` 映射至现有 `POST /api/v1/wiki/from-message/:messageId`。
- “后端已注册”只表示存在路由，不代表算法、数据和业务功能已经完整验收；客户端不会把注册状态当作功能健康状态。

## 方法与参数

普通方法按以下顺序传参：**路径 ID → 查询参数或请求体 → 请求选项**。多个路径 ID 按 URL 出现顺序传入。ID 会进行 URL 编码，缺失 ID 会抛出 `API_PARAMETER_REQUIRED`，不会发送请求。

```js
await ordoApi.getDocument(documentId);
await ordoApi.updateDataset(datasetId, { name, description });
await ordoApi.getRecallChunk(traceId, chunkId);
await ordoApi.getTracePromptStage(traceId, { version: 'latest' });
await ordoApi.replayTrace(traceId, {
  fromStage: '问题解析', overrides: { topK: 4 }
}, { idempotencyKey: crypto.randomUUID() });
```

普通方法返回响应中的 `data`。原始 JSON（如 OpenAPI、解析产物）保持原样。支持 `responseType: 'json' | 'text' | 'blob' | 'stream'`。`204` 返回 `null`，此时 `lastError` 仍为 `null`。

```js
const documents = await ordoApi.getDocuments(datasetId, { limit: 20, offset: 0 });
const page = await ordoApi.getDocumentsPage(datasetId, { limit: 20, offset: 0 });
// page.data / page.meta.total / page.meta.hasMore
const raw = await ordoApi.getTraces({ limit: 10 }, { envelope: true });
const markdown = await ordoApi.getArtifactMarkdown(artifactId);
const artifact = await ordoApi.getArtifactDocument(artifactId);
const file = await ordoApi.getArtifact(artifactId, 'markdown', {}, { responseType: 'blob' });
```

分页辅助方法为 `getDocumentsPage`、`getChunksPage`、`getTasksPage`、`getConversationsPage`、`getTracesPage`、`getFusionCandidatesPage`、`getAuditPage`、`getDatasetFilesPage`、`getRegisteredSourcesPage`、`getParsingTasksPage`。也可使用通用 `{ envelope: true }` 保留响应元数据。

## 现有调用形式兼容

保留 `createConversation(title, kbId, datasetId)`、`sendMessage(id, question)`、`createBackup(label)`、`waitTask(id, timeoutMs)` 等形式，同时接受完整对象，避免丢失模型、版本、TopK、恢复目录、规则或幂等参数。

```js
await ordoApi.createConversation({
  title, knowledgeBaseId, datasetId, releaseId, modelConnectionId, strictEvidence: true
});
await ordoApi.sendMessage(conversationId, { question, topK: 8 });
await ordoApi.directoryPreview(datasetId, { directory, rules });
await ordoApi.directoryImport(datasetId, { directory, rules, idempotencyKey });
await ordoApi.restoreBackup(backupId, { targetRoot, idempotencyKey });
await ordoApi.uploadDocument(datasetId, file, sourceId);
await ordoApi.uploadArchive(datasetId, file);
await ordoApi.uploadFile(file, { datasetId }); // 可省略 datasetId，先登记后归入数据集
```

上传使用 FormData，`sourceId` 放在文件字段前，由浏览器设置 multipart boundary。助手客户端创建接受旧 `origins` 并转换为 `allowedOrigins`；返回保留后端字段，同时提供现有弹窗使用的 `client` 和列表使用的 `origins`。

## 会话、错误和取消

管理接口使用同源 Cookie 和写请求 CSRF。明确收到 `SESSION_REQUIRED` 或 `CSRF_INVALID` 时，共享一次会话初始化并最多重试一次；业务失败和网络失败不会自动重试写操作。可通过初始化请求的 `headers` 传递远程部署需要的管理员令牌，客户端不将该令牌写入本地存储。

```js
const result = await ordoApi.getDataset(datasetId);
if (result === null && ordoApi.lastError) {
  // { status, code, message, requestId, details, url }
}
await ordoApi.getDataset(datasetId, {}, { throwOnError: true });
const controller = new AbortController();
await ordoApi.getDocuments(datasetId, {}, { signal: controller.signal });
```

需要独立客户端时使用 `OrdoApi.createClient({ fetch, baseUrl, throwOnError })`；Node 测试可 `require('./api')`。`request()` 返回完整响应，`call(name, args, options)` 可按接口目录动态调用。

## 流式问答与网站访客接口

```js
await ordoApi.sendMessageStream(conversationId, { question }, (event, data) => {
  // stage / token / done；本层不操作 DOM。
}, { signal: controller.signal });
await ordoApi.regenerateAnswerStream(traceId, { temperature: 0.2 }, onEvent);

// 签名由客户服务端产生；传入签名时使用完全相同的原始 JSON 字符串。
const token = await ordoApi.issueWidgetToken(signedBody, { headers: signedHeaders });
const visitor = await ordoApi.createWidgetSession({ origin }, { token: token.token });
await ordoApi.sendWidgetMessage(visitor.id, { question }, { token: token.token });
```

公开 Widget 接口不携带管理 Cookie 或 CSRF，使用显式传入的访客 Bearer Token。浏览器自身设置 Origin；客户端不在浏览器内生成或持久保存客户服务端的 HMAC 密钥。SSE 解析支持跨分片 UTF-8、CRLF、多行 data、错误事件和 AbortSignal；提前断流会报告 `STREAM_INCOMPLETE`。

## 验证

`npm test` 包含 Python API 的上传到恢复闭环、数据迁移、安全与流式传输测试，以及现有前端测试和真实 FastAPI HTTP 服务下的浏览器客户端对接测试。`npm run check` 检查 Python 导入和语法以及前端 JavaScript 语法。

以下矩阵与 `serverpy/ordo/api_contract.json` 和 FastAPI 实际 OpenAPI 比对。注册状态不等同于所有外部 Provider 已配置。

## 接口矩阵

| 客户端方法 | HTTP | 路径 | 后端路由 |
| --- | --- | --- | --- |
| `bootstrapSession` | GET | `/api/v1/session/bootstrap` | 已注册 |
| `getDashboard` | GET | `/api/v1/dashboard` | 已注册 |
| `getHealth` | GET | `/api/v1/health` | 已注册 |
| `getVersion` | GET | `/api/v1/version` | 已注册 |
| `getDiagnostics` | GET | `/api/v1/diagnostics` | 已注册 |
| `getOpenApi` | GET | `/api/v1/openapi.json` | 已注册 |
| `search` | GET | `/api/v1/search` | 已注册 |
| `getKnowledgeBases` | GET | `/api/v1/knowledge-bases` | 已注册 |
| `createKnowledgeBase` | POST | `/api/v1/knowledge-bases` | 已注册 |
| `getKnowledgeBase` | GET | `/api/v1/knowledge-bases/:kbId` | 已注册 |
| `updateKnowledgeBase` | PATCH | `/api/v1/knowledge-bases/:kbId` | 已注册 |
| `deleteKnowledgeBase` | DELETE | `/api/v1/knowledge-bases/:kbId` | 已注册 |
| `getKnowledgeBaseImpact` | GET | `/api/v1/knowledge-bases/:kbId/impact` | 已注册 |
| `getIndexProfiles` | GET | `/api/v1/knowledge-bases/:kbId/index-profiles` | 已注册 |
| `createIndexProfile` | POST | `/api/v1/knowledge-bases/:kbId/index-profiles` | 已注册 |
| `getIndexProfile` | GET | `/api/v1/index-profiles/:profileId` | 已注册 |
| `updateIndexProfile` | PATCH | `/api/v1/index-profiles/:profileId` | 已注册 |
| `deleteIndexProfile` | DELETE | `/api/v1/index-profiles/:profileId` | 已注册 |
| `setDefaultIndexProfile` | POST | `/api/v1/index-profiles/:profileId/default` | 已注册 |
| `getDatasets` | GET | `/api/v1/knowledge-bases/:kbId/datasets` | 已注册 |
| `createDataset` | POST | `/api/v1/knowledge-bases/:kbId/datasets` | 已注册 |
| `getDataset` | GET | `/api/v1/datasets/:datasetId` | 已注册 |
| `updateDataset` | PATCH | `/api/v1/datasets/:datasetId` | 已注册 |
| `deleteDataset` | DELETE | `/api/v1/datasets/:datasetId` | 已注册 |
| `getSources` | GET | `/api/v1/datasets/:datasetId/sources` | 已注册 |
| `createSource` | POST | `/api/v1/datasets/:datasetId/sources` | 已注册 |
| `uploadDocument` | POST | `/api/v1/datasets/:datasetId/files` | 已注册 |
| `uploadArchive` | POST | `/api/v1/datasets/:datasetId/archives` | 已注册 |
| `directoryPreview` | POST | `/api/v1/datasets/:datasetId/directory/preview` | 已注册 |
| `directoryImport` | POST | `/api/v1/datasets/:datasetId/directory/import` | 已注册 |
| `getDocuments` | GET | `/api/v1/datasets/:datasetId/documents` | 已注册 |
| `getDocument` | GET | `/api/v1/documents/:documentId` | 已注册 |
| `deleteDocument` | DELETE | `/api/v1/documents/:documentId` | 已注册 |
| `getArtifact` | GET | `/api/v1/artifacts/:artifactId/:kind` | 已注册 |
| `getChunks` | GET | `/api/v1/datasets/:datasetId/chunks` | 已注册 |
| `getIndexingStats` | GET | `/api/v1/datasets/:datasetId/indexing/stats` | 已注册 |
| `getIndexingPipeline` | GET | `/api/v1/datasets/:datasetId/indexing/pipeline` | 已注册 |
| `getChapters` | GET | `/api/v1/datasets/:datasetId/chapters` | 已注册 |
| `getChunk` | GET | `/api/v1/chunks/:chunkId` | 已注册 |
| `getChunkLineage` | GET | `/api/v1/chunks/:chunkId/lineage` | 已注册 |
| `editChunk` | POST | `/api/v1/chunks/:chunkId/revisions` | 已注册 |
| `vectorizeChunk` | POST | `/api/v1/chunks/:chunkId/vectorize` | 已注册 |
| `toggleChunkDisabled` | POST | `/api/v1/chunks/:chunkId/toggle-disable` | 已注册 |
| `restoreChunk` | POST | `/api/v1/chunks/:chunkId/restore` | 已注册 |
| `getChunkDiff` | GET | `/api/v1/chunks/:chunkId/diff` | 已注册 |
| `splitChunk` | POST | `/api/v1/chunks/:chunkId/split` | 已注册 |
| `mergeChunks` | POST | `/api/v1/chunks/merge` | 已注册 |
| `vectorizePending` | POST | `/api/v1/datasets/:datasetId/indexing/vectorize-pending` | 已注册 |
| `rebuildHnswIndex` | POST | `/api/v1/datasets/:datasetId/indexing/rebuild-hnsw` | 已注册 |
| `optimizeVectorIndex` | POST | `/api/v1/datasets/:datasetId/indexing/optimize-index` | 已注册 |
| `rebuildBm25Index` | POST | `/api/v1/datasets/:datasetId/indexing/rebuild-bm25` | 已注册 |
| `setHybridWeights` | PUT | `/api/v1/datasets/:datasetId/indexing/hybrid-weights` | 已注册 |
| `getReleases` | GET | `/api/v1/datasets/:datasetId/releases` | 已注册 |
| `buildRelease` | POST | `/api/v1/datasets/:datasetId/releases` | 已注册 |
| `getRelease` | GET | `/api/v1/releases/:releaseId` | 已注册 |
| `activateRelease` | POST | `/api/v1/releases/:releaseId/activate` | 已注册 |
| `rollbackRelease` | POST | `/api/v1/releases/:releaseId/rollback` | 已注册 |
| `getReleaseImpact` | GET | `/api/v1/releases/:releaseId/impact` | 已注册 |
| `searchRelease` | POST | `/api/v1/releases/:releaseId/search` | 已注册 |
| `getTasks` | GET | `/api/v1/tasks` | 已注册 |
| `getTask` | GET | `/api/v1/tasks/:taskId` | 已注册 |
| `cancelTask` | POST | `/api/v1/tasks/:taskId/cancel` | 已注册 |
| `pauseTask` | POST | `/api/v1/tasks/:taskId/pause` | 已注册 |
| `resumeTask` | POST | `/api/v1/tasks/:taskId/resume` | 已注册 |
| `retryTask` | POST | `/api/v1/tasks/:taskId/retry` | 已注册 |
| `waitTask` | GET | `/api/v1/tasks/:taskId/wait` | 已注册 |
| `getConnectors` | GET | `/api/v1/connectors` | 已注册 |
| `createConnector` | POST | `/api/v1/connectors` | 已注册 |
| `getConnector` | GET | `/api/v1/connectors/:connectorId` | 已注册 |
| `testConnector` | POST | `/api/v1/connectors/:connectorId/test` | 已注册 |
| `getConnectorSchema` | GET | `/api/v1/connectors/:connectorId/schema` | 已注册 |
| `getQueryTemplates` | GET | `/api/v1/connectors/:connectorId/templates` | 已注册 |
| `createQueryTemplate` | POST | `/api/v1/connectors/:connectorId/templates` | 已注册 |
| `executeQueryTemplate` | POST | `/api/v1/query-templates/:templateId/execute` | 已注册 |
| `snapshotQueryTemplate` | POST | `/api/v1/query-templates/:templateId/snapshot` | 已注册 |
| `getOntologies` | GET | `/api/v1/knowledge-bases/:kbId/ontologies` | 已注册 |
| `createOntology` | POST | `/api/v1/knowledge-bases/:kbId/ontologies` | 已注册 |
| `publishOntology` | POST | `/api/v1/ontologies/:ontologyId/publish` | 已注册 |
| `getGraph` | GET | `/api/v1/datasets/:datasetId/graph` | 已注册 |
| `getGraphEntities` | GET | `/api/v1/datasets/:datasetId/graph/entities` | 已注册 |
| `createGraphEntity` | POST | `/api/v1/datasets/:datasetId/graph/entities` | 已注册 |
| `createGraphRelation` | POST | `/api/v1/datasets/:datasetId/graph/relations` | 已注册 |
| `getConversations` | GET | `/api/v1/conversations` | 已注册 |
| `createConversation` | POST | `/api/v1/conversations` | 已注册 |
| `getConversation` | GET | `/api/v1/conversations/:conversationId` | 已注册 |
| `deleteConversation` | DELETE | `/api/v1/conversations/:conversationId` | 已注册 |
| `sendMessage` | POST | `/api/v1/conversations/:conversationId/messages` | 已注册 |
| `sendFeedback` | POST | `/api/v1/messages/:messageId/feedback` | 已注册 |
| `openCitation` | GET | `/api/v1/citations/:citationId` | 已注册 |
| `getTraces` | GET | `/api/v1/traces` | 已注册 |
| `getTrace` | GET | `/api/v1/traces/:traceId` | 已注册 |
| `getTracePipeline` | GET | `/api/v1/traces/:traceId/pipeline` | 已注册 |
| `replayTrace` | POST | `/api/v1/traces/:traceId/replay` | 已注册 |
| `compareTraces` | GET | `/api/v1/traces/:traceId/compare/:otherId` | 已注册 |
| `getTraceRouteStage` | GET | `/api/v1/traces/:traceId/stages/route` | 已注册 |
| `getTraceRecallStage` | GET | `/api/v1/traces/:traceId/stages/recall` | 已注册 |
| `filterRecall` | POST | `/api/v1/traces/:traceId/stages/recall/filter` | 已注册 |
| `retryRecallChannel` | POST | `/api/v1/traces/:traceId/stages/recall/retry-channel` | 已注册 |
| `getRecallChunk` | GET | `/api/v1/traces/:traceId/stages/recall/chunks/:chunkId` | 已注册 |
| `exportRecall` | GET | `/api/v1/traces/:traceId/stages/recall/export` | 已注册 |
| `getTraceFusionStage` | GET | `/api/v1/traces/:traceId/stages/fusion` | 已注册 |
| `updateFusionWeights` | PUT | `/api/v1/traces/:traceId/stages/fusion/weights` | 已注册 |
| `resetFusionWeights` | POST | `/api/v1/traces/:traceId/stages/fusion/reset-weights` | 已注册 |
| `getFusionCalculation` | GET | `/api/v1/traces/:traceId/stages/fusion/calculation/:candidateId` | 已注册 |
| `getFusionCandidates` | GET | `/api/v1/traces/:traceId/stages/fusion/candidates` | 已注册 |
| `getFusionChunk` | GET | `/api/v1/traces/:traceId/stages/fusion/chunks/:candidateId` | 已注册 |
| `getFusionLogs` | GET | `/api/v1/traces/:traceId/stages/fusion/logs` | 已注册 |
| `exportFusion` | GET | `/api/v1/traces/:traceId/stages/fusion/export` | 已注册 |
| `rerunFusionStage` | POST | `/api/v1/traces/:traceId/stages/fusion/rerun` | 已注册 |
| `getTraceRerankStage` | GET | `/api/v1/traces/:traceId/stages/rerank` | 已注册 |
| `getRerankChunk` | GET | `/api/v1/traces/:traceId/stages/rerank/chunks/:chunkId` | 已注册 |
| `updateRerankConfig` | PUT | `/api/v1/traces/:traceId/stages/rerank/config` | 已注册 |
| `compareRerank` | POST | `/api/v1/traces/:traceId/stages/rerank/compare` | 已注册 |
| `getRerankLogs` | GET | `/api/v1/traces/:traceId/stages/rerank/logs` | 已注册 |
| `getModels` | GET | `/api/v1/models` | 已注册 |
| `createModel` | POST | `/api/v1/models` | 已注册 |
| `getModel` | GET | `/api/v1/models/:modelId` | 已注册 |
| `patchModel` | PATCH | `/api/v1/models/:modelId` | 已注册 |
| `deleteModel` | DELETE | `/api/v1/models/:modelId` | 已注册 |
| `testModel` | POST | `/api/v1/models/:modelId/test` | 已注册 |
| `getSettings` | GET | `/api/v1/settings` | 已注册 |
| `updateSetting` | PUT | `/api/v1/settings/:key` | 已注册 |
| `getFeatureFlags` | GET | `/api/v1/feature-flags` | 已注册 |
| `putFeatureFlag` | PUT | `/api/v1/feature-flags/:key` | 已注册 |
| `getWikiPages` | GET | `/api/v1/wiki` | 已注册 |
| `createWikiPage` | POST | `/api/v1/wiki` | 已注册 |
| `getWikiPage` | GET | `/api/v1/wiki/:pageId` | 已注册 |
| `reviseWikiPage` | POST | `/api/v1/wiki/:pageId` | 已注册 |
| `wikiFromMessage` | POST | `/api/v1/wiki/from-message/:messageId` | 已注册 |
| `getAssistants` | GET | `/api/v1/assistants` | 已注册 |
| `createAssistant` | POST | `/api/v1/assistants` | 已注册 |
| `getAssistant` | GET | `/api/v1/assistants/:assistantId` | 已注册 |
| `updateAssistant` | PATCH | `/api/v1/assistants/:assistantId` | 已注册 |
| `publishAssistant` | POST | `/api/v1/assistants/:assistantId/publish` | 已注册 |
| `pauseAssistant` | POST | `/api/v1/assistants/:assistantId/pause` | 已注册 |
| `getAssistantClients` | GET | `/api/v1/assistants/:assistantId/clients` | 已注册 |
| `createAssistantClient` | POST | `/api/v1/assistants/:assistantId/clients` | 已注册 |
| `rotateWidgetClient` | POST | `/api/v1/widget-clients/:clientId/rotate` | 已注册 |
| `revokeWidgetClient` | DELETE | `/api/v1/widget-clients/:clientId` | 已注册 |
| `issueWidgetToken` | POST | `/api/v1/public/widget/token` | 已注册 |
| `createWidgetSession` | POST | `/api/v1/public/widget/sessions` | 已注册 |
| `sendWidgetMessage` | POST | `/api/v1/public/widget/sessions/:sessionId/messages` | 已注册 |
| `requestWidgetHandoff` | POST | `/api/v1/public/widget/sessions/:sessionId/handoff` | 已注册 |
| `deleteWidgetSession` | DELETE | `/api/v1/public/widget/sessions/:sessionId` | 已注册 |
| `getHandoffs` | GET | `/api/v1/handoffs` | 已注册 |
| `updateHandoff` | PATCH | `/api/v1/handoffs/:handoffId` | 已注册 |
| `getBackups` | GET | `/api/v1/backups` | 已注册 |
| `createBackup` | POST | `/api/v1/backups` | 已注册 |
| `restoreBackup` | POST | `/api/v1/backups/:backupId/restore` | 已注册 |
| `getAudit` | GET | `/api/v1/audit` | 已注册 |
| `verifyAudit` | GET | `/api/v1/audit/verify` | 已注册 |
| `getAllDatasets` | GET | `/api/v1/datasets` | 已注册 |
| `createUnscopedDataset` | POST | `/api/v1/datasets` | 已注册 |
| `getRegisteredSources` | GET | `/api/v1/sources` | 已注册 |
| `getSourceTree` | GET | `/api/v1/sources/tree` | 已注册 |
| `uploadFile` | POST | `/api/v1/files` | 已注册 |
| `assignSourceDataset` | PATCH | `/api/v1/sources/:sourceId/dataset` | 已注册 |
| `getRecentSources` | GET | `/api/v1/sources/recent` | 已注册 |
| `getSourceActivities` | GET | `/api/v1/sources/recent-activities` | 已注册 |
| `getSourceAttentionItems` | GET | `/api/v1/sources/attention-items` | 已注册 |
| `testConnectorConfig` | POST | `/api/v1/connectors/test` | 已注册 |
| `deleteSource` | DELETE | `/api/v1/sources/:sourceId` | 已注册 |
| `getDatasetTree` | GET | `/api/v1/datasets/:datasetId/tree` | 已注册 |
| `getDatasetFiles` | GET | `/api/v1/datasets/:datasetId/files` | 已注册 |
| `inspectFile` | GET | `/api/v1/files/:fileId/inspect` | 已注册 |
| `createDatasetFolder` | POST | `/api/v1/datasets/:datasetId/folders` | 已注册 |
| `deleteDatasetFile` | DELETE | `/api/v1/datasets/:datasetId/files/:fileId` | 已注册 |
| `batchDeleteDatasetFiles` | POST | `/api/v1/datasets/:datasetId/files/batch-delete` | 已注册 |
| `moveDatasetFile` | PATCH | `/api/v1/datasets/:datasetId/files/:fileId/move` | 已注册 |
| `batchMoveDatasetFiles` | POST | `/api/v1/datasets/:datasetId/files/batch-move` | 已注册 |
| `getParsingProfiles` | GET | `/api/v1/parsing/profiles` | 已注册 |
| `startParsing` | POST | `/api/v1/parsing/start` | 已注册 |
| `pauseParsing` | POST | `/api/v1/parsing/pause` | 已注册 |
| `resumeParsing` | POST | `/api/v1/parsing/resume` | 已注册 |
| `retryFailedParsing` | POST | `/api/v1/parsing/retry-failed` | 已注册 |
| `getParsingPipelineStats` | GET | `/api/v1/parsing/pipeline-stats` | 已注册 |
| `getParsingTasks` | GET | `/api/v1/parsing/tasks` | 已注册 |
| `clearPendingParsingTasks` | POST | `/api/v1/parsing/tasks/clear-pending` | 已注册 |
| `getParsingSettings` | GET | `/api/v1/parsing/settings` | 已注册 |
| `updateParsingSettings` | PATCH | `/api/v1/parsing/settings` | 已注册 |
| `exportParsingLogs` | GET | `/api/v1/parsing/logs/export` | 已注册 |
| `getDocumentPreviewPages` | GET | `/api/v1/documents/:documentId/preview/pages` | 已注册 |
| `getDocumentPage` | GET | `/api/v1/documents/:documentId/pages/:pageNum` | 已注册 |
| `getDocumentPageInspect` | GET | `/api/v1/documents/:documentId/pages/:pageNum/inspect` | 已注册 |
| `getDocumentPageDiff` | GET | `/api/v1/documents/:documentId/pages/:pageNum/diff` | 已注册 |
| `getSystemResources` | GET | `/api/v1/system/resources` | 已注册 |
| `getTraceParseStage` | GET | `/api/v1/traces/:traceId/stages/parse` | 已注册 |
| `quickParse` | POST | `/api/v1/traces/quick-parse` | 已注册 |
| `getConversationContext` | GET | `/api/v1/conversations/:conversationId/context` | 已注册 |
| `reparseTrace` | POST | `/api/v1/traces/:traceId/stages/parse/reparse` | 已注册 |
| `updateTraceParse` | PUT | `/api/v1/traces/:traceId/stages/parse` | 已注册 |
| `getTraceParseLogs` | GET | `/api/v1/traces/:traceId/stages/parse/logs` | 已注册 |
| `classifyTraceIntent` | POST | `/api/v1/traces/:traceId/stages/parse/classify-intent` | 已注册 |
| `extractTraceEntities` | POST | `/api/v1/traces/:traceId/stages/parse/extract-entities` | 已注册 |
| `updateTraceKeywords` | PATCH | `/api/v1/traces/:traceId/stages/parse/keywords` | 已注册 |
| `updateNormalizedQuery` | PATCH | `/api/v1/traces/:traceId/stages/parse/normalized-query` | 已注册 |
| `rewriteTraceQuery` | POST | `/api/v1/traces/:traceId/stages/parse/rewrite-query` | 已注册 |
| `updateRewrittenQuery` | PATCH | `/api/v1/traces/:traceId/stages/parse/rewritten-query` | 已注册 |
| `updateTraceFilters` | PATCH | `/api/v1/traces/:traceId/stages/parse/filters` | 已注册 |
| `updateTraceRawJson` | PUT | `/api/v1/traces/:traceId/stages/parse/raw-json` | 已注册 |
| `getTraceEmbedStage` | GET | `/api/v1/traces/:traceId/stages/embed` | 已注册 |
| `recomputeEmbedding` | POST | `/api/v1/traces/:traceId/stages/embed/recompute` | 已注册 |
| `getEmbeddingScatter` | GET | `/api/v1/traces/:traceId/stages/embed/scatter` | 已注册 |
| `compareEmbeddingModels` | POST | `/api/v1/traces/:traceId/stages/embed/compare-models` | 已注册 |
| `getEmbeddingVector` | GET | `/api/v1/traces/:traceId/stages/embed/vector` | 已注册 |
| `getEmbeddingLogs` | GET | `/api/v1/traces/:traceId/stages/embed/logs` | 已注册 |
| `updateTraceRoute` | PUT | `/api/v1/traces/:traceId/stages/route` | 已注册 |
| `simulateTraceRoute` | POST | `/api/v1/traces/:traceId/stages/route/simulate` | 已注册 |
| `getTraceRouteIndexes` | GET | `/api/v1/traces/:traceId/stages/route/indexes` | 已注册 |
| `getTraceRouteIntent` | GET | `/api/v1/traces/:traceId/stages/route/intent` | 已注册 |
| `getTraceRouteLogs` | GET | `/api/v1/traces/:traceId/stages/route/logs` | 已注册 |
| `getRecallChannel` | GET | `/api/v1/traces/:traceId/stages/recall/channels/:channelId` | 已注册 |
| `getTracePromptStage` | GET | `/api/v1/traces/:traceId/stages/prompt` | 已注册 |
| `updateTracePrompt` | PUT | `/api/v1/traces/:traceId/stages/prompt` | 已注册 |
| `getPromptVersions` | GET | `/api/v1/traces/:traceId/stages/prompt/versions` | 已注册 |
| `maskPrompt` | POST | `/api/v1/traces/:traceId/stages/prompt/mask` | 已注册 |
| `scanPromptSensitiveData` | GET | `/api/v1/traces/:traceId/stages/prompt/sensitive-scan` | 已注册 |
| `getTraceGenerationStage` | GET | `/api/v1/traces/:traceId/stages/generation` | 已注册 |
| `regenerateAnswer` | POST | `/api/v1/traces/:traceId/stages/generation/regenerate` | 已注册 |
| `sendTraceFeedback` | POST | `/api/v1/traces/:traceId/feedback` | 已注册 |
| `saveTraceQa` | POST | `/api/v1/traces/:traceId/save-qa` | 已注册 |
| `getTraceWaterfall` | GET | `/api/v1/traces/:traceId/waterfall` | 已注册 |
| `replayAll` | POST | `/api/v1/traces/:traceId/replay-all` | 已注册 |
| `deleteAssistant` | DELETE | `/api/v1/assistants/:assistantId` | 已注册 |
