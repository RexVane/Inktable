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
        self._autostart_cache = None
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

    def dashboard(self, workspace_id=None):
        ws = workspace_id or self.workspace_id()
        tables = {'knowledgeBases': ('knowledge_bases', "status='active'"),
                  'datasets': ('datasets', "status='active'"), 'documents': ('documents', "status!='deleted'"),
                  'chunks': ('chunk_logicals', '1=1'), 'activeReleases': ('knowledge_releases', "status='active'"),
                  'conversations': ('conversations', 'deleted_at IS NULL'), 'requests': ('messages', "role='user'"),
                  'pendingTasks': ('tasks', "status IN ('queued','running','paused')"), 'failedTasks': ('tasks', "status='failed'"),
                  'modelConnections': ('model_connections', '1=1'), 'wikiPages': ('wiki_pages', '1=1'),
                  'assistants': ('assistants', "status!='deleted'")}
        counts = {key: self.db.one(f'SELECT COUNT(*) n FROM {table} WHERE workspace_id=? AND {where}', ws)['n']
                  for key, (table, where) in tables.items()}
        return {'generatedAt': now(), 'deploymentProfile': self.config['deploymentProfile'], 'status': 'ready', 'counts': counts,
                'taskSummary': self.db.all('SELECT status,COUNT(*) count FROM tasks WHERE workspace_id=? GROUP BY status', ws),
                'recentTasks': self.tasks.list(ws, limit=10)['items'], 'recentKnowledgeBases': self.knowledge.list_knowledge_bases(ws)[:10],
                'requestTrend': self.db.all("SELECT substr(created_at,1,10) day,COUNT(*) count FROM messages WHERE workspace_id=? AND role='user' AND created_at>=datetime('now','-6 days') GROUP BY day ORDER BY day", ws),
                'evidence': self.db.all('SELECT evidence_status,COUNT(*) count FROM query_traces WHERE workspace_id=? GROUP BY evidence_status', ws),
                'storage': {'blobs': self.blob_store.count_and_size(ws), 'artifacts': self.artifact_store.count_and_size(),
                            'databaseBytes': self.config['dbPath'].stat().st_size},
                'lastBackup': self.db.one('SELECT * FROM backup_manifests WHERE workspace_id=? ORDER BY created_at DESC LIMIT 1', ws),
                'components': self.health(ws)['components']}

    def get_settings(self, workspace_id=None):
        ws = workspace_id or self.workspace_id()
        result = {row['key']: dict(row['value'], updatedAt=row['updated_at'])
                  for row in self.db.all('SELECT * FROM settings WHERE workspace_id=?', ws)}
        if sys.platform == 'win32':
            result.setdefault('general', {})['autoStart'] = self._windows_autostart_enabled()
        return result

    def update_setting(self, key, value, workspace_id=None, request_id=None):
        ws = workspace_id or self.workspace_id()
        if key not in ('general', 'query', 'ingestion', 'backup') or not isinstance(value, dict):
            raise AppError(400, 'VALIDATION_ERROR', '设置分组无效，设置值必须是对象')
        if key == 'general' and 'autoStart' in value and sys.platform == 'win32':
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as run_key:
                if value['autoStart']:
                    pythonw = Path(sys.executable).with_name('pythonw.exe')
                    executable = str(pythonw if pythonw.exists() else Path(sys.executable))
                    command = subprocess.list2cmdline([executable, str(self.config['projectRoot'] / 'ordo.py'), 'serve'])
                    winreg.SetValueEx(run_key, 'Ordo', 0, winreg.REG_SZ, command)
                else:
                    try:
                        winreg.DeleteValue(run_key, 'Ordo')
                    except FileNotFoundError:
                        pass
                self._autostart_cache = bool(value['autoStart'])
        self.db.run('INSERT INTO settings(workspace_id,key,value_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(workspace_id,key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at', ws, key, stable_json(value), now())
        self.audit.append(workspace_id=ws, action='setting.update', object_type='setting', object_id=key, request_id=request_id, details={'fields': list(value)})
        return {'key': key, 'value': value, 'updatedAt': now()}

    def feature_flags(self, workspace_id=None):
        return self.db.all('SELECT key,enabled,config_json,updated_at FROM feature_flags WHERE workspace_id=? ORDER BY key', workspace_id or self.workspace_id())

    def set_feature_flag(self, key, input, workspace_id=None, request_id=None):
        ws = workspace_id or self.workspace_id()
        row = self.db.one('SELECT * FROM feature_flags WHERE workspace_id=? AND key=?', ws, key)
        if not row:
            raise AppError(404, 'NOT_FOUND', '功能开关不存在')
        if input.get('enabled') not in (True, False, 0, 1) or 'enabled' not in input:
            raise AppError(400, 'VALIDATION_ERROR', 'enabled 必须是布尔值或 0/1')
        self.db.run('UPDATE feature_flags SET enabled=?,config_json=?,updated_at=? WHERE workspace_id=? AND key=?', int(input['enabled']), stable_json(input.get('config', row['config'])), now(), ws, key)
        self.audit.append(workspace_id=ws, action='feature_flag.update', object_type='feature_flag', object_id=key, request_id=request_id, details={'enabled': bool(input['enabled'])})
        return next(item for item in self.feature_flags(ws) if item['key'] == key)

    def global_search(self, query, workspace_id=None, limit=30):
        ws, q = workspace_id or self.workspace_id(), required(query, 'query')
        results = []
        for table, kind, title, route, condition in [
            ('knowledge_bases', 'knowledge_base', 'name', '#/knowledge/config?kb=', "status!='deleted'"),
            ('datasets', 'dataset', 'name', '#/knowledge/datasets?dataset=', "status!='deleted'"),
            ('documents', 'document', 'title', '#/knowledge/parsing?document=', "status!='deleted'"),
            ('conversations', 'conversation', 'title', '#/apps/chat?conversation=', 'deleted_at IS NULL'),
            ('wiki_pages', 'wiki', 'title', '#/knowledge/datasets?wiki=', '1=1')]:
            rows = self.db.all(f'SELECT id,{title} title FROM {table} WHERE workspace_id=? AND {condition} AND {title} LIKE ? LIMIT ?', ws, '%' + q + '%', limit)
            results.extend(dict(row, type=kind, subtitle='', route=route + row['id']) for row in rows)
        rows = self.db.all("SELECT cr.id,substr(cr.content_text,1,100) title,d.title subtitle FROM chunk_revisions cr JOIN documents d ON d.id=cr.document_id WHERE cr.workspace_id=? AND d.status!='deleted' AND cr.content_text LIKE ? AND cr.revision_number=(SELECT MAX(revision_number) FROM chunk_revisions WHERE chunk_logical_id=cr.chunk_logical_id) LIMIT ?", ws, '%' + q + '%', limit)
        results.extend(dict(row, type='chunk', route='#/knowledge/index?chunk=' + row['id']) for row in rows)
        return {'query': q, 'results': results[:limit], 'total': min(len(results), limit)}

    def list_wiki(self, workspace_id=None, knowledge_base_id=None):
        ws = workspace_id or self.workspace_id()
        return self.db.all('SELECT wp.*,(SELECT COUNT(*) FROM wiki_revisions wr WHERE wr.page_id=wp.id) revision_count FROM wiki_pages wp WHERE workspace_id=?' + (' AND knowledge_base_id=?' if knowledge_base_id else '') + ' ORDER BY updated_at DESC', *([ws, knowledge_base_id] if knowledge_base_id else [ws]))

    def get_wiki(self, page_id, workspace_id=None):
        ws = workspace_id or self.workspace_id()
        row = self.db.one('SELECT * FROM wiki_pages WHERE id=? AND workspace_id=?', page_id, ws)
        if not row:
            raise AppError(404, 'NOT_FOUND', 'Wiki 页面不存在或不可访问')
        row['revisions'] = self.db.all('SELECT * FROM wiki_revisions WHERE page_id=? AND workspace_id=? ORDER BY revision_number DESC', page_id, ws)
        return row

    def create_wiki(self, input, workspace_id=None, request_id=None):
        ws = workspace_id or self.workspace_id()
        kb = self.knowledge.ensure_kb(required(input.get('knowledgeBaseId'), 'knowledgeBaseId'), ws)
        if input.get('parentId') and self.get_wiki(input['parentId'], ws)['knowledge_base_id'] != kb['id']:
            raise AppError(400, 'SCOPE_MISMATCH', '父页面不属于所选知识库')
        page_id, revision_id, timestamp = gen_id('wiki'), gen_id('wrev'), now()
        title = required(input.get('title'), 'title')
        self.db.transaction(lambda: (
            self.db.run('INSERT INTO wiki_pages(id,workspace_id,knowledge_base_id,parent_id,title,status,current_revision_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)', page_id, ws, kb['id'], input.get('parentId'), title, 'draft', revision_id, timestamp, timestamp),
            self.db.run('INSERT INTO wiki_revisions(id,workspace_id,page_id,revision_number,content_md,sources_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)', revision_id, ws, page_id, 1, input.get('contentMd', ''), stable_json(input.get('sources', [])), 'draft', timestamp)))
        self.audit.append(workspace_id=ws, action='wiki.create', object_type='wiki_page', object_id=page_id, request_id=request_id)
        return self.get_wiki(page_id, ws)

    def revise_wiki(self, page_id, input, workspace_id=None, request_id=None):
        ws = workspace_id or self.workspace_id()
        current = self.get_wiki(page_id, ws)
        content = required(input.get('contentMd'), 'contentMd')
        revision_id, timestamp, status = gen_id('wrev'), now(), 'published' if input.get('publish') else 'draft'
        def persist():
            version = self.db.one('SELECT COALESCE(MAX(revision_number),0)+1 version FROM wiki_revisions WHERE page_id=?', page_id)['version']
            self.db.run('INSERT INTO wiki_revisions(id,workspace_id,page_id,revision_number,content_md,sources_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)', revision_id, ws, page_id, version, content, stable_json(input.get('sources', [])), status, timestamp)
            self.db.run('UPDATE wiki_pages SET title=?,status=?,current_revision_id=?,updated_at=? WHERE id=? AND workspace_id=?', input.get('title') or current['title'], status, revision_id, timestamp, page_id, ws)
        self.db.transaction(persist)
        self.audit.append(workspace_id=ws, action='wiki.publish' if input.get('publish') else 'wiki.revise', object_type='wiki_page', object_id=page_id, request_id=request_id)
        return self.get_wiki(page_id, ws)

    def wiki_from_message(self, message_id, input, workspace_id=None, request_id=None):
        ws = workspace_id or self.workspace_id()
        message = self.db.one("SELECT m.*,c.knowledge_base_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=? AND m.workspace_id=? AND m.role='assistant'", message_id, ws)
        if not message:
            raise AppError(404, 'NOT_FOUND', '回答消息不存在或不可访问')
        citations = self.db.all('SELECT id,title,document_id,document_revision_id,chunk_revision_id,release_id FROM citations WHERE message_id=? AND workspace_id=? ORDER BY ordinal', message_id, ws)
        return self.create_wiki({'knowledgeBaseId': message['knowledge_base_id'], 'title': input.get('title') or '问答草稿 ' + message['created_at'][:10], 'contentMd': message['content'], 'sources': citations}, ws, request_id)

    def list_assistants(self, workspace_id=None):
        return self.db.all("SELECT a.*,d.name dataset_name,ar.version release_version FROM assistants a JOIN datasets d ON d.id=a.dataset_id LEFT JOIN assistant_releases ar ON ar.id=a.active_release_id WHERE a.workspace_id=? AND a.status!='deleted' ORDER BY a.updated_at DESC", workspace_id or self.workspace_id())

    def get_assistant(self, assistant_id, workspace_id=None):
        ws = workspace_id or self.workspace_id()
        row = self.db.one("SELECT * FROM assistants WHERE id=? AND workspace_id=? AND status!='deleted'", assistant_id, ws)
        if not row:
            raise AppError(404, 'NOT_FOUND', '智能助手不存在或不可访问')
        row['releases'] = self.db.all('SELECT * FROM assistant_releases WHERE assistant_id=? AND workspace_id=? ORDER BY version DESC', assistant_id, ws)
        return row

    def create_assistant(self, input, workspace_id=None, request_id=None):
        ws = workspace_id or self.workspace_id()
        dataset = self.knowledge.ensure_dataset(required(input.get('datasetId'), 'datasetId'), ws)
        assistant_id, timestamp = gen_id('asst'), now()
        self.db.run('INSERT INTO assistants(id,workspace_id,name,dataset_id,status,draft_config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)', assistant_id, ws, required(input.get('name'), 'name'), dataset['id'], 'draft', stable_json(input.get('config') or {'strictEvidence': True, 'language': 'zh-CN'}), timestamp, timestamp)
        self.audit.append(workspace_id=ws, action='assistant.create', object_type='assistant', object_id=assistant_id, request_id=request_id)
        return self.get_assistant(assistant_id, ws)

    def update_assistant(self, assistant_id, input, workspace_id=None, request_id=None):
        ws = workspace_id or self.workspace_id()
        current = self.get_assistant(assistant_id, ws)
        dataset_id = input.get('datasetId') or current['dataset_id']
        self.knowledge.ensure_dataset(dataset_id, ws)
        self.db.run("UPDATE assistants SET name=?,dataset_id=?,status='draft',draft_config_json=?,updated_at=? WHERE id=? AND workspace_id=?", input.get('name') or current['name'], dataset_id, stable_json({**current['draft_config'], **input.get('config', {})}), now(), assistant_id, ws)
        self.audit.append(workspace_id=ws, action='assistant.update', object_type='assistant', object_id=assistant_id, request_id=request_id)
        return self.get_assistant(assistant_id, ws)

    def publish_assistant(self, assistant_id, input=None, workspace_id=None, request_id=None):
        ws, input = workspace_id or self.workspace_id(), input or {}
        assistant = self.get_assistant(assistant_id, ws)
        dataset = self.knowledge.ensure_dataset(assistant['dataset_id'], ws)
        release_id = input.get('knowledgeReleaseId') or dataset.get('active_release_id')
        if not release_id:
            raise AppError(409, 'ACTIVE_RELEASE_REQUIRED', '助手绑定的数据集没有活动知识版本')
        release = self.knowledge.get_release(release_id, ws)
        if release['dataset_id'] != dataset['id'] or release['status'] != 'active':
            raise AppError(409, 'RELEASE_INVALID', '助手只能发布当前数据集的活动 Release')
        release_id2, timestamp = gen_id('arel'), now()
        def persist():
            version = self.db.one('SELECT COALESCE(MAX(version),0)+1 version FROM assistant_releases WHERE assistant_id=?', assistant_id)['version']
            self.db.run('INSERT INTO assistant_releases(id,workspace_id,assistant_id,knowledge_release_id,version,config_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)', release_id2, ws, assistant_id, release_id, version, stable_json({**assistant['draft_config'], 'strictEvidence': True, 'knowledgeReleaseId': release_id}), 'published', timestamp)
            self.db.run("UPDATE assistants SET status='published',active_release_id=?,updated_at=? WHERE id=? AND workspace_id=?", release_id2, timestamp, assistant_id, ws)
        self.db.transaction(persist)
        self.audit.append(workspace_id=ws, action='assistant.publish', object_type='assistant_release', object_id=release_id2, request_id=request_id)
        return self.get_assistant(assistant_id, ws)

    def pause_assistant(self, assistant_id, workspace_id=None, request_id=None):
        ws = workspace_id or self.workspace_id()
        self.get_assistant(assistant_id, ws)
        self.db.run("UPDATE assistants SET status='paused',updated_at=? WHERE id=? AND workspace_id=?", now(), assistant_id, ws)
        self.audit.append(workspace_id=ws, action='assistant.pause', object_type='assistant', object_id=assistant_id, request_id=request_id)
        return self.get_assistant(assistant_id, ws)

    def delete_assistant(self, assistant_id, workspace_id=None, request_id=None):
        ws = workspace_id or self.workspace_id()
        self.get_assistant(assistant_id, ws)
        self.db.transaction(lambda: (
            self.db.run("UPDATE assistants SET status='deleted',updated_at=? WHERE id=? AND workspace_id=?", now(), assistant_id, ws),
            self.db.run("UPDATE widget_clients SET status='revoked' WHERE assistant_id=? AND workspace_id=?", assistant_id, ws)))
        self.audit.append(workspace_id=ws, action='assistant.delete', object_type='assistant', object_id=assistant_id, request_id=request_id)
        return {'deleted': True, 'assistantId': assistant_id}

    def diagnostics(self, workspace_id=None):
        ws = workspace_id or self.workspace_id()
        return {'generatedAt': now(), 'version': self.config['appVersion'], 'runtime': 'Python/FastAPI',
                'pythonVersion': sys.version.split()[0], 'schemaVersion': self.schema_version(),
                'health': self.health(ws), 'audit': self.audit.verify(ws),
                'taskSummary': self.db.all('SELECT status,COUNT(*) count FROM tasks WHERE workspace_id=? GROUP BY status', ws),
                'featureFlags': self.feature_flags(ws)}

    def _windows_autostart_enabled(self):
        if sys.platform != 'win32':
            return False
        if self._autostart_cache is not None:
            return self._autostart_cache
        try:
            out = subprocess.run(['reg', 'query', r'HKCU\Software\Microsoft\Windows\CurrentVersion\Run', '/v', 'Ordo'],
                                 capture_output=True, timeout=2).stdout.decode('utf-8', errors='ignore')
            self._autostart_cache = 'Ordo' in out and 'REG_SZ' in out
        except Exception:  # noqa: BLE001
            self._autostart_cache = False
        return self._autostart_cache

    def request_backup(self, input=None, workspace_id=None, request_id=None):
        input, ws = input or {}, workspace_id or self.workspace_id()
        key = input.get('idempotencyKey') or f'backup:{ws}:{now()[:13]}'
        existing = self.db.one('SELECT id FROM tasks WHERE workspace_id=? AND idempotency_key=?', ws, key)
        if existing and not input.get('idempotencyKey'):
            return self.tasks.get(existing['id'], ws)
        return self.tasks.create(workspace_id=ws, task_type='backup.create', object_type='workspace', object_id=ws, idempotency_key=key, input={'label': input.get('label') or 'manual'})

    def list_backups(self, workspace_id=None):
        return self.db.all('SELECT * FROM backup_manifests WHERE workspace_id=? ORDER BY created_at DESC', workspace_id or self.workspace_id())

    def request_restore(self, backup_id, input=None, workspace_id=None, request_id=None):
        from .backup import validate_target
        input, ws = input or {}, workspace_id or self.workspace_id()
        if not self.db.one('SELECT id FROM backup_manifests WHERE id=? AND workspace_id=?', backup_id, ws):
            raise AppError(404, 'NOT_FOUND', '备份不存在')
        target = validate_target(input.get('targetRoot') or str(self.config['dataRoot']) + '-restore-' + backup_id, self.config['dataRoot'])
        return self.tasks.create(workspace_id=ws, task_type='backup.restore', object_type='backup', object_id=backup_id, idempotency_key=input.get('idempotencyKey') or f'restore:{backup_id}:{target}', input={'backupId': backup_id, 'targetRoot': str(target)})

    async def backup_task(self, context):
        from .backup import create_backup
        return await create_backup(self, context)

    async def restore_task(self, context):
        from .backup import restore_backup
        return await restore_backup(self, context)
