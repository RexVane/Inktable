'use strict';

const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { spawn } = require('node:child_process');
const { id, now, hash, stableJson, required, AppError, parseJson } = require('./core');
const { parseDocument, detectFile, estimateTokens } = require('./parsers');

const DEFAULT_INDEX_CONFIG = {
  schemaVersion: 1,
  chunking: { targetTokens: 512, overlapTokens: 64, structureFirst: true },
  embedding: { provider: 'local-hash-v1', model: 'ordo-hash-embedding-v1', dimensions: 128, normalized: true },
  fullText: { provider: 'sqlite-fts5', tokenizer: 'unicode61', exactTerms: true },
  fusion: { method: 'rrf', k: 60, vectorTopK: 20, fullTextTopK: 20 },
  rerank: { enabled: true, provider: 'local-lexical-v1', topK: 8 },
  prompt: { template: 'strict-evidence-v1', maxEvidenceChars: 12000 }
};

function deepMerge(base, override) {
  if (!override || typeof override !== 'object' || Array.isArray(override)) return override === undefined ? base : override;
  const result = { ...(base && typeof base === 'object' && !Array.isArray(base) ? base : {}) };
  for (const [key, value] of Object.entries(override)) result[key] = deepMerge(result[key], value);
  return result;
}

function validateIndexConfig(config) {
  const embedding = config.embedding || {};
  const fusion = config.fusion || {};
  const rerank = config.rerank || {};
  if (config.schemaVersion !== 1) throw new AppError(400, 'INDEX_CONFIG_INVALID', '索引配置 schemaVersion 必须为 1');
  if (embedding.provider !== 'local-hash-v1') throw new AppError(400, 'INDEX_PROVIDER_UNSUPPORTED', '当前仅支持 local-hash-v1 向量 Provider');
  if (!Number.isInteger(Number(embedding.dimensions)) || Number(embedding.dimensions) < 8 || Number(embedding.dimensions) > 2048) throw new AppError(400, 'INDEX_CONFIG_INVALID', '向量维度必须在 8 到 2048 之间');
  if (fusion.method !== 'rrf' || !Number.isInteger(Number(fusion.k)) || Number(fusion.k) < 1 || Number(fusion.k) > 1000) throw new AppError(400, 'INDEX_CONFIG_INVALID', '当前仅支持 1 到 1000 的 RRF k');
  if (!Number.isInteger(Number(rerank.topK)) || Number(rerank.topK) < 1 || Number(rerank.topK) > 50) throw new AppError(400, 'INDEX_CONFIG_INVALID', '重排 TopK 必须在 1 到 50 之间');
  return config;
}

function localEmbedding(text, dimensions = 128) {
  const vector = new Array(dimensions).fill(0);
  const normalized = String(text || '').toLowerCase().normalize('NFKC');
  const tokens = [
    ...(normalized.match(/[\u3400-\u9fff]/g) || []),
    ...(normalized.match(/[a-z0-9_./-]+/g) || [])
  ];
  for (const token of tokens) {
    const digest = Buffer.from(hash(token), 'hex');
    for (let i = 0; i < Math.min(8, digest.length); i += 1) {
      const index = ((digest[i] << 8) + digest[(i + 1) % digest.length]) % dimensions;
      vector[index] += digest[(i + 2) % digest.length] % 2 ? 1 : -1;
    }
  }
  const length = Math.sqrt(vector.reduce((sum, value) => sum + value * value, 0)) || 1;
  return vector.map(value => Number((value / length).toFixed(8)));
}

function cosine(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return 0;
  let score = 0;
  for (let i = 0; i < a.length; i += 1) score += a[i] * b[i];
  return score;
}

class KnowledgeService {
  constructor({ db, blobStore, artifactStore, tasks, audit, config }) {
    this.db = db;
    this.blobStore = blobStore;
    this.artifactStore = artifactStore;
    this.tasks = tasks;
    this.audit = audit;
    this.config = config;
    tasks.register('document.parse', context => this.parseRevisionTask(context));
    tasks.register('release.build', context => this.buildReleaseTask(context));
  }

  workspaceId() { return this.config.localWorkspaceId; }

  ensureKnowledgeBase(kbId, workspaceId = this.workspaceId()) {
    const record = this.db.one("SELECT * FROM knowledge_bases WHERE id=? AND workspace_id=? AND status!='deleted'", kbId, workspaceId);
    if (!record) throw new AppError(404, 'NOT_FOUND', '知识库不存在或不可访问');
    return record;
  }

  ensureDataset(datasetId, workspaceId = this.workspaceId()) {
    const record = this.db.one("SELECT * FROM datasets WHERE id=? AND workspace_id=? AND status!='deleted'", datasetId, workspaceId);
    if (!record) throw new AppError(404, 'NOT_FOUND', '数据集不存在或不可访问');
    return record;
  }

  createKnowledgeBase(input, workspaceId = this.workspaceId(), requestId) {
    const name = required(input.name, 'name');
    const kbId = id('kb');
    const datasetId = id('ds');
    const profileId = id('idxp');
    const timestamp = now();
    const config = validateIndexConfig(deepMerge(DEFAULT_INDEX_CONFIG, input.indexConfig || {}));
    const configHash = hash(stableJson(config));
    try {
      this.db.transaction(() => {
        this.db.run('INSERT INTO knowledge_bases(id,workspace_id,name,description,status,default_dataset_id,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
          kbId, workspaceId, name, input.description || '', 'active', datasetId, JSON.stringify(input.config || {}), timestamp, timestamp);
        this.db.run('INSERT INTO datasets(id,workspace_id,knowledge_base_id,name,description,language,labels_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
          datasetId, workspaceId, kbId, input.defaultDatasetName || name, input.description || '', input.language || 'zh-CN', JSON.stringify(input.labels || []), 'active', timestamp, timestamp);
        this.db.run('INSERT INTO index_profiles(id,workspace_id,knowledge_base_id,name,schema_version,config_json,config_hash,created_at) VALUES(?,?,?,?,?,?,?,?)',
          profileId, workspaceId, kbId, '默认索引配置', 1, JSON.stringify(config), configHash, timestamp);
        this.db.run('UPDATE knowledge_bases SET default_index_profile_id=? WHERE id=? AND workspace_id=?', profileId, kbId, workspaceId);
      });
    } catch (error) {
      if (/UNIQUE constraint failed/.test(error.message)) throw new AppError(409, 'NAME_CONFLICT', '同名知识库已存在');
      throw error;
    }
    this.audit.append({ workspaceId, action: 'knowledge_base.create', objectType: 'knowledge_base', objectId: kbId, requestId, details: { name, datasetId, profileId } });
    return this.getKnowledgeBase(kbId, workspaceId);
  }

  listKnowledgeBases(workspaceId = this.workspaceId()) {
    return this.db.all(`SELECT kb.*,
      (SELECT COUNT(*) FROM datasets d WHERE d.knowledge_base_id=kb.id AND d.status!='deleted') AS dataset_count,
      (SELECT COUNT(*) FROM documents doc JOIN datasets ds ON ds.id=doc.dataset_id WHERE ds.knowledge_base_id=kb.id AND doc.status!='deleted') AS document_count,
      (SELECT COUNT(*) FROM chunk_revisions cr JOIN datasets ds ON ds.id=cr.dataset_id WHERE ds.knowledge_base_id=kb.id AND cr.id=(SELECT cr2.id FROM chunk_revisions cr2 WHERE cr2.chunk_logical_id=cr.chunk_logical_id ORDER BY revision_number DESC LIMIT 1) AND cr.excluded=0) AS chunk_count
      FROM knowledge_bases kb WHERE kb.workspace_id=? AND kb.status!='deleted' ORDER BY kb.updated_at DESC`, workspaceId);
  }

  getKnowledgeBase(kbId, workspaceId = this.workspaceId()) {
    const record = this.ensureKnowledgeBase(kbId, workspaceId);
    record.datasets = this.listDatasets(kbId, workspaceId);
    record.indexProfiles = this.db.all('SELECT * FROM index_profiles WHERE knowledge_base_id=? AND workspace_id=? ORDER BY created_at DESC', kbId, workspaceId);
    record.defaultIndexProfileId = record.default_index_profile_id || record.indexProfiles[0]?.id || null;
    if (record.active_release_id) record.activeRelease = this.getRelease(record.active_release_id, workspaceId);
    return record;
  }

  getIndexProfile(profileId, workspaceId = this.workspaceId()) {
    const profile = this.db.one('SELECT * FROM index_profiles WHERE id=? AND workspace_id=?', profileId, workspaceId);
    if (!profile) throw new AppError(404, 'NOT_FOUND', '索引配置不存在或不可访问');
    profile.config = profile.config_json ? JSON.parse(profile.config_json) : {};
    profile.releaseCount = this.db.one('SELECT COUNT(*) AS count FROM knowledge_releases WHERE index_profile_id=? AND workspace_id=?', profileId, workspaceId)?.count || 0;
    return profile;
  }

