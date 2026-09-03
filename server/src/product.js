'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { randomUUID } = crypto;
const { backup: sqliteBackup, DatabaseSync } = require('node:sqlite');
const tar = require('tar-stream');
const zlib = require('node:zlib');
const { pipeline } = require('node:stream/promises');
const { id, now, required, AppError, hash, page, parseJson, stableJson } = require('./core');
const { atomicWrite, assertWithin } = require('./storage');
const { validateArchivePath } = require('./ingest');

function backupKey(secretStore, workspaceId, backupId) {
  return Buffer.from(crypto.hkdfSync('sha256', secretStore.key, workspaceId, `ordo-backup:${backupId}`, 32));
}

async function encryptFile(inputPath, outputPath, key) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
  const output = fs.createWriteStream(outputPath, { flags: 'wx', mode: 0o600 });
  output.write(Buffer.from('ORDOENC1'));
  output.write(iv);
  for await (const chunk of fs.createReadStream(inputPath)) {
    const encrypted = cipher.update(chunk);
    if (encrypted.length && !output.write(encrypted)) await new Promise(resolve => output.once('drain', resolve));
  }
  output.write(cipher.final());
  output.write(cipher.getAuthTag());
  await new Promise((resolve, reject) => { output.once('error', reject); output.end(resolve); });
}

async function decryptFile(inputPath, outputPath, key) {
  const stat = fs.statSync(inputPath);
  if (stat.size < 8 + 12 + 16) throw new AppError(422, 'BACKUP_ENVELOPE_INVALID', '备份加密封装无效');
  const fd = fs.openSync(inputPath, 'r');
  const header = Buffer.alloc(20);
  try { fs.readSync(fd, header, 0, header.length, 0); } finally { fs.closeSync(fd); }
  if (header.subarray(0, 8).toString() !== 'ORDOENC1') throw new AppError(422, 'BACKUP_ENVELOPE_INVALID', '备份不是受支持的加密格式');
  const iv = header.subarray(8);
  const tagFd = fs.openSync(inputPath, 'r');
  const tag = Buffer.alloc(16);
  try { fs.readSync(tagFd, tag, 0, 16, stat.size - 16); } finally { fs.closeSync(tagFd); }
  const decipher = crypto.createDecipheriv('aes-256-gcm', key, iv);
  const output = fs.createWriteStream(outputPath, { flags: 'wx', mode: 0o600 });
  try {
    for await (const chunk of fs.createReadStream(inputPath, { start: 20, end: stat.size - 17 })) {
      const decrypted = decipher.update(chunk);
      if (decrypted.length && !output.write(decrypted)) await new Promise(resolve => output.once('drain', resolve));
    }
    decipher.setAuthTag(tag);
    const tail = decipher.final();
    if (tail.length) output.write(tail);
    await new Promise((resolve, reject) => { output.once('error', reject); output.end(resolve); });
  } catch (error) {
    output.destroy();
    try { fs.unlinkSync(outputPath); } catch {}
    throw new AppError(422, 'BACKUP_DECRYPT_FAILED', '备份解密或认证失败');
  }
}

function trackDirectory(directory, root, directories) {
  let current = path.resolve(directory);
  const boundary = path.resolve(root);
  while (current.startsWith(`${boundary}${path.sep}`)) {
    directories.add(current);
    current = path.dirname(current);
  }
  directories.add(boundary);
}

function cleanupCreatedPaths(files, directories) {
  for (const file of [...files].reverse()) { try { fs.unlinkSync(file); } catch {} }
  for (const directory of [...directories].sort((a, b) => b.length - a.length)) { try { fs.rmdirSync(directory); } catch {} }
}

function unlinkIfExists(file) {
  try { fs.unlinkSync(file); } catch (error) { if (error.code !== 'ENOENT') throw error; }
}

function validateRestoreTarget(targetRoot, currentDataRoot) {
  const resolved = path.resolve(String(targetRoot || ''));
  const current = path.resolve(currentDataRoot);
  if (!targetRoot || resolved === path.parse(resolved).root || resolved === current || resolved.startsWith(`${current}${path.sep}`)) {
    throw new AppError(400, 'RESTORE_TARGET_INVALID', '恢复目标必须是尚不存在的新数据目录');
  }
  if (fs.existsSync(resolved)) throw new AppError(400, 'RESTORE_TARGET_INVALID', '恢复目标必须是尚不存在的新数据目录');
  const parent = path.dirname(resolved);
  if (!fs.existsSync(parent) || !fs.statSync(parent).isDirectory()) throw new AppError(400, 'RESTORE_TARGET_PARENT_INVALID', '恢复目标的父目录必须已存在');
  return resolved;
}

class ProductService {
  constructor({ db, knowledge, query, models, tasks, audit, blobStore, artifactStore, secretStore, config }) {
    this.db = db;
    this.knowledge = knowledge;
    this.query = query;
    this.models = models;
    this.tasks = tasks;
    this.audit = audit;
    this.blobStore = blobStore;
    this.artifactStore = artifactStore;
    this.secretStore = secretStore;
    this.config = config;
    tasks.register('backup.create', context => this.backupTask(context));
    tasks.register('backup.restore', context => this.restoreTask(context));
  }

