'use strict';

const fs = require('node:fs');
const path = require('node:path');
const dns = require('node:dns').promises;
const net = require('node:net');
const { DatabaseSync } = require('node:sqlite');
const { Client } = require('pg');
const { id, now, required, AppError, hash, redact } = require('./core');

function assertReadOnlySql(sql) {
  const text = String(sql || '').trim();
  if (!/^(SELECT|WITH)\b/i.test(text)) throw new AppError(400, 'QUERY_NOT_READ_ONLY', '查询模板只允许 SELECT 或 WITH');
  const withoutTrailing = text.replace(/;\s*$/, '');
  if (withoutTrailing.includes(';') || /--|\/\*|\*\//.test(withoutTrailing)) throw new AppError(400, 'QUERY_MULTIPLE_OR_COMMENT', '查询模板不允许多语句或 SQL 注释');
  if (/\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|GRANT|REVOKE|COPY|CALL|EXECUTE|ATTACH|DETACH|PRAGMA|VACUUM|ANALYZE|REINDEX|LOAD_EXTENSION)\b/i.test(withoutTrailing)) {
    throw new AppError(400, 'QUERY_NOT_READ_ONLY', '查询模板包含禁止的数据库操作');
  }
  return withoutTrailing;
}

function isBlockedAddress(address) {
  return isDangerousAddress(address);
}

function ipv4Number(address) {
  const parts = String(address).split('.').map(Number);
  if (parts.length !== 4 || parts.some(part => !Number.isInteger(part) || part < 0 || part > 255)) return null;
  return (((parts[0] * 256 + parts[1]) * 256 + parts[2]) * 256 + parts[3]) >>> 0;
}

function blockedPrivateIpv4(address) {
  const value = ipv4Number(address);
  if (value === null) return false;
  return value <= 0x00ffffff || (value >= 0x0a000000 && value <= 0x0affffff) ||
    (value >= 0x64400000 && value <= 0x647fffff) || (value >= 0x7f000000 && value <= 0x7fffffff) ||
    (value >= 0xa9fe0000 && value <= 0xa9feffff) || (value >= 0xac100000 && value <= 0xac1fffff) ||
    (value >= 0xc0a80000 && value <= 0xc0a8ffff) || value >= 0xe0000000;
}

function isDangerousAddress(address) {
  const value = String(address || '').replace(/^\[|\]$/g, '').toLowerCase();
  const mapped = value.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
  const ipv4 = mapped ? mapped[1] : value;
  if (blockedPrivateIpv4(ipv4)) return true;
  if (!net.isIP(value)) return true;
  return value === '::' || value === '::1' || /^(fc|fd)/.test(value) || /^fe[89ab]/.test(value) || /^ff/.test(value) || value === '169.254.169.254' || value === '100.100.100.200';
}

async function resolveDatabaseHost(host, allowLocal = false) {
  const value = required(host, 'host').toLowerCase();
  if (['metadata.google.internal','metadata.azure.internal','metadata'].includes(value)) throw new AppError(400, 'HOST_BLOCKED', '禁止连接云元数据地址');
  let records;
  try { records = await dns.lookup(value, { all: true, verbatim: true }); }
  catch { throw new AppError(400, 'HOST_RESOLUTION_FAILED', '数据库主机无法解析'); }
  if (!records.length || records.some(record => ['169.254.169.254','100.100.100.200'].includes(record.address))) throw new AppError(400, 'HOST_BLOCKED', '数据库主机解析到云元数据地址');
  if (!allowLocal && records.some(record => isDangerousAddress(record.address))) throw new AppError(400, 'HOST_BLOCKED', '数据库主机解析到本机或私网地址');
  return { hostname: value, address: records[0].address, family: records[0].family };
}

async function validateDatabaseHost(host, allowLocal = false) {
  await resolveDatabaseHost(host, allowLocal);
  return required(host, 'host').toLowerCase();
}

function pinnedSocket(address, family) {
  const socket = new net.Socket();
  const connect = socket.connect.bind(socket);
  socket.connect = (_port, _host) => connect({ port: _port, host: address, family });
  return socket;
}

class ConnectorService {
  constructor({ db, secretStore, artifactStore, knowledge, audit, config }) {
    this.db = db;
    this.secretStore = secretStore;
    this.artifactStore = artifactStore;
    this.knowledge = knowledge;
    this.audit = audit;
    this.config = config;
  }

