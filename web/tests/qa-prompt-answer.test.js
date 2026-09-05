'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');
const pages = source.slice(source.indexOf('  function qaPromptAnswerValue('), source.indexOf('  /* Global Chat Interaction Handlers */'));
const handler = name => {
  const start = source.indexOf(`  window.${name} = `);
  assert.notEqual(start, -1);
  return source.slice(start, source.indexOf('\n  };', start) + 5);
};

function fixture() {
  const calls = [], toasts = [], copies = [], downloads = [];
  const elements = new Map();
  const trace = { id: 'trace-real', query: 'Unique evidence question', created_at: '2026-09-06T01:02:03Z', status: 'succeeded', metrics: { totalMs: 0 } };
  const messages = [{ role: 'system', content: 'Actual system <script>untrusted</script>' }, { role: 'user', content: 'Prior real question' }, { role: 'assistant', content: 'Prior real answer' }, { role: 'user', content: trace.query + '\n\n[1] Actual evidence' }];
  const prompt = { traceId: trace.id, query: trace.query, status: 'succeeded', dataSource: 'recorded', durationMs: 0, messages, draft: {}, output: { messages, evidenceCount: 1, historyCount: 2, maxEvidenceChars: 500, templateVersion: 'recorded-v7', security: { evidenceTreatedAsUntrusted: true } }, evidence: [{ ordinal: 1, title: 'Unique source', content: 'Actual evidence', chunkRevisionId: 'chunk-real' }], pipeline: [] };
  const generation = { traceId: trace.id, query: trace.query, status: 'succeeded', dataSource: 'recorded', durationMs: 0, answer: 'Actual answer [1]', citations: [{ id: 'cite-real', ordinal: 1, title: 'Unique source', excerpt: 'Actual evidence', chunk_revision_id: 'chunk-real' }], output: { modelId: 'real-model', provider: 'real-provider', usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 }, degraded: false }, pipeline: [] };
  const responses = {
    getTracePromptStage: prompt, getTraceGenerationStage: generation, getTrace: trace,
    updateTracePrompt: { saved: true, version: 2, applied: false },
    scanPromptSensitiveData: { method: 'local-patterns-v1', count: 0, matches: [] },
    maskPrompt: { saved: true, version: 3, maskedCount: 1, messages: [{ role: 'user', content: '[EMAIL]' }] },
    getPromptVersions: [{ version: 2, created_at: trace.created_at, config: { instructions: 'Saved addition' } }],
    openCitation: { title: 'Unique source', contentText: 'Actual source body', releaseId: 'release-real', locationLabel: 'page 7' },
    saveTraceQa: { id: 'wiki-real', status: 'draft' }, sendTraceFeedback: { id: 'fb-real', rating: 1 },
    regenerateAnswer: { trace: { ...trace, id: 'trace-derived', parent: trace.id, conversation_id: 'conv-real' } },
    getConversation: { id: 'conv-real', messages: [{ role: 'assistant', content: 'Regenerated answer', trace_id: 'trace-derived' }] }
  };
  const api = { connected: true, formatLocator: () => 'page 7' };
  for (const method of Object.keys(responses)) api[method] = async (...args) => {
    calls.push({ method, args });
    const response = responses[method];
    if (response instanceof Error) throw response;
    return response;
  };
  const state = { page: 'qaflow/prompt', activeTraceId: trace.id, chatMessages: [] };
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const context = vm.createContext({
    api, state, esc: escape, jsArg: value => escape(JSON.stringify(value)),
    getActiveQATrace: async () => ({ traces: [trace], activeTrace: trace }),
    emptyQATrace: () => ({ html: 'empty' }), renderQATraceBanner: () => '', renderQATraceHeader: () => '',
    flowNames: Array.from({ length: 8 }, (_, index) => 'stage ' + index), flowRoutes: ['parse', 'embed', 'route', 'recall', 'fuse', 'rerank', 'prompt', 'answer'],
    render: async () => {}, showToast: (message, tone) => toasts.push({ message, tone }),
    showOverlay: html => { context.overlay = html; }, closeOverlay: () => { context.overlay = ''; },
    requireConnection: () => api.connected, mapChatMessage: message => ({ ...message, text: message.content, traceId: message.trace_id }),
    navigator: { clipboard: { writeText: async text => { copies.push(text); } } },
    triggerDownloadFile: (filename, data, mime) => downloads.push({ filename, data, mime }),
    document: { getElementById: id => elements.get(id) }, console
  });
  for (const name of ['Shield', 'User', 'Chat', 'Help', 'Doc', 'Zap', 'Copy', 'Edit', 'Save', 'ThumbUp', 'ThumbDown']) context['icon' + name] = () => '<i></i>';
  context.window = context;
  vm.runInContext(pages + '\n' + ['openPromptTemplateModal', 'handleCopyFullAnswer', 'handleAnswerFeedback', 'handleRegenerateAnswer'].map(handler).join('\n'), context);
  return { context, state, trace, prompt, generation, responses, calls, toasts, copies, downloads, elements };
}