  workspaceId() { return this.config.localWorkspaceId; }

  requireFeature(key, workspaceId = this.workspaceId()) {
    const flag = this.db.one('SELECT key,enabled,config_json FROM feature_flags WHERE key=? AND workspace_id=?', key, workspaceId);
    if (!flag || !flag.enabled) throw new AppError(403, 'FEATURE_DISABLED', `功能“${key}”当前未启用`, { feature: key });
    return flag;
  }

  dashboard(workspaceId = this.workspaceId()) {
    const counts = this.db.one(`SELECT
      (SELECT COUNT(*) FROM knowledge_bases WHERE workspace_id=? AND status='active') knowledgeBases,
      (SELECT COUNT(*) FROM datasets WHERE workspace_id=? AND status='active') datasets,
      (SELECT COUNT(*) FROM documents WHERE workspace_id=? AND status NOT IN ('deleted')) documents,
      (SELECT COUNT(*) FROM chunk_logicals WHERE workspace_id=?) chunks,
      (SELECT COUNT(*) FROM knowledge_releases WHERE workspace_id=? AND status='active') activeReleases,
      (SELECT COUNT(*) FROM conversations WHERE workspace_id=? AND deleted_at IS NULL) conversations,
      (SELECT COUNT(*) FROM messages WHERE workspace_id=? AND role='user') requests,
      (SELECT COUNT(*) FROM tasks WHERE workspace_id=? AND status IN ('queued','running','paused')) pendingTasks,
      (SELECT COUNT(*) FROM tasks WHERE workspace_id=? AND status='failed') failedTasks,
      (SELECT COUNT(*) FROM model_connections WHERE workspace_id=?) modelConnections,
      (SELECT COUNT(*) FROM wiki_pages WHERE workspace_id=?) wikiPages,
      (SELECT COUNT(*) FROM assistants WHERE workspace_id=?) assistants`,
      ...new Array(12).fill(workspaceId));
    const taskSummary = this.db.all('SELECT status,COUNT(*) AS count FROM tasks WHERE workspace_id=? GROUP BY status', workspaceId);
    const recentTasks = this.tasks.list(workspaceId, { limit: 10 });
    const recentKnowledgeBases = this.knowledge.listKnowledgeBases(workspaceId).slice(0, 10);
    const requestTrend = this.db.all(`SELECT substr(created_at,1,10) AS day,COUNT(*) AS count FROM messages
      WHERE workspace_id=? AND role='user' AND created_at>=datetime('now','-6 days') GROUP BY substr(created_at,1,10) ORDER BY day`, workspaceId);
    const evidence = this.db.all(`SELECT evidence_status,COUNT(*) AS count FROM query_traces WHERE workspace_id=? GROUP BY evidence_status`, workspaceId);
    const blob = this.blobStore.countAndSize(workspaceId);
    const artifacts = this.artifactStore.countAndSize();
    const backupRecord = this.db.one('SELECT * FROM backup_manifests WHERE workspace_id=? ORDER BY created_at DESC LIMIT 1', workspaceId);
    return {
      generatedAt: now(), deploymentProfile: this.config.deploymentProfile, status: 'ready', counts,
      taskSummary, recentTasks, recentKnowledgeBases, requestTrend, evidence,
      storage: { blobs: blob, artifacts, databaseBytes: fs.existsSync(this.config.dbPath) ? fs.statSync(this.config.dbPath).size : 0 },
      lastBackup: backupRecord || null,
      components: this.health(workspaceId).components
    };
  }

  health(workspaceId = this.workspaceId()) {
    let database = { status: 'available' };
    try { this.db.one('SELECT 1 AS ok'); } catch (error) { database = { status: 'unavailable', error: error.message }; }
    const writeAccess = [this.config.blobRoot, this.config.artifactRoot, this.config.backupRoot].map(directory => ({
      directory: path.basename(directory), writable: fs.existsSync(directory) && Boolean(fs.statSync(directory).mode & 0o200)
    }));
    const models = this.models.list(workspaceId);
    return {
      status: database.status === 'available' && writeAccess.every(item => item.writable) ? 'ready' : 'degraded',
      checkedAt: now(),
      components: {
        metadata: { ...database, provider: 'sqlite', schemaVersion: this.config.schemaVersion },
        blob: { status: writeAccess[0].writable ? 'available' : 'unavailable', provider: 'local-managed' },
        artifacts: { status: writeAccess[1].writable ? 'available' : 'unavailable', provider: 'local-managed' },
        fullText: { status: 'available', provider: 'sqlite-fts5' },
        vector: { status: 'available', provider: 'ordo-local-hash-v1', note: 'deterministic local baseline; replaceable provider' },
        generation: { status: models.some(model => model.status === 'available') ? 'available' : 'degraded', connections: models.length },
        parser: { status: 'available', native: ['md','txt','csv','xlsx','docx','pptx','pdf-text'], reviewRequired: ['image','pdf-scan'] }
      }
    };
  }

