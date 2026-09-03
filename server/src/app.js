'use strict';

const path = require('node:path');
const crypto = require('node:crypto');
const Fastify = require('fastify');
const multipart = require('@fastify/multipart');
const cors = require('@fastify/cors');
const rateLimit = require('@fastify/rate-limit');
const fastifyStatic = require('@fastify/static');
const mime = require('mime-types');
const { resolveConfig } = require('./config');
const { AppError, page, boundedInt } = require('./core');
const { OrdoDatabase } = require('./db');
const { ensureDataLayout, BlobStore, ArtifactStore, SecretStore, AuditLog } = require('./storage');
const { TaskService } = require('./tasks');
const { KnowledgeService } = require('./knowledge');
const { ModelService } = require('./models');
const { QueryService } = require('./query');
const { IngestService } = require('./ingest');
const { ProductService } = require('./product');
const { ConnectorService } = require('./connectors');
const { GraphService } = require('./graph');
const { WidgetService } = require('./widget');

function parseCookies(header = '') {
  return Object.fromEntries(String(header).split(';').map(item => item.trim()).filter(Boolean).map(item => {
    const index = item.indexOf('=');
    return index < 0 ? [item, ''] : [item.slice(0, index), decodeURIComponent(item.slice(index + 1))];
  }));
}

