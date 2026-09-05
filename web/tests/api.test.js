'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createClient, operations, aliases, ApiError } = require('../api');
const normalize = value => value.split('?')[0].replace(/:[\w]+|\{\w+\}/g, ':id');
const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });

test('all API contracts in the 22 page documents and overview have a named client method or canonical alias', () => {
  const directory = path.resolve(__dirname, '../../planning/前端API清单');
  const covered = new Set(Object.values(operations).map(o => o.method + ' ' + normalize(o.path)));
  Object.keys(aliases).forEach(key => covered.add(normalize(key)));
  const contracts = new Set();
  const add = (method, url) => {
    // Concrete curl examples use these IDs; their templated operations are
    // checked separately from the contract tables and request headings.
    if (/chunk_000|rel_v7/.test(url)) return;
    contracts.add(method + ' ' + normalize(url));
  };
  for (const filename of fs.readdirSync(directory).filter(name => name.endsWith('.md'))) {
    const text = fs.readFileSync(path.join(directory, filename), 'utf8');
    for (const match of text.matchAll(/\b(GET|POST|PUT|PATCH|DELETE)\s+(\/api\/v1\/[^\s`|)<>]+)/g)) add(match[1], match[2]);
    for (const line of text.split('\n')) {
      const methodFirst = line.match(/`(GET|POST|PUT|PATCH|DELETE)`\s*\|\s*`(\/api\/v1\/[^`]+)`/);
      const pathFirst = line.match(/`(\/api\/v1\/[^`]+)`\s*\|\s*`(GET|POST|PUT|PATCH|DELETE)`/);
      if (methodFirst) add(methodFirst[1], methodFirst[2]);
      if (pathFirst) add(pathFirst[2], pathFirst[1]);
    }
  }
  assert.ok(contracts.size >= 203, 'contract extraction must include headings and both table layouts');
  assert.deepEqual([...contracts].filter(key => !covered.has(key)), []);
  const client = createClient();
  for (const name of Object.keys(operations)) assert.equal(typeof client[name], 'function', name);
  for (const target of Object.values(aliases)) assert.equal(typeof client[target], 'function', target);
});

test('every API method called by the existing workbench resolves after loading the client', () => {
  const source = fs.readFileSync(path.resolve(__dirname, '../app.js'), 'utf8');
  const client = createClient();
  const local = new Set(['bootstrap', 'syncContext', 'applyContextToState', 'parseCitationLocator']);
  const calls = [...source.matchAll(/\bapi\.([A-Za-z]\w*)\s*\(/g)].map(m => m[1]);
  assert.deepEqual([...new Set(calls)].filter(name => !local.has(name) && typeof client[name] !== 'function'), []);
});

test('encoded path IDs, filter values, false/zero and pagination survive the HTTP boundary', async () => {
  const requests = [];
  const client = createClient({ fetch: async (url, init) => {
    requests.push({ url, init });
    return json({ data: [{ id: 'doc_1' }], meta: { total: 11, limit: 1, offset: 0, hasMore: true } });
  } });
  const page = await client.getDocumentsPage('ds/中文?x#', { query: '文档 & A', limit: 1, offset: 0, excluded: false, empty: '', absent: undefined });
  assert.equal(page.meta.total, 11);
  const url = new URL(requests[0].url, 'http://ordo.test');
  assert.equal(decodeURIComponent(url.pathname.split('/')[4]), 'ds/中文?x#');
  assert.equal(url.searchParams.get('query'), '文档 & A');
  assert.equal(url.searchParams.get('excluded'), 'false');
  assert.equal(url.searchParams.get('offset'), '0');
  assert.equal(url.searchParams.has('empty'), false);
  assert.equal(requests[0].init.headers.has('Content-Type'), false);
  assert.deepEqual(await client.getDocuments('ds_1'), [{ id: 'doc_1' }]);
  await assert.rejects(client.getChunk(undefined), error => error.code === 'API_PARAMETER_REQUIRED');
  assert.equal(requests.length, 2, 'invalid IDs must not send an HTTP request');
});

test('existing positional methods also forward complete payloads, restore paths and idempotency keys', async () => {
  const calls = [];
  const client = createClient({ fetch: async (url, init) => { calls.push({ url, init, body: init.body && JSON.parse(init.body) }); return json({ data: { ok: true } }); } });
  client.csrfToken = 'csrf-test';
  await client.createConversation({ title: '问答', knowledgeBaseId: 'kb_1', datasetId: 'ds_1', modelConnectionId: 'model_1', releaseId: 'rel_1' });
  assert.equal(calls.at(-1).body.modelConnectionId, 'model_1');
  await client.sendMessage('conv_1', { question: '问题', topK: 4 });
  assert.equal(calls.at(-1).body.topK, 4);
  await client.createAssistantClient('ast_1', { origins: ['https://example.test'], name: 'site' });
  assert.deepEqual(calls.at(-1).body, { name: 'site', allowedOrigins: ['https://example.test'] });
  await client.directoryImport('ds_1', { directory: 'D:\\docs', rules: { maxFiles: 10 }, idempotencyKey: 'import_1' });
  assert.equal(calls.at(-1).body.rules.maxFiles, 10);
  await client.restoreBackup('backup_1', { targetRoot: 'D:\\restore', idempotencyKey: 'restore_1' });
  assert.equal(calls.at(-1).body.targetRoot, 'D:\\restore');
  await client.replayTrace('trace_1', { fromStage: '问题解析', overrides: { topK: 4 } }, { idempotencyKey: 'replay_1' });
  assert.equal(calls.at(-1).init.headers.get('Idempotency-Key'), 'replay_1');
  assert.equal(calls.at(-1).init.headers.get('x-ordo-csrf'), 'csrf-test');
  await client.saveMessageWiki('msg_1', { title: 'Wiki' });
  assert.equal(calls.at(-1).url, '/api/v1/wiki/from-message/msg_1');
});

test('multipart uploads put metadata before file and leave the boundary to fetch', async () => {
  const calls = [];
  const client = createClient({ fetch: async (url, init) => { calls.push({ url, init }); return json({ data: { task: { id: 'task_1' } } }); } });
  const file = new File(['# test'], 'test.md', { type: 'text/markdown' });
  await client.uploadDocument('ds_1', file, 'src_1');
  assert.deepEqual([...calls[0].init.body.keys()], ['sourceId', 'file']);
  assert.equal(calls[0].init.body.get('sourceId'), 'src_1');
  assert.equal(calls[0].init.headers.has('Content-Type'), false);
  await client.uploadArchive('ds_1', file);
  assert.equal(calls[1].url, '/api/v1/datasets/ds_1/archives');
  await client.uploadFile(file, { datasetId: 'ds_1', translate: false });
  assert.equal(calls[2].init.body.get('translate'), 'false');
});

test('raw OpenAPI, artifact text/JSON, binary downloads and empty responses retain their types', async () => {
  const client = createClient({ fetch: async url => {
    if (url.endsWith('openapi.json')) return json({ openapi: '3.1.0', paths: {} });
    if (url.endsWith('/markdown')) return new Response('# 文档');
    if (url.endsWith('/document')) return json({ schemaVersion: 1, blocks: [] });
    if (url.endsWith('/binary')) return new Response(new Uint8Array([1, 2, 3]));
    return new Response(null, { status: 204 });
  } });
  assert.equal((await client.getOpenApi()).openapi, '3.1.0');
  assert.equal(await client.getArtifactMarkdown('art_1'), '# 文档');
  assert.equal((await client.getArtifactDocument('art_1')).schemaVersion, 1);
  assert.equal((await client.request('/api/v1/binary', { responseType: 'blob' })).size, 3);
  assert.equal(await client.deleteDocument('doc_1'), null);
  assert.equal(client.lastError, null);
});

test('expired sessions refresh once for concurrent requests; other errors do not retry writes', async () => {
  let refreshes = 0, writes = 0;
  const client = createClient({ fetch: async (url, init) => {
    if (url.endsWith('/bootstrap')) {
      refreshes++;
      await new Promise(resolve => setTimeout(resolve, 5));
      return json({ data: { csrfToken: 'fresh', workspaceId: 'ws_local' } });
    }
    if (url.endsWith('/assistants')) { writes++; return json({ error: { code: 'VALIDATION_ERROR', message: '缺少数据集', requestId: 'req_1' } }, 400); }
    if (init.headers.get('x-ordo-csrf') !== 'fresh') return json({ error: { code: 'SESSION_REQUIRED', message: 'expired' } }, 401);
    return json({ data: { ok: true } });
  } });
  client.csrfToken = 'expired';
  const results = await Promise.all([client.createKnowledgeBase({ name: 'A' }), client.createKnowledgeBase({ name: 'B' })]);
  assert.equal(refreshes, 1);
  assert.deepEqual(results, [{ ok: true }, { ok: true }]);
  assert.equal(await client.createAssistant({ name: 'missing dataset' }), null);
  assert.equal(writes, 1);
  assert.equal(client.lastError.requestId, 'req_1');
  await assert.rejects(client.createAssistant({}, { throwOnError: true }), error => error instanceof ApiError && error.code === 'VALIDATION_ERROR');
});

test('unimplemented contracts preserve the backend error and never report simulated success', async () => {
  const client = createClient({ fetch: async () => json({ error: { code: 'ROUTE_NOT_FOUND', message: 'API 路由不存在' } }, 404) });
  assert.equal(await client.getTraceEmbedStage('trace_1'), null);
  assert.equal(client.lastError.status, 404);
  assert.equal(client.lastError.code, 'ROUTE_NOT_FOUND');
});

test('public widget requests keep signed bytes and omit administrator cookies and CSRF', async () => {
  const calls = [];
  const client = createClient({ fetch: async (url, init) => { calls.push({ url, init }); return json({ data: {} }); } });
  client.csrfToken = 'admin-csrf';
  const signedBody = '{ "origin": "https://example.test" }';
  await client.issueWidgetToken(signedBody, { headers: { 'x-ordo-signature': 'server-signature', 'x-ordo-client': 'client_1', 'x-ordo-timestamp': '123', 'x-ordo-nonce': 'nonce_1' } });
  assert.equal(calls[0].init.body, signedBody);
  assert.equal(calls[0].init.credentials, 'omit');
  assert.equal(calls[0].init.headers.has('x-ordo-csrf'), false);
  await client.sendWidgetMessage('visitor_1', { question: 'test' }, { token: 'visitor-token' });
  assert.equal(calls[1].init.headers.get('Authorization'), 'Bearer visitor-token');
});

test('SSE handles split UTF-8, CRLF, multiline data, errors and interrupted streams', async () => {
  const events = [];
  const bytes = new TextEncoder().encode('event: token\r\ndata: {"delta":"中文"}\r\n\r\nevent: done\r\ndata: {"ok":\r\ndata: true}\r\n\r\n');
  const client = createClient({ fetch: async () => new Response(new ReadableStream({
    start(controller) { for (const byte of bytes) controller.enqueue(new Uint8Array([byte])); controller.close(); }
  }), { headers: { 'Content-Type': 'text/event-stream' } }) });
  assert.deepEqual(await client.sendMessageStream('conv_1', 'hello', (event, data) => events.push({ event, data })), { ok: true });
  assert.deepEqual(events, [{ event: 'token', data: { delta: '中文' } }, { event: 'done', data: { ok: true } }]);
  for (const [text, code] of [['event: error\ndata: {"message":"failed","code":"MODEL_FAILED"}\n\n', 'MODEL_FAILED'], ['event: token\ndata: {"delta":"partial"}\n\n', 'STREAM_INCOMPLETE']]) {
    const failed = createClient({ fetch: async () => new Response(text, { headers: { 'Content-Type': 'text/event-stream' } }) });
    assert.equal(await failed.sendMessageStream('conv_1', 'hello', () => {}), null);
    assert.equal(failed.lastError.code, code);
  }
});

test('aborting SSE cancels the reader and reports cancellation', async () => {
  const controller = new AbortController();
  let cancelled = false;
  const client = createClient({ fetch: async () => new Response(new ReadableStream({
    cancel() { cancelled = true; }
  }), { headers: { 'Content-Type': 'text/event-stream' } }) });
  const pending = client.sendMessageStream('conv_1', 'hello', () => {}, { signal: controller.signal });
  setTimeout(() => controller.abort(), 5);
  assert.equal(await pending, null);
  assert.equal(cancelled, true);
  assert.equal(client.lastError.code, 'REQUEST_ABORTED');
});
