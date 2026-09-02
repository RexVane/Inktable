'use strict';

const crypto = require('node:crypto');
const { id, now, required, AppError, hash } = require('./core');

function canonicalRequest(method, route, timestamp, nonce, origin, body) {
  return [String(method).toUpperCase(), route, String(timestamp), nonce, origin, hash(Buffer.from(body || ''))].join('\n');
}

function sign(secret, canonical) { return crypto.createHmac('sha256', secret).update(canonical).digest('hex'); }

class WidgetService {
  constructor({ db, secretStore, product, query, audit, config }) {
    this.db = db;
    this.secretStore = secretStore;
    this.product = product;
    this.query = query;
    this.audit = audit;
    this.config = config;
    this.tokenKey = crypto.createHash('sha256').update(secretStore.key).update('ordo-widget-token-v1').digest();
  }

  createClient(assistantId, input, workspaceId = this.config.localWorkspaceId, requestId) {
    const assistant = this.product.getAssistant(assistantId, workspaceId);
    if (assistant.status !== 'published' || !assistant.active_release_id) throw new AppError(409, 'ASSISTANT_NOT_PUBLISHED', '助手必须发布后才能创建网站客户端');
    const origins = [...new Set((input.allowedOrigins || []).map(value => normalizeOrigin(value)))];
    if (!origins.length) throw new AppError(400, 'ORIGIN_REQUIRED', '至少配置一个允许来源');
    const clientId = `ordoc_${crypto.randomBytes(12).toString('hex')}`;
    const secretValue = crypto.randomBytes(32).toString('base64url');
    const secret = this.secretStore.create(workspaceId, `widget:${clientId}`, secretValue);
    const recordId = id('wcli');
    this.db.run('INSERT INTO widget_clients(id,workspace_id,assistant_id,client_id,secret_ref,allowed_origins_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
      recordId, workspaceId, assistantId, clientId, secret.id, JSON.stringify(origins), 'active', now());
    this.audit.append({ workspaceId, action: 'widget_client.create', objectType: 'widget_client', objectId: recordId, requestId, details: { assistantId, clientId, origins } });
    return { id: recordId, assistantId, clientId, clientSecret: secretValue, allowedOrigins: origins, warning: 'clientSecret 仅返回一次，请保存到客户服务端秘密存储。' };
  }

  listClients(assistantId, workspaceId = this.config.localWorkspaceId) {
    this.product.getAssistant(assistantId, workspaceId);
    return this.db.all(`SELECT wc.id,wc.assistant_id,wc.client_id,wc.allowed_origins_json,wc.status,wc.created_at,wc.rotated_at,s.mask AS secret_mask
      FROM widget_clients wc JOIN secrets s ON s.id=wc.secret_ref WHERE wc.assistant_id=? AND wc.workspace_id=? ORDER BY wc.created_at`, assistantId, workspaceId);
  }

  rotateClient(clientRecordId, workspaceId = this.config.localWorkspaceId, requestId) {
    const record = this.db.one('SELECT * FROM widget_clients WHERE id=? AND workspace_id=?', clientRecordId, workspaceId);
    if (!record) throw new AppError(404, 'NOT_FOUND', '网站客户端不存在或不可访问');
    const secretValue = crypto.randomBytes(32).toString('base64url');
    this.secretStore.replace(record.secret_ref, workspaceId, secretValue);
    this.db.run('UPDATE widget_clients SET rotated_at=? WHERE id=?', now(), record.id);
    this.audit.append({ workspaceId, action: 'widget_client.rotate', objectType: 'widget_client', objectId: record.id, requestId });
    return { id: record.id, clientId: record.client_id, clientSecret: secretValue, warning: '旧密钥已立即失效，新密钥仅返回一次。' };
  }

  verifySignedRequest({ clientId, timestamp, nonce, origin, signature, method, route, rawBody }) {
    const record = this.db.one("SELECT * FROM widget_clients WHERE client_id=? AND status='active'", required(clientId, 'clientId'));
    if (!record) throw new AppError(401, 'WIDGET_CLIENT_INVALID', '网站客户端无效');
    const normalizedOrigin = normalizeOrigin(origin);
    if (!(record.allowed_origins || []).includes(normalizedOrigin)) throw new AppError(403, 'WIDGET_ORIGIN_REJECTED', '请求来源不在允许列表');
    const milliseconds = Number(timestamp);
    if (!Number.isFinite(milliseconds) || Math.abs(Date.now() - milliseconds) > 5 * 60 * 1000) throw new AppError(401, 'WIDGET_TIMESTAMP_INVALID', '签名时间戳已过期');
    if (!/^[A-Za-z0-9_-]{16,128}$/.test(String(nonce || ''))) throw new AppError(400, 'WIDGET_NONCE_INVALID', 'nonce 格式无效');
    const secret = this.secretStore.resolve(record.secret_ref, record.workspace_id);
    const expected = sign(secret, canonicalRequest(method, route, timestamp, nonce, normalizedOrigin, rawBody));
    const provided = String(signature || '').toLowerCase();
    if (provided.length !== expected.length || !crypto.timingSafeEqual(Buffer.from(provided), Buffer.from(expected))) throw new AppError(401, 'WIDGET_SIGNATURE_INVALID', '网站请求签名无效');
    try {
      this.db.transaction(() => {
        this.db.run('DELETE FROM widget_nonces WHERE expires_at<?', now());
        this.db.run('INSERT INTO widget_nonces(client_id,nonce,expires_at,created_at) VALUES(?,?,?,?)', clientId, nonce, new Date(Date.now() + 10 * 60 * 1000).toISOString(), now());
      });
    } catch (error) {
      if (/UNIQUE constraint failed/i.test(error.message || '')) throw new AppError(409, 'WIDGET_REPLAY_REJECTED', '请求 nonce 已使用');
      throw error;
    }
    return { ...record, normalizedOrigin };
  }

