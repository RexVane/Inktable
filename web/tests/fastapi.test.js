'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { createClient, operations } = require('../api');
const { createWorkbench } = require('./helpers/workbench');

const pageIds = [
  'home', 'knowledge/config', 'knowledge/datasets', 'knowledge/registry', 'knowledge/parsing', 'knowledge/index',
  'qaflow/parse', 'qaflow/embed', 'qaflow/route', 'qaflow/recall', 'qaflow/fuse', 'qaflow/rerank', 'qaflow/prompt', 'qaflow/answer',
  'apps/chat', 'apps/assistants', 'settings/general', 'settings/models', 'settings/storage', 'settings/version'
];
const sampleContent = /用户手册_产品A|ds-demo-|产品使用文档 \(演示\)|如何为企业网站安装产品问答助手|获取安装代码|产品问答助手_安装指南/;
const escapeHtml = value => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);

test('unchanged browser client talks to the real Python/FastAPI HTTP service', { timeout: 60000 }, async () => {
  const root = path.resolve(__dirname, '../..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'ordo-fastapi-'));
  const probe = net.createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const localPython = path.join(root, 'serverpy/.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const python = process.env.ORDO_TEST_PYTHON || (fs.existsSync(localPython) ? localPython : 'python');
  const child = spawn(python, [path.join(root, 'ordo.py'), 'serve'], {
    cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, ORDO_HOST: '127.0.0.1', ORDO_PORT: String(port), ORDO_DATA_DIR: path.join(temp, 'data') }
  });
  let output = '';
  child.stdout.on('data', chunk => { output += chunk; });
  child.stderr.on('data', chunk => { output += chunk; });
  const exited = once(child, 'exit');
  const origin = `http://127.0.0.1:${port}`;
  try {
    let ready = false;
    for (let i = 0; i < 150; i++) {
      if (child.exitCode !== null) throw new Error(output);
      try { ready = (await fetch(origin + '/api/v1/health')).ok; } catch {}
      if (ready) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(ready, output);
    let cookie = '';
    let unavailablePath = null;
    const client = createClient({ baseUrl: origin, throwOnError: true, fetch: async (url, options = {}) => {
      if (new URL(url).pathname === unavailablePath) {
        return new Response(JSON.stringify({ error: { code: 'STAGE_TEST_UNAVAILABLE', message: 'Stage record temporarily unavailable' } }), {
          status: 503, headers: { 'Content-Type': 'application/json' }
        });
      }
      const headers = new Headers(options.headers);
      if (cookie && options.credentials !== 'omit') headers.set('cookie', cookie);
      const response = await fetch(url, { ...options, headers });
      const setCookie = response.headers.get('set-cookie');
      if (setCookie) cookie = setCookie.split(';')[0];
      return response;
    } });
    await client.bootstrapSession();
    const version = await client.getVersion();
    assert.equal(version.runtime, 'Python/FastAPI');
    const schema = await client.getOpenApi();
    for (const spec of Object.values(operations)) {
      const route = spec.path.replace(/:\w+/g, '{id}');
      assert.ok(Object.entries(schema.paths).some(([key, methods]) => key.replace(/\{\w+\}/g, '{id}') === route && methods[spec.method.toLowerCase()]));
    }
    const kb = await client.createKnowledgeBase({ name: 'Browser HTTP integration' });
    const dataset = kb.default_dataset_id;
    const workbench = await createWorkbench(client, { origin });
    for (const route of pageIds) {
      const html = await workbench.page(route);
      assert.doesNotMatch(html, /页面加载出错/, route + ': ' + workbench.errors.join('\n'));
      assert.doesNotMatch(html, sampleContent, route);
      assert.ok(html.length > 500, route);
    }
    const documentName = 'boreal-calibration.md';
    const documentText = '# Boreal-17 calibration record\n\nBoreal-17 calibration interval is 47 hours. Calibration code is CAL-9472.\n\n## Quarantine threshold\n\nThe Boreal-17 sensor tolerance is 0.42 kelvin. A reading beyond that tolerance requires quarantine review under ticket QA-284.';
    const firstQuestion = 'What is the Boreal-17 calibration interval?';
    const secondQuestion = 'Which Boreal-17 sensor tolerance requires quarantine?';
    const uploaded = await client.uploadDocument(dataset, new File([documentText], documentName, { type: 'text/markdown' }));
    assert.equal((await client.waitTask(uploaded.task.id, 30000)).status, 'succeeded');
    const build = await client.buildRelease(dataset, { activate: true });
    assert.equal((await client.waitTask(build.id, 30000)).status, 'succeeded');
    const conversation = await client.createConversation('Browser test', kb.id, dataset);
    const events = [];
    const answer = await client.sendMessageStream(conversation.id, firstQuestion, (name, value) => events.push({ name, value }));
    assert.ok(answer.assistantMessage.citations.length);
    assert.ok(events.some(event => event.name === 'token'));
    assert.equal(events.filter(event => event.name === 'done').length, 1);
    await client.openCitation(answer.assistantMessage.citations[0].id);
    workbench.context.ordoState.activeTraceId = answer.trace.id;
    for (const route of pageIds) {
      const html = await workbench.page(route);
      assert.doesNotMatch(html, /页面加载出错/, route + ': ' + workbench.errors.join('\n'));
      assert.doesNotMatch(html, sampleContent, route);
      assert.ok(html.length > 500, route);
    }
    await workbench.context.handleSwitchConversation(conversation.id);
    assert.equal(workbench.context.ordoState.chatMessages.at(-1).citations[0].citationId, answer.assistantMessage.citations[0].id);
    await workbench.page('knowledge/datasets');
    await workbench.context.handleCreateDatasetFolder('真实目录');
    const folder = (await client.getDatasetTree(dataset)).find(item => item.name === '真实目录');
    assert.ok(folder);
    await client.moveDatasetFile(dataset, uploaded.document.id, { folderId: folder.id });
    await workbench.context.handleSelectDatasetFolder(folder.id);
    const folderFiles = await client.getDatasetFiles(dataset, { folderId: folder.id });
    assert.equal(folderFiles.length, 1, JSON.stringify(folderFiles));
    assert.ok(workbench.element('body').innerHTML.includes(documentName), JSON.stringify({ dataset, folder, selected: workbench.context.ordoState.selectedDatasetId, selectedFolder: workbench.context.ordoState.selectedFolder, query: workbench.context.ordoState.datasetSearchQuery }));
    assert.match(workbench.element('body').innerHTML, /共 1 条文档/);
    await workbench.context.toggleAutoParsing(false);
    assert.equal((await client.getParsingSettings()).autoParsingEnabled, false);
    assert.deepEqual(workbench.errors, []);
    const sourceHtml = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
    assert.equal(await (await fetch(origin + '/')).text(), sourceHtml);
    assert.equal(await (await fetch(origin + '/app.css')).text(), fs.readFileSync(path.join(root, 'web/app.css'), 'utf8'));
    assert.equal((await client.verifyAudit()).valid, true);
  } finally {
    child.kill();
    await exited;
    const resolved = fs.realpathSync(temp);
    assert.equal(path.dirname(resolved).toLowerCase(), fs.realpathSync(os.tmpdir()).toLowerCase());
    assert.ok(path.basename(resolved).startsWith('ordo-fastapi-'));
    await fs.promises.rm(resolved, { recursive: true, force: true, maxRetries: 20, retryDelay: 100 });
  }
});
