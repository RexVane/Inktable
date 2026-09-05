'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');

function sourceOf(name, exported = false) {
  const pattern = exported ? `  window.${name} = ` : new RegExp(`  (?:async )?function ${name}\\(`);
  const start = typeof pattern === 'string' ? app.indexOf(pattern) : app.search(pattern);
  assert.ok(start >= 0, `Missing ${name}`);
  const end = app.indexOf(exported ? '\n  };' : '\n  }', start);
  assert.ok(end > start, `Missing end of ${name}`);
  return app.slice(start, end + (exported ? 5 : 4));
}

function setup(overrides = {}) {
  const trace = { id: 'trace-a', query: "What's <script>really</script> indexed?", conversation_id: 'conv-a', message_id: 'answer-a' };
  const parsed = {
    traceId: trace.id, status: 'succeeded', dataSource: 'recorded', durationMs: 0,
    output: {
      original: trace.query, normalized: 'Normalized real question', language: 'und', intent: 'knowledge_query',
      entities: ['IndexedItem'], filters: { datasetId: 'dataset-real', releaseId: 'release-real' }
    },
    contextSource: 'recorded', inputHistory: [], contextMessages: [{ role: 'user', content: trace.query }], draft: {}
  };
  const vector = [0.2, -0.4, 0];
  const embedding = {
    status: 'succeeded', dataSource: 'recorded', reconstructed: false, durationMs: 0,
    model: 'real-model', vector, dimensions: vector.length, query: trace.query,
    output: { model: 'real-model', inputHash: 'real-input-hash', vector }, cacheHit: null, cacheStatus: 'unrecorded'
  };
  const calls = [], toasts = [], elements = new Map(), copied = [];
  const api = {
    connected: true, context: {},
    async getTraceParseStage() { return parsed; },
    async getTracePipeline() { return { stages: [], totalMs: 0 }; },
    async getTraceEmbedStage() { return embedding; },
    async getEmbeddingVector() { return { model: embedding.model, vector, dimensions: vector.length, dataSource: 'recorded' }; },
    async getEmbeddingScatter() { return { method: 'fixed-linear-projection', reconstructed: true, points: [{ id: trace.id, label: 'query', x: 0.2, y: 0.4 }, { id: 'chunk-a', label: '<img onerror=bad()>', x: -0.5, y: -0.25 }] }; },
    async getConversation() { throw new Error('Live conversation must not replace recorded context'); },
    async updateTraceParse(id, input) { return { traceId: id, config: input, version: 4, saved: true, applied: false }; },
    async updateTraceRawJson(id, input) { return { traceId: id, config: input, version: 5, saved: true, applied: false }; },
    async reparseTrace() { return { derivedTraceId: 'trace-derived', trace: { id: 'trace-derived' } }; },
    async recomputeEmbedding() { return { model: 'real-model', vector: [0.3, 0.4], dimensions: 2, persisted: false }; },
    async compareEmbeddingModels() { return { results: [{ model: 'real-model', dimensions: 3, vector }], persisted: false }; },
    ...overrides
  };
  for (const [method, fn] of Object.entries(api)) {
    if (typeof fn !== 'function') continue;
    api[method] = async (...args) => { calls.push([method, ...args]); return fn(...args); };
  }
  const state = { activeTraceId: trace.id };
  const context = vm.createContext({
    state, api, Number, JSON, Promise,
    esc(value) { return String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]); },
    async getActiveQATrace() { return { traces: [trace], activeTrace: trace }; },
    renderQATraceBanner() { return ''; }, renderQATraceHeader() { return ''; }, emptyQATrace() { return { html: '' }; },
    showToast(message, tone) { toasts.push({ message, tone }); },
    showOverlay(html) { context.overlay = html; }, closeOverlay() { context.overlay = ''; },
    async render() { context.renders = (context.renders || 0) + 1; }, confirm() { return true; },
    document: { getElementById(id) { if (!elements.has(id)) elements.set(id, { value: '' }); return elements.get(id); } }
  });
  context.window = context;
  context.handleCopySnippet = async text => { copied.push(text); };
  for (const name of ['iconDoc', 'iconTarget', 'iconTag', 'iconClock', 'iconLock', 'iconCopy', 'iconEdit', 'iconRoute', 'iconCube', 'iconPulse', 'iconLayers', 'iconChat', 'iconRefresh', 'iconChart']) context[name] = () => '<svg></svg>';
  const functions = ['qaParseEmbedText', 'qaParseEmbedRequest', 'qaParseEmbedRecord', 'pageQA07_Parse', 'pageQA08_Embed'];
  const handlers = ['openEditParseResultModal', 'handleSaveParsedResult', 'openModelComparisonModal', 'handleReVectorize', 'handleConfirmReVectorize', 'handleReParse', 'handleSaveParseDraft', 'handleCopyParseResult', 'handleCopyEmbeddingVector', 'handleEditJSON', 'handleViewFullContext'];
  vm.runInContext(functions.map(name => sourceOf(name)).concat(handlers.map(name => sourceOf(name, true))).join('\n'), context);
  return { context, state, trace, parsed, embedding, calls, toasts, copied, elements, api };
}