function createOpenApi(config) {
  const paths = {};
  const groups = {
    'System': ['/api/v1/session/bootstrap','/api/v1/health','/api/v1/dashboard','/api/v1/version','/api/v1/diagnostics','/api/v1/openapi.json'],
    'Knowledge bases': ['/api/v1/knowledge-bases','/api/v1/knowledge-bases/{id}','/api/v1/knowledge-bases/{id}/impact','/api/v1/knowledge-bases/{id}/datasets','/api/v1/knowledge-bases/{id}/index-profiles','/api/v1/index-profiles/{id}','/api/v1/index-profiles/{id}/default'],
    'Datasets and ingest': ['/api/v1/datasets/{id}','/api/v1/datasets/{id}/sources','/api/v1/datasets/{id}/files','/api/v1/datasets/{id}/archives','/api/v1/datasets/{id}/directory/preview','/api/v1/datasets/{id}/directory/import','/api/v1/datasets/{id}/documents'],
    'Documents and chunks': ['/api/v1/documents/{id}','/api/v1/artifacts/{id}/{kind}','/api/v1/datasets/{id}/chunks','/api/v1/chunks/{id}','/api/v1/chunks/{id}/revisions','/api/v1/chunks/{id}/restore','/api/v1/chunks/{id}/diff','/api/v1/chunks/{id}/split','/api/v1/chunks/merge'],
    'Releases and retrieval': ['/api/v1/datasets/{id}/releases','/api/v1/releases/{id}','/api/v1/releases/{id}/activate','/api/v1/releases/{id}/rollback','/api/v1/releases/{id}/impact','/api/v1/releases/{id}/search'],
    'Tasks': ['/api/v1/tasks','/api/v1/tasks/{id}','/api/v1/tasks/{id}/cancel','/api/v1/tasks/{id}/pause','/api/v1/tasks/{id}/resume','/api/v1/tasks/{id}/retry','/api/v1/tasks/{id}/wait'],
    'Connectors': ['/api/v1/connectors','/api/v1/connectors/{id}','/api/v1/connectors/{id}/test','/api/v1/connectors/{id}/schema','/api/v1/connectors/{id}/templates','/api/v1/query-templates/{id}/execute','/api/v1/query-templates/{id}/snapshot'],
    'Graph': ['/api/v1/knowledge-bases/{id}/ontologies','/api/v1/ontologies/{id}/publish','/api/v1/datasets/{id}/graph','/api/v1/datasets/{id}/graph/entities','/api/v1/datasets/{id}/graph/relations'],
    'Widget': ['/api/v1/assistants/{id}/clients','/api/v1/widget-clients/{id}','/api/v1/widget-clients/{id}/rotate','/api/v1/public/widget/token','/api/v1/public/widget/sessions','/api/v1/public/widget/sessions/{id}/messages','/api/v1/public/widget/sessions/{id}/handoff','/api/v1/public/widget/sessions/{id}','/api/v1/handoffs','/api/v1/handoffs/{id}'],
    'Conversations': ['/api/v1/conversations','/api/v1/conversations/{id}','/api/v1/conversations/{id}/messages','/api/v1/messages/{id}/feedback','/api/v1/traces','/api/v1/traces/{id}','/api/v1/citations/{id}'],
    'Models': ['/api/v1/models','/api/v1/models/{id}','/api/v1/models/{id}/test'],
    'Product': ['/api/v1/search','/api/v1/settings','/api/v1/settings/{key}','/api/v1/feature-flags','/api/v1/feature-flags/{key}','/api/v1/wiki','/api/v1/wiki/{id}','/api/v1/wiki/from-message/{id}','/api/v1/assistants','/api/v1/assistants/{id}','/api/v1/assistants/{id}/publish','/api/v1/assistants/{id}/pause','/api/v1/backups','/api/v1/backups/{id}/restore','/api/v1/audit','/api/v1/audit/verify']
  };
  const methods = {
    '/api/v1/session/bootstrap':['get'], '/api/v1/health':['get'], '/api/v1/dashboard':['get'], '/api/v1/version':['get'], '/api/v1/diagnostics':['get'], '/api/v1/openapi.json':['get'],
    '/api/v1/knowledge-bases':['get','post'], '/api/v1/knowledge-bases/{id}':['get','patch','delete'], '/api/v1/knowledge-bases/{id}/impact':['get'], '/api/v1/knowledge-bases/{id}/datasets':['get','post'], '/api/v1/knowledge-bases/{id}/index-profiles':['get','post'], '/api/v1/index-profiles/{id}':['get','patch','delete'], '/api/v1/index-profiles/{id}/default':['post'],
    '/api/v1/datasets/{id}':['get','patch','delete'], '/api/v1/datasets/{id}/sources':['get','post'], '/api/v1/datasets/{id}/files':['post'], '/api/v1/datasets/{id}/archives':['post'], '/api/v1/datasets/{id}/directory/preview':['post'], '/api/v1/datasets/{id}/directory/import':['post'], '/api/v1/datasets/{id}/documents':['get'],
    '/api/v1/documents/{id}':['get','delete'], '/api/v1/artifacts/{id}/{kind}':['get'], '/api/v1/datasets/{id}/chunks':['get'], '/api/v1/chunks/{id}':['get'], '/api/v1/chunks/{id}/revisions':['post'], '/api/v1/chunks/{id}/restore':['post'], '/api/v1/chunks/{id}/diff':['get'], '/api/v1/chunks/{id}/split':['post'], '/api/v1/chunks/merge':['post'],
    '/api/v1/datasets/{id}/releases':['get','post'], '/api/v1/releases/{id}':['get'], '/api/v1/releases/{id}/activate':['post'], '/api/v1/releases/{id}/rollback':['post'], '/api/v1/releases/{id}/impact':['get'], '/api/v1/releases/{id}/search':['post'],
    '/api/v1/tasks':['get'], '/api/v1/tasks/{id}':['get'], '/api/v1/tasks/{id}/cancel':['post'], '/api/v1/tasks/{id}/pause':['post'], '/api/v1/tasks/{id}/resume':['post'], '/api/v1/tasks/{id}/retry':['post'], '/api/v1/tasks/{id}/wait':['get'],
    '/api/v1/connectors':['get','post'], '/api/v1/connectors/{id}':['get'], '/api/v1/connectors/{id}/test':['post'], '/api/v1/connectors/{id}/schema':['get'], '/api/v1/connectors/{id}/templates':['get','post'], '/api/v1/query-templates/{id}/execute':['post'], '/api/v1/query-templates/{id}/snapshot':['post'],
    '/api/v1/knowledge-bases/{id}/ontologies':['get','post'], '/api/v1/ontologies/{id}/publish':['post'], '/api/v1/datasets/{id}/graph':['get'], '/api/v1/datasets/{id}/graph/entities':['get','post'], '/api/v1/datasets/{id}/graph/relations':['post'],
    '/api/v1/assistants/{id}/clients':['get','post'], '/api/v1/widget-clients/{id}':['delete'], '/api/v1/widget-clients/{id}/rotate':['post'], '/api/v1/public/widget/token':['post'], '/api/v1/public/widget/sessions':['post'], '/api/v1/public/widget/sessions/{id}/messages':['post'], '/api/v1/public/widget/sessions/{id}/handoff':['post'], '/api/v1/public/widget/sessions/{id}':['delete'], '/api/v1/handoffs':['get'], '/api/v1/handoffs/{id}':['patch'],

    '/api/v1/conversations':['get','post'], '/api/v1/conversations/{id}':['get','delete'], '/api/v1/conversations/{id}/messages':['post'], '/api/v1/messages/{id}/feedback':['post'], '/api/v1/traces':['get'], '/api/v1/traces/{id}':['get'], '/api/v1/citations/{id}':['get'],
    '/api/v1/models':['get','post'], '/api/v1/models/{id}':['get','patch','delete'], '/api/v1/models/{id}/test':['post'],
    '/api/v1/search':['get'], '/api/v1/settings':['get'], '/api/v1/settings/{key}':['put'], '/api/v1/feature-flags':['get'], '/api/v1/feature-flags/{key}':['put'], '/api/v1/wiki':['get','post'], '/api/v1/wiki/{id}':['get','post'], '/api/v1/wiki/from-message/{id}':['post'], '/api/v1/assistants':['get','post'], '/api/v1/assistants/{id}':['get','patch'], '/api/v1/assistants/{id}/publish':['post'], '/api/v1/assistants/{id}/pause':['post'], '/api/v1/backups':['get','post'], '/api/v1/backups/{id}/restore':['post'], '/api/v1/audit':['get'], '/api/v1/audit/verify':['get']
  };
  for (const [tag, routes] of Object.entries(groups)) for (const route of routes) {
    paths[route] ||= {};
    for (const method of methods[route] || ['get']) paths[route][method] = {
      tags: [tag], summary: `${method.toUpperCase()} ${route}`, operationId: `${method}_${route.replace(/[^a-z0-9]+/gi,'_')}`,
      responses: { '200': { description: 'Successful response', content: { 'application/json': { schema: { $ref: '#/components/schemas/Envelope' } } } }, '400': { $ref: '#/components/responses/Error' }, '401': { $ref: '#/components/responses/Error' }, '404': { $ref: '#/components/responses/Error' } }
    };
  }
  return {
    openapi: '3.1.0', info: { title: 'Ordo Product API', version: config.appVersion, description: 'Local-first knowledge product API. All mutating requests require the local session cookie and CSRF header.' },
    servers: [{ url: `http://${config.host}:${config.port}` }], tags: Object.keys(groups).map(name => ({ name })), paths,
    components: {
      securitySchemes: { localSession: { type: 'apiKey', in: 'cookie', name: 'ordo_session' }, csrf: { type: 'apiKey', in: 'header', name: 'x-ordo-csrf' } },
      schemas: { Envelope: { type: 'object', required: ['data'], properties: { data: {}, meta: { type: 'object' } } }, Error: { type: 'object', properties: { error: { type: 'object', required: ['code','message','requestId'], properties: { code: { type:'string' }, message: { type:'string' }, requestId: { type:'string' }, details: {} } } } } },
      responses: { Error: { description: 'Error response', content: { 'application/json': { schema: { $ref: '#/components/schemas/Error' } } } } }
    }, security: [{ localSession: [], csrf: [] }]
  };
}