  getSettings(workspaceId = this.workspaceId()) {
    return Object.fromEntries(this.db.all('SELECT key,value_json,updated_at FROM settings WHERE workspace_id=? ORDER BY key', workspaceId).map(row => [row.key, { ...row.value, updatedAt: row.updated_at }]));
  }

  updateSetting(key, value, workspaceId = this.workspaceId(), requestId) {
    const allowed = new Set(['general','query','ingestion','backup']);
    if (!allowed.has(key)) throw new AppError(400, 'SETTING_KEY_INVALID', '设置分组无效');
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new AppError(400, 'VALIDATION_ERROR', '设置值必须是对象');
    this.db.run(`INSERT INTO settings(workspace_id,key,value_json,updated_at) VALUES(?,?,?,?)
      ON CONFLICT(workspace_id,key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`, workspaceId, key, JSON.stringify(value), now());
    this.audit.append({ workspaceId, action: 'setting.update', objectType: 'setting', objectId: key, requestId, details: { fields: Object.keys(value) } });
    return { key, value, updatedAt: now() };
  }

  featureFlags(workspaceId = this.workspaceId()) {
    return this.db.all('SELECT key,enabled,config_json,updated_at FROM feature_flags WHERE workspace_id=? ORDER BY key', workspaceId);
  }

  setFeatureFlag(key, input, workspaceId = this.workspaceId(), requestId) {
    const existing = this.db.one('SELECT * FROM feature_flags WHERE workspace_id=? AND key=?', workspaceId, key);
    if (!existing) throw new AppError(404, 'NOT_FOUND', '功能开关不存在');
    const enabled = typeof input.enabled === 'boolean' ? input.enabled : (input.enabled === 1 ? true : (input.enabled === 0 ? false : null));
    if (enabled === null) throw new AppError(400, 'VALIDATION_ERROR', '功能开关 enabled 必须是布尔值或 0/1');
    this.db.run('UPDATE feature_flags SET enabled=?,config_json=?,updated_at=? WHERE workspace_id=? AND key=?', enabled ? 1 : 0, JSON.stringify(input.config || existing.config || {}), now(), workspaceId, key);
    this.audit.append({ workspaceId, action: 'feature_flag.update', objectType: 'feature_flag', objectId: key, requestId, details: { enabled: Boolean(enabled) } });
    return this.db.one('SELECT key,enabled,config_json,updated_at FROM feature_flags WHERE workspace_id=? AND key=?', workspaceId, key);
  }

  globalSearch(query, workspaceId = this.workspaceId(), limit = 30) {
    const q = required(query, 'query');
    const pattern = `%${q}%`;
    const results = [];
    const add = (type, rows, titleField, route) => rows.forEach(row => results.push({ type, id: row.id, title: row[titleField], subtitle: row.subtitle || '', route: route(row) }));
    add('knowledge_base', this.db.all("SELECT id,name,description AS subtitle FROM knowledge_bases WHERE workspace_id=? AND status!='deleted' AND (name LIKE ? OR description LIKE ?) LIMIT ?", workspaceId, pattern, pattern, limit), 'name', row => `#/knowledge/config?kb=${row.id}`);
    add('dataset', this.db.all("SELECT id,name,description AS subtitle FROM datasets WHERE workspace_id=? AND status!='deleted' AND (name LIKE ? OR description LIKE ?) LIMIT ?", workspaceId, pattern, pattern, limit), 'name', row => `#/knowledge/datasets?dataset=${row.id}`);
    add('document', this.db.all("SELECT id,title,logical_path AS subtitle FROM documents WHERE workspace_id=? AND status!='deleted' AND (title LIKE ? OR logical_path LIKE ?) LIMIT ?", workspaceId, pattern, pattern, limit), 'title', row => `#/knowledge/parsing?document=${row.id}`);
    add('chunk', this.db.all(`SELECT cr.id,substr(cr.content_text,1,100) AS title,d.title AS subtitle FROM chunk_revisions cr JOIN documents d ON d.id=cr.document_id
      WHERE cr.workspace_id=? AND cr.id=(SELECT cr2.id FROM chunk_revisions cr2 WHERE cr2.chunk_logical_id=cr.chunk_logical_id ORDER BY revision_number DESC LIMIT 1)
      AND (cr.content_text LIKE ? OR cr.id LIKE ?) LIMIT ?`, workspaceId, pattern, pattern, limit), 'title', row => `#/knowledge/index?chunk=${row.id}`);
    add('conversation', this.db.all("SELECT id,title,'' AS subtitle FROM conversations WHERE workspace_id=? AND deleted_at IS NULL AND title LIKE ? LIMIT ?", workspaceId, pattern, limit), 'title', row => `#/apps/chat?conversation=${row.id}`);
    add('wiki', this.db.all("SELECT id,title,status AS subtitle FROM wiki_pages WHERE workspace_id=? AND title LIKE ? LIMIT ?", workspaceId, pattern, limit), 'title', row => `#/knowledge/datasets?wiki=${row.id}`);
    return { query: q, results: results.slice(0, limit), total: Math.min(results.length, limit) };
  }