  list(workspaceId = this.config.localWorkspaceId) {
    return this.db.all(`SELECT c.id,c.workspace_id,c.name,c.type,c.config_json,c.status,c.last_checked_at,c.last_error,c.created_at,c.updated_at,s.mask AS secret_mask
      FROM connectors c LEFT JOIN secrets s ON s.id=c.secret_ref WHERE c.workspace_id=? ORDER BY c.created_at`, workspaceId);
  }

  get(connectorId, workspaceId = this.config.localWorkspaceId, includeSecret = false) {
    const record = this.db.one('SELECT * FROM connectors WHERE id=? AND workspace_id=?', connectorId, workspaceId);
    if (!record) throw new AppError(404, 'NOT_FOUND', '数据库连接不存在或不可访问');
    if (!includeSecret) delete record.secret_ref;
    return record;
  }

  async create(input, workspaceId = this.config.localWorkspaceId, requestId) {
    const type = input.type;
    if (!['sqlite','postgresql'].includes(type)) throw new AppError(400, 'CONNECTOR_TYPE_INVALID', '仅支持 SQLite 或 PostgreSQL');
    const connectorId = id('conn');
    let config;
    let secret = null;
    if (type === 'sqlite') {
      const file = path.resolve(required(input.path, 'path'));
      if (!fs.existsSync(file) || !fs.statSync(file).isFile()) throw new AppError(400, 'SQLITE_FILE_INVALID', 'SQLite 文件不存在或不是普通文件');
      config = { path: file, timeoutMs: Math.min(Number(input.timeoutMs || 5000), 30000) };
    } else {
      const host = await validateDatabaseHost(input.host, this.config.allowLocalDatabaseHosts);
      const port = Number(input.port || 5432);
      if (!Number.isInteger(port) || port < 1 || port > 65535) throw new AppError(400, 'VALIDATION_ERROR', 'port 无效');
      config = { host, port, database: required(input.database, 'database'), ssl: input.ssl || false, timeoutMs: Math.min(Number(input.timeoutMs || 10000), 30000) };
      if (!input.username || !input.password) throw new AppError(400, 'CREDENTIALS_REQUIRED', 'PostgreSQL 需要用户名和密码');
      secret = this.secretStore.create(workspaceId, `connector:${connectorId}`, JSON.stringify({ username: input.username, password: input.password }));
    }
    const timestamp = now();
    try {
      this.db.run('INSERT INTO connectors(id,workspace_id,name,type,config_json,secret_ref,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
        connectorId, workspaceId, required(input.name, 'name'), type, JSON.stringify(config), secret?.id || null, 'unverified', timestamp, timestamp);
    } catch (error) {
      if (/UNIQUE constraint failed/.test(error.message)) throw new AppError(409, 'NAME_CONFLICT', '同名数据库连接已存在');
      throw error;
    }
    this.audit.append({ workspaceId, action: 'connector.create', objectType: 'connector', objectId: connectorId, requestId, details: { type, hasSecret: Boolean(secret) } });
    return this.get(connectorId, workspaceId);
  }

  async withConnection(record, workspaceId, fn) {
    if (record.type === 'sqlite') {
      let database;
      try { database = new DatabaseSync(record.config.path, { readOnly: true }); }
      catch { throw new AppError(502, 'CONNECTOR_UNREACHABLE', '无法以只读方式打开 SQLite 数据库'); }
      database.exec(`PRAGMA query_only=ON; PRAGMA trusted_schema=OFF; PRAGMA busy_timeout=${Math.min(Number(record.config.timeoutMs || 5000),30000)};`);
      try { return await fn({ type: 'sqlite', raw: database }); }
      finally { database.close(); }
    }
    const credentials = JSON.parse(this.secretStore.resolve(record.secret_ref, workspaceId));
    const resolved = await resolveDatabaseHost(record.config.host, this.config.allowLocalDatabaseHosts);
    const client = new Client({ host: record.config.host, port: record.config.port, database: record.config.database,
      user: credentials.username, password: credentials.password, ssl: record.config.ssl || false,
      stream: pinnedSocket(resolved.address, resolved.family),
      connectionTimeoutMillis: Math.min(Number(record.config.timeoutMs || 10000), 30000), application_name: 'ordo-readonly' });
    try {
      await client.connect();
      await client.query('BEGIN READ ONLY');
      await client.query(`SET LOCAL statement_timeout = ${Math.min(Number(record.config.timeoutMs || 10000),30000)}`);
      const result = await fn({ type: 'postgresql', raw: client });
      await client.query('ROLLBACK');
      return result;
    } catch (error) {
      try { await client.query('ROLLBACK'); } catch {}
      if (error instanceof AppError) throw error;
      throw new AppError(502, 'CONNECTOR_QUERY_FAILED', '数据库连接或只读查询失败', { reason: redact(error.message).slice(0, 180) });
    } finally { await client.end().catch(() => {}); }
  }