  createIndexProfile(kbId, input, workspaceId = this.workspaceId(), requestId) {
    this.ensureKnowledgeBase(kbId, workspaceId);
    const config = validateIndexConfig(deepMerge(DEFAULT_INDEX_CONFIG, input.config || input.indexConfig || {}));
    const profileId = id('idxp');
    const timestamp = now();
    try {
      this.db.run('INSERT INTO index_profiles(id,workspace_id,knowledge_base_id,name,schema_version,config_json,config_hash,created_at) VALUES(?,?,?,?,?,?,?,?)', profileId, workspaceId, kbId, required(input.name, 'name'), config.schemaVersion, JSON.stringify(config), hash(stableJson(config)), timestamp);
    } catch (error) {
      if (/UNIQUE constraint failed/.test(error.message)) throw new AppError(409, 'INDEX_PROFILE_CONFLICT', '相同索引配置已存在');
      throw error;
    }
    if (input.setDefault) this.setDefaultIndexProfile(kbId, profileId, workspaceId, requestId);
    this.audit.append({ workspaceId, action: 'index_profile.create', objectType: 'index_profile', objectId: profileId, requestId, details: { knowledgeBaseId: kbId, configHash: hash(stableJson(config)) } });
    return this.getIndexProfile(profileId, workspaceId);
  }

  updateIndexProfile(profileId, input, workspaceId = this.workspaceId(), requestId) {
    const current = this.getIndexProfile(profileId, workspaceId);
    if (current.releaseCount > 0) throw new AppError(409, 'INDEX_PROFILE_IMMUTABLE', '已被 Release 使用的索引配置不可原地修改，请创建新配置');
    const config = validateIndexConfig(deepMerge(DEFAULT_INDEX_CONFIG, input.config || input.indexConfig || current.config));
    const name = input.name === undefined ? current.name : required(input.name, 'name');
    const knowledgeBase = this.ensureKnowledgeBase(current.knowledge_base_id, workspaceId);
    const next = this.createIndexProfile(current.knowledge_base_id, { name, config, setDefault: knowledgeBase.default_index_profile_id === profileId }, workspaceId, requestId);
    this.archiveIndexProfile(profileId, workspaceId, requestId);
    return next;
  }

  archiveIndexProfile(profileId, workspaceId = this.workspaceId(), requestId) {
    const current = this.getIndexProfile(profileId, workspaceId);
    if (current.releaseCount > 0) throw new AppError(409, 'INDEX_PROFILE_IN_USE', '已被 Release 使用的索引配置不能删除');
    const refs = this.db.one('SELECT COUNT(*) AS count FROM knowledge_bases WHERE default_index_profile_id=? AND workspace_id=?', profileId, workspaceId)?.count || 0;
    if (refs) throw new AppError(409, 'INDEX_PROFILE_DEFAULT', '默认索引配置不能删除，请先切换默认配置');
    this.db.run('DELETE FROM index_profiles WHERE id=? AND workspace_id=?', profileId, workspaceId);
    this.audit.append({ workspaceId, action: 'index_profile.delete', objectType: 'index_profile', objectId: profileId, requestId });
    return { deleted: true };
  }

  setDefaultIndexProfile(kbId, profileId, workspaceId = this.workspaceId(), requestId) {
    const kb = this.ensureKnowledgeBase(kbId, workspaceId);
    const profile = this.getIndexProfile(profileId, workspaceId);
    if (profile.knowledge_base_id !== kb.id) throw new AppError(400, 'SCOPE_MISMATCH', '索引配置不属于所选知识库');
    this.db.run('UPDATE knowledge_bases SET default_index_profile_id=?,updated_at=? WHERE id=? AND workspace_id=?', profileId, now(), kbId, workspaceId);
    this.audit.append({ workspaceId, action: 'index_profile.set_default', objectType: 'knowledge_base', objectId: kbId, requestId, details: { profileId } });
    return this.getKnowledgeBase(kbId, workspaceId);
  }

  updateKnowledgeBase(kbId, input, workspaceId = this.workspaceId(), requestId) {
    const current = this.ensureKnowledgeBase(kbId, workspaceId);
    const name = input.name === undefined ? current.name : required(input.name, 'name');
    const description = input.description === undefined ? current.description : String(input.description);
    const status = input.status === undefined ? current.status : input.status;
    if (!['active','archived'].includes(status)) throw new AppError(400, 'VALIDATION_ERROR', '知识库状态无效');
    try {
      this.db.run('UPDATE knowledge_bases SET name=?,description=?,status=?,updated_at=? WHERE id=? AND workspace_id=?', name, description, status, now(), kbId, workspaceId);
    } catch (error) {
      if (/UNIQUE constraint failed/.test(error.message)) throw new AppError(409, 'NAME_CONFLICT', '同名知识库已存在');
      throw error;
    }
    this.audit.append({ workspaceId, action: 'knowledge_base.update', objectType: 'knowledge_base', objectId: kbId, requestId, details: { changed: Object.keys(input) } });
    return this.getKnowledgeBase(kbId, workspaceId);
  }

  deleteKnowledgeBase(kbId, workspaceId = this.workspaceId(), requestId) {
    const impact = this.knowledgeBaseImpact(kbId, workspaceId);
    if (impact.conversations > 0 || impact.assistants > 0) throw new AppError(409, 'DEPENDENCY_CONFLICT', '知识库仍被会话或助手引用，不能删除', impact);
    const timestamp = now();
    this.db.transaction(() => {
      this.db.run("UPDATE datasets SET status='deleted',deleted_at=?,updated_at=? WHERE knowledge_base_id=? AND workspace_id=?", timestamp, timestamp, kbId, workspaceId);
      this.db.run("UPDATE knowledge_bases SET status='deleted',deleted_at=?,updated_at=? WHERE id=? AND workspace_id=?", timestamp, timestamp, kbId, workspaceId);
    });
    this.audit.append({ workspaceId, action: 'knowledge_base.delete', objectType: 'knowledge_base', objectId: kbId, requestId, details: impact });
    return { deleted: true, impact };
  }

  knowledgeBaseImpact(kbId, workspaceId = this.workspaceId()) {
    this.ensureKnowledgeBase(kbId, workspaceId);
    return this.db.one(`SELECT
      (SELECT COUNT(*) FROM datasets WHERE knowledge_base_id=? AND workspace_id=? AND status!='deleted') datasets,
      (SELECT COUNT(*) FROM documents d JOIN datasets ds ON ds.id=d.dataset_id WHERE ds.knowledge_base_id=? AND d.workspace_id=? AND d.status!='deleted') documents,
      (SELECT COUNT(*) FROM knowledge_releases WHERE knowledge_base_id=? AND workspace_id=?) releases,
      (SELECT COUNT(*) FROM conversations WHERE knowledge_base_id=? AND workspace_id=? AND deleted_at IS NULL) conversations,
      (SELECT COUNT(*) FROM assistants a JOIN datasets ds ON ds.id=a.dataset_id WHERE ds.knowledge_base_id=? AND a.workspace_id=?) assistants`,
      kbId, workspaceId, kbId, workspaceId, kbId, workspaceId, kbId, workspaceId, kbId, workspaceId);
  }

  createDataset(kbId, input, workspaceId = this.workspaceId(), requestId) {
    this.ensureKnowledgeBase(kbId, workspaceId);
    const datasetId = id('ds');
    const timestamp = now();
    try {
      this.db.run('INSERT INTO datasets(id,workspace_id,knowledge_base_id,name,description,language,labels_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
        datasetId, workspaceId, kbId, required(input.name, 'name'), input.description || '', input.language || 'zh-CN', JSON.stringify(input.labels || []), 'active', timestamp, timestamp);
    } catch (error) {
      if (/UNIQUE constraint failed/.test(error.message)) throw new AppError(409, 'NAME_CONFLICT', '同名数据集已存在');
      throw error;
    }
    this.audit.append({ workspaceId, action: 'dataset.create', objectType: 'dataset', objectId: datasetId, requestId, details: { knowledgeBaseId: kbId } });
    return this.getDataset(datasetId, workspaceId);
  }

  listDatasets(kbId, workspaceId = this.workspaceId()) {
    return this.db.all(`SELECT d.*,
      (SELECT COUNT(*) FROM sources s WHERE s.dataset_id=d.id AND s.deleted_at IS NULL) source_count,
      (SELECT COUNT(*) FROM documents doc WHERE doc.dataset_id=d.id AND doc.status!='deleted') document_count,
      (SELECT COUNT(*) FROM chunk_logicals cl WHERE cl.dataset_id=d.id) chunk_count,
      (SELECT COUNT(*) FROM knowledge_releases kr WHERE kr.dataset_id=d.id) release_count
      FROM datasets d WHERE d.knowledge_base_id=? AND d.workspace_id=? AND d.status!='deleted' ORDER BY d.updated_at DESC`, kbId, workspaceId);
  }

