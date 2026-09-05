"""FastAPI composition root; the browser and API are served from one origin."""
import asyncio
import base64
import hmac
import json
import logging
import secrets
import sqlite3
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from .config import resolve_config
from .core import AppError
from .db import OrdoDatabase
from .storage import ensure_data_layout, BlobStore, ArtifactStore, SecretStore, AuditLog
from .knowledge import KnowledgeService
from .models import ModelService
from .tasks import TaskService
from .query import QueryService
from .product import ProductService
from .ingest import IngestService
from .connectors import ConnectorService
from .graph import GraphService
from .widget import WidgetService, normalize_origin
from .workbench import WorkbenchService
from .trace_workbench import TraceWorkbench


def error_envelope(code, message, request_id, details=None):
    error = {'code': code, 'message': message, 'requestId': request_id}
    if details is not None:
        error['details'] = details
    return {'error': error}


class Gateway:
    """Pure ASGI middleware preserves streaming and bounds bodies before parsing."""
    def __init__(self, app, config, session, db):
        self.app, self.config, self.session, self.db = app, config, session, db
        self.buckets = {}

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request = Request(scope, receive=receive)
        path, method = scope['path'], scope['method']
        request_id = request.headers.get('x-request-id') or 'req-' + secrets.token_hex(8)
        scope.setdefault('state', {}).update(request_id=request_id, workspace_id=self.config['localWorkspaceId'])
        cors = None
        async def respond(status, code, message):
            response = JSONResponse(error_envelope(code, message, request_id), status_code=status)
            await response(scope, receive, send)
        if path.startswith('/api/'):
            ip, current = (scope.get('client') or ('unknown', 0))[0], time.monotonic()
            bucket = self.buckets.setdefault(ip, deque())
            while bucket and bucket[0] < current - 60:
                bucket.popleft()
            if len(bucket) >= 1000:
                return await respond(429, 'RATE_LIMITED', '请求过于频繁，请稍后重试')
            bucket.append(current)
            origin = request.headers.get('origin')
            if origin and path.startswith('/api/v1/public/widget/'):
                try:
                    normalized = normalize_origin(origin)
                    rows = self.db.all("SELECT allowed_origins_json FROM widget_clients WHERE status='active'")
                    if any(normalized in (row.get('allowed_origins') or []) for row in rows):
                        cors = normalized
                except AppError:
                    pass
            if method == 'OPTIONS' and request.headers.get('access-control-request-method'):
                if not cors:
                    return await respond(403, 'ORIGIN_REJECTED', '来源未授权')
                response = JSONResponse(None, status_code=204, headers={'Access-Control-Allow-Origin': cors,
                    'Access-Control-Allow-Methods': 'POST,DELETE,OPTIONS', 'Access-Control-Allow-Headers': 'content-type,authorization,x-ordo-client,x-ordo-timestamp,x-ordo-nonce,x-ordo-signature', 'Vary': 'Origin'})
                return await response(scope, receive, send)
            public = path in ('/api/v1/session/bootstrap', '/api/v1/health', '/api/v1/openapi.json') or path.startswith('/api/v1/public/widget/')
            if not public:
                token = request.cookies.get('ordo_session') or request.headers.get('authorization', '').removeprefix('Bearer ')
                if not token or not hmac.compare_digest(token.encode(), self.session['token'].encode()) or time.time() > self.session['expires']:
                    return await respond(401, 'SESSION_REQUIRED', '本机会话无效或已过期')
                if method not in ('GET', 'HEAD', 'OPTIONS'):
                    csrf = request.headers.get('x-ordo-csrf', '')
                    if not hmac.compare_digest(csrf.encode(), self.session['csrf'].encode()):
                        return await respond(403, 'CSRF_INVALID', '写请求缺少有效 CSRF 令牌')
            length = request.headers.get('content-length')
            if length:
                try:
                    if int(length) > self.config['bodyLimit']:
                        return await respond(413, 'BODY_TOO_LARGE', '请求体超过大小预算')
                except ValueError:
                    return await respond(400, 'VALIDATION_ERROR', 'Content-Length 无效')
        received = 0
        async def limited_receive():
            nonlocal received
            message = await receive()
            if message['type'] == 'http.request':
                received += len(message.get('body', b''))
                if received > self.config['bodyLimit']:
                    raise AppError(413, 'BODY_TOO_LARGE', '请求体超过大小预算')
            return message
        async def add_headers(message):
            if message['type'] == 'http.response.start':
                headers = list(message.get('headers', []))
                headers.extend([(b'x-request-id', request_id.encode('ascii', 'replace')), (b'x-content-type-options', b'nosniff')])
                if path.startswith('/api/'):
                    headers.append((b'cache-control', b'no-store'))
                if cors:
                    headers.extend([(b'access-control-allow-origin', cors.encode()), (b'vary', b'Origin')])
                message = {**message, 'headers': headers}
            await send(message)
        await self.app(scope, limited_receive, add_headers)


