import base64
import hmac
import json
import os
import time
import uuid
from collections import deque
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .config import resolve_config
from .core import AppError, bounded_int, page
from .db import OrdoDatabase
from .models import ModelService
from .knowledge import KnowledgeService
from .product import ProductService
from .storage import ArtifactStore, AuditLog, BlobStore, SecretStore, ensure_data_layout
from .tasks import TaskService

PUBLIC_PATHS = {'/api/v1/session/bootstrap', '/api/v1/health', '/api/v1/openapi.json',
                '/api/v1/public/widget/token', '/api/v1/public/widget/sessions'}


class CompactJSONResponse(JSONResponse):
    # 与 Node JSON.stringify 输出一致：紧凑分隔符、非 ASCII 原样 UTF-8
    def render(self, content):
        return json.dumps(content, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class RateLimiter:
    def __init__(self):
        self._buckets = {}
        self._lock = __import__('threading').Lock()

    def check(self, key, maximum=300, window_seconds=60):
        now_ms = time.monotonic()
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            while bucket and bucket[0] <= now_ms - window_seconds:
                bucket.popleft()
            if len(bucket) >= maximum:
                raise AppError(429, 'RATE_LIMITED', 'Rate limit exceeded, retry later')
            bucket.append(now_ms)


def _origin_of(header_value):
    parts = urlsplit(str(header_value or ''))
    if not parts.scheme or not parts.netloc:
        return None
    port = parts.port
    if (parts.scheme == 'http' and port == 80) or (parts.scheme == 'https' and port == 443):
        return f'{parts.scheme}://{parts.hostname}'
    return f'{parts.scheme}://{parts.netloc}'


def parse_cookies(header=''):
    cookies = {}
    for item in str(header or '').split(';'):
        item = item.strip()
        if not item:
            continue
        index = item.find('=')
        if index < 0:
            cookies[item] = ''
        else:
            cookies[item[:index]] = item[index + 1:]
    return cookies


CREATE_OPENAPI_GROUPS = {
    'System': ['/api/v1/session/bootstrap', '/api/v1/health', '/api/v1/dashboard', '/api/v1/version', '/api/v1/diagnostics', '/api/v1/openapi.json'],
    'Knowledge bases': ['/api/v1/knowledge-bases', '/api/v1/knowledge-bases/{id}', '/api/v1/knowledge-bases/{id}/impact', '/api/v1/knowledge-bases/{id}/datasets', '/api/v1/knowledge-bases/{id}/index-profiles', '/api/v1/index-profiles/{id}', '/api/v1/index-profiles/{id}/default'],
    'Datasets and ingest': ['/api/v1/datasets/{id}', '/api/v1/datasets/{id}/sources', '/api/v1/datasets/{id}/files', '/api/v1/datasets/{id}/archives', '/api/v1/datasets/{id}/directory/preview', '/api/v1/datasets/{id}/directory/import', '/api/v1/datasets/{id}/documents'],
    'Documents and chunks': ['/api/v1/documents/{id}', '/api/v1/artifacts/{id}/{kind}', '/api/v1/datasets/{id}/chunks', '/api/v1/datasets/{id}/indexing/stats', '/api/v1/datasets/{id}/indexing/pipeline', '/api/v1/datasets/{id}/chapters', '/api/v1/chunks/{id}', '/api/v1/chunks/{id}/lineage', '/api/v1/chunks/{id}/revisions', '/api/v1/chunks/{id}/vectorize', '/api/v1/chunks/{id}/toggle-disable', '/api/v1/chunks/{id}/restore', '/api/v1/chunks/{id}/diff', '/api/v1/chunks/{id}/split', '/api/v1/chunks/merge', '/api/v1/datasets/{id}/indexing/vectorize-pending', '/api/v1/datasets/{id}/indexing/rebuild-hnsw', '/api/v1/datasets/{id}/indexing/optimize-index', '/api/v1/datasets/{id}/indexing/rebuild-bm25', '/api/v1/datasets/{id}/indexing/hybrid-weights'],
    'Releases and retrieval': ['/api/v1/datasets/{id}/releases', '/api/v1/releases/{id}', '/api/v1/releases/{id}/activate', '/api/v1/releases/{id}/rollback', '/api/v1/releases/{id}/impact', '/api/v1/releases/{id}/search'],
    'Tasks': ['/api/v1/tasks', '/api/v1/tasks/{id}', '/api/v1/tasks/{id}/cancel', '/api/v1/tasks/{id}/pause', '/api/v1/tasks/{id}/resume', '/api/v1/tasks/{id}/retry', '/api/v1/tasks/{id}/wait'],
    'Connectors': ['/api/v1/connectors', '/api/v1/connectors/{id}', '/api/v1/connectors/{id}/test', '/api/v1/connectors/{id}/schema', '/api/v1/connectors/{id}/templates', '/api/v1/query-templates/{id}/execute', '/api/v1/query-templates/{id}/snapshot'],
    'Graph': ['/api/v1/knowledge-bases/{id}/ontologies', '/api/v1/ontologies/{id}/publish', '/api/v1/datasets/{id}/graph', '/api/v1/datasets/{id}/graph/entities', '/api/v1/datasets/{id}/graph/relations'],
    'Widget': ['/api/v1/assistants/{id}/clients', '/api/v1/widget-clients/{id}', '/api/v1/widget-clients/{id}/rotate', '/api/v1/public/widget/token', '/api/v1/public/widget/sessions', '/api/v1/public/widget/sessions/{id}/messages', '/api/v1/public/widget/sessions/{id}/handoff', '/api/v1/public/widget/sessions/{id}', '/api/v1/handoffs', '/api/v1/handoffs/{id}'],
    'Conversations': ['/api/v1/conversations', '/api/v1/conversations/{id}', '/api/v1/conversations/{id}/messages', '/api/v1/messages/{id}/feedback', '/api/v1/traces', '/api/v1/traces/{id}', '/api/v1/traces/{id}/replay', '/api/v1/traces/{id}/compare/{otherId}', '/api/v1/citations/{id}'],
    'Models': ['/api/v1/models', '/api/v1/models/{id}', '/api/v1/models/{id}/test'],
    'Product': ['/api/v1/search', '/api/v1/settings', '/api/v1/settings/{key}', '/api/v1/feature-flags', '/api/v1/feature-flags/{key}', '/api/v1/wiki', '/api/v1/wiki/{id}', '/api/v1/wiki/from-message/{id}', '/api/v1/assistants', '/api/v1/assistants/{id}', '/api/v1/assistants/{id}/publish', '/api/v1/assistants/{id}/pause', '/api/v1/backups', '/api/v1/backups/{id}/restore', '/api/v1/audit', '/api/v1/audit/verify'],
}

CREATE_OPENAPI_METHODS = {
    '/api/v1/session/bootstrap': ['get'], '/api/v1/health': ['get'], '/api/v1/dashboard': ['get'], '/api/v1/version': ['get'], '/api/v1/diagnostics': ['get'], '/api/v1/openapi.json': ['get'],
    '/api/v1/knowledge-bases': ['get', 'post'], '/api/v1/knowledge-bases/{id}': ['get', 'patch', 'delete'], '/api/v1/knowledge-bases/{id}/impact': ['get'], '/api/v1/knowledge-bases/{id}/datasets': ['get', 'post'], '/api/v1/knowledge-bases/{id}/index-profiles': ['get', 'post'], '/api/v1/index-profiles/{id}': ['get', 'patch', 'delete'], '/api/v1/index-profiles/{id}/default': ['post'],
    '/api/v1/datasets/{id}': ['get', 'patch', 'delete'], '/api/v1/datasets/{id}/sources': ['get', 'post'], '/api/v1/datasets/{id}/files': ['post'], '/api/v1/datasets/{id}/archives': ['post'], '/api/v1/datasets/{id}/directory/preview': ['post'], '/api/v1/datasets/{id}/directory/import': ['post'], '/api/v1/datasets/{id}/documents': ['get'],
    '/api/v1/documents/{id}': ['get', 'delete'], '/api/v1/artifacts/{id}/{kind}': ['get'], '/api/v1/datasets/{id}/chunks': ['get'], '/api/v1/datasets/{id}/indexing/stats': ['get'], '/api/v1/datasets/{id}/indexing/pipeline': ['get'], '/api/v1/datasets/{id}/chapters': ['get'], '/api/v1/chunks/{id}': ['get'], '/api/v1/chunks/{id}/lineage': ['get'], '/api/v1/chunks/{id}/revisions': ['post'], '/api/v1/chunks/{id}/vectorize': ['post'], '/api/v1/chunks/{id}/toggle-disable': ['post'], '/api/v1/chunks/{id}/restore': ['post'], '/api/v1/chunks/{id}/diff': ['get'], '/api/v1/chunks/{id}/split': ['post'], '/api/v1/chunks/merge': ['post'], '/api/v1/datasets/{id}/indexing/vectorize-pending': ['post'], '/api/v1/datasets/{id}/indexing/rebuild-hnsw': ['post'], '/api/v1/datasets/{id}/indexing/optimize-index': ['post'], '/api/v1/datasets/{id}/indexing/rebuild-bm25': ['post'], '/api/v1/datasets/{id}/indexing/hybrid-weights': ['put'],
    '/api/v1/datasets/{id}/releases': ['get', 'post'], '/api/v1/releases/{id}': ['get'], '/api/v1/releases/{id}/activate': ['post'], '/api/v1/releases/{id}/rollback': ['post'], '/api/v1/releases/{id}/impact': ['get'], '/api/v1/releases/{id}/search': ['post'],
    '/api/v1/tasks': ['get'], '/api/v1/tasks/{id}': ['get'], '/api/v1/tasks/{id}/cancel': ['post'], '/api/v1/tasks/{id}/pause': ['post'], '/api/v1/tasks/{id}/resume': ['post'], '/api/v1/tasks/{id}/retry': ['post'], '/api/v1/tasks/{id}/wait': ['get'],
    '/api/v1/connectors': ['get', 'post'], '/api/v1/connectors/{id}': ['get'], '/api/v1/connectors/{id}/test': ['post'], '/api/v1/connectors/{id}/schema': ['get'], '/api/v1/connectors/{id}/templates': ['get', 'post'], '/api/v1/query-templates/{id}/execute': ['post'], '/api/v1/query-templates/{id}/snapshot': ['post'],
    '/api/v1/knowledge-bases/{id}/ontologies': ['get', 'post'], '/api/v1/ontologies/{id}/publish': ['post'], '/api/v1/datasets/{id}/graph': ['get'], '/api/v1/datasets/{id}/graph/entities': ['get', 'post'], '/api/v1/datasets/{id}/graph/relations': ['post'],
    '/api/v1/assistants/{id}/clients': ['get', 'post'], '/api/v1/widget-clients/{id}': ['delete'], '/api/v1/widget-clients/{id}/rotate': ['post'], '/api/v1/public/widget/token': ['post'], '/api/v1/public/widget/sessions': ['post'], '/api/v1/public/widget/sessions/{id}/messages': ['post'], '/api/v1/public/widget/sessions/{id}/handoff': ['post'], '/api/v1/public/widget/sessions/{id}': ['delete'], '/api/v1/handoffs': ['get'], '/api/v1/handoffs/{id}': ['patch'],
    '/api/v1/conversations': ['get', 'post'], '/api/v1/conversations/{id}': ['get', 'delete'], '/api/v1/conversations/{id}/messages': ['post'], '/api/v1/messages/{id}/feedback': ['post'], '/api/v1/traces': ['get'], '/api/v1/traces/{id}': ['get'], '/api/v1/traces/{id}/replay': ['post'], '/api/v1/traces/{id}/compare/{otherId}': ['get'], '/api/v1/citations/{id}': ['get'],
    '/api/v1/models': ['get', 'post'], '/api/v1/models/{id}': ['get', 'patch', 'delete'], '/api/v1/models/{id}/test': ['post'],
    '/api/v1/search': ['get'], '/api/v1/settings': ['get'], '/api/v1/settings/{key}': ['put'], '/api/v1/feature-flags': ['get'], '/api/v1/feature-flags/{key}': ['put'], '/api/v1/wiki': ['get', 'post'], '/api/v1/wiki/{id}': ['get', 'post'], '/api/v1/wiki/from-message/{id}': ['post'], '/api/v1/assistants': ['get', 'post'], '/api/v1/assistants/{id}': ['get', 'patch'], '/api/v1/assistants/{id}/publish': ['post'], '/api/v1/assistants/{id}/pause': ['post'], '/api/v1/backups': ['get', 'post'], '/api/v1/backups/{id}/restore': ['post'], '/api/v1/audit': ['get'], '/api/v1/audit/verify': ['get'],
}


def create_openapi(config):
    paths = {}
    for tag, routes in CREATE_OPENAPI_GROUPS.items():
        for route in routes:
            bucket = paths.setdefault(route, {})
            for method in CREATE_OPENAPI_METHODS.get(route, ['get']):
                bucket[method] = {
                    'tags': [tag], 'summary': f'{method.upper()} {route}',
                    'operationId': f"{method}_{ ''.join(ch if ch.isalnum() else '_' for ch in route) }",
                    'responses': {
                        '200': {'description': 'Successful response', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Envelope'}}}},
                        '400': {'$ref': '#/components/responses/Error'}, '401': {'$ref': '#/components/responses/Error'}, '404': {'$ref': '#/components/responses/Error'},
                    },
                }
    return {
        'openapi': '3.1.0',
        'info': {'title': 'Ordo Product API', 'version': config['appVersion'],
                 'description': 'Local-first knowledge product API. All mutating requests require the local session cookie and CSRF header.'},
        'servers': [{'url': f"http://{config['host']}:{config['port']}"}],
        'tags': [{'name': name} for name in CREATE_OPENAPI_GROUPS],
        'paths': paths,
        'components': {
            'securitySchemes': {'localSession': {'type': 'apiKey', 'in': 'cookie', 'name': 'ordo_session'},
                                'csrf': {'type': 'apiKey', 'in': 'header', 'name': 'x-ordo-csrf'}},
            'schemas': {'Envelope': {'type': 'object', 'required': ['data'], 'properties': {'data': {}, 'meta': {'type': 'object'}}},
                        'Error': {'type': 'object', 'properties': {'error': {'type': 'object', 'required': ['code', 'message', 'requestId'],
                                  'properties': {'code': {'type': 'string'}, 'message': {'type': 'string'}, 'requestId': {'type': 'string'}, 'details': {}}}}}},
            'responses': {'Error': {'description': 'Error response', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}}},
        },
        'security': [{'localSession': [], 'csrf': []}],
    }


def create_app(overrides=None):
    config = resolve_config(overrides)
    ensure_data_layout(config)
    remote_host = str(config['host']).lower() not in ('127.0.0.1', 'localhost', '::1')
    if remote_host and not (config['allowRemote'] and config['tlsTerminated'] and config['remoteAdminToken']):
        raise RuntimeError('Remote binding requires ORDO_ALLOW_REMOTE=true, TLS termination and ORDO_REMOTE_ADMIN_TOKEN')

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    limiter = RateLimiter()
    session = {
        'token': base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode('ascii'),
        'csrf': base64.urlsafe_b64encode(os.urandom(24)).rstrip(b'=').decode('ascii'),
        'createdAt': time.time() * 1000,
        'maxAgeMs': 12 * 60 * 60 * 1000,
    }
    request_counter = {'n': 0}

    db = OrdoDatabase(config)
    blob_store = BlobStore(config, db)
    artifact_store = ArtifactStore(config)
    secret_store = SecretStore(config, db)
    audit = AuditLog(db, config)
    tasks = TaskService(db, audit, config)
    models = ModelService(db, secret_store, audit, config)
    knowledge = KnowledgeService(db, blob_store, artifact_store, tasks, audit, config)
    product = ProductService(db, knowledge, None, models, tasks, audit, blob_store, artifact_store, secret_store, config)
    services = {'config': config, 'db': db, 'blobStore': blob_store, 'artifactStore': artifact_store,
                'secretStore': secret_store, 'audit': audit, 'tasks': tasks, 'models': models,
                'knowledge': knowledge, 'product': product}
    app.state.services = services

    def error_body(request, status, code, message, details=None):
        payload = {'error': {'code': code, 'message': message, 'requestId': getattr(request.state, 'request_id', '')}}
        if details is not None:
            payload['error']['details'] = details
        return payload

    @app.middleware('http')
    async def gateway(request: Request, call_next):
        request_counter['n'] += 1
        request.state.request_id = request.headers.get('x-request-id') or f'req-{request_counter["n"]}'
        path = request.url.path
        origin = request.headers.get('origin')

        if origin and path.startswith('/api/'):
            normalized = _origin_of(origin)
            rows = db.all("SELECT allowed_origins_json FROM widget_clients WHERE status='active'")
            allowed = any(normalized in (row.get('allowed_origins') or []) for row in rows)
            if request.method == 'OPTIONS' and request.headers.get('access-control-request-method'):
                if not allowed:
                    return PlainTextResponse('Invalid CORS origin', status_code=403)
                response = Response(status_code=204)
                response.headers['Access-Control-Allow-Origin'] = normalized
                response.headers['Access-Control-Allow-Methods'] = 'POST,DELETE,OPTIONS'
                response.headers['Access-Control-Allow-Headers'] = 'content-type,authorization,x-ordo-client,x-ordo-timestamp,x-ordo-nonce,x-ordo-signature'
                response.headers['Access-Control-Max-Age'] = '600'
                return response
            request.state.cors_origin = normalized if allowed else None

        if path.startswith('/api/'):
            limiter.check(request.client.host if request.client else 'unknown')

        if path.startswith('/api/'):
            route_path = path
            is_public = route_path in PUBLIC_PATHS or route_path.startswith('/api/v1/public/widget/sessions/')
            if not is_public:
                cookies = parse_cookies(request.headers.get('cookie', ''))
                token = cookies.get('ordo_session') or ''
                auth_header = request.headers.get('authorization') or ''
                if auth_header.lower().startswith('bearer '):
                    token = token or auth_header[7:]
                valid = (token
                         and len(token) == len(session['token'])
                         and hmac.compare_digest(token.encode(), session['token'].encode())
                         and time.time() * 1000 - session['createdAt'] <= session['maxAgeMs'])
                if not valid:
                    body = error_body(request, 401, 'SESSION_REQUIRED', '本机会话无效或已过期')
                    return CompactJSONResponse(body, status_code=401)
                if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                    csrf = request.headers.get('x-ordo-csrf') or ''
                    if not csrf or len(csrf) != len(session['csrf']) or not hmac.compare_digest(csrf.encode(), session['csrf'].encode()):
                        body = error_body(request, 403, 'CSRF_INVALID', '写请求缺少有效 CSRF 令牌')
                        return CompactJSONResponse(body, status_code=403)
            request.state.workspace_id = config['localWorkspaceId']
            request.state.actor_id = config['localOwnerId']

        response = await call_next(request)
        cors_origin = getattr(request.state, 'cors_origin', None)
        if cors_origin:
            response.headers['Access-Control-Allow-Origin'] = cors_origin
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, error: AppError):
        return CompactJSONResponse(error_body(request, error.status_code, error.code, error.message, error.details),
                                   status_code=error.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, error: StarletteHTTPException):
        if request.url.path.startswith('/api/'):
            status = 404 if error.status_code in (404, 405) else error.status_code
            code = 'ROUTE_NOT_FOUND' if status == 404 else f'HTTP_{status}'
            return CompactJSONResponse(error_body(request, status, code, 'API 路由不存在' if status == 404 else str(error.detail)),
                                       status_code=status)
        return PlainTextResponse('Not found', status_code=404)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, error: RequestValidationError):
        return CompactJSONResponse(error_body(request, 400, 'VALIDATION_ERROR', '请求参数无效',
                                              {'issues': str(error)[:500]}), status_code=400)

    @app.exception_handler(json.JSONDecodeError)
    async def json_error_handler(request: Request, error: json.JSONDecodeError):
        return CompactJSONResponse(error_body(request, 400, 'JSON_INVALID', '请求体不是有效 JSON'), status_code=400)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, error: Exception):
        import traceback
        traceback.print_exc()
        return CompactJSONResponse(error_body(request, 500, 'INTERNAL_ERROR', '服务处理请求时发生错误'), status_code=500)

    # ---- 路由辅助 ----

    def data(value, meta=None):
        payload = {'data': value}
        if meta is not None:
            payload['meta'] = meta
        return payload

    def paginated(result):
        return data(result['items'], {
            'total': result['total'], 'limit': result['limit'], 'offset': result['offset'],
            'hasMore': result['offset'] + len(result['items']) < result['total'],
        })

    def workspace(request):
        return getattr(request.state, 'workspace_id', config['localWorkspaceId'])

    async def body_of(request):
        raw = await request.body()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise AppError(400, 'JSON_INVALID', '请求体不是有效 JSON')

    def page_params(request):
        return page({'limit': request.query_params.get('limit'), 'offset': request.query_params.get('offset')})

    # ---- System (M1) ----

    @app.get('/api/v1/session/bootstrap')
    async def session_bootstrap(request: Request):
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise AppError(403, 'ORIGIN_REJECTED', '跨站页面不能创建本机会话')
        if remote_host:
            provided = request.headers.get('x-ordo-admin-token') or ''
            if not provided or len(provided) != len(config['remoteAdminToken']) or not hmac.compare_digest(provided.encode(), config['remoteAdminToken'].encode()):
                raise AppError(401, 'REMOTE_AUTH_REQUIRED', '远程部署需要管理员初始化令牌')
        if time.time() * 1000 - session['createdAt'] > session['maxAgeMs']:
            session['token'] = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode('ascii')
            session['csrf'] = base64.urlsafe_b64encode(os.urandom(24)).rstrip(b'=').decode('ascii')
            session['createdAt'] = time.time() * 1000
        secure = '; Secure' if remote_host and config['tlsTerminated'] else ''
        import datetime as _dt
        expires_at = _dt.datetime.utcfromtimestamp(session['createdAt'] / 1000 + session['maxAgeMs'] / 1000).strftime('%Y-%m-%dT%H:%M:%S.') + f"{_dt.datetime.utcfromtimestamp(session['createdAt'] / 1000 + session['maxAgeMs'] / 1000).microsecond // 1000:03d}Z"
        response = CompactJSONResponse(data({
            'csrfToken': session['csrf'], 'expiresAt': expires_at, 'workspaceId': config['localWorkspaceId'],
        }))
        response.headers['Set-Cookie'] = f"ordo_session={session['token']}; Path=/; HttpOnly; SameSite=Strict{secure}; Max-Age={int(session['maxAgeMs'] / 1000)}"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/v1/health')
    async def health(request: Request):
        return CompactJSONResponse(data(product.health(workspace(request))))

    @app.get('/api/v1/version')
    async def version(request: Request):
        import platform as _platform
        import sys as _sys
        return CompactJSONResponse(data({
            'appVersion': config['appVersion'], 'schemaVersion': product.schema_version(),
            'deploymentProfile': config['deploymentProfile'], 'platform': config['platform'],
            'node': f'python {_sys.version.split()[0]} ({_platform.python_implementation()})',
        }))

    @app.get('/api/v1/openapi.json')
    async def openapi(request: Request):
        return CompactJSONResponse(create_openapi(config))

    # ---- Knowledge bases, datasets, chunks and releases (M2) ----
    @app.get('/api/v1/knowledge-bases')
    async def list_knowledge_bases(request: Request):
        return CompactJSONResponse(data(knowledge.list_knowledge_bases(workspace(request))))

    @app.post('/api/v1/knowledge-bases')
    async def create_knowledge_base(request: Request):
        return CompactJSONResponse(data(knowledge.create_knowledge_base(await body_of(request), workspace(request), request.state.request_id)))

    @app.get('/api/v1/knowledge-bases/{kb_id}')
    async def get_knowledge_base(kb_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.get_knowledge_base(kb_id, workspace(request))))

    @app.patch('/api/v1/knowledge-bases/{kb_id}')
    async def update_knowledge_base(kb_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.update_knowledge_base(kb_id, await body_of(request), workspace(request), request.state.request_id)))

    @app.delete('/api/v1/knowledge-bases/{kb_id}')
    async def delete_knowledge_base(kb_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.delete_knowledge_base(kb_id, workspace(request), request.state.request_id)))

    @app.get('/api/v1/knowledge-bases/{kb_id}/impact')
    async def knowledge_base_impact(kb_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.knowledge_base_impact(kb_id, workspace(request))))

    @app.get('/api/v1/knowledge-bases/{kb_id}/datasets')
    async def list_kb_datasets(kb_id: str, request: Request):
        knowledge.ensure_kb(kb_id, workspace(request))
        return CompactJSONResponse(data(knowledge.list_datasets(kb_id, workspace(request))))

    @app.post('/api/v1/knowledge-bases/{kb_id}/datasets')
    async def create_kb_dataset(kb_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.create_dataset(kb_id, await body_of(request), workspace(request), request.state.request_id)))

    @app.get('/api/v1/knowledge-bases/{kb_id}/index-profiles')
    async def list_kb_index_profiles(kb_id: str, request: Request):
        knowledge.ensure_kb(kb_id, workspace(request))
        return CompactJSONResponse(data(knowledge.db.all('SELECT * FROM index_profiles WHERE knowledge_base_id=? AND workspace_id=? ORDER BY created_at DESC', kb_id, workspace(request))))

    @app.post('/api/v1/knowledge-bases/{kb_id}/index-profiles')
    async def create_kb_index_profile(kb_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.create_index_profile(kb_id, await body_of(request), workspace(request), request.state.request_id)))

    @app.get('/api/v1/index-profiles/{profile_id}')
    async def get_index_profile(profile_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.get_index_profile(profile_id, workspace(request))))

    @app.patch('/api/v1/index-profiles/{profile_id}')
    async def update_index_profile(profile_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.update_index_profile(profile_id, await body_of(request), workspace(request), request.state.request_id)))

    @app.delete('/api/v1/index-profiles/{profile_id}')
    async def delete_index_profile(profile_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.archive_index_profile(profile_id, workspace(request), request.state.request_id)))

    @app.post('/api/v1/index-profiles/{profile_id}/default')
    async def set_default_index_profile(profile_id: str, request: Request):
        profile = knowledge.get_index_profile(profile_id, workspace(request))
        return CompactJSONResponse(data(knowledge.set_default_index_profile(profile['knowledge_base_id'], profile_id, workspace(request), request.state.request_id)))

    @app.get('/api/v1/datasets/{dataset_id}')
    async def get_dataset(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.get_dataset(dataset_id, workspace(request))))

    @app.patch('/api/v1/datasets/{dataset_id}')
    async def update_dataset(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.update_dataset(dataset_id, await body_of(request), workspace(request), request.state.request_id)))

    @app.delete('/api/v1/datasets/{dataset_id}')
    async def delete_dataset(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.delete_dataset(dataset_id, workspace(request), request.state.request_id)))

    @app.get('/api/v1/datasets/{dataset_id}/sources')
    async def list_sources(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.list_sources(dataset_id, workspace(request))))

    @app.post('/api/v1/datasets/{dataset_id}/sources')
    async def create_source(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.create_source(dataset_id, await body_of(request), workspace(request), request.state.request_id)))

    @app.post('/api/v1/datasets/{dataset_id}/files')
    async def upload_file(dataset_id: str, request: Request):
        form = await request.form()
        file = form.get('file')
        if not file or not hasattr(file, 'read'):
            raise AppError(400, 'FILE_REQUIRED', '请选择上传文件')
        filename = getattr(file, 'filename', None) or ''
        buffer = await file.read()
        source_id = form.get('sourceId') or request.query_params.get('sourceId')
        if not source_id:
            source_id = knowledge.create_source(dataset_id, {'type': 'upload', 'name': filename}, workspace(request), request.state.request_id)['id']
        result = knowledge.register_upload(dataset_id, str(source_id), filename, buffer, getattr(file, 'content_type', None), workspace(request), request.state.request_id)
        return CompactJSONResponse(data(result))

    @app.get('/api/v1/datasets/{dataset_id}/documents')
    async def list_documents(dataset_id: str, request: Request):
        params = page_params(request)
        result = knowledge.list_documents(dataset_id, workspace(request), request.query_params.get('status'), request.query_params.get('query'), params['limit'], params['offset'])
        return CompactJSONResponse(paginated(result))

    @app.get('/api/v1/documents/{document_id}')
    async def get_document(document_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.get_document(document_id, workspace(request))))

    @app.delete('/api/v1/documents/{document_id}')
    async def delete_document(document_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.delete_document(document_id, workspace(request), request.state.request_id)))

    @app.get('/api/v1/artifacts/{artifact_id}/{kind}')
    async def read_artifact(artifact_id: str, kind: str, request: Request):
        content_type = 'text/markdown; charset=utf-8' if kind == 'markdown' else 'application/json; charset=utf-8'
        return Response(content=knowledge.artifact_file(artifact_id, kind, workspace(request)), media_type=content_type)

    @app.get('/api/v1/datasets/{dataset_id}/chunks')
    async def list_chunks(dataset_id: str, request: Request):
        params = page_params(request)
        result = knowledge.list_chunks(dataset_id, workspace(request), request.query_params.get('query'), request.query_params.get('documentId'), request.query_params.get('type'), request.query_params.get('warning') == 'true', params['limit'], params['offset'])
        return CompactJSONResponse(paginated(result))

    @app.get('/api/v1/datasets/{dataset_id}/indexing/stats')
    async def indexing_stats(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.get_indexing_stats(dataset_id, workspace(request))))

    @app.get('/api/v1/datasets/{dataset_id}/indexing/pipeline')
    async def indexing_pipeline(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.get_indexing_pipeline(dataset_id, workspace(request))))

    @app.get('/api/v1/chunks/{chunk_id}')
    async def get_chunk(chunk_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.get_chunk(chunk_id, workspace(request))))

    @app.get('/api/v1/chunks/{chunk_id}/lineage')
    async def chunk_lineage(chunk_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.get_chunk_lineage(chunk_id, workspace(request))))

    @app.get('/api/v1/chunks/{chunk_id}/diff')
    async def chunk_diff(chunk_id: str, request: Request):
        current = knowledge.get_chunk(chunk_id, workspace(request))
        against_id = request.query_params.get('against')
        against = knowledge.get_chunk(against_id, workspace(request)) if against_id else None
        if against and against['chunk_logical_id'] != current['chunk_logical_id']:
            raise AppError(400, 'SCOPE_MISMATCH', '只能比较同一逻辑块的修订')
        source = against or next((item for item in current['history'] if item['id'] != current['id']), None)
        if not source:
            raise AppError(400, 'REVISION_REQUIRED', '必须提供可比较的块修订')
        before, after = str(source.get('content_text', '')).splitlines(), str(current.get('content_text', '')).splitlines()
        changes = []
        for index in range(max(len(before), len(after))):
            old, new = before[index] if index < len(before) else None, after[index] if index < len(after) else None
            changes.append({'type': 'equal' if old == new else 'removed' if new is None else 'added' if old is None else 'removed', 'line': index + 1, 'before': old, 'after': new})
        return CompactJSONResponse(data({'chunkLogicalId': current['chunk_logical_id'], 'from': {'id': source['id'], 'revision': source['revision_number']}, 'to': {'id': current['id'], 'revision': current['revision_number']}, 'changed': any(item['type'] != 'equal' for item in changes), 'changes': changes}))

    @app.post('/api/v1/datasets/{dataset_id}/indexing/vectorize-pending')
    async def vectorize_pending(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.batch_vectorize_pending(dataset_id, workspace(request), request.state.request_id)))

    @app.post('/api/v1/datasets/{dataset_id}/indexing/rebuild-hnsw')
    async def rebuild_hnsw(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.rebuild_hnsw_index(dataset_id, workspace(request), request.state.request_id)))

    @app.post('/api/v1/datasets/{dataset_id}/indexing/optimize-index')
    async def optimize_index(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.optimize_vector_index(dataset_id, workspace(request), request.state.request_id)))

    @app.post('/api/v1/datasets/{dataset_id}/indexing/rebuild-bm25')
    async def rebuild_bm25(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.rebuild_bm25_index(dataset_id, workspace(request), request.state.request_id)))

    @app.put('/api/v1/datasets/{dataset_id}/indexing/hybrid-weights')
    async def hybrid_weights(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.set_hybrid_weights(dataset_id, await body_of(request), workspace(request), request.state.request_id)))

    @app.get('/api/v1/datasets/{dataset_id}/releases')
    async def list_releases(dataset_id: str, request: Request):
        knowledge.ensure_dataset(dataset_id, workspace(request))
        return CompactJSONResponse(data(knowledge.list_releases(dataset_id, workspace(request))))

    @app.post('/api/v1/datasets/{dataset_id}/releases')
    async def build_release(dataset_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.build_release(dataset_id, await body_of(request), workspace(request), request.state.request_id)))

    @app.get('/api/v1/releases/{release_id}')
    async def get_release(release_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.get_release(release_id, workspace(request))))

    @app.post('/api/v1/releases/{release_id}/activate')
    async def activate_release(release_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.activate_release(release_id, workspace(request), request.state.request_id)))

    @app.post('/api/v1/releases/{release_id}/rollback')
    async def rollback_release(release_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.rollback_release(release_id, workspace(request), request.state.request_id)))

    @app.get('/api/v1/releases/{release_id}/impact')
    async def release_impact(release_id: str, request: Request):
        return CompactJSONResponse(data(knowledge.release_impact(release_id, workspace(request))))

    @app.post('/api/v1/releases/{release_id}/search')
    async def search_release(release_id: str, request: Request):
        body = await body_of(request)
        return CompactJSONResponse(data(knowledge.search_release(release_id, body.get('query'), workspace(request), body.get('limit', 10))))

    # ---- Tasks ----
    @app.get('/api/v1/tasks')
    async def list_tasks(request: Request):
        params = page_params(request)
        result = tasks.list(workspace(request), request.query_params.get('status'), request.query_params.get('type'), params['limit'], params['offset'])
        return CompactJSONResponse(paginated(result))

    @app.get('/api/v1/tasks/{task_id}')
    async def get_task(task_id: str, request: Request):
        return CompactJSONResponse(data(tasks.get(task_id, workspace(request))))

    @app.post('/api/v1/tasks/{task_id}/cancel')
    async def cancel_task(task_id: str, request: Request):
        return CompactJSONResponse(data(tasks.cancel(task_id, workspace(request))))

    @app.post('/api/v1/tasks/{task_id}/pause')
    async def pause_task(task_id: str, request: Request):
        return CompactJSONResponse(data(tasks.pause(task_id, workspace(request))))

    @app.post('/api/v1/tasks/{task_id}/resume')
    async def resume_task(task_id: str, request: Request):
        return CompactJSONResponse(data(tasks.resume(task_id, workspace(request))))

    @app.post('/api/v1/tasks/{task_id}/retry')
    async def retry_task(task_id: str, request: Request):
        return CompactJSONResponse(data(tasks.retry(task_id, workspace(request))))

    @app.get('/api/v1/tasks/{task_id}/wait')
    async def wait_task(task_id: str, request: Request):
        timeout_ms = bounded_int(request.query_params.get('timeoutMs'), 10_000, 10, 120_000, 'timeoutMs')
        return CompactJSONResponse(data(await tasks.wait(task_id, workspace(request), timeout_ms)))

    # ---- 静态托管（web/ 前端，hash 路由无需 history fallback） ----

    app.mount('/', StaticFiles(directory=str(config['webRoot']), html=True, check_dir=False), name='web')

    async def _shutdown():
        db.close()
    app.router.on_shutdown.append(_shutdown)

    tasks.resume_queued()
    return app
