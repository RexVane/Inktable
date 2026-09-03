'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { id, hash, now, safeName, AppError, parseJson, redact } = require('./core');

function ensureDataLayout(config) {
  for (const directory of [
    path.dirname(config.dbPath), config.blobRoot, config.artifactRoot, config.backupRoot,
    config.taskRoot, config.runtimeRoot, config.logRoot
  ]) fs.mkdirSync(directory, { recursive: true });
}

function atomicWrite(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
  try {
    fs.writeFileSync(temporary, data, { flag: 'wx' });
    fs.renameSync(temporary, file);
  } catch (error) {
    try { fs.unlinkSync(temporary); } catch {}
    throw error;
  }
}

function assertWithin(root, candidate) {
  const resolvedRoot = path.resolve(root);
  const resolved = path.resolve(candidate);
  if (resolved !== resolvedRoot && !resolved.startsWith(`${resolvedRoot}${path.sep}`)) {
    throw new AppError(400, 'UNSAFE_PATH', '路径超出受管存储范围');
  }
  return resolved;
}

class BlobStore {
  constructor(config, db) {
    this.config = config;
    this.db = db;
  }

  put(workspaceId, buffer, mimeType = 'application/octet-stream') {
    if (!Buffer.isBuffer(buffer)) buffer = Buffer.from(buffer);
    const sha256 = hash(buffer);
    const existing = this.db.one('SELECT * FROM blobs WHERE workspace_id=? AND sha256=?', workspaceId, sha256);
    if (existing) return existing;
    const blobId = id('blob');
    const key = path.join(workspaceId, sha256.slice(0, 2), sha256.slice(2, 4), sha256);
    const target = assertWithin(this.config.blobRoot, path.join(this.config.blobRoot, key));
    if (!fs.existsSync(target)) atomicWrite(target, buffer);
    this.db.run('INSERT INTO blobs(id,workspace_id,sha256,size_bytes,mime_type,storage_key,created_at) VALUES(?,?,?,?,?,?,?)',
      blobId, workspaceId, sha256, buffer.length, mimeType, key, now());
    return this.db.one('SELECT * FROM blobs WHERE id=?', blobId);
  }

  get(blobId, workspaceId) {
    const record = this.db.one('SELECT * FROM blobs WHERE id=? AND workspace_id=?', blobId, workspaceId);
    if (!record) throw new AppError(404, 'NOT_FOUND', '对象不存在或不可访问');
    const file = assertWithin(this.config.blobRoot, path.join(this.config.blobRoot, record.storage_key));
    if (!fs.existsSync(file)) throw new AppError(500, 'BLOB_MISSING', '受管文件缺失', { blobId });
    return { record, file, buffer: fs.readFileSync(file) };
  }

  countAndSize(workspaceId) {
    return this.db.one('SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes),0) AS size_bytes FROM blobs WHERE workspace_id=?', workspaceId);
  }
}

class ArtifactStore {
  constructor(config) { this.config = config; }

  writeDocument(workspaceId, documentRevisionId, files) {
    const baseKey = path.join(workspaceId, documentRevisionId);
    const result = {};
    const written = [];
    try {
      for (const [name, value] of Object.entries(files)) {
        const filename = safeName(name);
        const key = path.join(baseKey, filename);
        const target = assertWithin(this.config.artifactRoot, path.join(this.config.artifactRoot, key));
        const content = typeof value === 'string' || Buffer.isBuffer(value) ? value : JSON.stringify(value, null, 2);
        atomicWrite(target, content);
        written.push(target);
        result[name] = key;
      }
      return result;
    } catch (error) {
      for (const file of written.reverse()) { try { fs.unlinkSync(file); } catch {} }
      throw error;
    }
  }

  read(key) {
    const file = assertWithin(this.config.artifactRoot, path.join(this.config.artifactRoot, key));
    if (!fs.existsSync(file)) throw new AppError(404, 'ARTIFACT_NOT_FOUND', '标准产物不存在');
    return fs.readFileSync(file);
  }

  countAndSize() {
    let count = 0;
    let size = 0;
    const walk = directory => {
      if (!fs.existsSync(directory)) return;
      for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
        const file = path.join(directory, entry.name);
        if (entry.isDirectory()) walk(file);
        else if (entry.isFile()) { count += 1; size += fs.statSync(file).size; }
      }
    };
    walk(this.config.artifactRoot);
    return { count, size_bytes: size };
  }
}

class SecretStore {
  constructor(config, db) {
    this.config = config;
    this.db = db;
    this.key = this.loadOrCreateKey();
  }

  loadOrCreateKey() {
    if (process.env.ORDO_MASTER_KEY) {
      const candidate = Buffer.from(process.env.ORDO_MASTER_KEY, 'base64');
      if (candidate.length !== 32) throw new Error('ORDO_MASTER_KEY must be a base64 encoded 32-byte key');
      return candidate;
    }
    if (fs.existsSync(this.config.keyPath)) {
      const candidate = fs.readFileSync(this.config.keyPath);
      if (candidate.length !== 32) throw new Error('Invalid Ordo master key file');
      return candidate;
    }
    const key = crypto.randomBytes(32);
    atomicWrite(this.config.keyPath, key);
    try { fs.chmodSync(this.config.keyPath, 0o600); } catch {}
    return key;
  }