  async test(connectorId, workspaceId = this.config.localWorkspaceId, requestId) {
    const record = this.get(connectorId, workspaceId, true);
    const started = performance.now();
    try {
      const result = await this.withConnection(record, workspaceId, async connection => {
        if (connection.type === 'sqlite') return connection.raw.prepare('SELECT sqlite_version() AS version').get();
        return (await connection.raw.query('SELECT current_setting(\'server_version\') AS version, current_database() AS database')).rows[0];
      });
      const response = { available: true, type: record.type, latencyMs: Math.round(performance.now() - started), version: result.version, database: result.database || path.basename(record.config.path || '') };
      this.db.run("UPDATE connectors SET status='available',last_checked_at=?,last_error=NULL,updated_at=? WHERE id=?", now(), now(), connectorId);
      this.audit.append({ workspaceId, action: 'connector.test', objectType: 'connector', objectId: connectorId, requestId, details: response });
      return response;
    } catch (error) {
      this.db.run("UPDATE connectors SET status='unavailable',last_checked_at=?,last_error=?,updated_at=? WHERE id=?", now(), redact(error.message), now(), connectorId);
      throw error;
    }
  }

  async schema(connectorId, workspaceId = this.config.localWorkspaceId) {
    const record = this.get(connectorId, workspaceId, true);
    return this.withConnection(record, workspaceId, async connection => {
      if (connection.type === 'sqlite') {
        const objects = connection.raw.prepare("SELECT name,type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name").all();
        return objects.map(object => ({ ...object, columns: connection.raw.prepare(`PRAGMA table_info(${JSON.stringify(object.name)})`).all().map(column => ({ name: column.name, type: column.type, nullable: !column.notnull, primaryKey: Boolean(column.pk) })) }));
      }
      const rows = (await connection.raw.query(`SELECT table_schema,table_name,table_type,column_name,data_type,is_nullable
        FROM information_schema.columns JOIN information_schema.tables USING(table_schema,table_name)
        WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY table_schema,table_name,ordinal_position`)).rows;
      const map = new Map();
      for (const row of rows) {
        const key = `${row.table_schema}.${row.table_name}`;
        if (!map.has(key)) map.set(key, { schema: row.table_schema, name: row.table_name, type: row.table_type, columns: [] });
        map.get(key).columns.push({ name: row.column_name, type: row.data_type, nullable: row.is_nullable === 'YES' });
      }
      return [...map.values()];
    });
  }

  createTemplate(connectorId, input, workspaceId = this.config.localWorkspaceId, requestId) {
    this.get(connectorId, workspaceId);
    const templateId = id('qtpl');
    const timestamp = now();
    const sql = assertReadOnlySql(required(input.sql, 'sql'));
    const rowLimit = Math.max(1, Math.min(Number(input.rowLimit || 1000), 10000));
    const timeoutMs = Math.max(100, Math.min(Number(input.timeoutMs || 10000), 30000));
    this.db.run('INSERT INTO database_query_templates(id,workspace_id,connector_id,name,sql_text,params_json,row_limit,timeout_ms,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
      templateId, workspaceId, connectorId, required(input.name, 'name'), sql, JSON.stringify(input.params || []), rowLimit, timeoutMs, 'active', timestamp, timestamp);
    this.audit.append({ workspaceId, action: 'database_template.create', objectType: 'database_query_template', objectId: templateId, requestId, details: { connectorId, rowLimit, timeoutMs } });
    return this.getTemplate(templateId, workspaceId);
  }

  getTemplate(templateId, workspaceId = this.config.localWorkspaceId) {
    const record = this.db.one('SELECT * FROM database_query_templates WHERE id=? AND workspace_id=?', templateId, workspaceId);
    if (!record) throw new AppError(404, 'NOT_FOUND', '查询模板不存在或不可访问');
    return record;
  }