def create_app(overrides=None):
    config = resolve_config(overrides)
    remote = config['host'].lower() not in ('127.0.0.1', 'localhost', '::1')
    if remote and not (config['allowRemote'] and config['tlsTerminated'] and config['remoteAdminToken']):
        raise RuntimeError('Remote binding requires ORDO_ALLOW_REMOTE=true, TLS termination and ORDO_REMOTE_ADMIN_TOKEN')
    ensure_data_layout(config)
    db = OrdoDatabase(config)
    blobs, artifacts, secret = BlobStore(config, db), ArtifactStore(config), SecretStore(config, db)
    audit = AuditLog(db, config)
    tasks, models = TaskService(db, audit, config), ModelService(db, secret, audit, config)
    knowledge = KnowledgeService(db, blobs, artifacts, tasks, audit, config)
    query = QueryService(db, knowledge, models, audit, config)
    product = ProductService(db, knowledge, query, models, tasks, audit, blobs, artifacts, secret, config)
    services = {'config': config, 'db': db, 'blobStore': blobs, 'artifactStore': artifacts, 'secretStore': secret,
                'audit': audit, 'tasks': tasks, 'models': models, 'knowledge': knowledge, 'query': query, 'product': product,
                'ingest': IngestService(db, knowledge, tasks, audit, config),
                'connectors': ConnectorService(db, secret, artifacts, knowledge, audit, config),
                'graph': GraphService(db, knowledge, audit, config), 'widget': WidgetService(db, secret, product, query, audit, config),
                'workbench': WorkbenchService(db, knowledge, tasks, product, config), 'traces': TraceWorkbench(db, query, knowledge, product)}
    @asynccontextmanager
    async def lifespan(app):
        tasks.resume_queued()
        try:
            yield
        finally:
            await tasks.shutdown()
            db.close()
    app = FastAPI(title='Ordo Product API', version=config['appVersion'], openapi_url=None, docs_url='/api/docs', redoc_url=None, lifespan=lifespan)
    app.state.services = services
    session = {'token': secrets.token_urlsafe(32), 'csrf': secrets.token_urlsafe(24), 'expires': time.time() + 43200}
    app.add_middleware(Gateway, config=config, session=session, db=db)
    @app.exception_handler(AppError)
    async def app_error(request: Request, error: AppError):
        return JSONResponse(error_envelope(error.code, error.message, request.state.request_id, error.details), status_code=error.status_code)
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        return JSONResponse(error_envelope('ROUTE_NOT_FOUND' if error.status_code in (404, 405) else 'HTTP_ERROR', str(error.detail), request.state.request_id), status_code=error.status_code)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error):
        return JSONResponse(error_envelope('VALIDATION_ERROR', '请求参数无效', request.state.request_id), status_code=400)
    @app.exception_handler(sqlite3.IntegrityError)
    async def constraint_error(request: Request, error):
        return JSONResponse(error_envelope('CONSTRAINT_CONFLICT', '操作与现有数据约束冲突', request.state.request_id), status_code=409)
    @app.exception_handler(Exception)
    async def unexpected(request: Request, error):
        logging.getLogger('ordo').exception('Unhandled request %s', request.state.request_id)
        return JSONResponse(error_envelope('INTERNAL_ERROR', '服务内部错误', request.state.request_id), status_code=500)
    async def bootstrap(request):
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise AppError(403, 'ORIGIN_REJECTED', '跨站页面不能创建本机会话')
        if remote and not hmac.compare_digest(request.headers.get('x-ordo-admin-token', '').encode(), config['remoteAdminToken'].encode()):
            raise AppError(401, 'REMOTE_AUTH_REQUIRED', '远程部署需要管理员初始化令牌')
        if time.time() > session['expires']:
            session.update(token=secrets.token_urlsafe(32), csrf=secrets.token_urlsafe(24), expires=time.time()+43200)
        response = JSONResponse({'data': {'csrfToken': session['csrf'], 'expiresAt': datetime.fromtimestamp(session['expires'], timezone.utc).isoformat(), 'workspaceId': config['localWorkspaceId']}})
        response.set_cookie('ordo_session', session['token'], max_age=43200, httponly=True, samesite='strict', secure=remote)
        return response
    from .routes import mount_routes
    mount_routes(app, services, bootstrap)
    app.mount('/', StaticFiles(directory=str(config['webRoot']), html=True), name='web')
    return app
