import base64
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import urlsplit

from .core import AppError, gen_id, hash_bytes, now, required


def normalize_origin(value):
    try:
        parsed = urlsplit(required(value, 'origin'))
    except AppError:
        raise
    except Exception:
        raise AppError(400, 'ORIGIN_INVALID', '来源 URL 无效')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise AppError(400, 'ORIGIN_INVALID', '来源必须是纯 HTTP/HTTPS Origin')
    port = parsed.port
    if (parsed.scheme == 'http' and port == 80) or (parsed.scheme == 'https' and port == 443):
        return f'{parsed.scheme}://{parsed.hostname}'
    return f'{parsed.scheme}://{parsed.netloc}'


def canonical_request(method, route, timestamp, nonce, origin, body):
    body_bytes = body.encode('utf-8') if isinstance(body, str) else (body or b'')
    return '\n'.join([str(method).upper(), route, str(timestamp), nonce, origin, hash_bytes(body_bytes)])


def _sign(secret, canonical):
    return hmac.new(secret.encode('utf-8') if isinstance(secret, str) else secret, canonical.encode('utf-8'), hashlib.sha256).hexdigest()


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(value):
    padding = '=' * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _timing_safe_equal(left, right):
    return hmac.compare_digest(left.encode('utf-8') if isinstance(left, str) else left,
                               right.encode('utf-8') if isinstance(right, str) else right)