  issueToken(input, headers, rawBody = '') {
    const client = this.verifySignedRequest({ clientId: headers['x-ordo-client'], timestamp: headers['x-ordo-timestamp'], nonce: headers['x-ordo-nonce'],
      origin: headers.origin || input.origin, signature: headers['x-ordo-signature'], method: 'POST', route: '/api/v1/public/widget/token', rawBody });
    const assistant = this.product.getAssistant(client.assistant_id, client.workspace_id);
    if (assistant.status !== 'published' || !assistant.active_release_id) throw new AppError(503, 'ASSISTANT_UNAVAILABLE', '网站助手当前不可用');
    const release = assistant.releases.find(item => item.id === assistant.active_release_id);
    if (!release || release.status !== 'published') throw new AppError(503, 'ASSISTANT_UNAVAILABLE', '网站助手发布版本不可用');
    const expiresAt = Date.now() + 15 * 60 * 1000;
    const payload = { version: 1, assistantId: assistant.id, assistantReleaseId: release.id, workspaceId: client.workspace_id, origin: client.normalizedOrigin, exp: expiresAt, nonce: crypto.randomBytes(12).toString('base64url') };
    const encoded = Buffer.from(JSON.stringify(payload)).toString('base64url');
    const signature = crypto.createHmac('sha256', this.tokenKey).update(encoded).digest('base64url');
    this.audit.append({ workspaceId: client.workspace_id, actorId: 'widget_client', action: 'widget.token_issue', objectType: 'assistant', objectId: assistant.id, details: { clientId: client.client_id, origin: client.normalizedOrigin, expiresAt: new Date(expiresAt).toISOString() } });
    return { token: `${encoded}.${signature}`, expiresAt: new Date(expiresAt).toISOString(), assistant: { id: assistant.id, name: assistant.name, config: release.config } };
  }

  verifyToken(token, origin) {
    const parts = String(token || '').split('.');
    if (parts.length !== 2) throw new AppError(401, 'WIDGET_TOKEN_INVALID', '访客令牌无效');
    const expected = crypto.createHmac('sha256', this.tokenKey).update(parts[0]).digest('base64url');
    if (parts[1].length !== expected.length || !crypto.timingSafeEqual(Buffer.from(parts[1]), Buffer.from(expected))) throw new AppError(401, 'WIDGET_TOKEN_INVALID', '访客令牌签名无效');
    let payload;
    try { payload = JSON.parse(Buffer.from(parts[0], 'base64url').toString('utf8')); } catch { throw new AppError(401, 'WIDGET_TOKEN_INVALID', '访客令牌载荷无效'); }
    if (payload.exp < Date.now()) throw new AppError(401, 'WIDGET_TOKEN_EXPIRED', '访客令牌已过期');
    if (normalizeOrigin(origin) !== payload.origin) throw new AppError(403, 'WIDGET_ORIGIN_REJECTED', '访客令牌与请求来源不匹配');
    return payload;
  }

  createVisitorSession(token, origin) {
    const payload = this.verifyToken(token, origin);
    const assistant = this.product.getAssistant(payload.assistantId, payload.workspaceId);
    const release = assistant.releases.find(item => item.id === payload.assistantReleaseId);
    if (!release || release.status !== 'published' || assistant.status !== 'published') throw new AppError(503, 'ASSISTANT_UNAVAILABLE', '网站助手当前不可用');
    const knowledgeRelease = this.product.knowledge.getRelease(release.knowledge_release_id, payload.workspaceId);
    const conversation = this.query.createConversation({ knowledgeBaseId: knowledgeRelease.knowledge_base_id, datasetId: knowledgeRelease.dataset_id, releaseId: knowledgeRelease.id, title: '网站访客会话', strictEvidence: true }, payload.workspaceId);
    const visitorSessionId = id('visitor');
    const pseudonym = `访客-${crypto.randomBytes(4).toString('hex')}`;
    const timestamp = now();
    const expiresAt = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toISOString();
    this.db.run('INSERT INTO visitor_sessions(id,workspace_id,assistant_id,assistant_release_id,conversation_id,pseudonym,origin,status,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
      visitorSessionId, payload.workspaceId, assistant.id, release.id, conversation.id, pseudonym, payload.origin, 'active', expiresAt, timestamp, timestamp);
    this.audit.append({ workspaceId: payload.workspaceId, actorId: pseudonym, action: 'visitor_session.create', objectType: 'visitor_session', objectId: visitorSessionId, details: { assistantId: assistant.id, origin: payload.origin, expiresAt } });
    return { id: visitorSessionId, pseudonym, expiresAt, privacy: '访客会话默认保留 30 天，可随时删除。不会跨站跟踪真实身份。' };
  }