  listWiki(workspaceId = this.workspaceId(), knowledgeBaseId) {
    const params = [workspaceId];
    let filter = '';
    if (knowledgeBaseId) { filter = ' AND knowledge_base_id=?'; params.push(knowledgeBaseId); }
    return this.db.all(`SELECT wp.*,(SELECT COUNT(*) FROM wiki_revisions wr WHERE wr.page_id=wp.id) revision_count FROM wiki_pages wp WHERE workspace_id=?${filter} ORDER BY updated_at DESC`, ...params);
  }

  getWiki(pageId, workspaceId = this.workspaceId()) {
    const record = this.db.one('SELECT * FROM wiki_pages WHERE id=? AND workspace_id=?', pageId, workspaceId);
    if (!record) throw new AppError(404, 'NOT_FOUND', 'Wiki 页面不存在或不可访问');
    record.revisions = this.db.all('SELECT * FROM wiki_revisions WHERE page_id=? AND workspace_id=? ORDER BY revision_number DESC', pageId, workspaceId);
    return record;
  }

  createWiki(input, workspaceId = this.workspaceId(), requestId) {
    this.knowledge.ensureKnowledgeBase(required(input.knowledgeBaseId, 'knowledgeBaseId'), workspaceId);
    const pageId = id('wiki');
    const revisionId = id('wrev');
    const timestamp = now();
    this.db.transaction(() => {
      this.db.run('INSERT INTO wiki_pages(id,workspace_id,knowledge_base_id,parent_id,title,status,current_revision_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
        pageId, workspaceId, input.knowledgeBaseId, input.parentId || null, required(input.title, 'title'), 'draft', revisionId, timestamp, timestamp);
      this.db.run('INSERT INTO wiki_revisions(id,workspace_id,page_id,revision_number,content_md,sources_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
        revisionId, workspaceId, pageId, 1, input.contentMd || '', JSON.stringify(input.sources || []), 'draft', timestamp);
    });
    this.audit.append({ workspaceId, action: 'wiki.create', objectType: 'wiki_page', objectId: pageId, requestId, details: { knowledgeBaseId: input.knowledgeBaseId } });
    return this.getWiki(pageId, workspaceId);
  }

  reviseWiki(pageId, input, workspaceId = this.workspaceId(), requestId) {
    const pageRecord = this.getWiki(pageId, workspaceId);
    const version = (this.db.one('SELECT COALESCE(MAX(revision_number),0)+1 AS version FROM wiki_revisions WHERE page_id=?', pageId)?.version || 1);
    const revisionId = id('wrev');
    const timestamp = now();
    this.db.transaction(() => {
      this.db.run('INSERT INTO wiki_revisions(id,workspace_id,page_id,revision_number,content_md,sources_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
        revisionId, workspaceId, pageId, version, required(input.contentMd, 'contentMd'), JSON.stringify(input.sources || []), input.publish ? 'published' : 'draft', timestamp);
      this.db.run('UPDATE wiki_pages SET title=?,status=?,current_revision_id=?,updated_at=? WHERE id=? AND workspace_id=?',
        input.title || pageRecord.title, input.publish ? 'published' : 'draft', revisionId, timestamp, pageId, workspaceId);
    });
    this.audit.append({ workspaceId, action: input.publish ? 'wiki.publish' : 'wiki.revise', objectType: 'wiki_page', objectId: pageId, requestId, details: { revisionId, version } });
    return this.getWiki(pageId, workspaceId);
  }

  wikiFromMessage(messageId, input, workspaceId = this.workspaceId(), requestId) {
    const message = this.db.one("SELECT m.*,c.knowledge_base_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=? AND m.workspace_id=? AND m.role='assistant'", messageId, workspaceId);
    if (!message) throw new AppError(404, 'NOT_FOUND', '回答消息不存在或不可访问');
    const citations = this.db.all('SELECT id,title,document_id,document_revision_id,chunk_revision_id,release_id FROM citations WHERE message_id=? AND workspace_id=? ORDER BY ordinal', messageId, workspaceId);
    return this.createWiki({ knowledgeBaseId: message.knowledge_base_id, title: input.title || `问答草稿 ${message.created_at.slice(0,10)}`, contentMd: message.content, sources: citations }, workspaceId, requestId);
  }

  listAssistants(workspaceId = this.workspaceId()) {
    return this.db.all(`SELECT a.*,d.name AS dataset_name,ar.version AS release_version FROM assistants a JOIN datasets d ON d.id=a.dataset_id
      LEFT JOIN assistant_releases ar ON ar.id=a.active_release_id WHERE a.workspace_id=? ORDER BY a.updated_at DESC`, workspaceId);
  }

  getAssistant(assistantId, workspaceId = this.workspaceId()) {
    const assistant = this.db.one('SELECT * FROM assistants WHERE id=? AND workspace_id=?', assistantId, workspaceId);
    if (!assistant) throw new AppError(404, 'NOT_FOUND', '智能助手不存在或不可访问');
    assistant.releases = this.db.all('SELECT * FROM assistant_releases WHERE assistant_id=? AND workspace_id=? ORDER BY version DESC', assistantId, workspaceId);
    return assistant;
  }