class WidgetService:
    def __init__(self, db, secret_store, product, query, audit, config):
        self.db = db
        self.secret_store = secret_store
        self.product = product
        self.query = query
        self.audit = audit
        self.config = config
        self.token_key = hashlib.sha256((secret_store.key if isinstance(secret_store.key, bytes) else secret_store.key.encode('utf-8')) + b'ordo-widget-token-v1').digest()

    def _client_record(self, client_record_id, workspace_id):
        record = self.db.one(
            """SELECT wc.id,wc.assistant_id,wc.client_id,wc.allowed_origins_json,wc.status,wc.created_at,wc.rotated_at,s.mask AS secret_mask
            FROM widget_clients wc JOIN secrets s ON s.id=wc.secret_ref WHERE wc.id=? AND wc.workspace_id=?""",
            client_record_id, workspace_id)
        if not record:
            raise AppError(404, 'NOT_FOUND', '网站客户端不存在或不可访问')
        return record

    def _client_response(self, record, client_secret=None, warning=None):
        result = {
            'id': record['id'], 'assistantId': record['assistant_id'], 'clientId': record['client_id'],
            'allowedOrigins': record.get('allowed_origins'), 'secret_mask': record.get('secret_mask'),
            'status': record['status'], 'createdAt': record['created_at'], 'rotatedAt': record.get('rotated_at'),
        }
        if client_secret:
            result['clientSecret'] = client_secret
        if warning:
            result['warning'] = warning
        return result

    def create_client(self, assistant_id, input, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        assistant = self.product.get_assistant(assistant_id, workspace_id)
        if assistant['status'] != 'published' or not assistant.get('active_release_id'):
            raise AppError(409, 'ASSISTANT_NOT_PUBLISHED', '助手必须发布后才能创建网站客户端')
        origins = list(dict.fromkeys(normalize_origin(value) for value in (input.get('allowedOrigins') or [])))
        if not origins:
            raise AppError(400, 'ORIGIN_REQUIRED', '至少配置一个允许来源')
        client_id = 'ordoc_' + os.urandom(12).hex()
        secret_value = _b64url(os.urandom(32))
        secret = self.secret_store.create(workspace_id, f'widget:{client_id}', secret_value)
        record_id = gen_id('wcli')
        self.db.run('INSERT INTO widget_clients(id,workspace_id,assistant_id,client_id,secret_ref,allowed_origins_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    record_id, workspace_id, assistant_id, client_id, secret['id'], json.dumps(origins, ensure_ascii=False), 'active', now())
        self.audit.append(workspace_id=workspace_id, action='widget_client.create', object_type='widget_client',
                          object_id=record_id, request_id=request_id, details={'assistantId': assistant_id, 'clientId': client_id, 'origins': origins})
        return self._client_response(self._client_record(record_id, workspace_id), secret_value,
                                     'clientSecret 仅返回一次，请保存到客户服务端秘密存储。')

    def list_clients(self, assistant_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        self.product.get_assistant(assistant_id, workspace_id)
        records = self.db.all(
            """SELECT wc.id,wc.assistant_id,wc.client_id,wc.allowed_origins_json,wc.status,wc.created_at,wc.rotated_at,s.mask AS secret_mask
            FROM widget_clients wc JOIN secrets s ON s.id=wc.secret_ref WHERE wc.assistant_id=? AND wc.workspace_id=? ORDER BY wc.created_at""",
            assistant_id, workspace_id)
        return [self._client_response(record) for record in records]

    def rotate_client(self, client_record_id, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        record = self.db.one('SELECT * FROM widget_clients WHERE id=? AND workspace_id=?', client_record_id, workspace_id)
        if not record:
            raise AppError(404, 'NOT_FOUND', '网站客户端不存在或不可访问')
        if record['status'] != 'active':
            raise AppError(409, 'WIDGET_CLIENT_REVOKED', '已撤销的网站客户端不能轮换密钥')
        secret_value = _b64url(os.urandom(32))
        self.secret_store.replace(record['secret_ref'], workspace_id, secret_value)
        self.db.run('UPDATE widget_clients SET rotated_at=? WHERE id=?', now(), record['id'])
        self.audit.append(workspace_id=workspace_id, action='widget_client.rotate', object_type='widget_client',
                          object_id=record['id'], request_id=request_id)
        return self._client_response(self._client_record(record['id'], workspace_id), secret_value,
                                     '旧密钥已立即失效，新密钥仅返回一次。')

    def revoke_client(self, client_record_id, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        record = self._client_record(client_record_id, workspace_id)
        if record['status'] != 'revoked':
            self.db.run("UPDATE widget_clients SET status='revoked' WHERE id=? AND workspace_id=?", record['id'], workspace_id)
            self.audit.append(workspace_id=workspace_id, action='widget_client.revoke', object_type='widget_client',
                              object_id=record['id'], request_id=request_id)
        return self._client_response(self._client_record(record['id'], workspace_id))

    def verify_signed_request(self, client_id, timestamp, nonce, origin, signature, method, route, raw_body):
        record = self.db.one("SELECT * FROM widget_clients WHERE client_id=? AND status='active'", required(client_id, 'clientId'))
        if not record:
            raise AppError(401, 'WIDGET_CLIENT_INVALID', '网站客户端无效')
        normalized_origin = normalize_origin(origin)
        if normalized_origin not in (record.get('allowed_origins') or []):
            raise AppError(403, 'WIDGET_ORIGIN_REJECTED', '请求来源不在允许列表')
        try:
            milliseconds = int(timestamp)
        except (TypeError, ValueError):
            raise AppError(401, 'WIDGET_TIMESTAMP_INVALID', '签名时间戳已过期')
        if abs(time.time() * 1000 - milliseconds) > 5 * 60 * 1000:
            raise AppError(401, 'WIDGET_TIMESTAMP_INVALID', '签名时间戳已过期')
        if not re.match(r'^[A-Za-z0-9_-]{16,128}$', str(nonce or '')):
            raise AppError(400, 'WIDGET_NONCE_INVALID', 'nonce 格式无效')
        secret = self.secret_store.resolve(record['secret_ref'], record['workspace_id'])
        expected = _sign(secret, canonical_request(method, route, timestamp, nonce, normalized_origin, raw_body))
        provided = str(signature or '').lower()
        if len(provided) != len(expected) or not _timing_safe_equal(provided, expected):
            raise AppError(401, 'WIDGET_SIGNATURE_INVALID', '网站请求签名无效')
        try:
            self.db.transaction(lambda: (
                self.db.run('DELETE FROM widget_nonces WHERE expires_at<?', now()),
                self.db.run('INSERT INTO widget_nonces(client_id,nonce,expires_at,created_at) VALUES(?,?,?,?)',
                            client_id, nonce, time.strftime('%Y-%m-%dT%H:%M:%S.', time.gmtime(time.time() + 10 * 60)) + f'{(time.time() % 1) * 1000:03.0f}Z', now())))
        except Exception as error:
            if 'UNIQUE constraint failed' in str(error):
                raise AppError(409, 'WIDGET_REPLAY_REJECTED', '请求 nonce 已使用')
            raise
        return {**record, 'normalizedOrigin': normalized_origin}

    def issue_token(self, input, headers, raw_body=''):
        client = self.verify_signed_request(
            client_id=headers.get('x-ordo-client'), timestamp=headers.get('x-ordo-timestamp'),
            nonce=headers.get('x-ordo-nonce'), origin=headers.get('origin') or input.get('origin'),
            signature=headers.get('x-ordo-signature'), method='POST', route='/api/v1/public/widget/token', raw_body=raw_body)
        assistant = self.product.get_assistant(client['assistant_id'], client['workspace_id'])
        if assistant['status'] != 'published' or not assistant.get('active_release_id'):
            raise AppError(503, 'ASSISTANT_UNAVAILABLE', '网站助手当前不可用')
        release = next((item for item in assistant.get('releases') or [] if item['id'] == assistant['active_release_id']), None)
        if not release or release['status'] != 'published':
            raise AppError(503, 'ASSISTANT_UNAVAILABLE', '网站助手发布版本不可用')
        expires_at = int(time.time() * 1000) + 15 * 60 * 1000
        payload = {'version': 1, 'assistantId': assistant['id'], 'assistantReleaseId': release['id'],
                   'clientId': client['client_id'], 'clientVersion': client.get('rotated_at'),
                   'workspaceId': client['workspace_id'], 'origin': client['normalizedOrigin'],
                   'exp': expires_at, 'nonce': _b64url(os.urandom(12))}
        encoded = _b64url(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        signature = _b64url(hmac.new(self.token_key, encoded.encode('utf-8'), hashlib.sha256).digest())
        self.audit.append(workspace_id=client['workspace_id'], actor_id='widget_client', action='widget.token_issue',
                          object_type='assistant', object_id=assistant['id'],
                          details={'clientId': client['client_id'], 'origin': client['normalizedOrigin'],
                                   'expiresAt': time.strftime('%Y-%m-%dT%H:%M:%S.', time.gmtime(expires_at / 1000)) + f'{expires_at % 1000:03d}Z'})
        return {'token': f'{encoded}.{signature}',
                'expiresAt': time.strftime('%Y-%m-%dT%H:%M:%S.', time.gmtime(expires_at / 1000)) + f'{expires_at % 1000:03d}Z',
                'assistant': {'id': assistant['id'], 'name': assistant['name'], 'config': release['config']}}

    def verify_token(self, token, origin):
        parts = str(token or '').split('.')
        if len(parts) != 2:
            raise AppError(401, 'WIDGET_TOKEN_INVALID', '访客令牌无效')
        expected = _b64url(hmac.new(self.token_key, parts[0].encode('utf-8'), hashlib.sha256).digest())
        if len(parts[1]) != len(expected) or not _timing_safe_equal(parts[1], expected):
            raise AppError(401, 'WIDGET_TOKEN_INVALID', '访客令牌签名无效')
        try:
            payload = json.loads(_b64url_decode(parts[0]).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise AppError(401, 'WIDGET_TOKEN_INVALID', '访客令牌载荷无效')
        if payload['exp'] < time.time() * 1000:
            raise AppError(401, 'WIDGET_TOKEN_EXPIRED', '访客令牌已过期')
        if normalize_origin(origin) != payload['origin']:
            raise AppError(403, 'WIDGET_ORIGIN_REJECTED', '访客令牌与请求来源不匹配')
        if payload.get('clientId'):
            client = self.db.one("SELECT * FROM widget_clients WHERE client_id=? AND workspace_id=? AND status='active'", payload['clientId'], payload['workspaceId'])
            if not client or client.get('rotated_at') != payload.get('clientVersion'):
                raise AppError(401, 'WIDGET_CLIENT_INVALID', '网站客户端已撤销或密钥已轮换')
        return payload

    def create_visitor_session(self, token, origin):
        payload = self.verify_token(token, origin)
        assistant = self.product.get_assistant(payload['assistantId'], payload['workspaceId'])
        release = next((item for item in assistant.get('releases') or [] if item['id'] == payload['assistantReleaseId']), None)
        if not release or release['status'] != 'published' or assistant['status'] != 'published':
            raise AppError(503, 'ASSISTANT_UNAVAILABLE', '网站助手当前不可用')
        knowledge_release = self.product.knowledge.get_release(release['knowledge_release_id'], payload['workspaceId'])
        conversation = self.query.create_conversation(
            {'knowledgeBaseId': knowledge_release['knowledge_base_id'], 'datasetId': knowledge_release['dataset_id'],
             'releaseId': knowledge_release['id'], 'title': '网站访客会话', 'strictEvidence': True},
            payload['workspaceId'])
        visitor_session_id = gen_id('visitor')
        pseudonym = f'访客-{os.urandom(4).hex()}'
        timestamp = now()
        expires_at = time.strftime('%Y-%m-%dT%H:%M:%S.', time.gmtime(time.time() + 30 * 24 * 60 * 60)) + '000Z'
        self.db.run('INSERT INTO visitor_sessions(id,workspace_id,assistant_id,assistant_release_id,conversation_id,pseudonym,origin,status,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    visitor_session_id, payload['workspaceId'], assistant['id'], release['id'], conversation['id'],
                    pseudonym, payload['origin'], 'active', expires_at, timestamp, timestamp)
        self.audit.append(workspace_id=payload['workspaceId'], actor_id=pseudonym, action='visitor_session.create',
                          object_type='visitor_session', object_id=visitor_session_id,
                          details={'assistantId': assistant['id'], 'origin': payload['origin'], 'expiresAt': expires_at})
        return {'id': visitor_session_id, 'pseudonym': pseudonym, 'expiresAt': expires_at,
                'privacy': '访客会话默认保留 30 天，可随时删除。不会跨站跟踪真实身份。'}

    def get_visitor(self, visitor_session_id, origin, token):
        payload = self.verify_token(token, origin)
        assistant = self.product.get_assistant(payload['assistantId'], payload['workspaceId'])
        if assistant['status'] != 'published':
            raise AppError(503, 'ASSISTANT_UNAVAILABLE', '网站助手当前不可用')
        visitor = self.db.one("SELECT * FROM visitor_sessions WHERE id=? AND status='active' AND deleted_at IS NULL", visitor_session_id)
        if (not visitor or visitor['expires_at'] < now() or payload['assistantId'] != visitor['assistant_id']
                or payload['assistantReleaseId'] != visitor['assistant_release_id'] or normalize_origin(origin) != visitor['origin']):
            raise AppError(404, 'NOT_FOUND', '访客会话不存在或已失效')
        return visitor

    async def ask(self, visitor_session_id, origin, token, input, request_id):
        input = input or {}
        visitor = self.get_visitor(visitor_session_id, origin, token)
        result = await self.query.ask(visitor['conversation_id'], {'question': input.get('question'), 'topK': input.get('topK') or 8},
                                      visitor['workspace_id'], request_id)
        self.db.run('UPDATE visitor_sessions SET updated_at=? WHERE id=?', now(), visitor['id'])
        return {'answer': result['assistantMessage']['content'], 'evidenceStatus': result['assistantMessage']['evidence_status'],
                'citations': [{'ordinal': item['ordinal'], 'title': item['title'], 'excerpt': item['excerpt']}
                              for item in result['assistantMessage']['citations']], 'traceId': result['trace']['id']}

    def delete_visitor(self, visitor_session_id, origin, token):
        visitor = self.get_visitor(visitor_session_id, origin, token)
        timestamp = now()
        self.db.transaction(lambda: (
            self.db.run("UPDATE visitor_sessions SET status='deleted',deleted_at=?,updated_at=? WHERE id=?", timestamp, timestamp, visitor['id']),
            self.db.run("UPDATE conversations SET status='deleted',deleted_at=?,updated_at=? WHERE id=?", timestamp, timestamp, visitor['conversation_id'])))
        self.audit.append(workspace_id=visitor['workspace_id'], actor_id=visitor['pseudonym'], action='visitor_session.delete',
                          object_type='visitor_session', object_id=visitor['id'])
        return {'deleted': True}

    def request_handoff(self, visitor_session_id, origin, token, input):
        input = input or {}
        visitor = self.get_visitor(visitor_session_id, origin, token)
        handoff_id = gen_id('handoff')
        timestamp = now()
        self.db.run('INSERT INTO handoff_requests(id,workspace_id,visitor_session_id,status,priority,summary,contact_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    handoff_id, visitor['workspace_id'], visitor['id'], 'queued', input.get('priority') or 'normal',
                    required(input.get('summary'), 'summary'), json.dumps(input.get('contact') or {}, ensure_ascii=False, separators=(',', ':')),
                    timestamp, timestamp)
        self.audit.append(workspace_id=visitor['workspace_id'], actor_id=visitor['pseudonym'], action='handoff.request',
                          object_type='handoff_request', object_id=handoff_id,
                          details={'priority': input.get('priority') or 'normal',
                                   'hasContact': bool(input.get('contact') and input.get('contact'))})
        return {'id': handoff_id, 'status': 'queued', 'createdAt': timestamp}

    def list_handoffs(self, workspace_id=None, status=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        if status:
            return self.db.all('SELECT * FROM handoff_requests WHERE workspace_id=? AND status=? ORDER BY created_at', workspace_id, status)
        return self.db.all('SELECT * FROM handoff_requests WHERE workspace_id=? ORDER BY created_at', workspace_id)

    def update_handoff(self, handoff_id, input, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        current = self.db.one('SELECT * FROM handoff_requests WHERE id=? AND workspace_id=?', handoff_id, workspace_id)
        if not current:
            raise AppError(404, 'NOT_FOUND', '人工转接请求不存在或不可访问')
        status = input.get('status') or current['status']
        if status not in ('queued', 'accepted', 'completed', 'closed'):
            raise AppError(400, 'VALIDATION_ERROR', '人工转接状态无效')
        assigned_to = input.get('assignedTo') if input.get('assignedTo') is not None else current.get('assigned_to')
        self.db.run('UPDATE handoff_requests SET status=?,assigned_to=?,updated_at=? WHERE id=? AND workspace_id=?',
                    status, assigned_to, now(), handoff_id, workspace_id)
        self.audit.append(workspace_id=workspace_id, action='handoff.update', object_type='handoff_request',
                          object_id=handoff_id, request_id=request_id,
                          details={'status': status, 'assignedTo': input.get('assignedTo')})
        return self.db.one('SELECT * FROM handoff_requests WHERE id=?', handoff_id)