  getVisitor(visitorSessionId, origin, token) {
    const payload = this.verifyToken(token, origin);
    const visitor = this.db.one("SELECT * FROM visitor_sessions WHERE id=? AND status='active' AND deleted_at IS NULL", visitorSessionId);
    if (!visitor || visitor.expires_at < now() || payload.assistantId !== visitor.assistant_id || payload.assistantReleaseId !== visitor.assistant_release_id || normalizeOrigin(origin) !== visitor.origin) throw new AppError(404, 'NOT_FOUND', '访客会话不存在或已失效');
    return visitor;
  }

  async ask(visitorSessionId, origin, token, input, requestId) {
    const visitor = this.getVisitor(visitorSessionId, origin, token);
    const result = await this.query.ask(visitor.conversation_id, { question: input.question, topK: input.topK || 8 }, visitor.workspace_id, requestId);
    this.db.run('UPDATE visitor_sessions SET updated_at=? WHERE id=?', now(), visitor.id);
    return { answer: result.assistantMessage.content, evidenceStatus: result.assistantMessage.evidence_status, citations: result.assistantMessage.citations.map(item => ({ ordinal: item.ordinal, title: item.title, excerpt: item.excerpt })), traceId: result.trace.id };
  }

  deleteVisitor(visitorSessionId, origin, token) {
    const visitor = this.getVisitor(visitorSessionId, origin, token);
    const timestamp = now();
    this.db.transaction(() => {
      this.db.run("UPDATE visitor_sessions SET status='deleted',deleted_at=?,updated_at=? WHERE id=?", timestamp, timestamp, visitor.id);
      this.db.run("UPDATE conversations SET status='deleted',deleted_at=?,updated_at=? WHERE id=?", timestamp, timestamp, visitor.conversation_id);
    });
    this.audit.append({ workspaceId: visitor.workspace_id, actorId: visitor.pseudonym, action: 'visitor_session.delete', objectType: 'visitor_session', objectId: visitor.id });
    return { deleted: true };
  }

  requestHandoff(visitorSessionId, origin, token, input) {
    const visitor = this.getVisitor(visitorSessionId, origin, token);
    const handoffId = id('handoff');
    const timestamp = now();
    this.db.run('INSERT INTO handoff_requests(id,workspace_id,visitor_session_id,status,priority,summary,contact_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
      handoffId, visitor.workspace_id, visitor.id, 'queued', input.priority || 'normal', required(input.summary, 'summary'), JSON.stringify(input.contact || {}), timestamp, timestamp);
    this.audit.append({ workspaceId: visitor.workspace_id, actorId: visitor.pseudonym, action: 'handoff.request', objectType: 'handoff_request', objectId: handoffId, details: { priority: input.priority || 'normal', hasContact: Boolean(input.contact && Object.keys(input.contact).length) } });
    return { id: handoffId, status: 'queued', createdAt: timestamp };
  }

  listHandoffs(workspaceId = this.config.localWorkspaceId, status) {
    if (status) return this.db.all('SELECT * FROM handoff_requests WHERE workspace_id=? AND status=? ORDER BY created_at', workspaceId, status);
    return this.db.all('SELECT * FROM handoff_requests WHERE workspace_id=? ORDER BY created_at', workspaceId);
  }

  updateHandoff(handoffId, input, workspaceId = this.config.localWorkspaceId, requestId) {
    const current = this.db.one('SELECT * FROM handoff_requests WHERE id=? AND workspace_id=?', handoffId, workspaceId);
    if (!current) throw new AppError(404, 'NOT_FOUND', '人工转接请求不存在或不可访问');
    const status = input.status || current.status;
    if (!['queued','accepted','completed','closed'].includes(status)) throw new AppError(400, 'VALIDATION_ERROR', '人工转接状态无效');
    this.db.run('UPDATE handoff_requests SET status=?,assigned_to=?,updated_at=? WHERE id=? AND workspace_id=?', status, input.assignedTo ?? current.assigned_to, now(), handoffId, workspaceId);
    this.audit.append({ workspaceId, action: 'handoff.update', objectType: 'handoff_request', objectId: handoffId, requestId, details: { status, assignedTo: input.assignedTo || null } });
    return this.db.one('SELECT * FROM handoff_requests WHERE id=?', handoffId);
  }
}

function normalizeOrigin(value) {
  let url;
  try { url = new URL(required(value, 'origin')); } catch { throw new AppError(400, 'ORIGIN_INVALID', '来源 URL 无效'); }
  if (!['http:','https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new AppError(400, 'ORIGIN_INVALID', '来源必须是纯 HTTP/HTTPS Origin');
  return url.origin;
}

module.exports = { WidgetService, normalizeOrigin, canonicalRequest, sign };