  createAssistant(input, workspaceId = this.workspaceId(), requestId) {
    this.knowledge.ensureDataset(required(input.datasetId, 'datasetId'), workspaceId);
    const assistantId = id('asst');
    const timestamp = now();
    this.db.run('INSERT INTO assistants(id,workspace_id,name,dataset_id,status,draft_config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
      assistantId, workspaceId, required(input.name, 'name'), input.datasetId, 'draft', JSON.stringify(input.config || { strictEvidence: true, language: 'zh-CN' }), timestamp, timestamp);
    this.audit.append({ workspaceId, action: 'assistant.create', objectType: 'assistant', objectId: assistantId, requestId, details: { datasetId: input.datasetId } });
    return this.getAssistant(assistantId, workspaceId);
  }

  updateAssistant(assistantId, input, workspaceId = this.workspaceId(), requestId) {
    const current = this.getAssistant(assistantId, workspaceId);
    if (input.datasetId) this.knowledge.ensureDataset(input.datasetId, workspaceId);
    const config = { ...current.draft_config, ...(input.config || {}) };
    this.db.run("UPDATE assistants SET name=?,dataset_id=?,status='draft',draft_config_json=?,updated_at=? WHERE id=? AND workspace_id=?",
      input.name || current.name, input.datasetId || current.dataset_id, JSON.stringify(config), now(), assistantId, workspaceId);
    this.audit.append({ workspaceId, action: 'assistant.update', objectType: 'assistant', objectId: assistantId, requestId, details: { changed: Object.keys(input) } });
    return this.getAssistant(assistantId, workspaceId);
  }

  publishAssistant(assistantId, input = {}, workspaceId = this.workspaceId(), requestId) {
    const assistant = this.getAssistant(assistantId, workspaceId);
    const dataset = this.knowledge.ensureDataset(assistant.dataset_id, workspaceId);
    const releaseId = input.knowledgeReleaseId || dataset.active_release_id;
    if (!releaseId) throw new AppError(409, 'ACTIVE_RELEASE_REQUIRED', '助手绑定的数据集没有活动知识版本');
    const release = this.knowledge.getRelease(releaseId, workspaceId);
    if (release.dataset_id !== dataset.id || release.status !== 'active') throw new AppError(409, 'RELEASE_INVALID', '助手只能发布当前数据集的活动 Release');
    const version = (this.db.one('SELECT COALESCE(MAX(version),0)+1 AS version FROM assistant_releases WHERE assistant_id=?', assistantId)?.version || 1);
    const assistantReleaseId = id('arel');
    const timestamp = now();
    const config = { ...assistant.draft_config, strictEvidence: true, knowledgeReleaseId: releaseId };
    this.db.transaction(() => {
      this.db.run('INSERT INTO assistant_releases(id,workspace_id,assistant_id,knowledge_release_id,version,config_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
        assistantReleaseId, workspaceId, assistantId, releaseId, version, JSON.stringify(config), 'published', timestamp);
      this.db.run("UPDATE assistants SET status='published',active_release_id=?,updated_at=? WHERE id=? AND workspace_id=?", assistantReleaseId, timestamp, assistantId, workspaceId);
    });
    this.audit.append({ workspaceId, action: 'assistant.publish', objectType: 'assistant_release', objectId: assistantReleaseId, requestId, details: { assistantId, knowledgeReleaseId: releaseId, version } });
    return this.getAssistant(assistantId, workspaceId);
  }

  pauseAssistant(assistantId, workspaceId = this.workspaceId(), requestId) {
    this.getAssistant(assistantId, workspaceId);
    this.db.run("UPDATE assistants SET status='paused',updated_at=? WHERE id=? AND workspace_id=?", now(), assistantId, workspaceId);
    this.audit.append({ workspaceId, action: 'assistant.pause', objectType: 'assistant', objectId: assistantId, requestId });
    return this.getAssistant(assistantId, workspaceId);
  }

  requestBackup(input = {}, workspaceId = this.workspaceId(), requestId) {
    const task = this.tasks.create({ workspaceId, type: 'backup.create', objectType: 'workspace', objectId: workspaceId,
      idempotencyKey: input.idempotencyKey || `backup:${workspaceId}:${new Date().toISOString().slice(0,13)}`, input: { label: input.label || 'manual' } });
    this.audit.append({ workspaceId, action: 'backup.request', objectType: 'workspace', objectId: workspaceId, requestId, details: { taskId: task.id } });
    return task;
  }

  listBackups(workspaceId = this.workspaceId()) {
    return this.db.all('SELECT * FROM backup_manifests WHERE workspace_id=? ORDER BY created_at DESC', workspaceId);
  }

