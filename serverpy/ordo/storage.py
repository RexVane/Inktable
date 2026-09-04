import base64
import json
import os
import re
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .core import AppError, gen_id, hash_bytes, now, parse_json, redact, row_to_object, safe_name

AUDIT_REDACT_KEY_RE = re.compile(r'password|secret|token|authorization|apiKey', re.IGNORECASE)


def ensure_data_layout(config):
    for directory in [config['dbPath'].parent, config['blobRoot'], config['artifactRoot'],
                      config['backupRoot'], config['taskRoot'], config['runtimeRoot'], config['logRoot']]:
        Path(directory).mkdir(parents=True, exist_ok=True)


def atomic_write(file, data):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode('utf-8')
    temporary = file.with_name(f'{file.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, file)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def assert_within(root, candidate):
    resolved_root = str(Path(root).resolve())
    resolved = str(Path(candidate).resolve())
    if resolved != resolved_root and not resolved.startswith(resolved_root + os.sep):
        raise AppError(400, 'UNSAFE_PATH', '路径超出受管存储范围')
    return resolved


def _redact_details(value):
    if isinstance(value, dict):
        return {key: ('[REDACTED]' if AUDIT_REDACT_KEY_RE.search(key) else _redact_details(inner))
                for key, inner in value.items()}
    if isinstance(value, list):
        return [_redact_details(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def _json_dump(value):
    # 与 Node JSON.stringify 字节等价：紧凑分隔符、非 ASCII 原样、保持插入键序
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class BlobStore:
    def __init__(self, config, db):
        self.config = config
        self.db = db

    def put(self, workspace_id, buffer, mime_type='application/octet-stream'):
        if isinstance(buffer, str):
            buffer = buffer.encode('utf-8')
        if not isinstance(buffer, (bytes, bytearray)):
            raise AppError(400, 'VALIDATION_ERROR', 'blob 内容必须是字节')
        buffer = bytes(buffer)
        sha256 = hash_bytes(buffer)
        existing = self.db.one('SELECT * FROM blobs WHERE workspace_id=? AND sha256=?', workspace_id, sha256)
        if existing:
            return existing
        blob_id = gen_id('blob')
        key = f'{workspace_id}/{sha256[0:2]}/{sha256[2:4]}/{sha256}'
        target = assert_within(self.config['blobRoot'], Path(self.config['blobRoot']) / key)
        if not Path(target).exists():
            atomic_write(target, buffer)
        self.db.run('INSERT INTO blobs(id,workspace_id,sha256,size_bytes,mime_type,storage_key,created_at) VALUES(?,?,?,?,?,?,?)',
                    blob_id, workspace_id, sha256, len(buffer), mime_type, key, now())
        return self.db.one('SELECT * FROM blobs WHERE id=?', blob_id)

    def get(self, blob_id, workspace_id):
        record = self.db.one('SELECT * FROM blobs WHERE id=? AND workspace_id=?', blob_id, workspace_id)
        if not record:
            raise AppError(404, 'NOT_FOUND', '对象不存在或不可访问')
        file = assert_within(self.config['blobRoot'], Path(self.config['blobRoot']) / record['storage_key'])
        if not Path(file).exists():
            raise AppError(500, 'BLOB_MISSING', '受管文件缺失', {'blobId': blob_id})
        with open(file, 'rb') as stream:
            return {'record': record, 'file': file, 'buffer': stream.read()}

    def count_and_size(self, workspace_id):
        return self.db.one('SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes),0) AS size_bytes FROM blobs WHERE workspace_id=?', workspace_id)


class ArtifactStore:
    def __init__(self, config):
        self.config = config

    def write_document(self, workspace_id, document_revision_id, files):
        base_key = Path(workspace_id) / document_revision_id
        result = {}
        written = []
        try:
            for name, value in files.items():
                filename = safe_name(name)
                key = str(base_key / filename)
                target = assert_within(self.config['artifactRoot'], Path(self.config['artifactRoot']) / key)
                if isinstance(value, (dict, list)):
                    content = json.dumps(value, ensure_ascii=False, indent=2)
                else:
                    content = value
                atomic_write(target, content)
                written.append(target)
                result[name] = key
            return result
        except BaseException:
            for file in reversed(written):
                try:
                    os.unlink(file)
                except OSError:
                    pass
            raise

    def read(self, key):
        file = assert_within(self.config['artifactRoot'], Path(self.config['artifactRoot']) / key)
        if not Path(file).exists():
            raise AppError(404, 'ARTIFACT_NOT_FOUND', '标准产物不存在')
        with open(file, 'rb') as stream:
            return stream.read()

    def count_and_size(self):
        count = 0
        size = 0
        root = Path(self.config['artifactRoot'])
        if root.exists():
            for file in root.rglob('*'):
                if file.is_file():
                    count += 1
                    size += file.stat().st_size
        return {'count': count, 'size_bytes': size}


class SecretStore:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.key = self._load_or_create_key()

    def _load_or_create_key(self):
        master = os.environ.get('ORDO_MASTER_KEY')
        if master:
            candidate = base64.b64decode(master)
            if len(candidate) != 32:
                raise RuntimeError('ORDO_MASTER_KEY must be a base64 encoded 32-byte key')
            return candidate
        key_path = Path(self.config['keyPath'])
        if key_path.exists():
            candidate = key_path.read_bytes()
            if len(candidate) != 32:
                raise RuntimeError('Invalid Ordo master key file')
            return candidate
        key = os.urandom(32)
        atomic_write(key_path, key)
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return key

    def encrypt(self, value):
        iv = os.urandom(12)
        sealed = AESGCM(self.key).encrypt(iv, str(value).encode('utf-8'), None)
        # packed = iv(12) + tag(16) + ciphertext，与 node:crypto createCipheriv 输出一致
        return base64.b64encode(iv + sealed).decode('ascii')

    def decrypt(self, payload):
        packed = base64.b64decode(payload)
        iv = packed[:12]
        sealed = packed[12:]
        return AESGCM(self.key).decrypt(iv, sealed, None).decode('utf-8')

    def _mask(self, text):
        return '••••' if len(text) <= 4 else f'••••{text[-4:]}'

    def create(self, workspace_id, purpose, value):
        if not value:
            raise AppError(400, 'VALIDATION_ERROR', '秘密值不能为空')
        secret_id = gen_id('sec')
        text = str(value)
        self.db.run('INSERT INTO secrets(id,workspace_id,purpose,encrypted_value,mask,created_at) VALUES(?,?,?,?,?,?)',
                    secret_id, workspace_id, purpose, self.encrypt(text), self._mask(text), now())
        return {'id': secret_id, 'purpose': purpose, 'mask': self._mask(text)}

    def replace(self, secret_id, workspace_id, value):
        record = self.db.one('SELECT * FROM secrets WHERE id=? AND workspace_id=?', secret_id, workspace_id)
        if not record:
            raise AppError(404, 'NOT_FOUND', '秘密引用不存在')
        text = str(value or '')
        if not text:
            raise AppError(400, 'VALIDATION_ERROR', '秘密值不能为空')
        self.db.run('UPDATE secrets SET encrypted_value=?,mask=?,rotated_at=? WHERE id=? AND workspace_id=?',
                    self.encrypt(text), self._mask(text), now(), secret_id, workspace_id)
        return {'id': secret_id, 'purpose': record['purpose'], 'mask': self._mask(text)}

    def resolve(self, secret_id, workspace_id):
        record = self.db.one('SELECT * FROM secrets WHERE id=? AND workspace_id=?', secret_id, workspace_id)
        if not record:
            raise AppError(404, 'NOT_FOUND', '秘密引用不存在')
        return self.decrypt(record['encrypted_value'])

    def metadata(self, secret_id, workspace_id):
        record = self.db.one('SELECT id,purpose,mask,created_at,rotated_at FROM secrets WHERE id=? AND workspace_id=?', secret_id, workspace_id)
        if not record:
            raise AppError(404, 'NOT_FOUND', '秘密引用不存在')
        return record


class AuditLog:
    def __init__(self, db, config):
        self.db = db
        self.config = config

    def append(self, workspace_id=None, actor_id=None, action=None, object_type=None, object_id=None,
               result='succeeded', request_id=None, details=None, **_ignored):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        actor_id = actor_id or self.config['localOwnerId']
        details = details or {}
        previous_row = self.db.one('SELECT event_hash FROM audit_events WHERE workspace_id=? ORDER BY rowid DESC LIMIT 1', workspace_id)
        previous = previous_row['event_hash'] if previous_row else 'GENESIS'
        event_id = gen_id('aud')
        timestamp = now()
        safe_details = _redact_details(details)
        payload = _json_dump({
            'eventId': event_id, 'workspaceId': workspace_id, 'actorId': actor_id, 'action': action,
            'objectType': object_type, 'objectId': object_id, 'result': result, 'requestId': request_id,
            'details': safe_details, 'timestamp': timestamp, 'previous': previous,
        })
        event_hash = hash_bytes(payload)
        self.db.run('INSERT INTO audit_events(id,workspace_id,actor_id,action,object_type,object_id,result,request_id,details_json,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    event_id, workspace_id, actor_id, action, object_type, object_id, result, request_id,
                    _json_dump(safe_details), previous, event_hash, timestamp)
        return self.db.one('SELECT * FROM audit_events WHERE id=?', event_id)

    def list(self, workspace_id, limit=100, offset=0):
        total = (self.db.one('SELECT COUNT(*) AS count FROM audit_events WHERE workspace_id=?', workspace_id) or {}).get('count', 0)
        items = self.db.all('SELECT id,workspace_id,actor_id,action,object_type,object_id,result,request_id,details_json,previous_hash,event_hash,created_at FROM audit_events WHERE workspace_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?',
                            workspace_id, limit, offset)
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    def verify(self, workspace_id):
        rows = self.db.all('SELECT * FROM audit_events WHERE workspace_id=? ORDER BY rowid', workspace_id)
        previous = 'GENESIS'
        for row in rows:
            if row['previous_hash'] != previous:
                return {'valid': False, 'eventId': row['id'], 'reason': 'previous_hash mismatch'}
            details = parse_json(row['details_json'], {})
            payload = _json_dump({
                'eventId': row['id'], 'workspaceId': row['workspace_id'], 'actorId': row['actor_id'],
                'action': row['action'], 'objectType': row['object_type'], 'objectId': row['object_id'],
                'result': row['result'], 'requestId': row['request_id'], 'details': details,
                'timestamp': row['created_at'], 'previous': previous,
            })
            if hash_bytes(payload) != row['event_hash']:
                return {'valid': False, 'eventId': row['id'], 'reason': 'event_hash mismatch'}
            previous = row['event_hash']
        return {'valid': True, 'count': len(rows), 'head': previous}