  getDataset(datasetId, workspaceId = this.workspaceId()) {
    const record = this.ensureDataset(datasetId, workspaceId);
    record.sources = this.listSources(datasetId, workspaceId);
    record.releases = this.listReleases(datasetId, workspaceId);
    record.counts = this.db.one(`SELECT
      (SELECT COUNT(*) FROM documents WHERE dataset_id=? AND workspace_id=? AND status!='deleted') documents,
      (SELECT COUNT(*) FROM chunk_logicals WHERE dataset_id=? AND workspace_id=?) chunks,
      (SELECT COUNT(*) FROM sources WHERE dataset_id=? AND workspace_id=? AND deleted_at IS NULL) sources`,
      datasetId, workspaceId, datasetId, workspaceId, datasetId, workspaceId);
    return record;
  }

  deleteDataset(datasetId, workspaceId = this.workspaceId(), requestId) {
    const current = this.ensureDataset(datasetId, workspaceId);
    const dependencies = this.db.one(`SELECT
      (SELECT COUNT(*) FROM assistants WHERE dataset_id=? AND workspace_id=?) assistants,
      (SELECT COUNT(*) FROM conversations WHERE dataset_id=? AND workspace_id=? AND deleted_at IS NULL) conversations,
      (SELECT COUNT(*) FROM knowledge_releases WHERE dataset_id=? AND workspace_id=?) releases`, datasetId, workspaceId, datasetId, workspaceId, datasetId, workspaceId);
    if (dependencies.assistants || dependencies.conversations) throw new AppError(409, 'DEPENDENCY_CONFLICT', '数据集仍被助手或会话引用，不能删除', dependencies);
    const timestamp = now();
    this.db.transaction(() => {
      this.db.run("UPDATE datasets SET status='deleted',deleted_at=?,updated_at=? WHERE id=? AND workspace_id=?", timestamp, timestamp, datasetId, workspaceId);
      this.db.run("UPDATE sources SET deleted_at=?,updated_at=? WHERE dataset_id=? AND workspace_id=? AND deleted_at IS NULL", timestamp, timestamp, datasetId, workspaceId);
    });
    this.audit.append({ workspaceId, action: 'dataset.delete', objectType: 'dataset', objectId: datasetId, requestId, details: { ...dependencies, releaseCount: dependencies.releases } });
    return { deleted: true, datasetId, dependencies };
  }

  deleteDocument(documentId, workspaceId = this.workspaceId(), requestId) {
    const document = this.db.one("SELECT * FROM documents WHERE id=? AND workspace_id=? AND status!='deleted'", documentId, workspaceId);
    if (!document) throw new AppError(404, 'NOT_FOUND', '文档不存在或不可访问');
    const references = this.db.one('SELECT COUNT(*) AS count FROM release_chunks rc JOIN chunk_revisions cr ON cr.id=rc.chunk_revision_id WHERE cr.document_id=?', documentId);
    const timestamp = now();
    this.db.run("UPDATE documents SET status='deleted',deleted_at=?,updated_at=? WHERE id=? AND workspace_id=?", timestamp, timestamp, documentId, workspaceId);
    this.audit.append({ workspaceId, action: 'document.delete', objectType: 'document', objectId: documentId, requestId, details: { releaseReferences: references.count } });
    return { deleted: true, documentId, releaseReferences: references.count, note: references.count ? '已发布知识版本保留其不可变块引用，后续版本不会自动包含已删除文档。' : null };
  }

  updateDataset(datasetId, input, workspaceId = this.workspaceId(), requestId) {
    const current = this.ensureDataset(datasetId, workspaceId);
    const values = {
      name: input.name === undefined ? current.name : required(input.name, 'name'),
      description: input.description === undefined ? current.description : String(input.description),
      language: input.language === undefined ? current.language : String(input.language),
      labels: input.labels === undefined ? current.labels : input.labels,
      status: input.status === undefined ? current.status : input.status
    };
    if (!['active','archived'].includes(values.status)) throw new AppError(400, 'VALIDATION_ERROR', '数据集状态无效');
    this.db.run('UPDATE datasets SET name=?,description=?,language=?,labels_json=?,status=?,updated_at=? WHERE id=? AND workspace_id=?',
      values.name, values.description, values.language, JSON.stringify(values.labels || []), values.status, now(), datasetId, workspaceId);
    this.audit.append({ workspaceId, action: 'dataset.update', objectType: 'dataset', objectId: datasetId, requestId, details: { changed: Object.keys(input) } });
    return this.getDataset(datasetId, workspaceId);
  }

  createSource(datasetId, input, workspaceId = this.workspaceId(), requestId) {
    this.ensureDataset(datasetId, workspaceId);
    const sourceId = id('src');
    const timestamp = now();
    const type = input.type || 'upload';
    if (!['upload','directory','archive','local_discovery','database','connector','synthetic'].includes(type)) throw new AppError(400, 'VALIDATION_ERROR', '来源类型无效');
    this.db.run('INSERT INTO sources(id,workspace_id,dataset_id,type,name,location_hint,config_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
      sourceId, workspaceId, datasetId, type, required(input.name, 'name'), input.locationHint || '', JSON.stringify(input.config || {}), 'registered', timestamp, timestamp);
    this.audit.append({ workspaceId, action: 'source.create', objectType: 'source', objectId: sourceId, requestId, details: { datasetId, type } });
    return this.db.one('SELECT * FROM sources WHERE id=?', sourceId);
  }

  listSources(datasetId, workspaceId = this.workspaceId()) {
    return this.db.all(`SELECT s.*,
      (SELECT COUNT(*) FROM documents d WHERE d.source_id=s.id AND d.status!='deleted') document_count
      FROM sources s WHERE s.dataset_id=? AND s.workspace_id=? AND s.deleted_at IS NULL ORDER BY s.created_at DESC`, datasetId, workspaceId);
  }