  encrypt(value) {
    const iv = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv('aes-256-gcm', this.key, iv);
    const encrypted = Buffer.concat([cipher.update(String(value), 'utf8'), cipher.final()]);
    const tag = cipher.getAuthTag();
    return Buffer.concat([iv, tag, encrypted]).toString('base64');
  }

  decrypt(payload) {
    const packed = Buffer.from(payload, 'base64');
    const iv = packed.subarray(0, 12);
    const tag = packed.subarray(12, 28);
    const encrypted = packed.subarray(28);
    const decipher = crypto.createDecipheriv('aes-256-gcm', this.key, iv);
    decipher.setAuthTag(tag);
    return Buffer.concat([decipher.update(encrypted), decipher.final()]).toString('utf8');
  }

  create(workspaceId, purpose, value) {
    if (!value) throw new AppError(400, 'VALIDATION_ERROR', '秘密值不能为空');
    const secretId = id('sec');
    const text = String(value);
    const mask = text.length <= 4 ? '••••' : `••••${text.slice(-4)}`;
    this.db.run('INSERT INTO secrets(id,workspace_id,purpose,encrypted_value,mask,created_at) VALUES(?,?,?,?,?,?)',
      secretId, workspaceId, purpose, this.encrypt(text), mask, now());
    return { id: secretId, purpose, mask };
  }

  replace(secretId, workspaceId, value) {
    const record = this.db.one('SELECT * FROM secrets WHERE id=? AND workspace_id=?', secretId, workspaceId);
    if (!record) throw new AppError(404, 'NOT_FOUND', '秘密引用不存在');
    const text = String(value || '');
    if (!text) throw new AppError(400, 'VALIDATION_ERROR', '秘密值不能为空');
    const mask = text.length <= 4 ? '••••' : `••••${text.slice(-4)}`;
    this.db.run('UPDATE secrets SET encrypted_value=?,mask=?,rotated_at=? WHERE id=? AND workspace_id=?',
      this.encrypt(text), mask, now(), secretId, workspaceId);
    return { id: secretId, purpose: record.purpose, mask };
  }

  resolve(secretId, workspaceId) {
    const record = this.db.one('SELECT * FROM secrets WHERE id=? AND workspace_id=?', secretId, workspaceId);
    if (!record) throw new AppError(404, 'NOT_FOUND', '秘密引用不存在');
    return this.decrypt(record.encrypted_value);
  }

  metadata(secretId, workspaceId) {
    const record = this.db.one('SELECT id,purpose,mask,created_at,rotated_at FROM secrets WHERE id=? AND workspace_id=?', secretId, workspaceId);
    if (!record) throw new AppError(404, 'NOT_FOUND', '秘密引用不存在');
    return record;
  }
}

class AuditLog {
  constructor(db, config) {
    this.db = db;
    this.config = config;
  }

  append({ workspaceId = this.config.localWorkspaceId, actorId = this.config.localOwnerId, action, objectType, objectId, result = 'succeeded', requestId = null, details = {} }) {
    const previous = this.db.one('SELECT event_hash FROM audit_events WHERE workspace_id=? ORDER BY rowid DESC LIMIT 1', workspaceId)?.event_hash || 'GENESIS';
    const eventId = id('aud');
    const timestamp = now();
    const safeDetails = JSON.parse(JSON.stringify(details, (key, value) => {
      if (/password|secret|token|authorization|apiKey/i.test(key)) return '[REDACTED]';
      return typeof value === 'string' ? redact(value) : value;
    }));
    const payload = JSON.stringify({ eventId, workspaceId, actorId, action, objectType, objectId, result, requestId, details: safeDetails, timestamp, previous });
    const eventHash = hash(payload);
    this.db.run('INSERT INTO audit_events(id,workspace_id,actor_id,action,object_type,object_id,result,request_id,details_json,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
      eventId, workspaceId, actorId, action, objectType, objectId, result, requestId, JSON.stringify(safeDetails), previous, eventHash, timestamp);
    return this.db.one('SELECT * FROM audit_events WHERE id=?', eventId);
  }

  list(workspaceId, limit = 100, offset = 0) {
    const total = this.db.one('SELECT COUNT(*) AS count FROM audit_events WHERE workspace_id=?', workspaceId)?.count || 0;
    const items = this.db.all('SELECT id,workspace_id,actor_id,action,object_type,object_id,result,request_id,details_json,previous_hash,event_hash,created_at FROM audit_events WHERE workspace_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?', workspaceId, limit, offset);
    return { items, total, limit, offset };
  }

  verify(workspaceId) {
    const rows = this.db.all('SELECT * FROM audit_events WHERE workspace_id=? ORDER BY rowid', workspaceId);
    let previous = 'GENESIS';
    for (const row of rows) {
      if (row.previous_hash !== previous) return { valid: false, eventId: row.id, reason: 'previous_hash mismatch' };
      const details = parseJson(row.details_json, {});
      const payload = JSON.stringify({ eventId: row.id, workspaceId: row.workspace_id, actorId: row.actor_id, action: row.action, objectType: row.object_type, objectId: row.object_id, result: row.result, requestId: row.request_id, details, timestamp: row.created_at, previous });
      if (hash(payload) !== row.event_hash) return { valid: false, eventId: row.id, reason: 'event_hash mismatch' };
      previous = row.event_hash;
    }
    return { valid: true, count: rows.length, head: previous };
  }
}

module.exports = { ensureDataLayout, atomicWrite, assertWithin, BlobStore, ArtifactStore, SecretStore, AuditLog };