  listTemplates(connectorId, workspaceId = this.config.localWorkspaceId) {
    return this.db.all('SELECT * FROM database_query_templates WHERE connector_id=? AND workspace_id=? ORDER BY created_at DESC', connectorId, workspaceId);
  }

  async executeTemplate(templateId, values = [], workspaceId = this.config.localWorkspaceId, requestId) {
    const template = this.getTemplate(templateId, workspaceId);
    const record = this.get(template.connector_id, workspaceId, true);
    const expected = template.params || [];
    if (!Array.isArray(values) || values.length !== expected.length) throw new AppError(400, 'QUERY_PARAMS_INVALID', '查询参数数量与模板定义不一致');
    const started = performance.now();
    const result = await this.withConnection({ ...record, config: { ...record.config, timeoutMs: template.timeout_ms } }, workspaceId, async connection => {
      const sql = assertReadOnlySql(template.sql_text);
      if (connection.type === 'sqlite') {
        const rows = connection.raw.prepare(`SELECT * FROM (${sql}) AS ordo_query LIMIT ?`).all(...values, template.row_limit);
        return { rows: rows.map(row => ({ ...row })), fields: rows[0] ? Object.keys(rows[0]) : [], truncated: rows.length >= template.row_limit };
      }
      const response = await connection.raw.query(`SELECT * FROM (${sql}) AS ordo_query LIMIT ${template.row_limit}`, values);
      return { rows: response.rows, fields: response.fields.map(field => field.name), truncated: response.rowCount >= template.row_limit };
    });
    const response = { ...result, rowCount: result.rows.length, elapsedMs: Math.round(performance.now() - started), templateId };
    this.audit.append({ workspaceId, action: 'database_template.execute', objectType: 'database_query_template', objectId: templateId, requestId, details: { rowCount: response.rowCount, truncated: response.truncated, elapsedMs: response.elapsedMs } });
    return response;
  }

  async snapshot(templateId, input, workspaceId = this.config.localWorkspaceId, requestId) {
    const template = this.getTemplate(templateId, workspaceId);
    const result = await this.executeTemplate(templateId, input.values || [], workspaceId, requestId);
    const dataset = this.knowledge.ensureDataset(required(input.datasetId, 'datasetId'), workspaceId);
    const source = this.knowledge.createSource(dataset.id, { type: 'database', name: input.name || template.name, config: { connectorId: template.connector_id, templateId } }, workspaceId, requestId);
    const columns = result.fields;
    const csv = [columns, ...result.rows.map(row => columns.map(column => row[column] ?? ''))].map(row => row.map(value => {
      const text = String(value);
      return /[",\n]/.test(text) ? `"${text.replaceAll('"','""')}"` : text;
    }).join(',')).join('\n');
    const artifactKeys = this.artifactStore.writeDocument(workspaceId, id('dbsnap_art'), { 'snapshot.json': { schemaVersion: 1, templateId, connectorId: template.connector_id, fields: columns, rows: result.rows, createdAt: now() } });
    const registered = await this.knowledge.registerUpload(dataset.id, source.id, `${input.name || template.name}.csv`, Buffer.from(csv), 'text/csv', workspaceId, requestId);
    const snapshotId = id('dbsnap');
    const schemaHash = hash(JSON.stringify(columns));
    this.db.run('INSERT INTO database_snapshots(id,workspace_id,connector_id,template_id,dataset_id,source_id,row_count,schema_hash,watermark,artifact_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
      snapshotId, workspaceId, template.connector_id, templateId, dataset.id, source.id, result.rowCount, schemaHash, input.watermark || null, artifactKeys['snapshot.json'], now());
    this.audit.append({ workspaceId, action: 'database_snapshot.create', objectType: 'database_snapshot', objectId: snapshotId, requestId, details: { datasetId: dataset.id, rowCount: result.rowCount, documentId: registered.document.id } });
    return { id: snapshotId, datasetId: dataset.id, sourceId: source.id, rowCount: result.rowCount, schemaHash, document: registered.document, task: registered.task };
  }
}

module.exports = { ConnectorService, assertReadOnlySql, validateDatabaseHost, resolveDatabaseHost, isBlockedAddress };