  async backupTask({ workspaceId, input, checkpoint }) {
    const backupId = id('backup');
    const timestamp = now();
    const snapshot = path.join(this.config.runtimeRoot, `${backupId}.sqlite3`);
    const archive = path.join(this.config.backupRoot, `${backupId}.tar.gz.enc`);
    const plainArchive = path.join(this.config.runtimeRoot, `${backupId}.tar.gz`);
    const encryptedTemporary = `${archive}.tmp`;
    let pack;
    let streamPromise;
    let manifestPersisted = false;
    try {
      const manifest = {
        schemaVersion: this.config.schemaVersion,
        appVersion: this.config.appVersion,
        backupId, workspaceId, createdAt: timestamp, label: input.label,
        encryption: this.config.backupEncryptionVersion,
        files: [], objectCounts: this.dashboard(workspaceId).counts
      };
      checkpoint(10, '创建一致性数据库快照');
      await sqliteBackup(this.db.raw, snapshot);
      pack = tar.pack();
      const gzip = zlib.createGzip({ level: 6 });
      const output = fs.createWriteStream(plainArchive, { flags: 'wx', mode: 0o600 });
      streamPromise = pipeline(pack, gzip, output);
      const addFile = (name, file) => new Promise((resolve, reject) => {
        const stat = fs.statSync(file);
        const entry = pack.entry({ name, size: stat.size, mode: 0o600, mtime: stat.mtime }, error => error ? reject(error) : resolve());
        const source = fs.createReadStream(file);
        source.once('error', reject);
        source.pipe(entry);
        manifest.files.push({ name, sizeBytes: stat.size, sha256: hash(fs.readFileSync(file)) });
      });
      const addBuffer = (name, content) => new Promise((resolve, reject) => {
        const buffer = Buffer.from(content);
        pack.entry({ name, size: buffer.length, mode: 0o600 }, buffer, error => error ? reject(error) : resolve());
        manifest.files.push({ name, sizeBytes: buffer.length, sha256: hash(buffer) });
      });
      await addFile('metadata/ordo.sqlite3', snapshot);
      await addBuffer('runtime/master.key', this.secretStore.key);
      const managed = [];
      const collect = (root, prefix) => {
        if (!fs.existsSync(root)) return;
        const walk = directory => fs.readdirSync(directory, { withFileTypes: true }).forEach(entry => {
          const file = path.join(directory, entry.name);
          if (entry.isDirectory()) walk(file);
          else if (entry.isFile()) managed.push({ file, name: `${prefix}/${path.relative(root, file).replaceAll('\\','/')}` });
        });
        walk(root);
      };
      collect(this.config.blobRoot, 'blobs');
      collect(this.config.artifactRoot, 'artifacts');
      for (let index = 0; index < managed.length; index += 1) {
        await addFile(managed[index].name, managed[index].file);
        if (index % 25 === 0) checkpoint(20 + (index / Math.max(managed.length,1)) * 65, '打包主数据', { packed: index, total: managed.length });
      }
      const manifestContent = Buffer.from(JSON.stringify(manifest, null, 2));
      await new Promise((resolve, reject) => pack.entry({ name: 'backup-manifest.json', size: manifestContent.length, mode: 0o600 }, manifestContent, error => error ? reject(error) : resolve()));
      pack.finalize();
      await streamPromise;
      await encryptFile(plainArchive, encryptedTemporary, backupKey(this.secretStore, workspaceId, backupId));
      const checksum = hash(fs.readFileSync(encryptedTemporary));
      checkpoint(95, '校验备份包完整性');
      fs.renameSync(encryptedTemporary, archive);
      this.db.transaction(() => {
        this.db.run('INSERT INTO backup_manifests(id,workspace_id,status,storage_key,manifest_json,checksum,created_at,verified_at) VALUES(?,?,?,?,?,?,?,?)',
          backupId, workspaceId, 'verified', path.basename(archive), JSON.stringify(manifest), checksum, timestamp, now());
        this.audit.append({ workspaceId, action: 'backup.create', objectType: 'backup', objectId: backupId, details: { files: manifest.files.length, checksum } });
      });
      manifestPersisted = true;
      return { backupId, status: 'verified', storageKey: path.basename(archive), checksum, fileCount: manifest.files.length };
    } catch (error) {
      if (pack) pack.destroy(error);
      throw error;
    } finally {
      if (streamPromise) await streamPromise.catch(() => {});
      for (const file of [snapshot, plainArchive, encryptedTemporary]) unlinkIfExists(file);
      if (!manifestPersisted) unlinkIfExists(archive);
    }
  }

  requestRestore(backupId, input = {}, workspaceId = this.workspaceId(), requestId) {
    const backup = this.db.one('SELECT * FROM backup_manifests WHERE id=? AND workspace_id=?', backupId, workspaceId);
    if (!backup) throw new AppError(404, 'NOT_FOUND', '备份不存在或不可访问');
    const targetRoot = validateRestoreTarget(input.targetRoot || path.join(path.dirname(this.config.dataRoot), `${path.basename(this.config.dataRoot)}-restore-${backupId}`), this.config.dataRoot);
    const task = this.tasks.create({ workspaceId, type: 'backup.restore', objectType: 'backup', objectId: backupId,
      idempotencyKey: input.idempotencyKey || `restore:${backupId}:${hash(targetRoot)}`, input: { backupId, targetRoot } });
    this.audit.append({ workspaceId, action: 'backup.restore_request', objectType: 'backup', objectId: backupId, requestId, details: { taskId: task.id, targetRoot } });
    return task;
  }