test('parse renders recorded fields and immutable context without invented example values', async () => {
  const fixture = setup();
  fixture.parsed.inputHistory = [{ role: 'user', content: 'Earlier question' }, { role: 'assistant', content: 'Earlier answer' }];
  fixture.parsed.contextMessages = [...fixture.parsed.inputHistory, { role: 'user', content: fixture.trace.query }];
  const page = await fixture.context.pageQA07_Parse();
  assert.match(page.html, /Normalized real question/);
  assert.match(page.html, /Earlier question/);
  assert.match(page.html, /Earlier answer/);
  assert.match(page.html, /&lt;script&gt;really&lt;\/script&gt;/);
  assert.doesNotMatch(page.html, /<script>|widget\.js|2025-05-20|218 ms|width:92%|width:88%/);
  assert.equal(fixture.state.qaParseRecord.fields.rewrittenQuery, undefined);
  assert.equal(fixture.calls.filter(call => call[0] === 'getConversation').length, 0);
  fixture.context.handleViewFullContext();
  assert.match(fixture.context.overlay, /Earlier question/);
  assert.doesNotMatch(fixture.context.overlay, /<script>/);
  await fixture.context.handleCopyParseResult('normalizedQuery');
  assert.equal(fixture.copied[0], 'Normalized real question');
  await fixture.context.handleCopyParseResult();
  assert.deepEqual(JSON.parse(fixture.copied[1]), fixture.parsed.output);
});

