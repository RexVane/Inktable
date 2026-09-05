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
    const client = createClient({ baseUrl: origin + '/api/v1', throwOnError: true, fetch: async (url, options = {}) => {
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
    const uploaded = await client.uploadDocument(dataset, new File(['# Guide\n\nOrdo runs on Python FastAPI.\n\n## Usage\n\nUpload documents and activate a knowledge release.'], 'guide.md', { type: 'text/markdown' }));
    assert.equal((await client.waitTask(uploaded.task.id, 30000)).status, 'succeeded');
    const build = await client.buildRelease(dataset, { activate: true });
    assert.equal((await client.waitTask(build.id, 30000)).status, 'succeeded');
    const conversation = await client.createConversation('Browser test', kb.id, dataset);
    const events = [];
    const answer = await client.sendMessageStream(conversation.id, 'What runs Ordo?', (name, value) => events.push({ name, value }));
    assert.ok(answer.assistantMessage.citations.length);
    assert.ok(events.some(event => event.name === 'token'));
    assert.equal(events.filter(event => event.name === 'done').length, 1);
    await client.openCitation(answer.assistantMessage.citations[0].id);
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
    fs.rmSync(resolved, { recursive: true, force: true, maxRetries: 8, retryDelay: 200 });
  }
});
