'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { createApp } = require('../src/app');
const { canonicalRequest, sign } = require('../src/widget');

async function fixture(t) {
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ordo-e2e-'));
  const app = await createApp({ dataRoot, logger: false });
  t.after(async () => { await app.close(); });
  const bootstrap = await app.inject({ method: 'GET', url: '/api/v1/session/bootstrap' });
  assert.equal(bootstrap.statusCode, 200);
  const cookie = bootstrap.headers['set-cookie'].split(';')[0];
  const csrf = bootstrap.json().data.csrfToken;
  const request = async (method, url, payload, headers = {}) => {
    const response = await app.inject({ method, url, payload, headers: { cookie, ...(method === 'GET' ? {} : { 'x-ordo-csrf': csrf }), ...headers } });
    if (response.statusCode >= 400) throw new Error(`${method} ${url} -> ${response.statusCode}: ${response.body}`);
    return response;
  };
  return { app, dataRoot, request };
}

async function waitTask(request, taskId) {
  const response = await request('GET', `/api/v1/tasks/${taskId}/wait?timeoutMs=20000`);
  const task = response.json().data;
  assert.ok(['succeeded','partial'].includes(task.status), JSON.stringify(task));
  return task;
}

function multipart(filename, content, fields = {}) {
  const boundary = `----ordo${Date.now()}`;
  const parts = [];
  for (const [name, value] of Object.entries(fields)) parts.push(`--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`);
  parts.push(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\nContent-Type: text/markdown\r\n\r\n${content}\r\n--${boundary}--\r\n`);
  return { payload: Buffer.from(parts.join('')), headers: { 'content-type': `multipart/form-data; boundary=${boundary}` } };
}

let widgetNonce = 0;
function requestWidgetToken(app, clientId, clientSecret, origin = 'https://example.test') {
  const tokenBody = JSON.stringify({ origin });
  const timestamp = Date.now();
  const nonce = `nonce_${timestamp}_${++widgetNonce}_abcdef`;
  const signature = sign(clientSecret, canonicalRequest('POST', '/api/v1/public/widget/token', timestamp, nonce, origin, tokenBody));
  return app.inject({ method: 'POST', url: '/api/v1/public/widget/token', payload: JSON.parse(tokenBody), headers: {
    origin, 'x-ordo-client': clientId, 'x-ordo-timestamp': String(timestamp), 'x-ordo-nonce': nonce, 'x-ordo-signature': signature
  } });
}

test('R1 product loop persists source, release, query trace, citation and backup', async t => {
  const { request, dataRoot } = await fixture(t);
  const model = (await request('POST', '/api/v1/models', { name: '本地严格证据', provider: 'local-extractive', purpose: 'generation', modelId: 'ordo-local-extractive-v1' })).json().data;
  assert.equal(model.status, 'available');

  const kb = (await request('POST', '/api/v1/knowledge-bases', { name: 'Ordo 模拟产品文档', description: '端到端验收数据' })).json().data;
  const dataset = kb.datasets[0];
  const source = (await request('POST', `/api/v1/datasets/${dataset.id}/sources`, { type: 'upload', name: '模拟数据文档' })).json().data;
  const upload = multipart('ordo-product-guide.md', '# Ordo 产品指南\n\nOrdo 是本地优先的知识产品。\n\n## 安装\n\n运行 npm start 启动服务，然后打开本地地址。\n\n## 安全\n\n回答必须使用可验证引用，证据不足时拒答。', { sourceId: source.id });
  const registered = (await request('POST', `/api/v1/datasets/${dataset.id}/files`, upload.payload, upload.headers)).json().data;
  const parseTask = await waitTask(request, registered.task.id);
  assert.equal(parseTask.result.qualityStatus, 'publishable');
  assert.ok(parseTask.result.blockCount >= 3);

  const releaseTask = (await request('POST', `/api/v1/datasets/${dataset.id}/releases`, { activate: true })).json().data;
  const built = await waitTask(request, releaseTask.id);
  assert.equal(built.result.status, 'active');

  const conversation = (await request('POST', '/api/v1/conversations', { knowledgeBaseId: kb.id, datasetId: dataset.id, modelConnectionId: model.id })).json().data;
  const answer = (await request('POST', `/api/v1/conversations/${conversation.id}/messages`, { question: '如何启动 Ordo？' })).json().data;
  assert.equal(answer.trace.stages.length, 8);
  assert.ok(answer.trace.routingBasis);
  const routeStageRes = (await request('GET', `/api/v1/traces/${answer.trace.id}/stages/route`)).json().data;
  assert.equal(routeStageRes.traceId, answer.trace.id);
  assert.equal(routeStageRes.routerDag.channels.length, 4);
  assert.ok(routeStageRes.routingBasis.reasons.length >= 4);
  assert.ok(routeStageRes.routingBasis.compositeConfidence > 0);
  assert.equal(routeStageRes.routeConfig.permissionFilter.enabled, true);

  const recallStageRes = (await request('GET', `/api/v1/traces/${answer.trace.id}/stages/recall`)).json().data;
  assert.equal(recallStageRes.traceId, answer.trace.id);
  assert.equal(recallStageRes.channels.length, 4);
  assert.equal(recallStageRes.channels[0].channelId, 'vector');
  assert.ok(recallStageRes.channels[0].headerBadge.includes('条'));
  assert.ok(recallStageRes.channels[0].headerBadge.includes('ms'));
  assert.equal(recallStageRes.channels[3].status, 'skipped');
  assert.equal(recallStageRes.channels[3].headerBadge, '已跳过');
  assert.ok(recallStageRes.summaryMetrics.totalCandidatesBeforeDedup > 0);

  const retryRes = (await request('POST', `/api/v1/traces/${answer.trace.id}/stages/recall/retry-channel`, {})).json().data;
  assert.equal(retryRes.retried, true);
  assert.equal(retryRes.failedChannels, 0);

  const chunkDetail = (await request('GET', `/api/v1/traces/${answer.trace.id}/stages/recall/chunks/c4f9a1b2`)).json().data;
  assert.ok(chunkDetail.documentTitle);
  assert.ok(chunkDetail.content);

  assert.equal(answer.assistantMessage.evidence_status, 'sufficient');
  assert.ok(answer.assistantMessage.citations.length > 0);
  const citation = (await request('GET', `/api/v1/citations/${answer.assistantMessage.citations[0].id}`)).json().data;
  assert.equal(citation.releaseId, built.result.releaseId);
  assert.match(citation.contentText, /npm start/);

  const wiki = (await request('POST', `/api/v1/wiki/from-message/${answer.assistantMessage.id}`, { title: '启动 Ordo' })).json().data;
  assert.equal(wiki.status, 'draft');
  assert.ok(wiki.revisions[0].sources.length > 0);

  const assistant = (await request('POST', '/api/v1/assistants', { name: '产品助手', datasetId: dataset.id })).json().data;
  const published = (await request('POST', `/api/v1/assistants/${assistant.id}/publish`, {})).json().data;
  assert.equal(published.status, 'published');
  assert.equal(published.releases.length, 1);

  const backupTask = (await request('POST', '/api/v1/backups', { label: 'e2e' })).json().data;
  const backup = await waitTask(request, backupTask.id);
  assert.equal(backup.result.status, 'verified');
  const restoreRoot = `${dataRoot}-restored`;
  const restoreTask = (await request('POST', `/api/v1/backups/${backup.result.backupId}/restore`, { targetRoot: restoreRoot })).json().data;
  const restored = await waitTask(request, restoreTask.id);
  assert.equal(restored.result.integrity, 'ok');
  assert.ok(fs.existsSync(path.join(restoreRoot, 'metadata', 'ordo.sqlite3')));
  const audit = (await request('GET', '/api/v1/audit/verify')).json().data;
  assert.equal(audit.valid, true);
  assert.ok(audit.count > 10);
});

test('trace replay creates an immutable child, supports idempotency, and compares runs', async t => {
  const { request } = await fixture(t);
  const kb = (await request('POST', '/api/v1/knowledge-bases', { name: 'Trace 重放测试库' })).json().data;
  const dataset = kb.datasets[0];
  const source = (await request('POST', `/api/v1/datasets/${dataset.id}/sources`, { type: 'upload', name: 'Trace 依据' })).json().data;
  const upload = multipart('trace-replay.md', '# Trace 重放\n\n运行 npm start 启动服务。', { sourceId: source.id });
  const registered = (await request('POST', `/api/v1/datasets/${dataset.id}/files`, upload.payload, upload.headers)).json().data;
  await waitTask(request, registered.task.id);
  const release = (await request('POST', `/api/v1/datasets/${dataset.id}/releases`, { activate: true })).json().data;
  await waitTask(request, release.id);
  const conversation = (await request('POST', '/api/v1/conversations', { knowledgeBaseId: kb.id, datasetId: dataset.id })).json().data;
  const original = (await request('POST', `/api/v1/conversations/${conversation.id}/messages`, { question: '如何启动 Ordo？' })).json().data.trace;
  const originalBefore = JSON.stringify(original);

  const replayResponse = await request('POST', `/api/v1/traces/${original.id}/replay`, { fromStage: '问题解析', overrides: { topK: 4 } }, { 'idempotency-key': 'trace-replay-e2e-1' });
  const replay = replayResponse.json().data;
  assert.equal(replay.replayed, true);
  assert.equal(replay.trace.trace_type, 'replay');
  assert.equal(replay.trace.parent, original.id);
  assert.equal(replay.trace.root, original.id);
  assert.equal(replay.trace.replay_from_stage, '问题解析');
  assert.equal(replay.trace.input_snapshot.sourceTraceId, original.id);
  assert.equal(replay.trace.config_snapshot.topK, 4);

  const originalAfter = (await request('GET', `/api/v1/traces/${original.id}`)).json().data;
  assert.equal(JSON.stringify(originalAfter), originalBefore);

  const repeated = (await request('POST', `/api/v1/traces/${original.id}/replay`, { fromStage: '问题解析', overrides: { topK: 4 } }, { 'idempotency-key': 'trace-replay-e2e-1' })).json().data;
  assert.equal(repeated.idempotent, true);
  assert.equal(repeated.trace.id, replay.trace.id);

  const compared = (await request('GET', `/api/v1/traces/${original.id}/compare/${replay.trace.id}`)).json().data;
  assert.equal(compared.traceId, original.id);
  assert.equal(compared.otherTraceId, replay.trace.id);
  assert.equal(Array.isArray(compared.stages), true);
  assert.equal(compared.stages.length, 8);
  assert.equal(typeof compared.timing.deltaMs, 'number');

  const unsupported = await requestError(request, 'POST', `/api/v1/traces/${original.id}/replay`, { fromStage: '回答生成' });
  assert.equal(unsupported.statusCode, 422);
  assert.equal(unsupported.json().error.code, 'REPLAY_UNSUPPORTED');
});

test('security boundaries reject unauthenticated writes, CSRF bypass and unsupported files', async t => {
  const { app, request } = await fixture(t);
  const unauthorized = await app.inject({ method: 'POST', url: '/api/v1/knowledge-bases', payload: { name: 'blocked' } });
  assert.equal(unauthorized.statusCode, 401);
  const bootstrap = await app.inject({ method: 'GET', url: '/api/v1/session/bootstrap' });
  const cookie = bootstrap.headers['set-cookie'].split(';')[0];
  const csrfMissing = await app.inject({ method: 'POST', url: '/api/v1/knowledge-bases', payload: { name: 'blocked' }, headers: { cookie } });
  assert.equal(csrfMissing.statusCode, 403);

  const kb = (await request('POST', '/api/v1/knowledge-bases', { name: '安全测试库' })).json().data;
  const dataset = kb.datasets[0];
  const bad = multipart('payload.html', '<script>alert(1)</script>');
  const rejected = await app.inject({ method: 'POST', url: `/api/v1/datasets/${dataset.id}/files`, payload: bad.payload,
    headers: { ...bad.headers, cookie, 'x-ordo-csrf': bootstrap.json().data.csrfToken } });
  assert.equal(rejected.statusCode, 415);
  assert.equal(rejected.json().error.code, 'UNSUPPORTED_FORMAT');
});

test('directory ingest authorizes real paths and rejects symlinks and changed files', async t => {
  const { app, request } = await fixture(t);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ordo-directory-'));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'ordo-outside-'));
  t.after(() => { fs.rmSync(root, { recursive: true, force: true }); fs.rmSync(outside, { recursive: true, force: true }); });
  fs.writeFileSync(path.join(root, 'safe.md'), '# 安全文档\n\n授权目录中的内容。');
  fs.writeFileSync(path.join(outside, 'secret.md'), '# 不应导入');
  try {
    fs.symlinkSync(path.join(outside, 'secret.md'), path.join(root, 'link.md'), 'file');
    fs.symlinkSync(outside, path.join(root, 'linked-directory'), 'junction');
  } catch (error) {
    if (['EPERM', 'EACCES'].includes(error.code)) return;
    throw error;
  }

  const kb = (await request('POST', '/api/v1/knowledge-bases', { name: '目录导入安全测试' })).json().data;
  const dataset = kb.datasets[0];
  const preview = await app.inject({ method: 'POST', url: `/api/v1/datasets/${dataset.id}/directory/preview`,
    payload: { directory: root }, headers: await authHeaders(app) });
  assert.equal(preview.statusCode, 400);
  assert.equal(preview.json().error.code, 'DIRECTORY_SYMLINK_REJECTED');

  fs.rmSync(path.join(root, 'link.md'), { force: true });
  fs.rmSync(path.join(root, 'linked-directory'), { recursive: true, force: true });
  const cleanPreview = await request('POST', `/api/v1/datasets/${dataset.id}/directory/preview`, { directory: root });
  assert.deepEqual(cleanPreview.json().data.candidates.map(item => item.relativePath), ['safe.md']);

  const source = (await request('POST', `/api/v1/datasets/${dataset.id}/directory/import`, { directory: root })).json().data;
  fs.writeFileSync(path.join(root, 'safe.md'), '# 被替换\n\n内容已发生变化。');
  const task = await waitTask(request, source.task.id);
  assert.equal(task.result.status, 'partial');
  assert.equal(task.result.manifest[0].status, 'failed');
  assert.equal(task.result.manifest[0].code, 'DIRECTORY_CHANGED');
});

test('connector, graph and signed website assistant APIs enforce their product boundaries', async t => {
  const { app, dataRoot, request } = await fixture(t);
  const kb = (await request('POST', '/api/v1/knowledge-bases', { name: '扩展能力测试库' })).json().data;
  await request('PUT', '/api/v1/feature-flags/databaseConnectors', { enabled: true });
  await request('PUT', '/api/v1/feature-flags/graph', { enabled: true });
  await request('PUT', '/api/v1/feature-flags/websiteAssistant', { enabled: true });
  const dataset = kb.datasets[0];
  const source = (await request('POST', `/api/v1/datasets/${dataset.id}/sources`, { type: 'upload', name: '图谱依据' })).json().data;
  const upload = multipart('facts.md', '# 产品关系\n\nOrdo 产品包含知识库模块。\n\n知识库模块支持严格证据问答。', { sourceId: source.id });
  const registered = (await request('POST', `/api/v1/datasets/${dataset.id}/files`, upload.payload, upload.headers)).json().data;
  await waitTask(request, registered.task.id);
  const chunks = (await request('GET', `/api/v1/datasets/${dataset.id}/chunks`)).json().data;
  assert.ok(chunks.length >= 2);
  const releaseTask = (await request('POST', `/api/v1/datasets/${dataset.id}/releases`, { activate: true })).json().data;
  await waitTask(request, releaseTask.id);

  const connector = (await request('POST', '/api/v1/connectors', { name: 'Ordo 元数据只读', type: 'sqlite', path: path.join(dataRoot, 'metadata', 'ordo.sqlite3') })).json().data;
  const tested = (await request('POST', `/api/v1/connectors/${connector.id}/test`, {})).json().data;
  assert.equal(tested.available, true);
  const schema = (await request('GET', `/api/v1/connectors/${connector.id}/schema`)).json().data;
  assert.ok(schema.some(item => item.name === 'knowledge_bases'));
  const template = (await request('POST', `/api/v1/connectors/${connector.id}/templates`, { name: '知识库清单', sql: 'SELECT id,name,status FROM knowledge_bases WHERE workspace_id=?', params: [{ name: 'workspaceId', type: 'text' }], rowLimit: 10 })).json().data;
  const queried = (await request('POST', `/api/v1/query-templates/${template.id}/execute`, { values: ['ws_local'] })).json().data;
  assert.equal(queried.rowCount, 1);
  const writeTemplate = await app.inject({ method: 'POST', url: `/api/v1/connectors/${connector.id}/templates`, payload: { name: '禁止写入', sql: 'DELETE FROM knowledge_bases' }, headers: await authHeaders(app) });
  assert.equal(writeTemplate.statusCode, 400);
  assert.equal(writeTemplate.json().error.code, 'QUERY_NOT_READ_ONLY');

  const ontology = (await request('POST', `/api/v1/knowledge-bases/${kb.id}/ontologies`, { name: '产品本体', publish: true, schema: { entityTypes: ['产品','模块'], relationTypes: ['包含'] } })).json().data;
  const productEntity = (await request('POST', `/api/v1/datasets/${dataset.id}/graph/entities`, { ontologyVersionId: ontology.id, entityType: '产品', name: 'Ordo', sourceChunkId: chunks[0].id })).json().data;
  const moduleEntity = (await request('POST', `/api/v1/datasets/${dataset.id}/graph/entities`, { ontologyVersionId: ontology.id, entityType: '模块', name: '知识库', sourceChunkId: chunks[1].id })).json().data;
  const relation = (await request('POST', `/api/v1/datasets/${dataset.id}/graph/relations`, { ontologyVersionId: ontology.id, relationType: '包含', sourceEntityId: productEntity.id, targetEntityId: moduleEntity.id, sourceChunkId: chunks[0].id })).json().data;
  assert.equal(relation.relation_type, '包含');
  const graph = (await request('GET', `/api/v1/datasets/${dataset.id}/graph`)).json().data;
  assert.equal(graph.entities.length, 2);
  assert.equal(graph.relations.length, 1);

  const assistant = (await request('POST', '/api/v1/assistants', { name: '网站证据助手', datasetId: dataset.id })).json().data;
  const published = (await request('POST', `/api/v1/assistants/${assistant.id}/publish`, {})).json().data;
  const client = (await request('POST', `/api/v1/assistants/${published.id}/clients`, { allowedOrigins: ['https://example.test'] })).json().data;
  assert.deepEqual(client.allowedOrigins, ['https://example.test']);
  assert.equal(client.secret_mask, `••••${client.clientSecret.slice(-4)}`);
  assert.equal(client.status, 'active');

  const clientsResponse = await request('GET', `/api/v1/assistants/${published.id}/clients`);
  const clients = clientsResponse.json().data;
  assert.equal(clients.length, 1);
  assert.deepEqual(clients[0].allowedOrigins, client.allowedOrigins);
  assert.equal(clients[0].secret_mask, client.secret_mask);
  assert.equal(Object.hasOwn(clients[0], 'clientSecret'), false);
  assert.equal(clientsResponse.body.includes(client.clientSecret), false);
  assert.equal(clientsResponse.body.includes('secret_ref'), false);

  const tokenBody = JSON.stringify({ origin: 'https://example.test' });
  const timestamp = Date.now();
  const nonce = `nonce_${Date.now()}_abcdef`;
  const signature = sign(client.clientSecret, canonicalRequest('POST', '/api/v1/public/widget/token', timestamp, nonce, 'https://example.test', tokenBody));
  const tokenResponse = await app.inject({ method: 'POST', url: '/api/v1/public/widget/token', payload: JSON.parse(tokenBody), headers: {
    origin: 'https://example.test', 'x-ordo-client': client.clientId, 'x-ordo-timestamp': String(timestamp), 'x-ordo-nonce': nonce, 'x-ordo-signature': signature
  } });
  assert.equal(tokenResponse.statusCode, 200, tokenResponse.body);
  const replay = await app.inject({ method: 'POST', url: '/api/v1/public/widget/token', payload: JSON.parse(tokenBody), headers: {
    origin: 'https://example.test', 'x-ordo-client': client.clientId, 'x-ordo-timestamp': String(timestamp), 'x-ordo-nonce': nonce, 'x-ordo-signature': signature
  } });
  assert.equal(replay.statusCode, 409);
  assert.equal(replay.json().error.code, 'WIDGET_REPLAY_REJECTED');
  const token = tokenResponse.json().data.token;
  const visitorResponse = await app.inject({ method: 'POST', url: '/api/v1/public/widget/sessions', payload: {}, headers: { origin: 'https://example.test', authorization: `Bearer ${token}` } });
  assert.equal(visitorResponse.statusCode, 200, visitorResponse.body);
  const visitor = visitorResponse.json().data;
  const answerResponse = await app.inject({ method: 'POST', url: `/api/v1/public/widget/sessions/${visitor.id}/messages`, payload: { question: 'Ordo 包含什么模块？' }, headers: { origin: 'https://example.test', authorization: `Bearer ${token}` } });
  assert.equal(answerResponse.statusCode, 200, answerResponse.body);
  assert.ok(answerResponse.json().data.citations.length > 0);
  const wrongOrigin = await app.inject({ method: 'POST', url: `/api/v1/public/widget/sessions/${visitor.id}/messages`, payload: { question: '泄露内容' }, headers: { origin: 'https://evil.test', authorization: `Bearer ${token}` } });
  assert.equal(wrongOrigin.statusCode, 403);

  const rotated = (await request('POST', `/api/v1/widget-clients/${client.id}/rotate`, {})).json().data;
  assert.notEqual(rotated.clientSecret, client.clientSecret);
  assert.deepEqual(rotated.allowedOrigins, client.allowedOrigins);
  assert.equal(rotated.secret_mask, `••••${rotated.clientSecret.slice(-4)}`);
  assert.equal(rotated.status, 'active');
  const oldSecret = await requestWidgetToken(app, client.clientId, client.clientSecret);
  assert.equal(oldSecret.statusCode, 401);
  assert.equal(oldSecret.json().error.code, 'WIDGET_SIGNATURE_INVALID');
  const newSecret = await requestWidgetToken(app, client.clientId, rotated.clientSecret);
  assert.equal(newSecret.statusCode, 200, newSecret.body);

  const revoked = (await request('DELETE', `/api/v1/widget-clients/${client.id}`)).json().data;
  assert.deepEqual(revoked.allowedOrigins, client.allowedOrigins);
  assert.equal(revoked.secret_mask, rotated.secret_mask);
  assert.equal(revoked.status, 'revoked');
  assert.equal(Object.hasOwn(revoked, 'clientSecret'), false);
  const revokedSecret = await requestWidgetToken(app, client.clientId, rotated.clientSecret);
  assert.equal(revokedSecret.statusCode, 401);
  assert.equal(revokedSecret.json().error.code, 'WIDGET_CLIENT_INVALID');
});

test('index profiles preserve nested defaults and enforce immutable release references', async t => {
  const { request } = await fixture(t);
  const kb = (await request('POST', '/api/v1/knowledge-bases', { name: '配置生命周期测试' })).json().data;
  const profiles = (await request('GET', `/api/v1/knowledge-bases/${kb.id}/index-profiles`)).json().data;
  assert.equal(profiles.length, 1);
  const defaultConfig = profiles[0].config;
  assert.equal(defaultConfig.embedding.provider, 'local-hash-v1');
  assert.equal(defaultConfig.fusion.k, 60);
  const created = (await request('POST', `/api/v1/knowledge-bases/${kb.id}/index-profiles`, { name: '更大向量', config: { embedding: { dimensions: 256 } }, setDefault: true })).json().data;
  assert.equal(created.config.embedding.provider, 'local-hash-v1');
  assert.equal(created.config.embedding.dimensions, 256);
  const detail = (await request('GET', `/api/v1/index-profiles/${created.id}`)).json().data;
  assert.equal(detail.releaseCount, 0);
  const selected = (await request('POST', `/api/v1/index-profiles/${created.id}/default`, {})).json().data;
  assert.equal(selected.defaultIndexProfileId, created.id);
});

test('release build idempotency follows the current publishable chunk revisions', async t => {
  const { request } = await fixture(t);
  const kb = (await request('POST', '/api/v1/knowledge-bases', { name: '发布快照幂等测试' })).json().data;
  const dataset = kb.datasets[0];
  const source = (await request('POST', `/api/v1/datasets/${dataset.id}/sources`, { type: 'upload', name: '发布内容' })).json().data;
  const upload = multipart('release-idempotency.md', '# 发布内容\n\n第一版可发布内容。', { sourceId: source.id });
  const registered = (await request('POST', `/api/v1/datasets/${dataset.id}/files`, upload.payload, upload.headers)).json().data;
  await waitTask(request, registered.task.id);

  const firstTask = (await request('POST', `/api/v1/datasets/${dataset.id}/releases`, { activate: false })).json().data;
  const first = await waitTask(request, firstTask.id);
  const repeatedTask = (await request('POST', `/api/v1/datasets/${dataset.id}/releases`, { activate: false })).json().data;
  assert.equal(repeatedTask.id, firstTask.id);

  const chunks = (await request('GET', `/api/v1/datasets/${dataset.id}/chunks`)).json().data;
  const revised = (await request('POST', `/api/v1/chunks/${chunks[0].id}/revisions`, {
    contentMd: `${chunks[0].content_md}\n\n第二版修订`,
    contentText: `${chunks[0].content_text}\n\n第二版修订`
  })).json().data;
  assert.notEqual(revised.id, chunks[0].id);

  const revisedTask = (await request('POST', `/api/v1/datasets/${dataset.id}/releases`, { activate: false })).json().data;
  assert.notEqual(revisedTask.id, firstTask.id);
  const second = await waitTask(request, revisedTask.id);
  assert.notEqual(second.result.releaseId, first.result.releaseId);
});

test('model create and update roll back secret changes when model writes fail', async t => {
  const { app } = await fixture(t);
  const { db, models, secretStore } = app.services;
  const workspaceId = app.services.config.localWorkspaceId;
  const primary = await models.create({ name: '事务模型 A', provider: 'local-extractive', apiKey: 'original-key-A' }, workspaceId);
  const primaryRow = db.one('SELECT * FROM model_connections WHERE id=?', primary.id);
  assert.equal(secretStore.resolve(primaryRow.secret_ref, workspaceId), 'original-key-A');
  const initialSecretCount = db.one('SELECT COUNT(*) AS count FROM secrets WHERE workspace_id=?', workspaceId).count;

  await assert.rejects(
    models.create({ name: '事务模型 A', provider: 'local-extractive', apiKey: 'orphan-candidate' }, workspaceId),
    error => error.code === 'NAME_CONFLICT'
  );
  assert.equal(db.one('SELECT COUNT(*) AS count FROM secrets WHERE workspace_id=?', workspaceId).count, initialSecretCount);

  const secondary = await models.create({ name: '事务模型 B', provider: 'local-extractive' }, workspaceId);
  await assert.rejects(
    models.update(primary.id, { name: secondary.name, apiKey: 'replacement-key' }, workspaceId),
    error => error.code === 'NAME_CONFLICT'
  );
  const unchangedPrimary = db.one('SELECT * FROM model_connections WHERE id=?', primary.id);
  assert.equal(unchangedPrimary.name, '事务模型 A');
  assert.equal(secretStore.resolve(unchangedPrimary.secret_ref, workspaceId), 'original-key-A');
  assert.equal(db.one('SELECT COUNT(*) AS count FROM secrets WHERE workspace_id=?', workspaceId).count, initialSecretCount);

  await assert.rejects(
    models.update(secondary.id, { name: primary.name, apiKey: 'new-orphan-candidate' }, workspaceId),
    error => error.code === 'NAME_CONFLICT'
  );
  const unchangedSecondary = db.one('SELECT * FROM model_connections WHERE id=?', secondary.id);
  assert.equal(unchangedSecondary.name, '事务模型 B');
  assert.equal(unchangedSecondary.secret_ref, null);
  assert.equal(db.one('SELECT COUNT(*) AS count FROM secrets WHERE workspace_id=?', workspaceId).count, initialSecretCount);
});

test('strict evidence mode refuses unsupported questions without calling a remote model', async t => {
  const { request } = await fixture(t);
  const model = (await request('POST', '/api/v1/models', { name: '本地拒答模型', provider: 'local-extractive', purpose: 'generation', modelId: 'ordo-local-extractive-v1' })).json().data;
  const kb = (await request('POST', '/api/v1/knowledge-bases', { name: '严格拒答测试' })).json().data;
  const dataset = kb.datasets[0];
  const source = (await request('POST', `/api/v1/datasets/${dataset.id}/sources`, { type: 'upload', name: '依据' })).json().data;
  const upload = multipart('facts.md', '# 启动\n\n运行 npm start 启动服务。', { sourceId: source.id });
  const registered = (await request('POST', `/api/v1/datasets/${dataset.id}/files`, upload.payload, upload.headers)).json().data;
  await waitTask(request, registered.task.id);
  const built = await waitTask(request, (await request('POST', `/api/v1/datasets/${dataset.id}/releases`, { activate: true })).json().data.id);
  const conversation = (await request('POST', '/api/v1/conversations', { knowledgeBaseId: kb.id, datasetId: dataset.id, modelConnectionId: model.id })).json().data;
  const answer = (await request('POST', `/api/v1/conversations/${conversation.id}/messages`, { question: '数据库的迁移策略是什么？' })).json().data;
  assert.equal(answer.assistantMessage.evidence_status, 'insufficient');
  assert.equal(answer.assistantMessage.citations.length, 0);
  assert.match(answer.assistantMessage.content, /无法回答|没有找到/);
  assert.equal(answer.trace.stages.length, 8);
  assert.equal(built.result.status, 'active');
});

test('web workbench api client request shapes are accepted alongside canonical ones', async t => {
  const { request } = await fixture(t);
  const kb = (await request('POST', '/api/v1/knowledge-bases', { name: '前端形状测试库' })).json().data;
  const dataset = kb.datasets[0];
  const source = (await request('POST', `/api/v1/datasets/${dataset.id}/sources`, { type: 'upload', name: '形状依据' })).json().data;
  const upload = multipart('frontend-shape.md', '# 启动\n\n运行 npm start 启动服务。', { sourceId: source.id });
  const registered = (await request('POST', `/api/v1/datasets/${dataset.id}/files`, upload.payload, upload.headers)).json().data;
  await waitTask(request, registered.task.id);
  await waitTask(request, (await request('POST', `/api/v1/datasets/${dataset.id}/releases`, { activate: true })).json().data.id);

  // 工作台 api.createConversation 形状：{ title, knowledge_base_id }（snake_case，无模型连接）
  const conversation = (await request('POST', '/api/v1/conversations', { title: '前端形状', knowledge_base_id: kb.id })).json().data;
  assert.equal(conversation.knowledge_base_id, kb.id);
  // 工作台 api.sendMessage 形状：{ query }，无外部模型时降级本地抽取回答
  const answer = (await request('POST', `/api/v1/conversations/${conversation.id}/messages`, { query: '如何启动 Ordo？' })).json().data;
  assert.equal(answer.assistantMessage.evidence_status, 'sufficient');
  assert.ok(answer.assistantMessage.citations.length > 0);
  assert.equal(answer.trace.stages.length, 8);
  // 工作台 api.updateSetting 形状：{ value: {...} } 包装
  await request('PUT', '/api/v1/settings/general', { value: { language: 'zh-CN' } });
  const settings = (await request('GET', '/api/v1/settings')).json().data;
  assert.equal(settings.general.language, 'zh-CN');
});

test('runtime schema version and list pagination metadata come from persisted data', async t => {
  const { app, request } = await fixture(t);
  const expectedSchemaVersion = app.services.db.one('SELECT MAX(version) AS version FROM schema_migrations').version;

  const version = (await request('GET', '/api/v1/version')).json().data;
  const health = (await request('GET', '/api/v1/health')).json().data;
  const diagnostics = (await request('GET', '/api/v1/diagnostics')).json().data;
  assert.equal(version.schemaVersion, expectedSchemaVersion);
  assert.equal(health.components.metadata.schemaVersion, expectedSchemaVersion);
  assert.equal(diagnostics.schemaVersion, expectedSchemaVersion);
  assert.equal(diagnostics.health.components.metadata.schemaVersion, expectedSchemaVersion);

  const kb = (await request('POST', '/api/v1/knowledge-bases', { name: '版本分页测试库' })).json().data;
  const dataset = kb.datasets[0];
  const source = (await request('POST', `/api/v1/datasets/${dataset.id}/sources`, { type: 'upload', name: '分页文档' })).json().data;
  for (const [filename, content] of [
    ['page-one.md', '# 第一页\n\nOrdo 分页测试第一份文档。'],
    ['page-two.md', '# 第二页\n\nOrdo 分页测试第二份文档。']
  ]) {
    const upload = multipart(filename, content, { sourceId: source.id });
    const registered = (await request('POST', `/api/v1/datasets/${dataset.id}/files`, upload.payload, upload.headers)).json().data;
    await waitTask(request, registered.task.id);
  }
  const releaseTask = (await request('POST', `/api/v1/datasets/${dataset.id}/releases`, { activate: true })).json().data;
  await waitTask(request, releaseTask.id);
  for (const title of ['分页会话一', '分页会话二']) {
    const conversation = (await request('POST', '/api/v1/conversations', { knowledgeBaseId: kb.id, datasetId: dataset.id, title })).json().data;
    await request('POST', `/api/v1/conversations/${conversation.id}/messages`, { question: 'Ordo 分页测试是什么？' });
  }

  const assertPaginated = async url => {
    const separator = url.includes('?') ? '&' : '?';
    const first = (await request('GET', `${url}${separator}limit=1&offset=0`)).json();
    assert.equal(Array.isArray(first.data), true);
    assert.equal(first.data.length, 1);
    assert.ok(first.meta.total >= 2, `${url} total=${first.meta.total}`);
    assert.deepEqual({ limit: first.meta.limit, offset: first.meta.offset, hasMore: first.meta.hasMore }, { limit: 1, offset: 0, hasMore: true });

    const last = (await request('GET', `${url}${separator}limit=1&offset=${first.meta.total - 1}`)).json();
    assert.equal(Array.isArray(last.data), true);
    assert.equal(last.data.length, 1);
    assert.deepEqual(last.meta, { total: first.meta.total, limit: 1, offset: first.meta.total - 1, hasMore: false });
  };

  await assertPaginated(`/api/v1/datasets/${dataset.id}/documents`);
  await assertPaginated(`/api/v1/datasets/${dataset.id}/chunks`);
  await assertPaginated('/api/v1/tasks');
  await assertPaginated('/api/v1/conversations');
  await assertPaginated('/api/v1/traces');
  const tracesList = (await request('GET', '/api/v1/traces')).json().data;
  if (tracesList.length > 0) {
    const traceId = tracesList[0].id;
    const fusionStage = (await request('GET', `/api/v1/traces/${traceId}/stages/fusion`)).json().data;
    assert.equal(fusionStage.summaryMetrics.rawCandidateCount, 41);
    assert.equal(fusionStage.summaryMetrics.dedupCandidateCount, 32);
    assert.equal(fusionStage.summaryMetrics.fusedCandidateCount, 20);

    const weightsRes = (await request('PUT', `/api/v1/traces/${traceId}/stages/fusion/weights`, { topKFinal: 20 })).json().data;
    assert.equal(weightsRes.newFusedCount, 20);

    const resetRes = (await request('POST', `/api/v1/traces/${traceId}/stages/fusion/reset-weights`)).json().data;
    assert.equal(resetRes.topKFinal, 20);

    const calcRes = (await request('GET', `/api/v1/traces/${traceId}/stages/fusion/calculation/cand_01`)).json().data;
    assert.equal(calcRes.candidateId, 'cand_01');
    assert.equal(calcRes.normalizedScore, 0.842);

    const candsRes = (await request('GET', `/api/v1/traces/${traceId}/stages/fusion/candidates?pageSize=5`)).json();
    assert.equal(candsRes.data.length, 5);
    assert.equal(candsRes.meta.total, 20);

    const chunkRes = (await request('GET', `/api/v1/traces/${traceId}/stages/fusion/chunks/cand_01`)).json().data;
    assert.equal(chunkRes.candidateId, 'cand_01');
    assert.equal(chunkRes.permissionStatus, 'passed');

    const logsRes = (await request('GET', `/api/v1/traces/${traceId}/stages/fusion/logs`)).json().data;
    assert.equal(logsRes.stage, 'fusion');
    assert.equal(logsRes.logs.length, 5);

    const exportRes = (await request('GET', `/api/v1/traces/${traceId}/stages/fusion/export`)).json().data;
    assert.equal(exportRes.fusedCandidates.length, 20);

    const rerunRes = (await request('POST', `/api/v1/traces/${traceId}/stages/fusion/rerun`, { topKFinal: 20 })).json().data;
    assert.ok(rerunRes.derivedTraceId);

    const rerankRes = (await request('GET', `/api/v1/traces/${traceId}/stages/rerank`)).json().data;
    assert.equal(rerankRes.modelCard.modelName, 'bge-reranker-v2-m3');
    assert.equal(rerankRes.afterCandidates.length, 8);
    assert.equal(rerankRes.scoreCurve.dataPoints.length, 20);
    assert.ok(typeof rerankRes.totalDuration === 'string' && rerankRes.totalDuration.endsWith(' s'));
    assert.ok(typeof rerankRes.totalElapsedMs === 'number');

    const pipelineRes = (await request('GET', `/api/v1/traces/${traceId}/pipeline`)).json().data;
    assert.equal(pipelineRes.currentStage, 6);
    assert.ok(typeof pipelineRes.totalDuration === 'string' && pipelineRes.totalDuration.endsWith(' s'));
    assert.ok(typeof pipelineRes.totalElapsedMs === 'number');
    assert.equal(pipelineRes.completedCount, 6);
    assert.equal(pipelineRes.stages[6].status, 'pending');
    assert.equal(pipelineRes.stages[6].durationMs, null);
    assert.equal(pipelineRes.stages[7].status, 'pending');
    assert.equal(pipelineRes.stages[7].durationMs, null);

    const demoRerank = (await request('GET', '/api/v1/traces/QA-2025-0520-0086/stages/rerank')).json().data;
    assert.equal(demoRerank.totalDuration, '1.32 s');
    assert.equal(demoRerank.totalElapsedMs, 1321);

    const demoPipeline = (await request('GET', '/api/v1/traces/QA-2025-0520-0086/pipeline')).json().data;
    assert.equal(demoPipeline.currentStage, 6);
    assert.equal(demoPipeline.totalDuration, '1.32 s');
    assert.equal(demoPipeline.totalElapsedMs, 1321);
    assert.equal(demoPipeline.completedCount, 6);
    assert.equal(demoPipeline.stages[5].durationMs, 512);
    assert.equal(demoPipeline.stages[6].status, 'pending');
    assert.equal(demoPipeline.stages[6].durationMs, null);
    assert.equal(demoPipeline.stages[7].status, 'pending');
    assert.equal(demoPipeline.stages[7].durationMs, null);

    const demoPipelineStage4 = (await request('GET', '/api/v1/traces/QA-2025-0520-0086/pipeline?stage=4')).json().data;
    assert.equal(demoPipelineStage4.currentStage, 4);
    assert.equal(demoPipelineStage4.totalDuration, '0.60 s');
    assert.equal(demoPipelineStage4.totalElapsedMs, 599);
    assert.equal(demoPipelineStage4.stages[4].status, 'pending');
    assert.equal(demoPipelineStage4.stages[4].durationMs, null);

    const rerankChunkRes = (await request('GET', `/api/v1/traces/${traceId}/stages/rerank/chunks/chunk_00321`)).json().data;
    assert.equal(rerankChunkRes.chunkId, 'chunk_00321');
    assert.equal(rerankChunkRes.afterScore, 0.912);

    const rerankConfigRes = (await request('PUT', `/api/v1/traces/${traceId}/stages/rerank/config`, { scoreThreshold: 0.75, maxRetainedTopK: 8 })).json().data;
    assert.equal(rerankConfigRes.retainedCount, 8);

    const rerankCompareRes = (await request('POST', `/api/v1/traces/${traceId}/stages/rerank/compare`)).json().data;
    assert.equal(rerankCompareRes.ndcgAt10.after, 0.892);

    const rerankLogsRes = (await request('GET', `/api/v1/traces/${traceId}/stages/rerank/logs`)).json().data;
    assert.equal(rerankLogsRes.stage, 'rerank');
    assert.equal(rerankLogsRes.logs.length, 4);
  }
  await assertPaginated('/api/v1/audit');

  const backupTask = (await request('POST', '/api/v1/backups', { label: 'schema-version-e2e' })).json().data;
  const backup = await waitTask(request, backupTask.id);
  const manifests = (await request('GET', '/api/v1/backups')).json().data;
  const manifest = manifests.find(item => item.id === backup.result.backupId)?.manifest;
  assert.ok(manifest);
  assert.equal(manifest.schemaVersion, expectedSchemaVersion);
});

async function requestError(request, method, url, payload, headers = {}) {
  try {
    await request(method, url, payload, headers);
    throw new Error(`${method} ${url} unexpectedly succeeded`);
  } catch (error) {
    const match = String(error.message || '').match(/-> (\d+): (\{.*\})$/s);
    if (!match) throw error;
    return { statusCode: Number(match[1]), json: () => JSON.parse(match[2]) };
  }
}

async function authHeaders(app) {
  const bootstrap = await app.inject({ method: 'GET', url: '/api/v1/session/bootstrap' });
  return { cookie: bootstrap.headers['set-cookie'].split(';')[0], 'x-ordo-csrf': bootstrap.json().data.csrfToken };
}
