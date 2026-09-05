'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { createApp, createOpenApi } = require('../src/app');
const { canonicalRequest, sign } = require('../src/widget');
const { createClient, operations } = require('../../web/api');

test('the frontend catalog covers every published backend method and path', () => {
  const normalize = value => value.replace(/:[\w]+|\{\w+\}/g, ':id');
  const catalog = new Set(Object.values(operations).map(o => o.method + ' ' + normalize(o.path)));
  const spec = createOpenApi({ host: '127.0.0.1', port: 8790, appVersion: '1.0.0' });
  for (const [url, methods] of Object.entries(spec.paths)) {
    for (const method of Object.keys(methods)) assert.ok(catalog.has(method.toUpperCase() + ' ' + normalize(url)), method + ' ' + url);
  }
});

test('browser client completes real upload, release, trace, assistant, widget and restore flows', async t => {
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ordo-client-'));
  const restoreRoot = dataRoot + '-restored';
  const app = await createApp({ dataRoot, logger: false });
  t.after(async () => {
    await app.close();
    for (const directory of [restoreRoot, dataRoot]) {
      if (!fs.existsSync(directory)) continue;
      const resolved = fs.realpathSync(directory);
      assert.equal(path.dirname(resolved).toLowerCase(), fs.realpathSync(os.tmpdir()).toLowerCase());
      assert.ok(path.basename(resolved).startsWith('ordo-client-'));
      assert.equal(fs.lstatSync(directory).isSymbolicLink(), false);
      fs.rmSync(resolved, { recursive: true, force: true });
    }
  });
  let cookie = '';
  const client = createClient({ throwOnError: true, fetch: async (url, init) => {
    const request = new Request('http://ordo.test' + url, init);
    const headers = Object.fromEntries(request.headers);
    if (cookie && init.credentials !== 'omit') headers.cookie = cookie;
    const response = await app.inject({ method: request.method, url, headers,
      ...(request.body ? { payload: Buffer.from(await request.arrayBuffer()) } : {}) });
    if (response.headers['set-cookie']) cookie = response.headers['set-cookie'].split(';')[0];
    return new Response(response.statusCode === 204 ? null : response.rawPayload, { status: response.statusCode, headers: response.headers });
  } });
  assert.ok((await client.bootstrapSession()).csrfToken);
  assert.equal((await client.getOpenApi()).openapi, '3.1.0');
  const entry = await app.inject('/');
  assert.ok(entry.body.indexOf('api.js') < entry.body.indexOf('app.js'));
  assert.equal((await app.inject('/api.js')).statusCode, 200);
  const kb = await client.createKnowledgeBase({ name: 'Frontend client fixture' });
  const dataset = kb.datasets[0];
  const source = await client.createSource(dataset.id, { type: 'upload', name: 'Client upload' });
  const file = new File(['# Ordo\n\nOrdo installation uses npm start.\n\nOrdo stores knowledge locally.'], 'client.md', { type: 'text/markdown' });
  const upload = await client.uploadDocument(dataset.id, file, source.id);
  assert.equal(upload.document.source_id, source.id);
  assert.equal((await client.getSources(dataset.id)).length, 1, 'the upload must use the provided source');
  const parsed = await client.waitTask(upload.task.id);
  assert.equal(parsed.status, 'succeeded');
  assert.match(await client.getArtifactMarkdown(parsed.result.artifactId), /npm start/);
  assert.ok((await client.getArtifactDocument(parsed.result.artifactId)).blocks.length > 0);
  const documents = await client.getDocumentsPage(dataset.id, { limit: 1, offset: 0 });
  assert.equal(documents.meta.total, 1);
  const profiles = await client.getIndexProfiles(kb.id);
  assert.equal((await client.getIndexProfile(profiles[0].id)).id, profiles[0].id);
  const releaseTask = await client.buildRelease(dataset.id, { activate: true });
  const release = await client.waitTask(releaseTask.id);
  assert.equal(release.status, 'succeeded');
  const model = await client.createModel({ name: 'Local client model', provider: 'local-extractive', modelId: 'ordo-local-extractive-v1' });
  const conversation = await client.createConversation({ title: 'Client query', knowledgeBaseId: kb.id, datasetId: dataset.id, modelConnectionId: model.id, releaseId: release.result.releaseId });
  const answer = await client.sendMessage(conversation.id, { question: 'Ordo installation', topK: 4 });
  assert.ok(answer.assistantMessage.citations.length > 0);
  const citation = await client.openCitation(answer.assistantMessage.citations[0].id);
  assert.equal(citation.releaseId, release.result.releaseId);
  const replay = await client.replayTrace(answer.trace.id, { fromStage: '问题解析', overrides: { topK: 4 } }, { idempotencyKey: 'client-replay' });
  const repeated = await client.replayTrace(answer.trace.id, { fromStage: '问题解析', overrides: { topK: 4 } }, { idempotencyKey: 'client-replay' });
  assert.equal(replay.trace.id, repeated.trace.id);
  assert.equal((await client.compareTraces(answer.trace.id, replay.trace.id)).stages.length, 8);
  const wiki = await client.saveMessageWiki(answer.assistantMessage.id, { title: 'Client wiki' });
  assert.equal((await client.getWikiPage(wiki.id)).id, wiki.id);
  const assistant = await client.createAssistant({ name: 'Client assistant', datasetId: dataset.id });
  await client.publishAssistant(assistant.id, { knowledgeReleaseId: release.result.releaseId });
  const widget = await client.createAssistantClient(assistant.id, { origins: ['https://example.test'] });
  assert.equal(widget.client.id, widget.id, 'existing credential modal expects a nested client');
  assert.equal((await client.getAssistantClients(assistant.id))[0].clientId, widget.clientId);
  assert.deepEqual((await client.getAssistantClients(assistant.id))[0].origins, ['https://example.test']);
  const rotated = await client.rotateWidgetClient(widget.id);
  const origin = 'https://example.test';
  const rawBody = JSON.stringify({ origin });
  const timestamp = Date.now();
  const nonce = 'client-test-nonce-123456';
  const token = await client.issueWidgetToken(rawBody, { headers: {
    origin, 'x-ordo-client': widget.clientId, 'x-ordo-timestamp': String(timestamp), 'x-ordo-nonce': nonce,
    'x-ordo-signature': sign(rotated.clientSecret, canonicalRequest('POST', '/api/v1/public/widget/token', timestamp, nonce, origin, rawBody))
  } });
  const publicOptions = { token: token.token, headers: { origin } };
  const visitor = await client.createWidgetSession({ origin }, publicOptions);
  const visitorAnswer = await client.sendWidgetMessage(visitor.id, { question: 'Ordo installation' }, publicOptions);
  assert.ok(visitorAnswer.citations.length > 0);
  const handoff = await client.requestWidgetHandoff(visitor.id, { summary: 'Client integration handoff' }, publicOptions);
  assert.equal((await client.getHandoffs({ status: 'queued' }))[0].id, handoff.id);
  assert.equal((await client.updateHandoff(handoff.id, { status: 'accepted' })).status, 'accepted');
  await client.deleteWidgetSession(visitor.id, {}, publicOptions);
  assert.equal((await client.revokeWidgetClient(widget.id)).status, 'revoked');
  const backup = await client.waitTask((await client.createBackup({ label: 'client-test' })).id);
  assert.equal(backup.status, 'succeeded');
  const restore = await client.restoreBackup(backup.result.backupId, { targetRoot: restoreRoot });
  assert.equal((await client.waitTask(restore.id)).result.integrity, 'ok');
  assert.equal((await client.verifyAudit()).valid, true);
  assert.equal(await client.getTraceEmbedStage(answer.trace.id, {}, { throwOnError: false }), null);
  assert.equal(client.lastError.code, 'ROUTE_NOT_FOUND');
});