test('unrecorded context is not reconstructed from a later live conversation', async () => {
  const fixture = setup();
  fixture.parsed.contextSource = 'unavailable';
  fixture.parsed.contextMessages = [{ role: 'user', content: fixture.trace.query }];
  await fixture.context.pageQA07_Parse();
  assert.equal(fixture.state.qaParseRecord.context.available, false);
  assert.equal(fixture.calls.filter(call => call[0] === 'getConversation').length, 0);
  fixture.context.handleViewFullContext();
  assert.doesNotMatch(fixture.context.overlay, /What's|Earlier|Future/);
});

test('legacy conversation fallback stops at the selected trace question', async () => {
  const fixture = setup({ getConversation: async () => ({ messages: [
    { role: 'user', content: 'Earlier question' }, { role: 'assistant', content: 'Earlier answer' },
    { role: 'user', content: 'Current question' }, { id: 'answer-a', role: 'assistant', trace_id: 'trace-a', content: 'Current answer' },
    { role: 'user', content: 'Future question' }, { role: 'assistant', content: 'Future answer' }
  ] }) });
  delete fixture.parsed.contextSource;
  const page = await fixture.context.pageQA07_Parse();
  assert.match(page.html, /Earlier question/);
  assert.doesNotMatch(page.html, /Future question|Future answer/);
  fixture.context.handleViewFullContext();
  assert.match(fixture.context.overlay, /Current question/);
  assert.doesNotMatch(fixture.context.overlay, /Current answer|Future question|Future answer/);
});

test('parse drafts are saved through the API and errors retain the edit session', async () => {
  const fixture = setup();
  await fixture.context.pageQA07_Parse();
  fixture.context.openEditParseResultModal();
  assert.match(fixture.context.overlay, /Normalized real question/);
  fixture.context.document.getElementById('editNormQuestion').value = 'Edited normalized question';
  fixture.context.document.getElementById('editMainIntent').value = 'procedure';
  fixture.context.document.getElementById('editEntities').value = 'one, two';
  fixture.context.document.getElementById('editRewrittenQuery').value = 'Edited rewrite';
  const result = await fixture.context.handleSaveParsedResult();
  assert.equal(result.saved, true);
  const call = fixture.calls.find(item => item[0] === 'updateTraceParse');
  assert.equal(call[1], 'trace-a');
  assert.equal(call[2].normalizedQuery, 'Edited normalized question');
  assert.deepEqual(Array.from(call[2].entities), ['one', 'two']);
  assert.equal(fixture.parsed.output.normalized, 'Normalized real question');
  assert.equal(fixture.toasts.at(-1).tone, 'ok');
  fixture.context.handleEditJSON();
  fixture.context.document.getElementById('qaParseRawJson').value = '{bad';
  await fixture.context.handleSaveParseDraft(null, true);
  assert.equal(fixture.toasts.at(-1).tone, 'error');
  assert.ok(fixture.context.overlay);
  assert.equal(fixture.calls.filter(item => item[0] === 'updateTraceRawJson').length, 0);
  fixture.context.document.getElementById('qaParseRawJson').value = '{"intent":"changed"}';
  fixture.api.updateTraceRawJson = async () => { throw new Error('write denied'); };
  await fixture.context.handleSaveParseDraft(null, true);
  assert.match(fixture.toasts.at(-1).message, /write denied/);
  assert.ok(fixture.context.overlay);
});

test('parse reruns select a returned derived trace and do not claim failed requests succeeded', async () => {
  const fixture = setup();
  await fixture.context.pageQA07_Parse();
  await fixture.context.handleReParse();
  assert.equal(fixture.state.activeTraceId, 'trace-derived');
  assert.equal(fixture.toasts.at(-1).tone, 'ok');
  const failed = setup({ reparseTrace: async () => { throw new Error('replay unavailable'); } });
  await failed.context.pageQA07_Parse();
  await failed.context.handleReParse();
  assert.equal(failed.state.activeTraceId, 'trace-a');
  assert.equal(failed.toasts.at(-1).tone, 'error');
  assert.match(failed.toasts.at(-1).message, /replay unavailable/);
});

test('embedding previews and projections use real numeric data and copy the complete vector', async () => {
  const fixture = setup();
  const page = await fixture.context.pageQA08_Embed();
  assert.match(page.html, /real-model/);
  assert.match(page.html, /real-input-hash/);
  assert.match(page.html, /0\.2000/);
  assert.match(page.html, /-0\.4000/);
  assert.match(page.html, /0\.4472/);
  assert.match(page.html, /fixed-linear-projection/);
  assert.match(page.html, /cx="188" cy="62"/);
  assert.match(page.html, /&lt;img onerror=bad\(\)&gt;/);
  assert.doesNotMatch(page.html, /text-embedding-3-large|1536|vec_q_7f3b|0\.8621|c9d13a8f4b7e2a91|<img/);
  await fixture.context.handleCopyEmbeddingVector();
  assert.deepEqual(JSON.parse(fixture.copied[0]), [0.2, -0.4, 0]);
  fixture.context.handleReVectorize();
  await fixture.context.handleConfirmReVectorize();
  assert.equal(fixture.state.qaEmbedComputedResult.persisted, false);
  assert.deepEqual(Array.from(fixture.state.qaEmbedRecord.vector), [0.2, -0.4, 0]);
  await fixture.context.handleCopyEmbeddingVector(true);
  assert.deepEqual(JSON.parse(fixture.copied[1]), [0.3, 0.4]);
  await fixture.context.openModelComparisonModal();
  assert.match(fixture.context.overlay, /real-model/);
  assert.doesNotMatch(fixture.context.overlay, /bge-large|text2vec|64\.6|35 ms/);
});

test('stage and auxiliary API failures are visible, with no fabricated fallback vector', async () => {
  const fail = async () => { throw new Error('offline'); };
  const fixture = setup({ getTraceParseStage: fail, getTraceEmbedStage: fail, getEmbeddingVector: fail, getEmbeddingScatter: fail });
  const parse = await fixture.context.pageQA07_Parse();
  assert.match(parse.html, /role="alert"/);
  assert.match(parse.html, /offline/);
  fixture.context.openEditParseResultModal();
  assert.equal(fixture.toasts.at(-1).tone, 'error');
  const embed = await fixture.context.pageQA08_Embed();
  assert.match(embed.html, /role="alert"/);
  assert.match(embed.html, /offline/);
  assert.equal(fixture.state.qaEmbedRecord.vector.length, 0);
  assert.doesNotMatch(embed.html, /1536|128|0\.1234|1\.0000|text-embedding-3-large/);
  await fixture.context.handleCopyEmbeddingVector();
  assert.equal(fixture.toasts.at(-1).tone, 'error');
  assert.equal(fixture.copied.length, 0);
});

test('trace switching prevents stale parse edits and vector copies', async () => {
  const fixture = setup();
  await fixture.context.pageQA07_Parse();
  fixture.context.openEditParseResultModal();
  fixture.state.activeTraceId = 'trace-other';
  await fixture.context.handleSaveParsedResult();
  assert.equal(fixture.calls.filter(call => call[0] === 'updateTraceParse').length, 0);
  assert.equal(fixture.toasts.at(-1).tone, 'error');
  fixture.state.activeTraceId = 'trace-a';
  await fixture.context.pageQA08_Embed();
  fixture.context.handleReVectorize();
  fixture.state.activeTraceId = 'trace-other';
  await fixture.context.handleConfirmReVectorize();
  await fixture.context.handleCopyEmbeddingVector();
  assert.equal(fixture.calls.filter(call => call[0] === 'recomputeEmbedding').length, 0);
  assert.equal(fixture.copied.length, 0);
  assert.equal(fixture.toasts.at(-1).tone, 'error');
});

test('reconstruction provenance and operation failures remain explicit', async () => {
  const fixture = setup({
    getEmbeddingVector: async () => ({ vector: [1, 0], model: 'legacy-local', reconstructed: true, dataSource: 'reconstructed' }),
    recomputeEmbedding: async () => { throw new Error('provider unavailable'); },
    compareEmbeddingModels: async () => { throw new Error('comparison unavailable'); }
  });
  await fixture.context.pageQA08_Embed();
  assert.equal(fixture.state.qaEmbedRecord.reconstructed, true);
  fixture.context.handleReVectorize();
  await fixture.context.handleConfirmReVectorize();
  assert.equal(fixture.state.qaEmbedComputedResult, undefined);
  assert.match(fixture.toasts.at(-1).message, /provider unavailable/);
  await fixture.context.openModelComparisonModal();
  assert.match(fixture.toasts.at(-1).message, /comparison unavailable/);
  fixture.context.handleCopySnippet = async () => { throw new Error('clipboard denied'); };
  await fixture.context.handleCopyEmbeddingVector();
  assert.equal(fixture.toasts.at(-1).tone, 'error');
  assert.match(fixture.toasts.at(-1).message, /clipboard denied/);
});
