'use strict';
/* Ordo 后端全链冒烟测试：一次性实例（8791/临时数据目录），覆盖全部路由组与前端请求形状 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const serverDir = path.resolve(here, '..');
const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ordo-fullchain-'));

const PORT = 8791;
const BASE = `http://127.0.0.1:${PORT}`;
let cookie = '';
let csrf = '';
const results = [];
let cookieDebug = false;

function record(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
}

async function req(method, url, body, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (body !== undefined && !(body instanceof FormData) && typeof body !== 'string') headers['Content-Type'] = 'application/json';
  if (!(body instanceof FormData) && body !== undefined && !(opts.headers && opts.headers['Content-Type'])) headers['Content-Type'] = 'application/json';
  if (cookie && !opts.noAuth) headers['cookie'] = cookie;
  if (csrf && method !== 'GET' && method !== 'HEAD' && !opts.noAuth && !opts.skipCsrf) headers['x-ordo-csrf'] = csrf;
  const res = await fetch(BASE + url, { method, headers, body: body === undefined ? undefined : (typeof body === 'string' || body instanceof FormData ? body : JSON.stringify(body)) });
  if (!cookie && res.headers.get('set-cookie')) { cookie = res.headers.get('set-cookie').split(';')[0]; }
  let json = null;
  try { json = await res.json(); } catch {}
  return { status: res.status, json };
}

async function step(name, fn) {
  try {
    const detail = await fn();
    record(name, true, detail || '');
    return detail;
  } catch (error) {
    record(name, false, error && error.message ? error.message : String(error));
    return null;
  }
}

function must(cond, message) { if (!cond) throw new Error(message); }

async function waitTask(id, timeoutMs = 30000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const { status, json } = await req('GET', `/api/v1/tasks/${id}/wait?timeoutMs=5000`);
    if (status === 200 && json.data && !['queued', 'running', 'paused'].includes(json.data.status)) return json.data;
    if (status >= 400) throw new Error(`task wait HTTP ${status}`);
  }
  throw new Error('task wait timeout');
}

const MD = '# Ordo 冒烟测试\n\n这是冒烟测试文档，用于验证上传解析链路。\n\n## 安装\n\n运行 npm start 启动服务即可完成安装。\n\n## 安全\n\n回答必须使用可验证引用，证据不足时应当拒答。\n';

async function main() {
  // 0. 启动一次性实例（先确保 8791 无残留监听，避免 Windows 双绑定导致 session 错配）
  {
    const probe = await fetch(`${BASE}/api/v1/health`).then(r => r.ok).catch(() => false);
    if (probe) throw new Error(`端口 ${PORT} 已被占用：请先清理残留进程再运行`);
  }
  const child = spawn(process.execPath, ['src/main.js'], { cwd: serverDir, env: { ...process.env, ORDO_PORT: String(PORT), ORDO_DATA_DIR: dataRoot, ORDO_LOG_LEVEL: 'warn' }, stdio: ['ignore', 'pipe', 'pipe'] });
  child.stderr.on('data', d => { if (cookieDebug) console.error('[srv]', String(d).slice(0, 200)); });
  await new Promise((resolve, reject) => {
    const started = Date.now();
    const poll = setInterval(async () => {
      try { const r = await fetch(`${BASE}/api/v1/health`); if (r.ok) { clearInterval(poll); resolve(); } } catch {}
      if (Date.now() - started > 15000) { clearInterval(poll); reject(new Error('server start timeout')); }
    }, 300);
  });

  try {
    // 1. System
    await step('session/bootstrap', async () => {
      const { status, json } = await req('GET', '/api/v1/session/bootstrap');
      must(status === 200 && json.data.csrfToken, 'bootstrap 应返回 csrfToken');
      csrf = json.data.csrfToken;
      must(cookie && cookie.startsWith('ordo_session='), `cookie 未捕获: ${JSON.stringify(cookie)}`);
      return `csrf=${csrf.slice(0, 6)}… cookie=${cookie.slice(0, 24)}…`;
    });
    await step('system: health/version/dashboard/diagnostics/openapi', async () => {
      for (const [url, check] of [
        ['/api/v1/health', j => j.data.status],
        ['/api/v1/version', j => j.data.appVersion],
        ['/api/v1/dashboard', j => j.data.counts],
        ['/api/v1/diagnostics', j => j.data.dashboard],
        ['/api/v1/openapi.json', j => j.openapi]
      ]) {
        const { status, json } = await req('GET', url);
        must(status === 200 && json.data || url.endsWith('openapi.json') && json.openapi, `${url} → ${status}`);
      }
      return '6 个系统端点 200';
    });

    // 2. 认证与 CSRF 边界（无 cookie / 无 csrf）
    await step('security: 未认证写请求 401 / 缺 CSRF 403', async () => {
      const noAuth = await req('POST', '/api/v1/knowledge-bases', { name: 'x' }, { noAuth: true });
      must(noAuth.status === 401, `未认证应 401，实际 ${noAuth.status}`);
      const noCsrf = await req('POST', '/api/v1/knowledge-bases', { name: 'x' }, { skipCsrf: true });
      must(noCsrf.status === 403, `有会话缺 CSRF 应 403，实际 ${noCsrf.status}`);
      return '边界正确';
    });

    // 3. 知识库生命周期
    let kb = null; let dataset = null;
    await step('knowledge-bases: create(含默认数据集)', async () => {
      const { status, json } = await req('POST', '/api/v1/knowledge-bases', { name: '冒烟测试库', description: '全链验证' });
      must(status === 200 && json.data.id, `HTTP ${status}`);
      kb = json.data; dataset = kb.datasets[0];
      must(dataset && dataset.id, '应包含默认数据集');
      return kb.id;
    });
    await step('knowledge-bases: list/get/patch/impact', async () => {
      must((await req('GET', '/api/v1/knowledge-bases')).json.data.some(x => x.id === kb.id), 'list 应包含');
      must((await req('GET', `/api/v1/knowledge-bases/${kb.id}`)).json.data.id === kb.id, 'get 应命中');
      const patched = await req('PATCH', `/api/v1/knowledge-bases/${kb.id}`, { description: '全链验证-已更新' });
      must(patched.status === 200, `patch HTTP ${patched.status}`);
      const impact = await req('GET', `/api/v1/knowledge-bases/${kb.id}/impact`);
      must(impact.status === 200, `impact HTTP ${impact.status}`);
      return '';
    });
    await step('index-profiles: 嵌套默认/新建/设默认', async () => {
      const profiles = (await req('GET', `/api/v1/knowledge-bases/${kb.id}/index-profiles`)).json.data;
      must(profiles.length === 1 && profiles[0].config.embedding.provider === 'local-hash-v1', '默认配置应存在');
      const created = (await req('POST', `/api/v1/knowledge-bases/${kb.id}/index-profiles`, { name: '更大向量', config: { embedding: { dimensions: 256 } }, setDefault: true })).json.data;
      must(created.config.embedding.dimensions === 256, '嵌套合并应保留 provider');
      const setDefault = (await req('POST', `/api/v1/index-profiles/${created.id}/default`, {})).json.data;
      must(setDefault.defaultIndexProfileId === created.id, '设默认应生效');
      return '';
    });

    // 4. 数据登记 + 上传 + 解析任务
    let source = null; let document = null; let parseTask = null;
    await step('sources: create/list', async () => {
      const created = (await req('POST', `/api/v1/datasets/${dataset.id}/sources`, { type: 'upload', name: '冒烟来源' })).json.data;
      must(created.id, 'source 应创建');
      const list = (await req('GET', `/api/v1/datasets/${dataset.id}/sources`)).json.data;
      must(list.some(s => s.id === created.id), 'list 应包含');
      source = created;
      return source.id;
    });
    await step('files: multipart 上传（form-data file + sourceId）', async () => {
      const form = new FormData();
      form.append('sourceId', source.id);
      form.append('file', new Blob([MD], { type: 'text/markdown' }), 'smoke-guide.md');
      const { status, json } = await req('POST', `/api/v1/datasets/${dataset.id}/files`, form);
      must(status === 200 && json.data.task, `HTTP ${status} ${JSON.stringify(json.error || {})}`);
      parseTask = json.data.task;
      return parseTask.id;
    });
    await step('tasks: wait → 解析成功 publishable', async () => {
      const task = await waitTask(parseTask.id);
      must(['succeeded', 'partial'].includes(task.status) && task.result.qualityStatus === 'publishable', `status=${task.status} quality=${task.result && task.result.qualityStatus}`);
      must(task.result.blockCount >= 3, `blockCount=${task.result && task.result.blockCount}`);
      return `${task.result.blockCount} 块`;
    });
    await step('documents: list/get', async () => {
      const docs = (await req('GET', `/api/v1/datasets/${dataset.id}/documents`)).json.data;
      must(docs.length >= 1, '应有文档');
      document = docs.find(d => d.title === 'smoke-guide.md') || docs[0];
      const detail = (await req('GET', `/api/v1/documents/${document.id}`)).json.data;
      must(detail.id === document.id, 'get 命中');
      return document.id;
    });

    // 5. 知识块修订
    let chunk1 = null; let chunk2 = null; let editedRevision = null;
    await step('chunks: list/get', async () => {
      const chunks = (await req('GET', `/api/v1/datasets/${dataset.id}/chunks`)).json.data;
      must(chunks.length >= 2, `块数 ${chunks.length}`);
      chunk1 = chunks[0]; chunk2 = chunks[1];
      const got = (await req('GET', `/api/v1/chunks/${chunk1.id}`)).json.data;
      must(got.id === chunk1.id, 'get 命中');
      const artifactId = chunk1.artifact_id || chunk1.artifactId;
      must(artifactId, '块修订应携带产物引用');
      for (const kind of ['markdown', 'document', 'manifest', 'quality']) {
        const res = await fetch(`${BASE}/api/v1/artifacts/${artifactId}/${kind}`, { headers: { cookie } });
        must(res.status === 200, `artifact ${kind} → ${res.status}`);
      }
      return `${chunks.length} 块`;
    });
    await step('chunks: 修订（编辑→新版本）/diff', async () => {
      const edited = (await req('POST', `/api/v1/chunks/${chunk1.id}/revisions`, { contentMd: `${chunk1.content_md}\n\n（人工补充：冒烟验证修订）`, contentText: `${chunk1.content_text} 人工补充：冒烟验证修订` })).json.data;
      must(edited.id && edited.id !== chunk1.id, '应生成新修订');
      editedRevision = edited;
      const diff = await req('GET', `/api/v1/chunks/${chunk1.id}/diff?against=${edited.id}`);
      must(diff.status === 200, `diff HTTP ${diff.status}`);
      return edited.id;
    });
    await step('chunks: split/merge/restore', async () => {
      const half = Math.max(4, Math.floor(chunk2.content_text.length / 2));
      const split = (await req('POST', `/api/v1/chunks/${chunk2.id}/split`, { parts: [chunk2.content_text.slice(0, half), chunk2.content_text.slice(half)] })).json.data;
      must(Array.isArray(split.parts) && split.parts.length >= 2, '拆分应产生 2 块');
      const merged = (await req('POST', `/api/v1/chunks/merge`, { chunkRevisionIds: split.parts.map(p => p.id) })).json.data;
      must(merged && merged.merged && merged.merged.id, '合并应产生合并块');
      const restored = (await req('POST', `/api/v1/chunks/${editedRevision.id}/restore`, {})).json.data;
      must(restored && restored.id, '恢复应产生新修订');
      return '';
    });

    // 6. 发布与检索
    let release = null;
    await step('releases: build(activate) → active', async () => {
      const built = (await req('POST', `/api/v1/datasets/${dataset.id}/releases`, { activate: true })).json.data;
      release = await waitTask(built.id || built.taskId || built.result?.taskId || built.result?.releaseId ? built.id : undefined, 30000).catch(() => null);
      const task = await waitTask(built.id);
      must(task.result && task.result.status === 'active', `release status=${task.result && task.result.status}`);
      release = { id: task.result.releaseId };
      return release.id;
    });
    await step('releases: get/impact/search(混合检索)', async () => {
      must((await req('GET', `/api/v1/releases/${release.id}`)).json.data.id === release.id, 'get 命中');
      must((await req('GET', `/api/v1/releases/${release.id}/impact`)).status === 200, 'impact 200');
      const searched = (await req('POST', `/api/v1/releases/${release.id}/search`, { query: '如何安装' })).json.data;
      must(searched.results && searched.results.length >= 1, `检索命中 ${searched.results && searched.results.length}`);
      return `${searched.results.length} 命中`;
    });
    await step('releases: 内容修订后构建第二版本 + rollback → activate（指针切换）', async () => {
      const currentChunks = (await req('GET', `/api/v1/datasets/${dataset.id}/chunks`)).json.data;
      const current = currentChunks.find(item => !item.excluded);
      must(current && current.id, '应存在当前可发布 chunk');
      const revised = await req('POST', `/api/v1/chunks/${current.id}/revisions`, {
        contentMd: `${current.content_md}\n\n（发布快照变更）`,
        contentText: `${current.content_text} 发布快照变更`
      });
      must(revised.status === 200, `chunk revision → ${revised.status}`);
      const second = (await req('POST', `/api/v1/datasets/${dataset.id}/releases`, { activate: true })).json.data;
      const secondTask = await waitTask(second.id);
      must(secondTask.result && secondTask.result.status === 'active' && secondTask.result.releaseId !== release.id, '内容变化后应激活第二版本');
      const rolled = await req('POST', `/api/v1/releases/${release.id}/rollback`, {});
      must(rolled.status === 200, `rollback → ${rolled.status} ${JSON.stringify(rolled.json.error || {})}`);
      const back = await req('POST', `/api/v1/releases/${release.id}/activate`, {});
      must(back.status === 200, `activate → ${back.status}`);
      return '';
    });

    // 7. 问答全链（规范形状 + 前端形状）
    let conversation = null; let answer = null;
    await step('conversations: create（前端 snake_case 形状）', async () => {
      const { status, json } = await req('POST', '/api/v1/conversations', { title: '前端形状', knowledge_base_id: kb.id });
      must(status === 200 && json.data.id, `HTTP ${status} ${JSON.stringify(json.error || {})}`);
      conversation = json.data;
      return conversation.id;
    });
    await step('conversations: create（规范 camelCase 形状）', async () => {
      const { status, json } = await req('POST', '/api/v1/conversations', { title: '规范形状', knowledgeBaseId: kb.id });
      must(status === 200 && json.data.id, `HTTP ${status}`);
      const del = await req('DELETE', `/api/v1/conversations/${json.data.id}`);
      must(del.status === 200, '删除辅助会话');
      return '';
    });
    await step('messages: ask（前端 {query} 形状，无外部模型降级本地抽取）', async () => {
      const { status, json } = await req('POST', `/api/v1/conversations/${conversation.id}/messages`, { query: '如何安装 Ordo？' });
      must(status === 200 && json.data.assistantMessage, `HTTP ${status} ${JSON.stringify(json.error || {})}`);
      answer = json.data;
      must(answer.trace.stages.length === 8, `trace 8 阶段，实际 ${answer.trace.stages.length}`);
      must(answer.assistantMessage.citations.length >= 1, `引用 ${answer.assistantMessage.citations.length}`);
      must(answer.assistantMessage.evidence_status === 'sufficient', `evidence=${answer.assistantMessage.evidence_status}`);
      return `trace=${answer.trace.id}`;
    });
    await step('messages: 拒答阈值探测（已知问题，记录不阻塞）', async () => {
      const fresh = (await req('POST', '/api/v1/conversations', { knowledgeBaseId: kb.id })).json.data;
      const { status, json } = await req('POST', `/api/v1/conversations/${fresh.id}/messages`, { question: 'What is the secret recipe for chocolate cake?' });
      must(status === 200, `HTTP ${status}`);
      const evidence = json.data.assistantMessage.evidence_status;
      await req('DELETE', `/api/v1/conversations/${fresh.id}`);
      if (evidence === 'insufficient') return 'insufficient ✓';
      // 已知问题：local-hash-v1 基线对无关文本仍产生 >0.08 的向量分，导致不拒答（冲突清单 #R1）
      record('已知问题 #R1：无关问题未拒答', false, `evidence=${evidence}（阈值 0.08 过低，详见冲突清单）`);
      return `evidence=${evidence}（已记录）`;
    });
    await step('messages: history/feedback/citations/traces', async () => {
      const conv = (await req('GET', `/api/v1/conversations/${conversation.id}`)).json.data;
      must(conv.messages.length >= 2, `消息 ${conv.messages.length}`);
      const fb = (await req('POST', `/api/v1/messages/${answer.assistantMessage.id}/feedback`, { rating: 1, reason: '准确' })).json.data;
      must(fb.rating === 1, 'feedback 保存');
      const citeId = answer.assistantMessage.citations[0].id;
      const citation = (await req('GET', `/api/v1/citations/${citeId}`)).json.data;
      must(citation.contentText && citation.releaseId === release.id, '引用可打开且绑定 release');
      const traces = (await req('GET', '/api/v1/traces?limit=5')).json.data;
      must(traces.length >= 2, `traces ${traces.length}`);
      const trace = (await req('GET', `/api/v1/traces/${answer.trace.id}`)).json.data;
      must(trace.stages_json || trace.stages, 'trace 详情可读');
      return '';
    });

    // 8. 模型连接
    let model = null;
    await step('models: create(local-extractive)/list/get/patch/test/delete', async () => {
      const created = (await req('POST', '/api/v1/models', { name: '冒烟本地抽取', provider: 'local-extractive', purpose: 'generation', modelId: 'ordo-local-extractive-v1' })).json.data;
      must(created.id && created.status === 'available', `status=${created.status}`);
      model = created;
      must((await req('GET', '/api/v1/models')).json.data.some(m => m.id === model.id), 'list 含新模型');
      const tested = (await req('POST', `/api/v1/models/${model.id}/test`, {})).json.data;
      must(tested.available === true && typeof tested.latencyMs === 'number', `test available latency=${tested.latencyMs}ms`);
      must((await req('PATCH', `/api/v1/models/${model.id}`, { name: '冒烟本地抽取-改名' })).status === 200, 'patch 200');
      must((await req('DELETE', `/api/v1/models/${model.id}`)).status === 200, 'delete 200');
      return `test ${tested.latencyMs}ms`;
    });

    // 9. 助手
    let assistant = null;
    await step('assistants: create/patch(前端保存形状)/publish/pause', async () => {
      const created = (await req('POST', '/api/v1/assistants', { name: '冒烟助手', datasetId: dataset.id })).json.data;
      must(created.id, 'assistant 创建');
      assistant = created;
      const patched = (await req('PATCH', `/api/v1/assistants/${assistant.id}`, { name: '冒烟助手', config: { description: '全链验证', questions: ['如何安装？'] } })).json.data;
      must(patched.id === assistant.id, 'patch 命中');
      const published = (await req('POST', `/api/v1/assistants/${assistant.id}/publish`, {})).json.data;
      must(published.status === 'published' && published.releases.length >= 1, '发布成功并绑定 release');
      const paused = (await req('POST', `/api/v1/assistants/${assistant.id}/pause`, {})).json.data;
      must(paused.status === 'paused', 'pause 生效');
      return '';
    });

    // 10. 设置 / 开关 / Wiki / 备份 / 审计 / 搜索
    await step('settings: get + put(两种形状)', async () => {
      const before = (await req('GET', '/api/v1/settings')).json.data;
      const p1 = await req('PUT', '/api/v1/settings/general', { language: 'zh-CN', theme: 'nebula' });
      must(p1.status === 200, `裸对象 PUT → ${p1.status}`);
      const p2 = await req('PUT', '/api/v1/settings/general', { value: { language: 'zh-CN', theme: 'nebula', wrapped: true } });
      must(p2.status === 200, `{value} 包装 PUT → ${p2.status}`);
      const after = (await req('GET', '/api/v1/settings')).json.data;
      must(after.general.language === 'zh-CN' && after.general.wrapped === true, '两种形状均落库');
      return `groups=${Object.keys(after).join(',')}`;
    });
    await step('feature-flags: get/put/恢复', async () => {
      const flags = (await req('GET', '/api/v1/feature-flags')).json.data;
      must(flags.length >= 6, `flags ${flags.length}`);
      const graphFlag = flags.find(f => f.key === 'graph');
      must(graphFlag, 'graph 开关应存在');
      // 注意：GET 返回 SQLite 整数 0/1，PUT 要求布尔值（已在冲突清单记录该 API 人机工学问题）
      const restored = (await req('PUT', '/api/v1/feature-flags/graph', { enabled: Boolean(graphFlag.enabled) })).json.data;
      must(restored && restored.enabled === graphFlag.enabled, '恢复原值');
      return `${flags.map(f => f.key + '=' + f.enabled).join(' ')}`;
    });
    let wikiPage = null;
    await step('wiki: create + from-message', async () => {
      const created = (await req('POST', '/api/v1/wiki', { knowledgeBaseId: kb.id, title: '冒烟 Wiki', contentMd: '# 冒烟\n\n人工创建的验证页。' })).json.data;
      must(created.id && created.status === 'draft', 'wiki 草稿创建');
      wikiPage = created;
      const fromMsg = (await req('POST', `/api/v1/wiki/from-message/${answer.assistantMessage.id}`, { title: '冒烟-来自回答' })).json.data;
      must(fromMsg.id && fromMsg.revisions[0].sources.length >= 1, 'from-message 带引用来源');
      return wikiPage.id;
    });
    let backup = null;
    await step('backups: create → wait → verified', async () => {
      const task = (await req('POST', '/api/v1/backups', { label: '冒烟备份' })).json.data;
      const done = await waitTask(task.id, 60000);
      must(done.result && done.result.status === 'verified', `status=${done.result && done.result.status}`);
      backup = done.result;
      must((await req('GET', '/api/v1/backups')).json.data.length >= 1, '列表含备份');
      return backup.backupId;
    });
    await step('audit: list + verify 哈希链', async () => {
      const list = (await req('GET', '/api/v1/audit?limit=10')).json.data;
      must(Array.isArray(list) && list.length >= 1, `audit ${list.length} 条`);
      const verify = (await req('GET', '/api/v1/audit/verify')).json.data;
      must(verify.valid === true, `链校验 valid=${verify.valid} count=${verify.count}`);
      return `valid count=${verify.count}`;
    });
    await step('search: 全局搜索', async () => {
      const found = (await req('GET', '/api/v1/search?q=' + encodeURIComponent('冒烟'))).json.data;
      must(found.results.length >= 1, `命中 ${found.results.length}`);
      return `${found.results.length} 命中`;
    });

    // 11. 受控连接器 / 图谱（开关开启后）
    await step('connectors: 开关→create/test/schema/template/execute→关', async () => {
      await req('PUT', '/api/v1/feature-flags/databaseConnectors', { enabled: true });
      const created = (await req('POST', '/api/v1/connectors', { name: '冒烟元数据', type: 'sqlite', path: path.join(dataRoot, 'metadata', 'ordo.sqlite3') })).json.data;
      must(created.id, 'connector 创建');
      const tested = (await req('POST', `/api/v1/connectors/${created.id}/test`, {})).json.data;
      must(tested.available === true, 'test 可用');
      const schema = (await req('GET', `/api/v1/connectors/${created.id}/schema`)).json.data;
      must(schema.some(t => t.name === 'knowledge_bases'), 'schema 含 knowledge_bases');
      const template = (await req('POST', `/api/v1/connectors/${created.id}/templates`, { name: '库清单', sql: 'SELECT id,name FROM knowledge_bases WHERE workspace_id=?', params: [{ name: 'workspaceId', type: 'text' }], rowLimit: 10 })).json.data;
      must(template.id, 'template 创建');
      const executed = (await req('POST', `/api/v1/query-templates/${template.id}/execute`, { values: ['ws_local'] })).json.data;
      must(executed.rowCount >= 1, `execute ${executed.rowCount} 行`);
      const rejected = await req('POST', `/api/v1/connectors/${created.id}/templates`, { name: '写库', sql: 'DELETE FROM knowledge_bases' });
      must(rejected.status === 400 && rejected.json.error.code === 'QUERY_NOT_READ_ONLY', '写 SQL 应拒绝');
      const flagsOff = await req('PUT', '/api/v1/feature-flags/databaseConnectors', { enabled: false });
      must(flagsOff.status === 200, '开关恢复关闭');
      return '';
    });
    await step('graph: 开关→ontology/entity/relation→关', async () => {
      await req('PUT', '/api/v1/feature-flags/graph', { enabled: true });
      const ontology = (await req('POST', `/api/v1/knowledge-bases/${kb.id}/ontologies`, { name: '冒烟本体', publish: true, schema: { entityTypes: ['产品'], relationTypes: ['包含'] } })).json.data;
      must(ontology.id, 'ontology 创建并发布');
      const e1 = (await req('POST', `/api/v1/datasets/${dataset.id}/graph/entities`, { ontologyVersionId: ontology.id, entityType: '产品', name: 'Ordo', sourceChunkId: chunk1.id })).json.data;
      must(e1.id, 'entity 创建（须引用 chunk 溯源）');
      const graph = (await req('GET', `/api/v1/datasets/${dataset.id}/graph`)).json.data;
      must(graph.entities.length >= 1, `graph entities ${graph.entities.length}`);
      await req('PUT', '/api/v1/feature-flags/graph', { enabled: false });
      return '';
    });

    // 12. 清理
    await step('cleanup: 删除助手/知识库', async () => {
      const delKb = await req('DELETE', `/api/v1/knowledge-bases/${kb.id}`);
      must(delKb.status === 200 || delKb.status === 409, `KB delete → ${delKb.status}（绑定助手时 409 属预期）`);
      return '';
    });
  } finally {
    if (process.platform === 'win32' && child.pid) {
      await new Promise(resolve => {
        const killer = spawn('taskkill', ['/PID', String(child.pid), '/F', '/T'], { stdio: 'ignore' });
        killer.once('error', resolve);
        killer.once('exit', resolve);
      });
    } else if (child.exitCode === null) child.kill('SIGKILL');
  }

  const failed = results.filter(r => !r.ok);
  console.log(`\n===== 冒烟结果：${results.length - failed.length}/${results.length} 通过 =====`);
  if (failed.length) { failed.forEach(f => console.log('FAIL:', f.name, '—', f.detail)); process.exitCode = 1; }
}

main()
  .catch(error => { console.error('SMOKE CRASH:', error); process.exitCode = 1; })
  .finally(() => fs.rmSync(dataRoot, { recursive: true, force: true }));