  async restoreTask({ workspaceId, input, checkpoint }) {
    const backup = this.db.one('SELECT * FROM backup_manifests WHERE id=? AND workspace_id=?', input.backupId, workspaceId);
    if (!backup) throw new AppError(404, 'NOT_FOUND', '备份不存在');
    const archive = assertWithin(this.config.backupRoot, path.join(this.config.backupRoot, backup.storage_key));
    if (!fs.existsSync(archive) || hash(fs.readFileSync(archive)) !== backup.checksum) throw new AppError(422, 'BACKUP_CHECKSUM_INVALID', '备份包校验失败');
    const targetRoot = validateRestoreTarget(input.targetRoot, this.config.dataRoot);
    input.targetRoot = targetRoot;
    const stagingRoot = path.join(this.config.runtimeRoot, `restore-${randomUUID()}`);
    const encryptedArchive = path.join(stagingRoot, 'backup.enc');
    const plainArchive = path.join(stagingRoot, 'backup.tar.gz');
    const stagingDataRoot = path.join(stagingRoot, 'data');
    const createdFiles = new Set([encryptedArchive, plainArchive]);
    const createdDirs = new Set([stagingRoot, stagingDataRoot]);
    let committed = false;
    let restored = 0;
    try {
      fs.mkdirSync(stagingRoot, { recursive: true });
      fs.copyFileSync(archive, encryptedArchive, fs.constants.COPYFILE_EXCL);
      await decryptFile(encryptedArchive, plainArchive, backupKey(this.secretStore, workspaceId, input.backupId));
      checkpoint(15, '验证备份并创建隔离恢复目录');
      fs.mkdirSync(stagingDataRoot, { recursive: true });
    const extract = tar.extract();
    if (backup.manifest?.backupId !== input.backupId || backup.manifest?.workspaceId !== workspaceId || backup.manifest?.encryption !== this.config.backupEncryptionVersion || !Array.isArray(backup.manifest?.files)) throw new AppError(422, 'BACKUP_MANIFEST_INVALID', '备份清单版本、空间或加密状态无效');
    const expected = new Map();
    for (const item of backup.manifest.files) {
      if (!item || typeof item.name !== 'string' || expected.has(item.name) || validateArchivePath(item.name) !== item.name || !Number.isSafeInteger(item.sizeBytes) || item.sizeBytes < 0 || !/^[a-f0-9]{64}$/i.test(String(item.sha256 || ''))) throw new AppError(422, 'BACKUP_MANIFEST_INVALID', '备份清单文件条目无效');
      expected.set(item.name, item);
    }
    if (!expected.has('metadata/ordo.sqlite3') || !expected.has('runtime/master.key')) throw new AppError(422, 'BACKUP_MANIFEST_INVALID', '备份清单缺少元数据数据库或秘密库主密钥');
    const restoredNames = new Set();
    const entryNames = new Set();
    let controlManifest = null;
    let controlManifestSeen = false;
    let restoredBytes = 0;
    await new Promise((resolve, reject) => {
      extract.on('entry', (header, stream, next) => {
        try {
          const name = validateArchivePath(header.name);
          if (entryNames.has(name)) throw new AppError(422, 'BACKUP_FILE_INVALID', `备份归档包含重复条目: ${name}`);
          entryNames.add(name);
          if (header.type !== 'file' && header.type !== 'directory') throw new AppError(422, 'BACKUP_ENTRY_REJECTED', '备份包含不支持的链接或特殊文件');
          if (header.type === 'directory') { stream.resume(); stream.on('error', reject); stream.on('end', next); return; }
          const expectedItem = expected.get(name);
          const declaredSize = Number(header.size);
          if (!Number.isSafeInteger(declaredSize) || declaredSize < 0) throw new AppError(422, 'BACKUP_FILE_INVALID', `备份条目大小无效: ${name}`);
          const chunks = [];
          if (name === 'backup-manifest.json') {
            if (controlManifestSeen || declaredSize > 1024 * 1024) throw new AppError(422, 'BACKUP_MANIFEST_INVALID', '备份控制清单重复或超过大小预算');
            controlManifestSeen = true;
            stream.on('data', chunk => { if (chunks.reduce((sum, item) => sum + item.length, 0) + chunk.length > 1024 * 1024) { reject(new AppError(422, 'BACKUP_MANIFEST_INVALID', '备份控制清单超过大小预算')); return; } chunks.push(chunk); });
            stream.on('error', reject);
            stream.on('end', () => {
              try { controlManifest = JSON.parse(Buffer.concat(chunks).toString('utf8')); next(); }
              catch { reject(new AppError(422, 'BACKUP_MANIFEST_INVALID', '备份控制清单不是有效 JSON')); }
            });
            return;
          }
          if (!expectedItem || declaredSize !== expectedItem.sizeBytes) throw new AppError(422, 'BACKUP_FILE_INVALID', `备份条目与清单大小不一致: ${name}`);
          const target = assertWithin(stagingDataRoot, path.join(stagingDataRoot, name));
          const parent = path.dirname(target);
          trackDirectory(parent, stagingDataRoot, createdDirs);
          fs.mkdirSync(parent, { recursive: true });
          stream.on('data', chunk => chunks.push(chunk));
          stream.on('error', reject);
          stream.on('end', () => {
            try {
              const content = Buffer.concat(chunks);
            const item = expected.get(name);
            if (item && (content.length !== item.sizeBytes || hash(content) !== item.sha256)) { reject(new AppError(422, 'BACKUP_FILE_INVALID', `备份文件校验失败: ${name}`)); return; }
            if (!item || restoredNames.has(name)) { reject(new AppError(422, 'BACKUP_FILE_INVALID', `备份文件不在清单中: ${name}`)); return; }
            restoredNames.add(name);
            restoredBytes += content.length;
            atomicWrite(target, content);
            createdFiles.add(target);
            restored += 1;
              checkpoint(Math.min(85, 15 + restored / Math.max(expected.size,1) * 70), '恢复主数据', { restored, total: expected.size });
              next();
            } catch (error) { reject(error); }
          });
        } catch (error) { stream.resume(); reject(error); }
      });
      extract.on('finish', resolve);
      extract.on('error', reject);
      const inputStream = fs.createReadStream(plainArchive);
      const gunzip = zlib.createGunzip();
      inputStream.on('error', reject);
      gunzip.on('error', reject);
      inputStream.pipe(gunzip).pipe(extract);
    });
    if (!controlManifest || stableJson(controlManifest) !== stableJson(backup.manifest)) throw new AppError(422, 'BACKUP_MANIFEST_INVALID', '备份控制清单与备份记录不一致');
    if (restoredNames.size !== expected.size || [...expected.keys()].some(name => !restoredNames.has(name))) throw new AppError(422, 'BACKUP_FILE_MISSING', '备份清单中的文件未全部恢复');
    checkpoint(90, '验证恢复后的数据库');
    const restoredKeyPath = path.join(stagingDataRoot, 'runtime', 'master.key');
    if (fs.statSync(restoredKeyPath).size !== 32) throw new AppError(422, 'BACKUP_FILE_INVALID', '恢复后的秘密库主密钥大小无效');
    const restoredDbPath = path.join(stagingDataRoot, 'metadata', 'ordo.sqlite3');
    let restoredDb;
    let integrity;
    let workspace;
    try {
      restoredDb = new DatabaseSync(restoredDbPath, { readOnly: true });
      restoredDb.exec('PRAGMA foreign_keys=ON;');
      integrity = restoredDb.prepare('PRAGMA integrity_check').all();
      workspace = restoredDb.prepare('SELECT id,name,type FROM workspaces WHERE id=?').get(workspaceId);
    } finally { restoredDb?.close(); }
    if (!integrity?.every(row => row.integrity_check === 'ok') || !workspace) throw new AppError(422, 'RESTORE_VALIDATION_FAILED', '恢复数据库完整性检查失败');
    if (fs.existsSync(input.targetRoot)) throw new AppError(409, 'RESTORE_TARGET_RACE', '恢复目标在校验期间已被占用');
    const report = { backupId: input.backupId, targetRoot: input.targetRoot, restoredFiles: restored, integrity: 'ok', workspace, switchRequired: true, note: '使用 ORDO_DATA_DIR 指向此目录并重启后切换；当前实例未被覆盖。' };
    const reportPath = path.join(stagingDataRoot, 'restore-report.json');
    atomicWrite(reportPath, JSON.stringify(report, null, 2));
    createdFiles.add(reportPath);
    fs.renameSync(stagingDataRoot, input.targetRoot);
    committed = true;
    this.audit.append({ workspaceId, action: 'backup.restore_validated', objectType: 'backup', objectId: input.backupId, details: { targetRoot: input.targetRoot, restoredFiles: restored } });
    return report;
    } finally {
      if (committed) {
        createdFiles.delete(path.join(stagingDataRoot, 'restore-report.json'));
        for (const file of [...createdFiles]) if (file.startsWith(`${stagingDataRoot}${path.sep}`)) createdFiles.delete(file);
        createdDirs.delete(stagingDataRoot);
        for (const directory of [...createdDirs]) if (directory.startsWith(`${stagingDataRoot}${path.sep}`)) createdDirs.delete(directory);
      }
      cleanupCreatedPaths(createdFiles, createdDirs);
    }
  }

  diagnostics(workspaceId = this.workspaceId()) {
    return {
      generatedAt: now(), appVersion: this.config.appVersion, schemaVersion: this.config.schemaVersion,
      deploymentProfile: this.config.deploymentProfile, platform: this.config.platform,
      health: this.health(workspaceId), dashboard: this.dashboard(workspaceId), audit: this.audit.verify(workspaceId),
      capabilities: {
        formats: ['pdf-text','docx-lightweight','pptx-lightweight','xlsx','csv','md','txt','archive-one-level'],
        reviewRequired: ['scanned-pdf','image-ocr','visual-description'],
        query: ['sqlite-fts5','local-hash-vector','rrf','strict-evidence','query-trace'],
        providers: ['openai-compatible','ollama','local-extractive']
      }
    };
  }
}

module.exports = { ProductService };
