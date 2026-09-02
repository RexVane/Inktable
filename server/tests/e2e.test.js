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

async function authHeaders(app) {
  const bootstrap = await app.inject({ method: 'GET', url: '/api/v1/session/bootstrap' });
  return { cookie: bootstrap.headers['set-cookie'].split(';')[0], 'x-ordo-csrf': bootstrap.json().data.csrfToken };
}
