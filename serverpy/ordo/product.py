import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import stat as stat_module
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

from .core import AppError, gen_id, hash_bytes, now, parse_json, required, safe_name, stable_json
from .models import ModelService  # noqa: F401


class ProductService:
    def __init__(self, db, knowledge, query, models, tasks, audit, blob_store, artifact_store, secret_store, config):
        self.db = db
        self.knowledge = knowledge
        self.query = query
        self.models = models
        self.tasks = tasks
        self.audit = audit
        self.blob_store = blob_store
        self.artifact_store = artifact_store
        self.secret_store = secret_store
        self.config = config
        tasks.register('backup.create', self.backup_task)
        tasks.register('backup.restore', self.restore_task)

    def workspace_id(self):
        return self.config['localWorkspaceId']

    def schema_version(self):
        row = self.db.one('SELECT COALESCE(MAX(version),0) AS version FROM schema_migrations')
        return int((row or {}).get('version', 0) or 0)

    def require_feature(self, key, workspace_id=None):
        workspace_id = workspace_id or self.workspace_id()
        flag = self.db.one('SELECT key,enabled,config_json FROM feature_flags WHERE key=? AND workspace_id=?', key, workspace_id)
        if not flag or not flag['enabled']:
            raise AppError(403, 'FEATURE_DISABLED', f'功能“{key}”当前未启用', {'feature': key})
        return flag

    def health(self, workspace_id=None):
        workspace_id = workspace_id or self.workspace_id()
        database = {'status': 'available'}
        schema_version = None
        try:
            self.db.one('SELECT 1 AS ok')
            schema_version = self.schema_version()
        except Exception as error:  # noqa: BLE001
            database = {'status': 'unavailable', 'error': str(error)}
        write_access = []
        for directory in [self.config['blobRoot'], self.config['artifactRoot'], self.config['backupRoot']]:
            writable = False
            try:
                writable = Path(directory).exists() and bool(Path(directory).stat().st_mode & stat_module.S_IWUSR)
            except OSError:
                writable = False
            write_access.append({'directory': Path(directory).name, 'writable': writable})
        models = self.models.list(workspace_id)
        return {
            'status': 'ready' if database['status'] == 'available' and all(item['writable'] for item in write_access) else 'degraded',
            'checkedAt': now(),
            'components': {
                'metadata': {**database, 'provider': 'sqlite', 'schemaVersion': schema_version},
                'blob': {'status': 'available' if write_access[0]['writable'] else 'unavailable', 'provider': 'local-managed'},
                'artifacts': {'status': 'available' if write_access[1]['writable'] else 'unavailable', 'provider': 'local-managed'},
                'fullText': {'status': 'available', 'provider': 'sqlite-fts5'},
                'vector': {'status': 'available', 'provider': 'ordo-local-hash-v1', 'note': 'deterministic local baseline; replaceable provider'},
                'generation': {'status': 'available' if any(model.get('status') == 'available' for model in models) else 'degraded', 'connections': len(models)},
                'parser': {'status': 'available', 'native': ['md', 'txt', 'csv', 'xlsx', 'docx', 'pptx', 'pdf-text'], 'reviewRequired': ['image', 'pdf-scan']},
            },
        }

    # ---- 以下方法在后续里程碑补全（dashboard/settings/backup 等） ----

    def _windows_autostart_enabled(self):
        if sys.platform != 'win32':
            return False
        try:
            out = subprocess.run(['reg', 'query', r'HKCU\Software\Microsoft\Windows\CurrentVersion\Run', '/v', 'Ordo'],
                                 capture_output=True, timeout=2).stdout.decode('utf-8', errors='ignore')
            return 'Ordo' in out and 'REG_SZ' in out
        except Exception:  # noqa: BLE001
            return False

    # 任务处理器占位（M5 实现）
    async def backup_task(self, context):
        raise AppError(500, 'INTERNAL_ERROR', 'backup task not implemented')

    async def restore_task(self, context):
        raise AppError(500, 'INTERNAL_ERROR', 'restore task not implemented')