  async registerUpload(datasetId, sourceId, filename, buffer, mimeType, workspaceId = this.workspaceId(), requestId) {
    const dataset = this.ensureDataset(datasetId, workspaceId);
    const source = this.db.one('SELECT * FROM sources WHERE id=? AND dataset_id=? AND workspace_id=? AND deleted_at IS NULL', sourceId, datasetId, workspaceId);
    if (!source) throw new AppError(404, 'NOT_FOUND', '数据来源不存在或不可访问');
    if (!Buffer.isBuffer(buffer) || !buffer.length) throw new AppError(400, 'EMPTY_FILE', '上传文件为空');
    if (buffer.length > this.config.maxFileBytes) throw new AppError(413, 'FILE_TOO_LARGE', '文件超过当前大小预算', { maxBytes: this.config.maxFileBytes });
    const detection = await detectFile(buffer, filename);
    const blob = this.blobStore.put(workspaceId, buffer, mimeType || detection.mimeType);
    const existingRevision = this.db.one(`SELECT dr.*,d.id AS document_id,d.title FROM document_revisions dr
      JOIN documents d ON d.id=dr.document_id WHERE dr.workspace_id=? AND dr.content_hash=? AND d.dataset_id=? AND d.source_id=? AND d.status!='deleted' LIMIT 1`,
      workspaceId, blob.sha256, datasetId, sourceId);
    if (existingRevision) {
      this.audit.append({ workspaceId, action: 'document.duplicate_detected', objectType: 'document_revision', objectId: existingRevision.id, requestId, details: { sourceId, filename } });
      return { duplicate: true, document: this.getDocument(existingRevision.document_id, workspaceId), revision: existingRevision };
    }
    const documentId = id('doc');
    const revisionId = id('rev');
    const timestamp = now();
    this.db.transaction(() => {
      this.db.run('INSERT INTO documents(id,workspace_id,dataset_id,source_id,title,logical_path,media_type,status,current_revision_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        documentId, workspaceId, datasetId, sourceId, path.basename(filename), filename, detection.mimeType, 'queued', revisionId, timestamp, timestamp);
      this.db.run('INSERT INTO document_revisions(id,workspace_id,document_id,source_id,blob_id,revision_number,content_hash,size_bytes,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
        revisionId, workspaceId, documentId, sourceId, blob.id, 1, blob.sha256, buffer.length, 'queued', timestamp);
      this.db.run("UPDATE sources SET status='queued',updated_at=? WHERE id=?", timestamp, sourceId);
    });
    const task = this.tasks.create({ workspaceId, type: 'document.parse', objectType: 'document_revision', objectId: revisionId,
      idempotencyKey: `parse:${revisionId}:${blob.sha256}`, input: { revisionId } });
    this.audit.append({ workspaceId, action: 'document.register', objectType: 'document', objectId: documentId, requestId, details: { datasetId, sourceId, revisionId, taskId: task.id, filename } });
    return { duplicate: false, document: this.getDocument(documentId, workspaceId), revision: this.db.one('SELECT * FROM document_revisions WHERE id=?', revisionId), task };
  }

  getDocument(documentId, workspaceId = this.workspaceId()) {
    const document = this.db.one(`SELECT d.*,dr.revision_number,dr.content_hash,dr.size_bytes,dr.parser_id,dr.warnings_json,
      pa.id AS artifact_id,pa.quality_status,
      (SELECT COUNT(*) FROM chunk_logicals cl WHERE cl.document_id=d.id) AS chunk_count
      FROM documents d LEFT JOIN document_revisions dr ON dr.id=d.current_revision_id
      LEFT JOIN parsed_artifacts pa ON pa.document_revision_id=dr.id
      WHERE d.id=? AND d.workspace_id=? AND d.status!='deleted'`, documentId, workspaceId);
    if (!document) throw new AppError(404, 'NOT_FOUND', '文档不存在或不可访问');
    document.revisions = this.db.all('SELECT * FROM document_revisions WHERE document_id=? AND workspace_id=? ORDER BY revision_number DESC', documentId, workspaceId);
    return document;
  }

  listDocuments(datasetId, workspaceId = this.workspaceId(), { status, query, limit = 100, offset = 0 } = {}) {
    this.ensureDataset(datasetId, workspaceId);
    const clauses = ["d.dataset_id=?", 'd.workspace_id=?', "d.status!='deleted'"];
    const params = [datasetId, workspaceId];
    if (status) { clauses.push('d.status=?'); params.push(status); }
    if (query) { clauses.push('(d.title LIKE ? OR d.logical_path LIKE ?)'); params.push(`%${query}%`, `%${query}%`); }
    params.push(limit, offset);
    return this.db.all(`SELECT d.*,dr.revision_number,dr.content_hash,dr.size_bytes,dr.parser_id,dr.warnings_json,
      pa.id AS artifact_id,pa.quality_status,
      (SELECT COUNT(*) FROM chunk_logicals cl WHERE cl.document_id=d.id) AS chunk_count
      FROM documents d LEFT JOIN document_revisions dr ON dr.id=d.current_revision_id
      LEFT JOIN parsed_artifacts pa ON pa.document_revision_id=dr.id
      WHERE ${clauses.join(' AND ')} ORDER BY d.updated_at DESC LIMIT ? OFFSET ?`, ...params);
  }

  parseInWorker(buffer, filename) {
    const worker = path.join(__dirname, 'parser-worker.js');
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ordo-parser-'));
    const inputPath = path.join(root, 'input.bin');
    const outputPath = path.join(root, 'result.json');
    fs.writeFileSync(inputPath, buffer, { flag: 'wx', mode: 0o600 });
    return new Promise((resolve, reject) => {
      const permissionArgs = process.env.ORDO_PARSER_PERMISSIONS === 'false' ? [] : ['--permission', `--allow-fs-read=${root}`, `--allow-fs-read=${path.join(this.config.projectRoot, 'server')}`, `--allow-fs-write=${root}`];
      const child = spawn(process.execPath, [...permissionArgs, '--max-old-space-size=256', worker, inputPath, outputPath, String(this.config.maxParserOutputBytes), filename], {
        cwd: root, stdio: ['ignore','ignore','pipe'], windowsHide: true, env: { NODE_ENV: 'production', PATH: process.env.PATH || '' }
      });
      let stderr = '';
      child.stderr.on('data', chunk => { stderr += chunk.toString().slice(0, 4096); });
      const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new AppError(504, 'PARSER_TIMEOUT', '解析任务超过时间预算')); }, this.config.parserTimeoutMs);
      child.once('error', error => { clearTimeout(timer); reject(new AppError(502, 'PARSER_WORKER_FAILED', '解析 Worker 无法启动')); });
      child.once('exit', code => {
        clearTimeout(timer);
        try {
          if (code !== 0) {
            let detail = {};
            try { detail = JSON.parse(stderr.trim().split(/\n/).pop() || '{}'); } catch {}
            reject(new AppError(422, detail.code || 'PARSER_WORKER_FAILED', detail.message || '解析 Worker 执行失败'));
            return;
          }
          const stat = fs.statSync(outputPath);
          if (stat.size > this.config.maxParserOutputBytes) throw new AppError(422, 'PARSER_OUTPUT_LIMIT', '解析产物超过大小预算');
          resolve(JSON.parse(fs.readFileSync(outputPath, 'utf8')));
        } catch (error) { reject(error instanceof AppError ? error : new AppError(422, 'PARSER_OUTPUT_INVALID', '解析 Worker 输出无效')); }
      });
    }).finally(() => {
      for (const file of [inputPath, outputPath]) { try { fs.unlinkSync(file); } catch {} }
      try { fs.rmdirSync(root); } catch {}
    });
  }

  async parseRevisionTask({ workspaceId, input, checkpoint }) {
    const revision = this.db.one(`SELECT dr.*,d.title,d.dataset_id,d.media_type,d.id AS document_id,d.source_id
      FROM document_revisions dr JOIN documents d ON d.id=dr.document_id WHERE dr.id=? AND dr.workspace_id=?`, input.revisionId, workspaceId);
    if (!revision) throw new AppError(404, 'NOT_FOUND', '待解析修订不存在');
    const existing = this.db.one('SELECT * FROM parsed_artifacts WHERE document_revision_id=? AND workspace_id=?', revision.id, workspaceId);
    if (existing) return { artifactId: existing.id, reused: true };
    checkpoint(10, '读取受管原件');
    const blob = this.blobStore.get(revision.blob_id, workspaceId);
    this.db.run("UPDATE document_revisions SET status='parsing' WHERE id=?", revision.id);
    this.db.run("UPDATE documents SET status='parsing',updated_at=? WHERE id=?", now(), revision.document_id);
    checkpoint(25, '执行格式预检与解析路由');
    let parsed;
    try { parsed = await this.parseInWorker(blob.buffer, revision.title); }
    catch (error) {
      const state = error.code === 'NEEDS_PASSWORD' ? 'needs_password' : error.code === 'UNSUPPORTED_FORMAT' ? 'unsupported' : error.code === 'MIME_MISMATCH' ? 'quarantined' : 'failed';
      this.db.run('UPDATE document_revisions SET status=?,warnings_json=? WHERE id=?', state, JSON.stringify([{ code: error.code || 'PARSE_FAILED', message: error.message }]), revision.id);
      this.db.run('UPDATE documents SET status=?,updated_at=? WHERE id=?', state, now(), revision.document_id);
      this.db.run("UPDATE sources SET status=?,updated_at=? WHERE id=?", state, now(), revision.source_id);
      throw error;
    }
    checkpoint(55, '生成标准 Markdown 与 JSON');
    const artifactId = id('art');
    const manifest = {
      schemaVersion: 1,
      artifactId,
      workspaceId,
      datasetId: revision.dataset_id,
      documentId: revision.document_id,
      documentRevisionId: revision.id,
      sourceId: revision.source_id,
      blobId: revision.blob_id,
      originalHash: revision.content_hash,
      mimeType: revision.media_type,
      sizeBytes: revision.size_bytes,
      parser: parsed.metadata.parser,
      parserVersion: '1.0.0',
      createdAt: now(),
      warnings: parsed.warnings,
      checksums: {
        markdown: hash(parsed.markdown), document: hash(JSON.stringify(parsed.document)), quality: hash(JSON.stringify(parsed.quality))
      }
    };
    const keys = this.artifactStore.writeDocument(workspaceId, revision.id, {
      'manifest.json': manifest,
      'document.md': parsed.markdown,
      'document.json': parsed.document,
      'quality.json': parsed.quality
    });
    const artifactHash = hash(JSON.stringify(manifest.checksums));
    checkpoint(95, '校验标准产物与来源链');
    try {
      this.db.transaction(() => {
      this.db.run('INSERT INTO parsed_artifacts(id,workspace_id,document_revision_id,schema_version,markdown_key,json_key,manifest_key,quality_key,content_hash,quality_status,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        artifactId, workspaceId, revision.id, 1, keys['document.md'], keys['document.json'], keys['manifest.json'], keys['quality.json'], artifactHash, parsed.qualityStatus, JSON.stringify(parsed.metadata), now());
      parsed.blocks.forEach((block, index) => {
        const logicalId = id('chunk');
        const chunkRevisionId = id('cr');
        const embedding = localEmbedding(block.contentText);
        this.db.run('INSERT INTO chunk_logicals(id,workspace_id,dataset_id,document_id,created_at) VALUES(?,?,?,?,?)', logicalId, workspaceId, revision.dataset_id, revision.document_id, now());
        this.db.run('INSERT INTO chunk_revisions(id,workspace_id,chunk_logical_id,dataset_id,document_id,document_revision_id,artifact_id,revision_number,type,content_md,content_text,source_locator_json,token_count,language,generated_by,confidence,warnings_json,sensitivity,excluded,embedding_json,embedding_model,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          chunkRevisionId, workspaceId, logicalId, revision.dataset_id, revision.document_id, revision.id, artifactId, 1, block.type, block.contentMd, block.contentText, JSON.stringify(block.locator), block.tokenCount, 'zh-CN', block.generatedBy, block.confidence, JSON.stringify(block.warnings), 'internal', 0, JSON.stringify(embedding), 'ordo-hash-embedding-v1', now());
        this.db.run('INSERT INTO chunks_fts(chunk_revision_id,workspace_id,dataset_id,title,breadcrumb,content) VALUES(?,?,?,?,?,?)',
          chunkRevisionId, workspaceId, revision.dataset_id, revision.title, `${revision.title} / ${block.type} ${index + 1}`, block.contentText);
      });
      const documentState = parsed.qualityStatus === 'publishable' ? 'ready' : 'review_required';
      this.db.run('UPDATE document_revisions SET status=?,parser_id=?,parser_version=?,warnings_json=? WHERE id=?',
        documentState, parsed.metadata.parser, '1.0.0', JSON.stringify(parsed.warnings), revision.id);
      this.db.run('UPDATE documents SET status=?,updated_at=? WHERE id=?', documentState, now(), revision.document_id);
      this.db.run("UPDATE sources SET status=?,updated_at=? WHERE id=?", documentState, now(), revision.source_id);
      this.audit.append({ workspaceId, action: 'document.parse', objectType: 'document_revision', objectId: revision.id,
        details: { artifactId, qualityStatus: parsed.qualityStatus, blocks: parsed.blocks.length, warnings: parsed.warnings.length } });
      });
    } catch (error) {
      for (const key of Object.values(keys)) { try { fs.unlinkSync(assertWithin(this.config.artifactRoot, path.join(this.config.artifactRoot, key))); } catch {} }
      throw error;
    }
    return { artifactId, documentId: revision.document_id, revisionId: revision.id, qualityStatus: parsed.qualityStatus, blockCount: parsed.blocks.length, warnings: parsed.warnings };
  }

  getArtifact(artifactId, workspaceId = this.workspaceId()) {
    const artifact = this.db.one('SELECT * FROM parsed_artifacts WHERE id=? AND workspace_id=?', artifactId, workspaceId);
    if (!artifact) throw new AppError(404, 'NOT_FOUND', '标准产物不存在或不可访问');
    return artifact;
  }

  artifactFile(artifactId, kind, workspaceId = this.workspaceId()) {
    const artifact = this.getArtifact(artifactId, workspaceId);
    const keys = { markdown: artifact.markdown_key, document: artifact.json_key, manifest: artifact.manifest_key, quality: artifact.quality_key };
    if (!keys[kind]) throw new AppError(400, 'VALIDATION_ERROR', '标准产物类型无效');
    return this.artifactStore.read(keys[kind]);
  }

  listChunks(datasetId, workspaceId = this.workspaceId(), { query, documentId, type, warning, limit = 100, offset = 0 } = {}) {
    this.ensureDataset(datasetId, workspaceId);
    const clauses = ['cr.dataset_id=?', 'cr.workspace_id=?', "d.status!='deleted'", 'cr.id=(SELECT cr2.id FROM chunk_revisions cr2 WHERE cr2.chunk_logical_id=cr.chunk_logical_id ORDER BY cr2.revision_number DESC LIMIT 1)'];
    const params = [datasetId, workspaceId];
    if (documentId) { clauses.push('cr.document_id=?'); params.push(documentId); }
    if (type) { clauses.push('cr.type=?'); params.push(type); }
    if (warning) clauses.push("cr.warnings_json!='[]'");
    if (query) { clauses.push('(cr.content_text LIKE ? OR cr.id LIKE ? OR cr.chunk_logical_id LIKE ?)'); params.push(`%${query}%`, `%${query}%`, `%${query}%`); }
    params.push(limit, offset);
    return this.db.all(`SELECT cr.*,d.title AS document_title FROM chunk_revisions cr JOIN documents d ON d.id=cr.document_id
      WHERE ${clauses.join(' AND ')} ORDER BY d.title,cr.created_at LIMIT ? OFFSET ?`, ...params);
  }

  getChunk(chunkRevisionId, workspaceId = this.workspaceId()) {
    const chunk = this.db.one(`SELECT cr.*,d.title AS document_title FROM chunk_revisions cr JOIN documents d ON d.id=cr.document_id
      WHERE cr.id=? AND cr.workspace_id=? AND d.status!='deleted'`, chunkRevisionId, workspaceId);
    if (!chunk) throw new AppError(404, 'NOT_FOUND', '知识块不存在或不可访问');
    chunk.history = this.db.all('SELECT * FROM chunk_revisions WHERE chunk_logical_id=? AND workspace_id=? ORDER BY revision_number DESC', chunk.chunk_logical_id, workspaceId);
    chunk.releases = this.db.all('SELECT kr.id,kr.version,kr.status,kr.created_at FROM release_chunks rc JOIN knowledge_releases kr ON kr.id=rc.release_id WHERE rc.chunk_revision_id=? AND kr.workspace_id=? ORDER BY kr.version DESC', chunkRevisionId, workspaceId);
    return chunk;
  }

  editChunk(chunkRevisionId, input, workspaceId = this.workspaceId(), requestId) {
    const current = this.getChunk(chunkRevisionId, workspaceId);
    const contentMd = required(input.contentMd ?? input.content, 'contentMd');
    const contentText = String(input.contentText || contentMd.replace(/[#*_`>\[\]()]/g, ' ')).trim();
    const nextId = id('cr');
    let nextRevision;
    const embedding = localEmbedding(contentText);
    this.db.transaction(() => {
      const latest = this.db.one('SELECT id,revision_number FROM chunk_revisions WHERE chunk_logical_id=? AND workspace_id=? ORDER BY revision_number DESC LIMIT 1', current.chunk_logical_id, workspaceId);
      if (current.id && (!latest || latest.id !== current.id)) throw new AppError(409, 'CHUNK_REVISION_CONFLICT', '知识块已被其他修订更新，请重新载入最新版本', { expectedRevisionId: current.id, currentRevisionId: latest?.id || null });
      nextRevision = (latest?.revision_number ?? current.revision_number) + 1;
      this.db.run('INSERT INTO chunk_revisions(id,workspace_id,chunk_logical_id,dataset_id,document_id,document_revision_id,artifact_id,revision_number,parent_chunk_id,type,content_md,content_text,source_locator_json,token_count,language,generated_by,confidence,warnings_json,sensitivity,excluded,supersedes_id,embedding_json,embedding_model,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        nextId, workspaceId, current.chunk_logical_id, current.dataset_id, current.document_id, current.document_revision_id, current.artifact_id, nextRevision,
        input.parentChunkId === undefined ? current.parent_chunk_id : input.parentChunkId, input.type || current.type, contentMd, contentText,
        JSON.stringify(input.sourceLocator || current.source_locator || {}), estimateTokens(contentText), current.language, 'human', 1,
        JSON.stringify([]), input.sensitivity || current.sensitivity, input.excluded === undefined ? current.excluded : input.excluded ? 1 : 0,
        current.id, JSON.stringify(embedding), 'ordo-hash-embedding-v1', now());
      this.db.run('INSERT INTO chunks_fts(chunk_revision_id,workspace_id,dataset_id,title,breadcrumb,content) VALUES(?,?,?,?,?,?)',
        nextId, workspaceId, current.dataset_id, current.document_title, `${current.document_title} / ${input.type || current.type}`, contentText);
    });
    this.audit.append({ workspaceId, action: 'chunk.revise', objectType: 'chunk_revision', objectId: nextId, requestId, details: { supersedes: current.id, revision: nextRevision } });
    return this.getChunk(nextId, workspaceId);
  }

  restoreChunk(chunkRevisionId, workspaceId = this.workspaceId(), requestId) {
    const source = this.getChunk(chunkRevisionId, workspaceId);
    const latest = this.db.one('SELECT * FROM chunk_revisions WHERE chunk_logical_id=? AND workspace_id=? ORDER BY revision_number DESC LIMIT 1', source.chunk_logical_id, workspaceId);
    return this.editChunk(latest.id, { contentMd: source.content_md, contentText: source.content_text, type: source.type, sourceLocator: source.source_locator, excluded: source.excluded, sensitivity: source.sensitivity }, workspaceId, requestId);
  }

  diffChunk(chunkRevisionId, againstId, workspaceId = this.workspaceId()) {
    const current = this.getChunk(chunkRevisionId, workspaceId);
    const against = againstId ? this.getChunk(againstId, workspaceId) : current.history.find(item => item.id !== current.id);
    if (!against) throw new AppError(400, 'REVISION_REQUIRED', '必须提供可比较的块修订');
    if (against.chunk_logical_id !== current.chunk_logical_id) throw new AppError(400, 'SCOPE_MISMATCH', '只能比较同一逻辑块的修订');
    const before = String(against.content_text || '').split(/\r?\n/);
    const after = String(current.content_text || '').split(/\r?\n/);
    const changes = [];
    const max = Math.max(before.length, after.length);
    for (let index = 0; index < max; index += 1) {
      if (before[index] === after[index]) changes.push({ type: 'equal', line: index + 1, before: before[index] ?? null, after: after[index] ?? null });
      else {
        if (before[index] !== undefined) changes.push({ type: 'removed', line: index + 1, before: before[index], after: null });
        if (after[index] !== undefined) changes.push({ type: 'added', line: index + 1, before: null, after: after[index] });
      }
    }
    return { chunkLogicalId: current.chunk_logical_id, from: { id: against.id, revision: against.revision_number }, to: { id: current.id, revision: current.revision_number }, changed: changes.some(item => item.type !== 'equal'), changes };
  }

  splitChunk(chunkRevisionId, input = {}, workspaceId = this.workspaceId(), requestId) {
    const current = this.getChunk(chunkRevisionId, workspaceId);
    let parts = Array.isArray(input.parts) ? input.parts : [];
    if (!parts.length && input.separator) parts = current.content_text.split(String(input.separator)).map(item => item.trim()).filter(Boolean);
    if (parts.length < 2) throw new AppError(400, 'SPLIT_PARTS_INVALID', '拆分至少需要两个非空部分');
    const created = [];
    this.db.transaction(() => {
      const originalNext = this.createChunkRevision(current, { contentMd: current.content_md, contentText: current.content_text, excluded: true, generatedBy: 'human' }, workspaceId, current.parent_chunk_id);
      created.push(originalNext);
      parts.forEach((part, index) => {
        const content = required(part, `parts[${index}]`);
        const logicalId = id('chunk');
        this.db.run('INSERT INTO chunk_logicals(id,workspace_id,dataset_id,document_id,created_at) VALUES(?,?,?,?,?)', logicalId, workspaceId, current.dataset_id, current.document_id, now());
        created.push(this.createChunkRevision({ ...current, chunk_logical_id: logicalId, id: null, revision_number: 0, content_md: content, content_text: content, type: input.type || current.type, parent_chunk_id: current.id }, { contentMd: content, contentText: content, excluded: false, generatedBy: 'human' }, workspaceId, current.id));
      });
    });
    this.audit.append({ workspaceId, action: 'chunk.split', objectType: 'chunk_revision', objectId: current.id, requestId, details: { parts: parts.length, created: created.map(item => item.id) } });
    return { original: this.getChunk(created[0].id, workspaceId), parts: created.slice(1).map(item => this.getChunk(item.id, workspaceId)) };
  }

  mergeChunks(input = {}, workspaceId = this.workspaceId(), requestId) {
    const ids = Array.isArray(input.chunkRevisionIds) ? input.chunkRevisionIds : [];
    if (ids.length < 2) throw new AppError(400, 'MERGE_CHUNKS_INVALID', '合并至少需要两个知识块');
    const chunks = ids.map(chunkId => this.getChunk(chunkId, workspaceId));
    const first = chunks[0];
    if (chunks.some(chunk => chunk.dataset_id !== first.dataset_id || chunk.document_id !== first.document_id)) throw new AppError(400, 'SCOPE_MISMATCH', '只能合并同一数据集和文档中的知识块');
    const contentText = input.contentText || chunks.map(chunk => chunk.content_text).join(input.separator === undefined ? '\n\n' : String(input.separator));
    const contentMd = input.contentMd || chunks.map(chunk => chunk.content_md).join(input.separator === undefined ? '\n\n' : String(input.separator));
    let merged;
    const excluded = [];
    this.db.transaction(() => {
      const logicalId = id('chunk');
      this.db.run('INSERT INTO chunk_logicals(id,workspace_id,dataset_id,document_id,created_at) VALUES(?,?,?,?,?)', logicalId, workspaceId, first.dataset_id, first.document_id, now());
      merged = this.createChunkRevision({ ...first, chunk_logical_id: logicalId, id: null, revision_number: 0, content_md: contentMd, content_text: contentText, type: input.type || first.type, parent_chunk_id: first.id }, { contentMd, contentText, excluded: false, generatedBy: 'human' }, workspaceId, first.id, true);
      chunks.forEach(chunk => {
        const next = this.createChunkRevision(chunk, { contentMd: chunk.content_md, contentText: chunk.content_text, excluded: true, generatedBy: 'human' }, workspaceId, merged.id);
        excluded.push(next);
      });
    });
    this.audit.append({ workspaceId, action: 'chunk.merge', objectType: 'chunk_revision', objectId: merged.id, requestId, details: { inputs: ids, excluded: excluded.map(item => item.id) } });
    return { merged: this.getChunk(merged.id, workspaceId), excluded: excluded.map(item => this.getChunk(item.id, workspaceId)) };
  }

  createChunkRevision(current, input, workspaceId, parentChunkId, returnLogical = false) {
    const logicalId = current.chunk_logical_id;
    if (!this.db.one('SELECT id FROM chunk_logicals WHERE id=? AND workspace_id=?', logicalId, workspaceId)) throw new AppError(409, 'CHUNK_LOGICAL_MISSING', '逻辑块不存在');
    const nextId = id('cr');
    const latest = current.id ? this.db.one('SELECT id,revision_number FROM chunk_revisions WHERE chunk_logical_id=? AND workspace_id=? ORDER BY revision_number DESC LIMIT 1', logicalId, workspaceId) : null;
    if (current.id && (!latest || latest.id !== current.id)) throw new AppError(409, 'CHUNK_REVISION_CONFLICT', '知识块已被其他修订更新，请重新载入最新版本', { expectedRevisionId: current.id, currentRevisionId: latest?.id || null });
    const nextRevision = (latest?.revision_number ?? Number(current.revision_number || 0)) + 1;
    const contentMd = required(input.contentMd ?? input.contentText, 'contentMd');
    const contentText = String(input.contentText ?? contentMd).trim();
    const embedding = localEmbedding(contentText);
    this.db.run('INSERT INTO chunk_revisions(id,workspace_id,chunk_logical_id,dataset_id,document_id,document_revision_id,artifact_id,revision_number,parent_chunk_id,type,content_md,content_text,source_locator_json,token_count,language,generated_by,confidence,warnings_json,sensitivity,excluded,supersedes_id,embedding_json,embedding_model,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
      nextId, workspaceId, logicalId, current.dataset_id, current.document_id, current.document_revision_id, current.artifact_id, nextRevision, parentChunkId ?? current.parent_chunk_id ?? null, input.type || current.type,
      contentMd, contentText, JSON.stringify(input.sourceLocator || current.source_locator || {}), estimateTokens(contentText), current.language || 'zh-CN', input.generatedBy || 'human', 1, JSON.stringify([]), input.sensitivity || current.sensitivity || 'internal', input.excluded ? 1 : 0,
      current.id || null, JSON.stringify(embedding), 'ordo-hash-embedding-v1', now());
    this.db.run('INSERT INTO chunks_fts(chunk_revision_id,workspace_id,dataset_id,title,breadcrumb,content) VALUES(?,?,?,?,?,?)', nextId, workspaceId, current.dataset_id, current.document_title || '', `${current.document_title || ''} / ${input.type || current.type}`, contentText);
    return returnLogical ? { id: nextId, logicalId } : { id: nextId };
  }

  releaseContentFingerprint(datasetId, workspaceId) {
    const chunks = this.db.all(`SELECT cr.id,cr.chunk_logical_id,cr.revision_number,cr.document_revision_id,cr.artifact_id,
      cr.content_text,cr.sensitivity,d.id AS document_id,d.title AS document_title,d.status AS document_status
      FROM chunk_revisions cr JOIN documents d ON d.id=cr.document_id
      WHERE cr.dataset_id=? AND cr.workspace_id=? AND d.status!='deleted' AND cr.excluded=0
      AND cr.id=(SELECT cr2.id FROM chunk_revisions cr2 WHERE cr2.chunk_logical_id=cr.chunk_logical_id ORDER BY cr2.revision_number DESC LIMIT 1)
      ORDER BY cr.chunk_logical_id`, datasetId, workspaceId);
    return hash(stableJson(chunks.map(chunk => ({
      id: chunk.id,
      logicalId: chunk.chunk_logical_id,
      revision: chunk.revision_number,
      documentId: chunk.document_id,
      documentRevisionId: chunk.document_revision_id,
      artifactId: chunk.artifact_id,
      documentTitle: chunk.document_title,
      documentStatus: chunk.document_status,
      sensitivity: chunk.sensitivity,
      contentHash: hash(chunk.content_text || '')
    }))));
  }

  buildRelease(datasetId, input = {}, workspaceId = this.workspaceId(), requestId) {
    const dataset = this.ensureDataset(datasetId, workspaceId);
    const profile = input.indexProfileId
      ? this.db.one('SELECT * FROM index_profiles WHERE id=? AND knowledge_base_id=? AND workspace_id=?', input.indexProfileId, dataset.knowledge_base_id, workspaceId)
      : this.db.one('SELECT * FROM index_profiles WHERE id=(SELECT default_index_profile_id FROM knowledge_bases WHERE id=? AND workspace_id=?) OR (knowledge_base_id=? AND workspace_id=? AND NOT EXISTS (SELECT 1 FROM knowledge_bases WHERE id=? AND workspace_id=? AND default_index_profile_id IS NOT NULL)) ORDER BY created_at DESC LIMIT 1', dataset.knowledge_base_id, workspaceId, dataset.knowledge_base_id, workspaceId, dataset.knowledge_base_id, workspaceId);
    if (!profile) throw new AppError(409, 'INDEX_PROFILE_REQUIRED', '知识库没有可用索引配置');
    const activate = input.activate !== false;
    const allowReviewRequired = Boolean(input.allowReviewRequired);
    const contentFingerprint = this.releaseContentFingerprint(datasetId, workspaceId);
    const idempotencyKey = input.idempotencyKey || `release:${datasetId}:${profile.id}:${profile.config_hash}:${contentFingerprint}:${activate ? 'active' : 'ready'}:${allowReviewRequired ? 'review' : 'strict'}`;
    const releaseId = input.releaseId || `rel_${hash(idempotencyKey).slice(0, 32)}`;
    const task = this.tasks.create({ workspaceId, type: 'release.build', objectType: 'dataset', objectId: datasetId,
      idempotencyKey, input: { datasetId, indexProfileId: profile.id, releaseId, contentFingerprint, allowReviewRequired, activate } });
    this.audit.append({ workspaceId, action: 'release.build_requested', objectType: 'dataset', objectId: datasetId, requestId, details: { taskId: task.id, indexProfileId: profile.id } });
    return task;
  }

  async buildReleaseTask({ workspaceId, input, checkpoint }) {
    const dataset = this.ensureDataset(input.datasetId, workspaceId);
    const profile = this.db.one('SELECT * FROM index_profiles WHERE id=? AND workspace_id=? AND knowledge_base_id=?', input.indexProfileId, workspaceId, dataset.knowledge_base_id);
    if (!profile) throw new AppError(404, 'NOT_FOUND', '索引配置不存在');
    const currentContentFingerprint = this.releaseContentFingerprint(input.datasetId, workspaceId);
    if (input.contentFingerprint && input.contentFingerprint !== currentContentFingerprint) {
      throw new AppError(409, 'RELEASE_CONTENT_CHANGED', '可发布知识块在任务排队后发生变化，请重新发起发布');
    }
    const releaseId = input.releaseId || `rel_${hash(`release:${input.datasetId}:${input.indexProfileId}`).slice(0, 32)}`;
    const existingRelease = this.db.one('SELECT * FROM knowledge_releases WHERE id=? AND workspace_id=?', releaseId, workspaceId);
    if (existingRelease && ['active','ready','superseded','retained'].includes(existingRelease.status)) {
      return { releaseId: existingRelease.id, version: existingRelease.version, status: existingRelease.status, chunkCount: existingRelease.manifest?.chunkCount || 0, quality: existingRelease.quality || {} };
    }
    const version = existingRelease?.version || (this.db.one('SELECT COALESCE(MAX(version),0)+1 AS version FROM knowledge_releases WHERE dataset_id=? AND workspace_id=?', input.datasetId, workspaceId)?.version || 1);
    if (!existingRelease) {
      this.db.run('INSERT INTO knowledge_releases(id,workspace_id,dataset_id,knowledge_base_id,index_profile_id,version,status,manifest_json,quality_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)', releaseId, workspaceId, input.datasetId, dataset.knowledge_base_id, profile.id, version, 'building', '{}', '{}', 'pending', now());
    } else {
      this.db.run("UPDATE knowledge_releases SET status='building',manifest_json='{}',quality_json='{}',content_hash='pending',activated_at=NULL WHERE id=? AND workspace_id=?", releaseId, workspaceId);
      this.db.run('DELETE FROM release_chunks WHERE release_id=?', releaseId);
    }
    checkpoint(10, '快照文档和知识块');
    const problematic = this.db.all(`SELECT d.id,d.title,d.status FROM documents d WHERE d.dataset_id=? AND d.workspace_id=? AND d.status IN ('review_required','partial','failed','needs_password','quarantined')`, input.datasetId, workspaceId);
    if (problematic.length && !input.allowReviewRequired) throw new AppError(409, 'RELEASE_QUALITY_BLOCKED', '存在未通过质量门的文档，发布已阻止', { documents: problematic });
    const chunks = this.db.all(`SELECT cr.*,d.title AS document_title FROM chunk_revisions cr JOIN documents d ON d.id=cr.document_id
      WHERE cr.dataset_id=? AND cr.workspace_id=? AND d.status!='deleted' AND cr.excluded=0
      AND cr.id=(SELECT cr2.id FROM chunk_revisions cr2 WHERE cr2.chunk_logical_id=cr.chunk_logical_id ORDER BY cr2.revision_number DESC LIMIT 1)
      ORDER BY d.title,cr.created_at`, input.datasetId, workspaceId);
    if (!chunks.length) throw new AppError(409, 'NO_PUBLISHABLE_CHUNKS', '数据集没有可发布知识块');
    const contentHash = hash(chunks.map(chunk => `${chunk.id}:${hash(chunk.content_text)}`).join('|') + profile.config_hash);
    const manifest = {
      schemaVersion: 1, releaseId, workspaceId, knowledgeBaseId: dataset.knowledge_base_id, datasetId: input.datasetId,
      indexProfileId: profile.id, version, chunkCount: chunks.length,
      chunkRevisionIds: chunks.map(chunk => chunk.id), createdAt: now(), contentHash
    };
    checkpoint(35, '构建隔离全文投影');
    for (let index = 0; index < chunks.length; index += 1) {
      if (!chunks[index].embedding_json) {
        const embedding = localEmbedding(chunks[index].content_text);
        this.db.run('UPDATE chunk_revisions SET embedding_json=?,embedding_model=? WHERE id=?', JSON.stringify(embedding), 'ordo-hash-embedding-v1', chunks[index].id);
      }
      if (index % 50 === 0) checkpoint(35 + (index / chunks.length) * 35, '构建向量与全文投影', { indexed: index, total: chunks.length });
    }
    checkpoint(75, '验证条目、引用和投影一致性');
    this.db.run("UPDATE knowledge_releases SET status='validating' WHERE id=? AND workspace_id=?", releaseId, workspaceId);
    const invalid = chunks.filter(chunk => !chunk.document_id || !chunk.document_revision_id || !chunk.artifact_id || !chunk.content_text);
    if (invalid.length) throw new AppError(500, 'RELEASE_VALIDATION_FAILED', 'Release 来源链验证失败', { invalidChunkIds: invalid.map(item => item.id) });
    const quality = { valid: true, chunkCount: chunks.length, invalidReferences: 0, reviewRequiredDocuments: problematic.length, checkedAt: now() };
    checkpoint(95, input.activate ? '原子切换活动 Release' : 'Release 已就绪');
    this.db.transaction(() => {
      this.db.run('UPDATE knowledge_releases SET status=?,manifest_json=?,quality_json=?,content_hash=?,activated_at=? WHERE id=? AND workspace_id=?',
        input.activate ? 'active' : 'ready', JSON.stringify(manifest), JSON.stringify(quality), contentHash, input.activate ? now() : null, releaseId, workspaceId);
      chunks.forEach((chunk, index) => this.db.run('INSERT INTO release_chunks(release_id,chunk_revision_id,ordinal) VALUES(?,?,?)', releaseId, chunk.id, index + 1));
      if (input.activate) {
        this.db.run("UPDATE knowledge_releases SET status='superseded' WHERE dataset_id=? AND workspace_id=? AND status='active' AND id!=?", input.datasetId, workspaceId, releaseId);
        this.db.run('UPDATE datasets SET active_release_id=?,updated_at=? WHERE id=? AND workspace_id=?', releaseId, now(), input.datasetId, workspaceId);
        if (dataset.id === this.db.one('SELECT default_dataset_id FROM knowledge_bases WHERE id=?', dataset.knowledge_base_id)?.default_dataset_id) {
          this.db.run('UPDATE knowledge_bases SET active_release_id=?,updated_at=? WHERE id=? AND workspace_id=?', releaseId, now(), dataset.knowledge_base_id, workspaceId);
        }
      }
      this.audit.append({ workspaceId, action: input.activate ? 'release.activate' : 'release.ready', objectType: 'knowledge_release', objectId: releaseId,
        details: { datasetId: input.datasetId, version, chunkCount: chunks.length, previousReleaseId: dataset.active_release_id } });
    });
    return { releaseId, version, status: input.activate ? 'active' : 'ready', chunkCount: chunks.length, quality };
  }

  listReleases(datasetId, workspaceId = this.workspaceId()) {
    return this.db.all('SELECT * FROM knowledge_releases WHERE dataset_id=? AND workspace_id=? ORDER BY version DESC', datasetId, workspaceId);
  }

  getRelease(releaseId, workspaceId = this.workspaceId()) {
    const release = this.db.one('SELECT * FROM knowledge_releases WHERE id=? AND workspace_id=?', releaseId, workspaceId);
    if (!release) throw new AppError(404, 'NOT_FOUND', '知识版本不存在或不可访问');
    release.chunkCount = this.db.one('SELECT COUNT(*) AS count FROM release_chunks WHERE release_id=?', releaseId).count;
    return release;
  }

  releaseImpact(releaseId, workspaceId = this.workspaceId()) {
    const release = this.getRelease(releaseId, workspaceId);
    return {
      releaseId,
      datasetId: release.dataset_id,
      version: release.version,
      status: release.status,
      conversations: this.db.one('SELECT COUNT(*) AS count FROM conversations WHERE release_id=? AND workspace_id=? AND deleted_at IS NULL', releaseId, workspaceId)?.count || 0,
      traces: this.db.one('SELECT COUNT(*) AS count FROM query_traces WHERE release_id=? AND workspace_id=?', releaseId, workspaceId)?.count || 0,
      citations: this.db.one('SELECT COUNT(*) AS count FROM citations WHERE release_id=? AND workspace_id=?', releaseId, workspaceId)?.count || 0,
      assistantReleases: this.db.one('SELECT COUNT(*) AS count FROM assistant_releases WHERE knowledge_release_id=? AND workspace_id=?', releaseId, workspaceId)?.count || 0,
      active: release.status === 'active'
    };
  }

  rollbackRelease(releaseId, workspaceId = this.workspaceId(), requestId) {
    const target = this.getRelease(releaseId, workspaceId);
    if (!['ready','superseded','retained'].includes(target.status)) throw new AppError(409, 'INVALID_STATE', '只有已验证的历史 Release 可以回滚');
    const dataset = this.ensureDataset(target.dataset_id, workspaceId);
    const previousReleaseId = dataset.active_release_id;
    this.db.transaction(() => {
      this.db.run("UPDATE knowledge_releases SET status='superseded' WHERE dataset_id=? AND workspace_id=? AND status='active' AND id!=?", target.dataset_id, workspaceId, target.id);
      this.db.run("UPDATE knowledge_releases SET status='active',activated_at=? WHERE id=? AND workspace_id=?", now(), target.id, workspaceId);
      this.db.run('UPDATE datasets SET active_release_id=?,updated_at=? WHERE id=? AND workspace_id=?', target.id, now(), target.dataset_id, workspaceId);
      const kb = this.db.one('SELECT default_dataset_id FROM knowledge_bases WHERE id=? AND workspace_id=?', target.knowledge_base_id, workspaceId);
      if (kb?.default_dataset_id === target.dataset_id) this.db.run('UPDATE knowledge_bases SET active_release_id=?,updated_at=? WHERE id=? AND workspace_id=?', target.id, now(), target.knowledge_base_id, workspaceId);
    });
    const impact = this.releaseImpact(target.id, workspaceId);
    this.audit.append({ workspaceId, action: 'release.rollback', objectType: 'knowledge_release', objectId: target.id, requestId, details: { previousReleaseId, targetReleaseId: target.id, impact } });
    return { ...this.getRelease(target.id, workspaceId), previousReleaseId, impact };
  }

  activateRelease(releaseId, workspaceId = this.workspaceId(), requestId) {
    const release = this.getRelease(releaseId, workspaceId);
    if (!['ready','active','superseded','retained'].includes(release.status)) throw new AppError(409, 'INVALID_STATE', '当前 Release 不能激活');
    this.db.transaction(() => {
      this.db.run("UPDATE knowledge_releases SET status='superseded' WHERE dataset_id=? AND workspace_id=? AND status='active' AND id!=?", release.dataset_id, workspaceId, releaseId);
      this.db.run("UPDATE knowledge_releases SET status='active',activated_at=? WHERE id=? AND workspace_id=?", now(), releaseId, workspaceId);
      this.db.run('UPDATE datasets SET active_release_id=?,updated_at=? WHERE id=? AND workspace_id=?', releaseId, now(), release.dataset_id, workspaceId);
      const knowledgeBase = this.db.one('SELECT default_dataset_id FROM knowledge_bases WHERE id=? AND workspace_id=?', release.knowledge_base_id, workspaceId);
      if (knowledgeBase?.default_dataset_id === release.dataset_id) {
        this.db.run('UPDATE knowledge_bases SET active_release_id=?,updated_at=? WHERE id=? AND workspace_id=?', releaseId, now(), release.knowledge_base_id, workspaceId);
      }
    });
    this.audit.append({ workspaceId, action: 'release.activate', objectType: 'knowledge_release', objectId: releaseId, requestId, details: { datasetId: release.dataset_id, version: release.version } });
    return this.getRelease(releaseId, workspaceId);
  }

  searchRelease(releaseId, query, workspaceId = this.workspaceId(), options = {}) {
    const release = this.getRelease(releaseId, workspaceId);
    const profile = this.getIndexProfile(release.index_profile_id, workspaceId);
    const fusion = profile.config?.fusion || {};
    const fusionK = Math.max(1, Math.min(1000, Number(fusion.k || 60)));
    const limit = Math.max(1, Math.min(50, Number(options.limit || 10)));
    const vectorLimit = Math.max(limit * 2, Number(fusion.vectorTopK || 20));
    const fullTextLimit = Math.max(limit * 2, Number(fusion.fullTextTopK || 20));
    const releaseChunks = this.db.all(`SELECT cr.*,d.title AS document_title FROM release_chunks rc
      JOIN chunk_revisions cr ON cr.id=rc.chunk_revision_id JOIN documents d ON d.id=cr.document_id
      WHERE rc.release_id=? AND cr.workspace_id=? ORDER BY rc.ordinal`, releaseId, workspaceId);
    const queryVector = localEmbedding(query);
    const vector = releaseChunks.map(chunk => ({ chunk, score: cosine(queryVector, parseJson(chunk.embedding_json, [])) }))
      .sort((a,b) => b.score - a.score).slice(0, vectorLimit);
    let fullText = [];
    const terms = String(query).trim().split(/\s+/).filter(Boolean).map(term => `"${term.replaceAll('"', '""')}"`).join(' OR ');
    if (terms) {
      try {
        fullText = this.db.all(`SELECT f.chunk_revision_id, bm25(chunks_fts,2.0,1.5,1.0) AS rank
          FROM chunks_fts f JOIN release_chunks rc ON rc.chunk_revision_id=f.chunk_revision_id
          WHERE chunks_fts MATCH ? AND rc.release_id=? AND f.workspace_id=? LIMIT ?`, terms, releaseId, workspaceId, fullTextLimit);
      } catch { fullText = []; }
    }
    const scores = new Map();
    vector.forEach((item, index) => scores.set(item.chunk.id, { chunk: item.chunk, vectorScore: item.score, vectorRank: index + 1, fullTextRank: null, fullTextScore: null, rrf: 1 / (fusionK + index + 1) }));
    fullText.forEach((item, index) => {
      const chunk = releaseChunks.find(candidate => candidate.id === item.chunk_revision_id);
      if (!chunk) return;
      const current = scores.get(chunk.id) || { chunk, vectorScore: null, vectorRank: null, fullTextRank: null, fullTextScore: null, rrf: 0 };
      current.fullTextRank = index + 1;
      current.fullTextScore = Number((-item.rank).toFixed(6));
      current.rrf += 1 / (fusionK + index + 1);
      scores.set(chunk.id, current);
    });
    const results = [...scores.values()].sort((a,b) => {
      const aLex = lexicalScore(query, a.chunk.content_text);
      const bLex = lexicalScore(query, b.chunk.content_text);
      return (b.rrf + bLex * 0.01) - (a.rrf + aLex * 0.01);
    }).slice(0, limit).map((item, index) => ({
      rank: index + 1,
      chunkRevisionId: item.chunk.id,
      documentId: item.chunk.document_id,
      documentRevisionId: item.chunk.document_revision_id,
      title: item.chunk.document_title,
      content: item.chunk.content_text,
      locator: item.chunk.source_locator,
      vectorScore: item.vectorScore === null ? null : Number(item.vectorScore.toFixed(6)),
      vectorRank: item.vectorRank,
      fullTextScore: item.fullTextScore,
      fullTextRank: item.fullTextRank,
      fusionScore: Number(item.rrf.toFixed(8)),
      rerankScore: lexicalScore(query, item.chunk.content_text)
    }));
    return { release, query, routes: { vector: true, fullText: true, fusion: { method: 'rrf', k: fusionK, vectorTopK: vectorLimit, fullTextTopK: fullTextLimit }, rerank: 'local-lexical-v1' }, results };
  }
}

function lexicalScore(query, content) {
  const haystack = String(content || '').toLowerCase();
  const terms = [...new Set([
    ...(String(query || '').toLowerCase().match(/[\u3400-\u9fff]/g) || []),
    ...(String(query || '').toLowerCase().match(/[a-z0-9_./-]+/g) || [])
  ])];
  if (!terms.length) return 0;
  const matched = terms.filter(term => haystack.includes(term)).length;
  return matched / terms.length;
}

module.exports = { KnowledgeService, DEFAULT_INDEX_CONFIG, localEmbedding, cosine, lexicalScore };