async function createApp(overrides = {}) {
  const config = resolveConfig(overrides);
  ensureDataLayout(config);
  const remoteHost = !['127.0.0.1','localhost','::1'].includes(String(config.host).toLowerCase());
  if (remoteHost && (!config.allowRemote || !config.tlsTerminated || !config.remoteAdminToken)) throw new Error('Remote binding requires ORDO_ALLOW_REMOTE=true, TLS termination and ORDO_REMOTE_ADMIN_TOKEN');
  const app = Fastify({ logger: overrides.logger ?? { level: process.env.ORDO_LOG_LEVEL || 'info', redact: ['req.headers.authorization','req.headers.cookie','req.body.apiKey','req.body.password'] }, bodyLimit: config.bodyLimit, trustProxy: false, requestIdHeader: 'x-request-id' });
  app.addContentTypeParser('application/json', { parseAs: 'string' }, (request, body, done) => {
    request.rawBody = body;
    try { done(null, JSON.parse(body)); } catch { done(new AppError(400, 'JSON_INVALID', '请求体不是有效 JSON')); }
  });
  await app.register(multipart, { limits: { fileSize: config.maxFileBytes, files: 1, fields: 10, parts: 12 } });
  await app.register(rateLimit, { max: 300, timeWindow: '1 minute', keyGenerator: request => request.ip });

  const db = new OrdoDatabase(config);
  const blobStore = new BlobStore(config, db);
  const artifactStore = new ArtifactStore(config);
  const secretStore = new SecretStore(config, db);
  const audit = new AuditLog(db, config);
  const tasks = new TaskService(db, audit, config);
  const knowledge = new KnowledgeService({ db, blobStore, artifactStore, tasks, audit, config });
  const models = new ModelService({ db, secretStore, audit, config });
  const query = new QueryService({ db, knowledge, models, audit, config });
  const ingest = new IngestService({ db, knowledge, tasks, audit, config });
  const product = new ProductService({ db, knowledge, query, models, tasks, audit, blobStore, artifactStore, secretStore, config });
  const connectors = new ConnectorService({ db, secretStore, artifactStore, knowledge, audit, config });
  const graph = new GraphService({ db, knowledge, audit, config });
  const widget = new WidgetService({ db, secretStore, product, query, audit, config });
  const services = { config, db, blobStore, artifactStore, secretStore, audit, tasks, knowledge, models, query, ingest, product, connectors, graph, widget };
  app.decorate('services', services);
  await app.register(cors, {
    origin(origin, callback) {
      if (!origin) return callback(null, false);
      let normalized;
      try { normalized = new URL(origin).origin; } catch { return callback(null, false); }
      const allowed = db.all("SELECT allowed_origins_json FROM widget_clients WHERE status='active'").some(record => (record.allowed_origins || []).includes(normalized));
      callback(null, allowed);
    },
    methods: ['POST','DELETE','OPTIONS'],
    allowedHeaders: ['content-type','authorization','x-ordo-client','x-ordo-timestamp','x-ordo-nonce','x-ordo-signature'],
    credentials: false,
    maxAge: 600,
    strictPreflight: true
  });

  const session = { token: crypto.randomBytes(32).toString('base64url'), csrf: crypto.randomBytes(24).toString('base64url'), createdAt: Date.now(), maxAgeMs: 12 * 60 * 60 * 1000 };
  const publicPaths = new Set(['/api/v1/session/bootstrap','/api/v1/health','/api/v1/openapi.json','/api/v1/public/widget/token','/api/v1/public/widget/sessions']);
  app.addHook('onRequest', async request => {
    if (!request.url.startsWith('/api/')) return;
    const routePath = request.url.split('?')[0];
    if (publicPaths.has(routePath) || routePath.startsWith('/api/v1/public/widget/sessions/')) return;
    const cookies = parseCookies(request.headers.cookie);
    const token = cookies.ordo_session || String(request.headers.authorization || '').replace(/^Bearer\s+/i, '');
    if (!token || token.length !== session.token.length || !crypto.timingSafeEqual(Buffer.from(token), Buffer.from(session.token)) || Date.now() - session.createdAt > session.maxAgeMs) {
      throw new AppError(401, 'SESSION_REQUIRED', '本机会话无效或已过期');
    }
    if (!['GET','HEAD','OPTIONS'].includes(request.method)) {
      const csrf = String(request.headers['x-ordo-csrf'] || '');
      if (!csrf || csrf.length !== session.csrf.length || !crypto.timingSafeEqual(Buffer.from(csrf), Buffer.from(session.csrf))) throw new AppError(403, 'CSRF_INVALID', '写请求缺少有效 CSRF 令牌');
    }
    request.workspaceId = config.localWorkspaceId;
    request.actorId = config.localOwnerId;
  });

  app.setErrorHandler((error, request, reply) => {
    const status = error instanceof AppError ? error.statusCode : error.statusCode >= 400 && error.statusCode < 600 ? error.statusCode : 500;
    const code = error instanceof AppError ? error.code : error.code === 'FST_REQ_FILE_TOO_LARGE' ? 'FILE_TOO_LARGE' : status === 429 ? 'RATE_LIMITED' : 'INTERNAL_ERROR';
    const message = error instanceof AppError ? error.message : status === 500 ? '服务处理请求时发生错误' : error.message;
    if (status >= 500) request.log.error({ err: error, requestId: request.id }, 'request failed');
    reply.status(status).send({ error: { code, message, requestId: request.id, ...(error.details ? { details: error.details } : {}) } });
  });
  app.setNotFoundHandler((request, reply) => {
    if (request.url.startsWith('/api/')) return reply.status(404).send({ error: { code: 'ROUTE_NOT_FOUND', message: 'API 路由不存在', requestId: request.id } });
    reply.status(404).type('text/plain; charset=utf-8').send('Not found');
  });
  const data = (value, meta) => ({ data: value, ...(meta ? { meta } : {}) });
  const paginated = result => data(result.items, {
    total: result.total,
    limit: result.limit,
    offset: result.offset,
    hasMore: result.offset + result.items.length < result.total
  });
  const workspace = request => request.workspaceId || config.localWorkspaceId;

  app.get('/api/v1/session/bootstrap', async (request, reply) => {
    if (request.headers['sec-fetch-site'] === 'cross-site') throw new AppError(403, 'ORIGIN_REJECTED', '跨站页面不能创建本机会话');
    if (remoteHost) {
      const provided = String(request.headers['x-ordo-admin-token'] || '');
      if (!provided || provided.length !== config.remoteAdminToken.length || !crypto.timingSafeEqual(Buffer.from(provided), Buffer.from(config.remoteAdminToken))) throw new AppError(401, 'REMOTE_AUTH_REQUIRED', '远程部署需要管理员初始化令牌');
    }
    if (Date.now() - session.createdAt > session.maxAgeMs) {
      session.token = crypto.randomBytes(32).toString('base64url');
      session.csrf = crypto.randomBytes(24).toString('base64url');
      session.createdAt = Date.now();
    }
    reply.header('Set-Cookie', `ordo_session=${encodeURIComponent(session.token)}; Path=/; HttpOnly; SameSite=Strict${remoteHost && config.tlsTerminated ? '; Secure' : ''}; Max-Age=${Math.floor(session.maxAgeMs / 1000)}`);
    reply.header('Cache-Control', 'no-store');
    return data({ csrfToken: session.csrf, expiresAt: new Date(session.createdAt + session.maxAgeMs).toISOString(), workspaceId: config.localWorkspaceId });
  });
  app.get('/api/v1/health', async () => data(product.health()));
  app.get('/api/v1/dashboard', async request => data(product.dashboard(workspace(request))));
  app.get('/api/v1/version', async () => data({ appVersion: config.appVersion, schemaVersion: product.schemaVersion(), deploymentProfile: config.deploymentProfile, platform: config.platform, node: process.version }));
  app.get('/api/v1/diagnostics', async request => data(product.diagnostics(workspace(request))));
  app.get('/api/v1/openapi.json', async () => createOpenApi(config));

  app.get('/api/v1/knowledge-bases', async request => data(knowledge.listKnowledgeBases(workspace(request))));
  app.post('/api/v1/knowledge-bases', async request => data(knowledge.createKnowledgeBase(request.body || {}, workspace(request), request.id)));
  app.get('/api/v1/knowledge-bases/:id', async request => data(knowledge.getKnowledgeBase(request.params.id, workspace(request))));
  app.patch('/api/v1/knowledge-bases/:id', async request => data(knowledge.updateKnowledgeBase(request.params.id, request.body || {}, workspace(request), request.id)));
  app.delete('/api/v1/knowledge-bases/:id', async request => data(knowledge.deleteKnowledgeBase(request.params.id, workspace(request), request.id)));
  app.get('/api/v1/knowledge-bases/:id/impact', async request => data(knowledge.knowledgeBaseImpact(request.params.id, workspace(request))));
  app.get('/api/v1/knowledge-bases/:id/datasets', async request => data(knowledge.listDatasets(request.params.id, workspace(request))));
  app.post('/api/v1/knowledge-bases/:id/datasets', async request => data(knowledge.createDataset(request.params.id, request.body || {}, workspace(request), request.id)));
  app.get('/api/v1/knowledge-bases/:id/index-profiles', async request => data(knowledge.getKnowledgeBase(request.params.id, workspace(request)).indexProfiles));
  app.post('/api/v1/knowledge-bases/:id/index-profiles', async request => data(knowledge.createIndexProfile(request.params.id, request.body || {}, workspace(request), request.id)));
  app.get('/api/v1/index-profiles/:id', async request => data(knowledge.getIndexProfile(request.params.id, workspace(request))));
  app.patch('/api/v1/index-profiles/:id', async request => data(knowledge.updateIndexProfile(request.params.id, request.body || {}, workspace(request), request.id)));
  app.delete('/api/v1/index-profiles/:id', async request => data(knowledge.archiveIndexProfile(request.params.id, workspace(request), request.id)));
  app.post('/api/v1/index-profiles/:id/default', async request => {
    const profile = knowledge.getIndexProfile(request.params.id, workspace(request));
    return data(knowledge.setDefaultIndexProfile(profile.knowledge_base_id, profile.id, workspace(request), request.id));
  });
  app.get('/api/v1/datasets/:id', async request => data(knowledge.getDataset(request.params.id, workspace(request))));
  app.patch('/api/v1/datasets/:id', async request => data(knowledge.updateDataset(request.params.id, request.body || {}, workspace(request), request.id)));
  app.delete('/api/v1/datasets/:id', async request => data(knowledge.deleteDataset(request.params.id, workspace(request), request.id)));
  app.get('/api/v1/datasets/:id/sources', async request => data(knowledge.listSources(request.params.id, workspace(request))));
  app.post('/api/v1/datasets/:id/sources', async request => data(knowledge.createSource(request.params.id, request.body || {}, workspace(request), request.id)));
  app.post('/api/v1/datasets/:id/files', async request => {
    const file = await request.file();
    if (!file) throw new AppError(400, 'FILE_REQUIRED', '请选择上传文件');
    const buffer = await file.toBuffer();
    const sourceId = file.fields?.sourceId?.value || request.query?.sourceId || knowledge.createSource(request.params.id, { type: 'upload', name: file.filename }, workspace(request), request.id).id;
    return data(await knowledge.registerUpload(request.params.id, sourceId, file.filename, buffer, file.mimetype, workspace(request), request.id));
  });
  app.post('/api/v1/datasets/:id/archives', async request => {
    const file = await request.file();
    if (!file) throw new AppError(400, 'FILE_REQUIRED', '请选择压缩包');
    return data(await ingest.archiveImport(request.params.id, file.filename, await file.toBuffer(), {}, workspace(request), request.id));
  });
  app.post('/api/v1/datasets/:id/directory/preview', async request => data(ingest.directoryPreview(request.body?.directory, request.body?.rules || {})));
  app.post('/api/v1/datasets/:id/directory/import', async request => data(ingest.directoryImport(request.params.id, request.body || {}, workspace(request), request.id)));
  app.get('/api/v1/datasets/:id/documents', async request => paginated(knowledge.listDocuments(request.params.id, workspace(request), { ...page(request.query), status: request.query?.status, query: request.query?.query })));
  app.get('/api/v1/documents/:id', async request => data(knowledge.getDocument(request.params.id, workspace(request))));
  app.delete('/api/v1/documents/:id', async request => data(knowledge.deleteDocument(request.params.id, workspace(request), request.id)));
  app.get('/api/v1/artifacts/:id/:kind', async (request, reply) => {
    const buffer = knowledge.artifactFile(request.params.id, request.params.kind, workspace(request));
    reply.type(request.params.kind === 'markdown' ? 'text/markdown; charset=utf-8' : 'application/json; charset=utf-8');
    return reply.send(buffer);
  });
  app.get('/api/v1/datasets/:id/chunks', async request => paginated(knowledge.listChunks(request.params.id, workspace(request), { ...page(request.query), query: request.query?.query, documentId: request.query?.documentId, type: request.query?.type, warning: request.query?.warning === 'true' })));
  app.get('/api/v1/chunks/:id', async request => data(knowledge.getChunk(request.params.id, workspace(request))));
  app.post('/api/v1/chunks/:id/revisions', async request => data(knowledge.editChunk(request.params.id, request.body || {}, workspace(request), request.id)));
  app.post('/api/v1/chunks/:id/restore', async request => data(knowledge.restoreChunk(request.params.id, workspace(request), request.id)));
  app.get('/api/v1/chunks/:id/diff', async request => data(knowledge.diffChunk(request.params.id, request.query?.against, workspace(request))));
  app.post('/api/v1/chunks/:id/split', async request => data(knowledge.splitChunk(request.params.id, request.body || {}, workspace(request), request.id)));
  app.post('/api/v1/chunks/merge', async request => data(knowledge.mergeChunks(request.body || {}, workspace(request), request.id)));

  app.get('/api/v1/datasets/:id/releases', async request => data(knowledge.listReleases(request.params.id, workspace(request))));
  app.post('/api/v1/datasets/:id/releases', async request => data(knowledge.buildRelease(request.params.id, request.body || {}, workspace(request), request.id)));
  app.get('/api/v1/releases/:id', async request => data(knowledge.getRelease(request.params.id, workspace(request))));
  app.post('/api/v1/releases/:id/activate', async request => data(knowledge.activateRelease(request.params.id, workspace(request), request.id)));
  app.post('/api/v1/releases/:id/rollback', async request => data(knowledge.rollbackRelease(request.params.id, workspace(request), request.id)));
  app.get('/api/v1/releases/:id/impact', async request => data(knowledge.releaseImpact(request.params.id, workspace(request))));
  app.post('/api/v1/releases/:id/search', async request => data(knowledge.searchRelease(request.params.id, request.body?.query, workspace(request), { limit: request.body?.limit })));

  app.get('/api/v1/tasks', async request => paginated(tasks.list(workspace(request), { ...page(request.query), status: request.query?.status, type: request.query?.type })));
  app.get('/api/v1/tasks/:id', async request => data(tasks.get(request.params.id, workspace(request))));
  app.post('/api/v1/tasks/:id/cancel', async request => data(tasks.cancel(request.params.id, workspace(request))));
  app.post('/api/v1/tasks/:id/pause', async request => data(tasks.pause(request.params.id, workspace(request))));
  app.post('/api/v1/tasks/:id/resume', async request => data(tasks.resume(request.params.id, workspace(request))));
  app.post('/api/v1/tasks/:id/retry', async request => data(tasks.retry(request.params.id, workspace(request))));
  app.get('/api/v1/tasks/:id/wait', async request => data(await tasks.wait(request.params.id, workspace(request), boundedInt(request.query?.timeoutMs, 10000, 10, 120000, 'timeoutMs'))));

  app.get('/api/v1/conversations', async request => paginated(query.listConversations(workspace(request), page(request.query))));
  app.post('/api/v1/conversations', async request => data(query.createConversation(request.body || {}, workspace(request), request.id)));
  app.get('/api/v1/conversations/:id', async request => data(query.getConversation(request.params.id, workspace(request))));
  app.delete('/api/v1/conversations/:id', async request => data(query.deleteConversation(request.params.id, workspace(request), request.id)));
  app.post('/api/v1/conversations/:id/messages', async request => data(await query.ask(request.params.id, request.body || {}, workspace(request), request.id)));
  app.post('/api/v1/messages/:id/feedback', async request => data(query.feedback(request.params.id, request.body || {}, workspace(request), request.id)));
  app.get('/api/v1/traces', async request => paginated(query.listTraces(workspace(request), { ...page(request.query), conversationId: request.query?.conversationId })));
  app.get('/api/v1/traces/:id', async request => data(query.getTrace(request.params.id, workspace(request))));
  app.get('/api/v1/citations/:id', async request => data(query.openCitation(request.params.id, workspace(request))));

  app.get('/api/v1/connectors', async request => { product.requireFeature('databaseConnectors', workspace(request)); return data(connectors.list(workspace(request))); });
  app.post('/api/v1/connectors', async request => { product.requireFeature('databaseConnectors', workspace(request)); return data(await connectors.create(request.body || {}, workspace(request), request.id)); });
  app.get('/api/v1/connectors/:id', async request => { product.requireFeature('databaseConnectors', workspace(request)); return data(connectors.get(request.params.id, workspace(request))); });
  app.post('/api/v1/connectors/:id/test', async request => { product.requireFeature('databaseConnectors', workspace(request)); return data(await connectors.test(request.params.id, workspace(request), request.id)); });
  app.get('/api/v1/connectors/:id/schema', async request => { product.requireFeature('databaseConnectors', workspace(request)); return data(await connectors.schema(request.params.id, workspace(request))); });
  app.get('/api/v1/connectors/:id/templates', async request => { product.requireFeature('databaseConnectors', workspace(request)); return data(connectors.listTemplates(request.params.id, workspace(request))); });
  app.post('/api/v1/connectors/:id/templates', async request => { product.requireFeature('databaseConnectors', workspace(request)); return data(connectors.createTemplate(request.params.id, request.body || {}, workspace(request), request.id)); });
  app.post('/api/v1/query-templates/:id/execute', async request => { product.requireFeature('databaseConnectors', workspace(request)); return data(await connectors.executeTemplate(request.params.id, request.body?.values || [], workspace(request), request.id)); });
  app.post('/api/v1/query-templates/:id/snapshot', async request => { product.requireFeature('databaseConnectors', workspace(request)); return data(await connectors.snapshot(request.params.id, request.body || {}, workspace(request), request.id)); });

  app.get('/api/v1/knowledge-bases/:id/ontologies', async request => { product.requireFeature('graph', workspace(request)); return data(graph.listOntologies(request.params.id, workspace(request))); });
  app.post('/api/v1/knowledge-bases/:id/ontologies', async request => { product.requireFeature('graph', workspace(request)); return data(graph.createOntology(request.params.id, request.body || {}, workspace(request), request.id)); });
  app.post('/api/v1/ontologies/:id/publish', async request => { product.requireFeature('graph', workspace(request)); return data(graph.publishOntology(request.params.id, workspace(request), request.id)); });
  app.get('/api/v1/datasets/:id/graph', async request => { product.requireFeature('graph', workspace(request)); return data(graph.graph(request.params.id, workspace(request))); });
  app.get('/api/v1/datasets/:id/graph/entities', async request => { product.requireFeature('graph', workspace(request)); return data(graph.listEntities(request.params.id, workspace(request), { ...page(request.query), q: request.query?.q, type: request.query?.type })); });
  app.post('/api/v1/datasets/:id/graph/entities', async request => { product.requireFeature('graph', workspace(request)); return data(graph.createEntity(request.params.id, request.body || {}, workspace(request), request.id)); });
  app.post('/api/v1/datasets/:id/graph/relations', async request => { product.requireFeature('graph', workspace(request)); return data(graph.createRelation(request.params.id, request.body || {}, workspace(request), request.id)); });

  app.get('/api/v1/models', async request => data(models.list(workspace(request))));
  app.post('/api/v1/models', async request => { if (request.body?.provider !== 'local-extractive') product.requireFeature('externalModels', workspace(request)); return data(await models.create(request.body || {}, workspace(request), request.id)); });
  app.get('/api/v1/models/:id', async request => data(models.get(request.params.id, workspace(request))));
  app.patch('/api/v1/models/:id', async request => { if (request.body?.baseUrl) product.requireFeature('externalModels', workspace(request)); return data(await models.update(request.params.id, request.body || {}, workspace(request), request.id)); });
  app.delete('/api/v1/models/:id', async request => data(models.remove(request.params.id, workspace(request), request.id)));
  app.post('/api/v1/models/:id/test', async request => { const model = models.get(request.params.id, workspace(request)); if (model.provider !== 'local-extractive') product.requireFeature('externalModels', workspace(request)); return data(await models.test(request.params.id, workspace(request), request.id)); });

  app.get('/api/v1/search', async request => data(product.globalSearch(request.query?.q, workspace(request), boundedInt(request.query?.limit, 30, 1, 100, 'limit'))));
  app.get('/api/v1/settings', async request => data(product.getSettings(workspace(request))));
  app.put('/api/v1/settings/:key', async request => {
    const body = request.body || {};
    // 兼容 Web 工作台 api 客户端的 {value:{...}} 包装；裸分组对象同样接受
    const value = body && typeof body === 'object' && !Array.isArray(body) && Object.keys(body).length === 1 && 'value' in body ? body.value : body;
    return data(product.updateSetting(request.params.key, value, workspace(request), request.id));
  });
  app.get('/api/v1/feature-flags', async request => data(product.featureFlags(workspace(request))));
  app.put('/api/v1/feature-flags/:key', async request => data(product.setFeatureFlag(request.params.key, request.body || {}, workspace(request), request.id)));
  app.get('/api/v1/wiki', async request => { product.requireFeature('wiki', workspace(request)); return data(product.listWiki(workspace(request), request.query?.knowledgeBaseId)); });
  app.post('/api/v1/wiki', async request => { product.requireFeature('wiki', workspace(request)); return data(product.createWiki(request.body || {}, workspace(request), request.id)); });
  app.get('/api/v1/wiki/:id', async request => { product.requireFeature('wiki', workspace(request)); return data(product.getWiki(request.params.id, workspace(request))); });
  app.post('/api/v1/wiki/:id', async request => { product.requireFeature('wiki', workspace(request)); return data(product.reviseWiki(request.params.id, request.body || {}, workspace(request), request.id)); });
  app.post('/api/v1/wiki/from-message/:id', async request => { product.requireFeature('wiki', workspace(request)); return data(product.wikiFromMessage(request.params.id, request.body || {}, workspace(request), request.id)); });
  app.get('/api/v1/assistants', async request => { product.requireFeature('assistants', workspace(request)); return data(product.listAssistants(workspace(request))); });
  app.post('/api/v1/assistants', async request => { product.requireFeature('assistants', workspace(request)); return data(product.createAssistant(request.body || {}, workspace(request), request.id)); });
  app.get('/api/v1/assistants/:id', async request => { product.requireFeature('assistants', workspace(request)); return data(product.getAssistant(request.params.id, workspace(request))); });
  app.patch('/api/v1/assistants/:id', async request => { product.requireFeature('assistants', workspace(request)); return data(product.updateAssistant(request.params.id, request.body || {}, workspace(request), request.id)); });
  app.post('/api/v1/assistants/:id/publish', async request => { product.requireFeature('assistants', workspace(request)); return data(product.publishAssistant(request.params.id, request.body || {}, workspace(request), request.id)); });
  app.post('/api/v1/assistants/:id/pause', async request => { product.requireFeature('assistants', workspace(request)); return data(product.pauseAssistant(request.params.id, workspace(request), request.id)); });
  app.get('/api/v1/assistants/:id/clients', async request => { product.requireFeature('websiteAssistant', workspace(request)); return data(widget.listClients(request.params.id, workspace(request))); });
  app.post('/api/v1/assistants/:id/clients', async request => { product.requireFeature('websiteAssistant', workspace(request)); return data(widget.createClient(request.params.id, request.body || {}, workspace(request), request.id)); });
  app.delete('/api/v1/widget-clients/:id', async request => { product.requireFeature('websiteAssistant', workspace(request)); return data(widget.revokeClient(request.params.id, workspace(request), request.id)); });
  app.post('/api/v1/widget-clients/:id/rotate', async request => { product.requireFeature('websiteAssistant', workspace(request)); return data(widget.rotateClient(request.params.id, workspace(request), request.id)); });
  app.post('/api/v1/public/widget/token', { config: { rateLimit: { max: 30, timeWindow: '1 minute' } } }, async request => { product.requireFeature('websiteAssistant', config.localWorkspaceId); return data(widget.issueToken(request.body || {}, request.headers, request.rawBody || JSON.stringify(request.body || {}))); });
  app.post('/api/v1/public/widget/sessions', { config: { rateLimit: { max: 30, timeWindow: '1 minute' } } }, async request => { product.requireFeature('websiteAssistant', config.localWorkspaceId); return data(widget.createVisitorSession(String(request.headers.authorization || '').replace(/^Bearer\s+/i,''), request.headers.origin || request.body?.origin)); });
  app.post('/api/v1/public/widget/sessions/:id/messages', { config: { rateLimit: { max: 20, timeWindow: '1 minute' } } }, async request => { product.requireFeature('websiteAssistant', config.localWorkspaceId); return data(await widget.ask(request.params.id, request.headers.origin, String(request.headers.authorization || '').replace(/^Bearer\s+/i,''), request.body || {}, request.id)); });
  app.post('/api/v1/public/widget/sessions/:id/handoff', { config: { rateLimit: { max: 5, timeWindow: '1 minute' } } }, async request => { product.requireFeature('websiteAssistant', config.localWorkspaceId); return data(widget.requestHandoff(request.params.id, request.headers.origin, String(request.headers.authorization || '').replace(/^Bearer\s+/i,''), request.body || {})); });
  app.delete('/api/v1/public/widget/sessions/:id', async request => { product.requireFeature('websiteAssistant', config.localWorkspaceId); return data(widget.deleteVisitor(request.params.id, request.headers.origin, String(request.headers.authorization || '').replace(/^Bearer\s+/i,''))); });
  app.get('/api/v1/handoffs', async request => { product.requireFeature('websiteAssistant', workspace(request)); return data(widget.listHandoffs(workspace(request), request.query?.status)); });
  app.patch('/api/v1/handoffs/:id', async request => { product.requireFeature('websiteAssistant', workspace(request)); return data(widget.updateHandoff(request.params.id, request.body || {}, workspace(request), request.id)); });
  app.get('/api/v1/backups', async request => data(product.listBackups(workspace(request))));
  app.post('/api/v1/backups', async request => data(product.requestBackup(request.body || {}, workspace(request), request.id)));
  app.post('/api/v1/backups/:id/restore', async request => data(product.requestRestore(request.params.id, request.body || {}, workspace(request), request.id)));
  app.get('/api/v1/audit', async request => {
    const pagination = page(request.query);
    return paginated(audit.list(workspace(request), pagination.limit, pagination.offset));
  });
  app.get('/api/v1/audit/verify', async request => data(audit.verify(workspace(request))));

  await app.register(fastifyStatic, { root: config.webRoot, prefix: '/', wildcard: false, index: ['index.html'], dotfiles: 'deny', decorateReply: false });

  app.addHook('onClose', async () => db.close());
  tasks.resumeQueued();
  return app;
}

module.exports = { createApp, createOpenApi, parseCookies };