test('prompt page renders recorded messages, history and evidence without sample budgets', async () => {
  const f = fixture();
  const page = await f.context.pageQA13_Prompt();
  assert.match(page.html, /id="qaPromptPreview"/);
  for (const text of ['Actual system', 'Prior real question', 'Prior real answer', 'Actual evidence', 'recorded-v7']) assert.ok(page.html.includes(text));
  assert.match(page.html, /&lt;script&gt;untrusted&lt;\/script&gt;/);
  assert.doesNotMatch(page.html, /<script>untrusted/);
  assert.doesNotMatch(page.html, /prompt-v12|8,000|4,210|GPT-5/);
  assert.match(page.html, /\u672a\u8bb0\u5f55/);
  assert.equal(f.calls[0].method, 'getTracePromptStage');
  assert.equal(f.calls[0].args[2].throwOnError, true);
});

test('unrecorded prompt has no invented prompt or success scan and disables content actions', async () => {
  const f = fixture();
  Object.assign(f.prompt, { messages: [], evidence: [], output: {}, dataSource: 'unavailable', status: 'failed' });
  const page = await f.context.pageQA13_Prompt();
  assert.match(page.html, /id="qaPromptPreview"[^>]*>\u672a\u8bb0\u5f55/);
  assert.match(page.html, /disabled onclick="window.handleQAPromptCopy/);
  assert.match(page.html, /\u672a\u626b\u63cf/);
  await f.context.handleQAPromptCopy();
  assert.equal(f.copies.length, 0);
  assert.equal(f.toasts.at(-1).tone, 'error');
});

test('generation page preserves real zero usage and zero duration', async () => {
  const f = fixture();
  const page = await f.context.pageQA14_Answer();
  for (const text of ['qaFinalAnswer', 'Actual answer [1]', 'Unique source', 'real-provider', 'real-model', '0 ms']) assert.ok(page.html.includes(text));
  assert.doesNotMatch(page.html, /1,624|2,266|379 ms|96%|v3.2.1|GPT-5/);
  assert.match(page.html, /\u603b Tokens<\/span><span[^>]*>0<\/span>/);
  assert.match(page.html, /\u8bc1\u636e\u8986\u76d6\u7387<\/span><span[^>]*>\u672a\u8bb0\u5f55/);
});

test('local generation reports unavailable usage and empty answer without an example fallback', async () => {
  const f = fixture();
  Object.assign(f.generation, { answer: '', citations: [], output: { usage: null, usageStatus: 'not_applicable' } });
  const page = await f.context.pageQA14_Answer();
  assert.match(page.html, /id="qaFinalAnswer"[^>]*>\u672a\u8bb0\u5f55/);
  assert.match(page.html, /\u4e0d\u9002\u7528\uff08\u672c\u5730\u62bd\u53d6\uff09/);
  assert.match(page.html, /\u5f15\u7528\u4e0e\u8bc1\u636e \(0\)/);
  assert.doesNotMatch(page.html, /\u5b89\u88c5\u4ee3\u7801|3 \/ 3/);
});

test('stage API failures propagate and clear stale stage records', async () => {
  const f = fixture();
  await f.context.pageQA13_Prompt();
  f.responses.getTracePromptStage = new Error('503 prompt failed');
  await assert.rejects(f.context.pageQA13_Prompt(), /503 prompt failed/);
  assert.equal(f.state.qaPromptRecord, null);
  await f.context.pageQA14_Answer();
  f.responses.getTraceGenerationStage = new Error('503 generation failed');
  await assert.rejects(f.context.pageQA14_Answer(), /503 generation failed/);
  assert.equal(f.state.qaGenerationRecord, null);
});

test('copy uses exact recorded data and clipboard rejection never reports success', async () => {
  const f = fixture();
  await f.context.pageQA13_Prompt();
  await f.context.handleQAPromptCopy();
  assert.match(f.copies[0], /^\[system\]\nActual system/);
  await f.context.pageQA14_Answer();
  await f.context.handleCopyFullAnswer();
  assert.equal(f.copies[1], f.generation.answer);
  f.context.navigator.clipboard.writeText = async () => { throw new Error('Clipboard denied'); };
  await f.context.handleCopyFullAnswer();
  assert.deepEqual(f.toasts.at(-1), { message: 'Clipboard denied', tone: 'error' });
});

test('prompt edits save a versioned draft and validate limits before sending', async () => {
  const f = fixture();
  await f.context.pageQA13_Prompt();
  f.context.openPromptTemplateModal();
  assert.match(f.context.overlay, /qaPromptInstructions/);
  f.elements.set('qaPromptInstructions', { value: 'Append actual instruction' });
  f.elements.set('qaPromptEvidenceLimit', { value: '0' });
  f.elements.set('qaPromptMaskSensitive', { checked: true });
  await f.context.handleQAPromptSave();
  assert.equal(f.calls.filter(call => call.method === 'updateTracePrompt').length, 0);
  f.elements.set('qaPromptEvidenceLimit', { value: '400' });
  await f.context.handleQAPromptSave();
  const save = f.calls.find(call => call.method === 'updateTracePrompt');
  assert.equal(save.args[0], f.trace.id);
  assert.equal(save.args[1].instructions, 'Append actual instruction');
  assert.equal(save.args[1].maxEvidenceChars, 400);
  assert.equal(save.args[1].maskSensitive, true);
  assert.match(f.toasts.at(-1).message, /\u5c1a\u672a\u5e94\u7528/);
  f.responses.updateTracePrompt = new Error('draft write denied');
  await f.context.handleQAPromptSave();
  assert.deepEqual(f.toasts.at(-1), { message: 'draft write denied', tone: 'error' });
});

test('sensitive scan, masking and versions use API results without mutating the recorded preview', async () => {
  const f = fixture();
  await f.context.pageQA13_Prompt();
  const preview = f.state.qaPromptRecord.promptText;
  await f.context.handleQAPromptScan();
  assert.equal(f.state.qaPromptScan.result.count, 0);
  assert.match(f.context.overlay, /local-patterns-v1/);
  await f.context.handleQAPromptMask();
  assert.equal(f.state.qaPromptRecord.promptText, preview);
  assert.match(f.context.overlay, /\[EMAIL\]/);
  assert.match(f.context.overlay, /\u5c1a\u672a\u5e94\u7528/);
  await f.context.handleQAPromptVersions();
  assert.match(f.context.overlay, /Saved addition/);
  assert.match(f.context.overlay, /v2/);
});

test('exports contain recorded prompt and actual trace report', async () => {
  const f = fixture();
  await f.context.pageQA13_Prompt();
  f.context.handleQAPromptExport();
  assert.equal(JSON.parse(f.downloads[0].data).messages[0].content, f.prompt.messages[0].content);
  await f.context.pageQA14_Answer();
  await f.context.handleQAAnswerReport();
  const report = JSON.parse(f.downloads[1].data);
  assert.equal(report.trace.id, f.trace.id);
  assert.equal(report.generation.answer, f.generation.answer);
});

test('answer source details reject other traces and save only real Wiki drafts and feedback', async () => {
  const f = fixture();
  await f.context.pageQA14_Answer();
  await f.context.handleQAAnswerCitation('cite-other');
  assert.equal(f.calls.filter(call => call.method === 'openCitation').length, 0);
  await f.context.handleQAAnswerCitation('cite-real');
  assert.match(f.context.overlay, /Actual source body/);
  await f.context.handleQAAnswerSave();
  assert.equal(f.state.qaAnswerSaved.id, 'wiki-real');
  assert.match(f.toasts.at(-1).message, /Wiki \u8349\u7a3f/);
  await f.context.handleAnswerFeedback('thumb_up');
  assert.equal(f.state.qaAnswerFeedback.rating, 1);
  assert.equal(f.toasts.at(-1).message, '\u53cd\u9988\u5df2\u4fdd\u5b58');
  f.responses.sendTraceFeedback = new Error('feedback denied');
  await f.context.handleAnswerFeedback('thumb_down');
  assert.equal(f.state.qaAnswerFeedback.rating, 1);
  assert.deepEqual(f.toasts.at(-1), { message: 'feedback denied', tone: 'error' });
});

test('regeneration creates a derived trace and syncs its conversation; failures do not advance selection', async () => {
  const f = fixture();
  f.state.activeConversationId = 'conv-real';
  await f.context.handleRegenerateAnswer();
  assert.equal(f.calls.find(call => call.method === 'regenerateAnswer').args[0], 'trace-real');
  assert.equal(f.state.activeTraceId, 'trace-derived');
  assert.equal(f.state.chatMessages[0].text, 'Regenerated answer');
  assert.equal(f.state.qaGenerationBusy, null);
  f.responses.regenerateAnswer = new Error('provider unavailable');
  await f.context.handleRegenerateAnswer();
  assert.equal(f.state.activeTraceId, 'trace-derived');
  assert.deepEqual(f.toasts.at(-1), { message: 'provider unavailable', tone: 'error' });
});
