import hashlib
import json
import math
import re
from pathlib import Path

from .core import AppError, gen_id, now, hash_bytes, parse_json, required, stable_json
from .parsers import detect_file, parse_document

# Keep the deterministic local baseline compatible with the Node implementation.
DEFAULT_INDEX_CONFIG = {
    'schemaVersion': 1,
    'chunking': {'targetTokens': 512, 'overlapTokens': 64, 'structureFirst': True},
    'embedding': {'provider': 'local-hash-v1', 'model': 'ordo-hash-embedding-v1', 'dimensions': 128, 'normalized': True},
    'fullText': {'provider': 'sqlite-fts5', 'tokenizer': 'unicode61', 'exactTerms': True},
    'fusion': {'method': 'rrf', 'k': 60, 'vectorTopK': 20, 'fullTextTopK': 20},
    'rerank': {'enabled': True, 'provider': 'local-lexical-v1', 'topK': 8},
    'prompt': {'template': 'strict-evidence-v1', 'maxEvidenceChars': 12000},
}


def estimate_tokens(text):
    value = str(text or '')
    chinese = len(re.findall(r'[\u3400-\u9fff]', value))
    rest = re.sub(r'[\u3400-\u9fff]', ' ', value).strip()
    words = len(rest.split()) if rest else 0
    return max(1, math.ceil(chinese * 0.7 + words * 1.3))


def local_embedding(text, dimensions=128):
    vector = [0.0] * dimensions
    value = str(text or '').lower().replace('\u3000', ' ')
    tokens = re.findall(r'[\u3400-\u9fff]|[a-z0-9_./-]+', value)
    for token in tokens:
        digest = bytes.fromhex(hash_bytes(token))
        for index in range(min(8, len(digest))):
            slot = ((digest[index] << 8) + digest[(index + 1) % len(digest)]) % dimensions
            vector[slot] += 1 if digest[(index + 2) % len(digest)] % 2 else -1
    length = math.sqrt(sum(item * item for item in vector)) or 1
    return [round(item / length, 8) for item in vector]


def cosine(left, right):
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return 0
    return sum(float(a) * float(b) for a, b in zip(left, right))


def lexical_score(query, content):
    terms = list(dict.fromkeys(re.findall(r'[\u3400-\u9fff]|[a-z0-9_./-]+', str(query or '').lower())))
    haystack = str(content or '').lower()
    return sum(term in haystack for term in terms) / len(terms) if terms else 0


def deep_merge(base, override):
    if not isinstance(override, dict):
        return base if override is None else override
    result = dict(base or {})
    for key, value in override.items():
        result[key] = deep_merge(result.get(key), value) if isinstance(value, dict) else value
    return result


def validate_index_config(config):
    embedding = config.get('embedding', {})
    fusion = config.get('fusion', {})
    rerank = config.get('rerank', {})
    if config.get('schemaVersion') != 1:
        raise AppError(400, 'INDEX_CONFIG_INVALID', '索引配置 schemaVersion 必须为 1')
    if embedding.get('provider') != 'local-hash-v1':
        raise AppError(400, 'INDEX_PROVIDER_UNSUPPORTED', '当前仅支持 local-hash-v1 向量 Provider')
    if not 8 <= int(embedding.get('dimensions', 0)) <= 2048:
        raise AppError(400, 'INDEX_CONFIG_INVALID', '向量维度必须在 8 到 2048 之间')
    if fusion.get('method') != 'rrf' or not 1 <= int(fusion.get('k', 0)) <= 1000:
        raise AppError(400, 'INDEX_CONFIG_INVALID', '当前仅支持 1 到 1000 的 RRF k')
    if not 1 <= int(rerank.get('topK', 0)) <= 50:
        raise AppError(400, 'INDEX_CONFIG_INVALID', '重排 TopK 必须在 1 到 50 之间')
    return config


from .knowledge_workbench import KnowledgeWorkbench


class KnowledgeService(KnowledgeWorkbench):
    def __init__(self, db, blob_store, artifact_store, tasks, audit, config):
        self.db, self.blob_store, self.artifact_store = db, blob_store, artifact_store
        self.tasks, self.audit, self.config = tasks, audit, config
        tasks.register('document.parse', self.parse_revision_task)
        tasks.register('release.build', self.build_release_task)

    def workspace_id(self):
        return self.config['localWorkspaceId']

    def ensure_kb(self, kb_id, workspace_id=None):
        row = self.db.one("SELECT * FROM knowledge_bases WHERE id=? AND workspace_id=? AND status!='deleted'", kb_id, workspace_id or self.workspace_id())
        if not row:
            raise AppError(404, 'NOT_FOUND', '知识库不存在或不可访问')
        return row

    def ensure_dataset(self, dataset_id, workspace_id=None):
        row = self.db.one("SELECT * FROM datasets WHERE id=? AND workspace_id=? AND status!='deleted'", dataset_id, workspace_id or self.workspace_id())
        if not row:
            raise AppError(404, 'NOT_FOUND', '数据集不存在或不可访问')
        return row

    def list_knowledge_bases(self, workspace_id):
        return self.db.all("""SELECT kb.*,
          (SELECT COUNT(*) FROM datasets d WHERE d.knowledge_base_id=kb.id AND d.status!='deleted') dataset_count,
          (SELECT COUNT(*) FROM documents doc JOIN datasets ds ON ds.id=doc.dataset_id WHERE ds.knowledge_base_id=kb.id AND doc.status!='deleted') document_count,
          (SELECT COUNT(*) FROM chunk_revisions cr JOIN datasets ds ON ds.id=cr.dataset_id WHERE ds.knowledge_base_id=kb.id AND cr.excluded=0 AND cr.id=(SELECT cr2.id FROM chunk_revisions cr2 WHERE cr2.chunk_logical_id=cr.chunk_logical_id ORDER BY revision_number DESC LIMIT 1)) chunk_count
          FROM knowledge_bases kb WHERE kb.workspace_id=? AND kb.status!='deleted' ORDER BY kb.updated_at DESC""", workspace_id)

    def create_knowledge_base(self, input, workspace_id, request_id=None):
        name = required(input.get('name'), 'name')
        kb_id, ds_id, profile_id, timestamp = gen_id('kb'), gen_id('ds'), gen_id('idxp'), now()
        index_config = validate_index_config(deep_merge(DEFAULT_INDEX_CONFIG, input.get('indexConfig') or {}))
        try:
            self.db.transaction(lambda: self._create_kb_rows(kb_id, ds_id, profile_id, workspace_id, name, input, index_config, timestamp))
        except Exception as error:
            if 'UNIQUE constraint failed' in str(error):
                raise AppError(409, 'NAME_CONFLICT', '同名知识库已存在')
            raise
        self.audit.append(workspace_id=workspace_id, action='knowledge_base.create', object_type='knowledge_base', object_id=kb_id, request_id=request_id, details={'name': name, 'datasetId': ds_id, 'profileId': profile_id})
        return self.get_knowledge_base(kb_id, workspace_id)

    def _create_kb_rows(self, kb_id, ds_id, profile_id, workspace_id, name, input, config, timestamp):
        self.db.run('INSERT INTO knowledge_bases(id,workspace_id,name,description,status,default_dataset_id,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)', kb_id, workspace_id, name, input.get('description', ''), 'active', ds_id, json.dumps(input.get('config', {}), ensure_ascii=False), timestamp, timestamp)
        self.db.run('INSERT INTO datasets(id,workspace_id,knowledge_base_id,name,description,language,labels_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', ds_id, workspace_id, kb_id, input.get('defaultDatasetName', name), input.get('description', ''), input.get('language', 'zh-CN'), json.dumps(input.get('labels', []), ensure_ascii=False), 'active', timestamp, timestamp)
        self.db.run('INSERT INTO index_profiles(id,workspace_id,knowledge_base_id,name,schema_version,config_json,config_hash,created_at) VALUES(?,?,?,?,?,?,?,?)', profile_id, workspace_id, kb_id, '默认索引配置', 1, json.dumps(config, ensure_ascii=False), hash_bytes(stable_json(config)), timestamp)
        self.db.run('UPDATE knowledge_bases SET default_index_profile_id=? WHERE id=?', profile_id, kb_id)

    def get_knowledge_base(self, kb_id, workspace_id):
        row = self.ensure_kb(kb_id, workspace_id)
        row['datasets'] = self.list_datasets(kb_id, workspace_id)
        row['indexProfiles'] = self.db.all('SELECT * FROM index_profiles WHERE knowledge_base_id=? AND workspace_id=? ORDER BY created_at DESC', kb_id, workspace_id)
        row['defaultIndexProfileId'] = row.get('default_index_profile_id') or (row['indexProfiles'][0]['id'] if row['indexProfiles'] else None)
        return row

    def update_knowledge_base(self, kb_id, input, workspace_id, request_id=None):
        current = self.ensure_kb(kb_id, workspace_id)
        values = (required(input.get('name'), 'name') if 'name' in input else current['name'], str(input.get('description', current['description'])), input.get('status', current['status']))
        if values[2] not in ('active', 'archived'):
            raise AppError(400, 'VALIDATION_ERROR', '知识库状态无效')
        try:
            self.db.run('UPDATE knowledge_bases SET name=?,description=?,status=?,updated_at=? WHERE id=? AND workspace_id=?', *values, now(), kb_id, workspace_id)
        except Exception as error:
            if 'UNIQUE constraint failed' in str(error):
                raise AppError(409, 'NAME_CONFLICT', '同名知识库已存在')
            raise
        self.audit.append(workspace_id=workspace_id, action='knowledge_base.update', object_type='knowledge_base', object_id=kb_id, request_id=request_id, details={'changed': list(input)})
        return self.get_knowledge_base(kb_id, workspace_id)

    def knowledge_base_impact(self, kb_id, workspace_id):
        self.ensure_kb(kb_id, workspace_id)
        return self.db.one('''SELECT (SELECT COUNT(*) FROM datasets WHERE knowledge_base_id=? AND workspace_id=? AND status!='deleted') datasets,
          (SELECT COUNT(*) FROM documents d JOIN datasets ds ON ds.id=d.dataset_id WHERE ds.knowledge_base_id=? AND d.workspace_id=? AND d.status!='deleted') documents,
          (SELECT COUNT(*) FROM knowledge_releases WHERE knowledge_base_id=? AND workspace_id=?) releases,
          (SELECT COUNT(*) FROM conversations WHERE knowledge_base_id=? AND workspace_id=? AND deleted_at IS NULL) conversations,
          (SELECT COUNT(*) FROM assistants a JOIN datasets ds ON ds.id=a.dataset_id WHERE ds.knowledge_base_id=? AND a.workspace_id=?) assistants''', kb_id, workspace_id, kb_id, workspace_id, kb_id, workspace_id, kb_id, workspace_id, kb_id, workspace_id)

    def delete_knowledge_base(self, kb_id, workspace_id, request_id=None):
        impact = self.knowledge_base_impact(kb_id, workspace_id)
        if impact['conversations'] or impact['assistants']:
            raise AppError(409, 'DEPENDENCY_CONFLICT', '知识库仍被会话或助手引用，不能删除', impact)
        timestamp = now()
        self.db.transaction(lambda: (self.db.run("UPDATE datasets SET status='deleted',deleted_at=?,updated_at=? WHERE knowledge_base_id=? AND workspace_id=?", timestamp, timestamp, kb_id, workspace_id), self.db.run("UPDATE knowledge_bases SET status='deleted',deleted_at=?,updated_at=? WHERE id=? AND workspace_id=?", timestamp, timestamp, kb_id, workspace_id)))
        self.audit.append(workspace_id=workspace_id, action='knowledge_base.delete', object_type='knowledge_base', object_id=kb_id, request_id=request_id, details=impact)
        return {'deleted': True, 'impact': impact}

    def list_datasets(self, kb_id, workspace_id):
        return self.db.all("""SELECT d.*,
          (SELECT COUNT(*) FROM sources s WHERE s.dataset_id=d.id AND s.deleted_at IS NULL) source_count,
          (SELECT COUNT(*) FROM documents doc WHERE doc.dataset_id=d.id AND doc.status!='deleted') document_count,
          (SELECT COUNT(*) FROM chunk_logicals cl WHERE cl.dataset_id=d.id) chunk_count,
          (SELECT COUNT(*) FROM knowledge_releases kr WHERE kr.dataset_id=d.id) release_count
          FROM datasets d WHERE d.knowledge_base_id=? AND d.workspace_id=? AND d.status!='deleted' ORDER BY d.updated_at DESC""", kb_id, workspace_id)

    def create_dataset(self, kb_id, input, workspace_id, request_id=None):
        self.ensure_kb(kb_id, workspace_id)
        dataset_id, timestamp = gen_id('ds'), now()
        try:
            self.db.run('INSERT INTO datasets(id,workspace_id,knowledge_base_id,name,description,language,labels_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', dataset_id, workspace_id, kb_id, required(input.get('name'), 'name'), input.get('description', ''), input.get('language', 'zh-CN'), json.dumps(input.get('labels', []), ensure_ascii=False), 'active', timestamp, timestamp)
        except Exception as error:
            if 'UNIQUE constraint failed' in str(error): raise AppError(409, 'NAME_CONFLICT', '同名数据集已存在')
            raise
        self.audit.append(workspace_id=workspace_id, action='dataset.create', object_type='dataset', object_id=dataset_id, request_id=request_id, details={'knowledgeBaseId': kb_id})
        return self.get_dataset(dataset_id, workspace_id)

    def get_dataset(self, dataset_id, workspace_id):
        row = self.ensure_dataset(dataset_id, workspace_id)
        row['sources'] = self.list_sources(dataset_id, workspace_id)
        row['releases'] = self.list_releases(dataset_id, workspace_id)
        row['counts'] = self.db.one('SELECT (SELECT COUNT(*) FROM documents WHERE dataset_id=? AND workspace_id=? AND status!=\'deleted\') documents, (SELECT COUNT(*) FROM chunk_logicals WHERE dataset_id=? AND workspace_id=?) chunks, (SELECT COUNT(*) FROM sources WHERE dataset_id=? AND workspace_id=? AND deleted_at IS NULL) sources', dataset_id, workspace_id, dataset_id, workspace_id, dataset_id, workspace_id)
        return row

    def update_dataset(self, dataset_id, input, workspace_id, request_id=None):
        current = self.ensure_dataset(dataset_id, workspace_id)
        if 'name' in input and not isinstance(input['name'], str):
            raise AppError(400, 'VALIDATION_ERROR', 'name 必须为非空字符串', {'field': 'name'})
        name = required(input['name'], 'name') if 'name' in input else current['name']
        values = (name, input.get('description', current['description']), input.get('language', current['language']), json.dumps(input.get('labels', current.get('labels', [])), ensure_ascii=False), input.get('status', current['status']))
        if values[4] not in ('active', 'archived'): raise AppError(400, 'VALIDATION_ERROR', '数据集状态无效')
        try:
            self.db.run('UPDATE datasets SET name=?,description=?,language=?,labels_json=?,status=?,updated_at=? WHERE id=? AND workspace_id=?', *values, now(), dataset_id, workspace_id)
        except Exception as error:
            if 'UNIQUE constraint failed' in str(error):
                raise AppError(409, 'NAME_CONFLICT', '同名数据集已存在')
            raise
        self.audit.append(workspace_id=workspace_id, action='dataset.update', object_type='dataset', object_id=dataset_id, request_id=request_id, details={'changed': list(input)})
        return self.get_dataset(dataset_id, workspace_id)

    def delete_dataset(self, dataset_id, workspace_id, request_id=None):
        self.ensure_dataset(dataset_id, workspace_id)
        dependencies = self.db.one("SELECT (SELECT COUNT(*) FROM assistants WHERE dataset_id=? AND workspace_id=? AND status!='deleted') assistants, (SELECT COUNT(*) FROM conversations WHERE dataset_id=? AND workspace_id=? AND deleted_at IS NULL) conversations, (SELECT COUNT(*) FROM knowledge_releases WHERE dataset_id=? AND workspace_id=?) releases", dataset_id, workspace_id, dataset_id, workspace_id, dataset_id, workspace_id)
        if dependencies['assistants'] or dependencies['conversations']: raise AppError(409, 'DEPENDENCY_CONFLICT', '数据集仍被助手或会话引用，不能删除', dependencies)
        timestamp = now()
        self.db.transaction(lambda: (self.db.run("UPDATE datasets SET status='deleted',deleted_at=?,updated_at=? WHERE id=? AND workspace_id=?", timestamp, timestamp, dataset_id, workspace_id), self.db.run("UPDATE sources SET deleted_at=?,updated_at=? WHERE dataset_id=? AND workspace_id=? AND deleted_at IS NULL", timestamp, timestamp, dataset_id, workspace_id)))
        self.audit.append(workspace_id=workspace_id, action='dataset.delete', object_type='dataset', object_id=dataset_id, request_id=request_id, details=dependencies)
        return {'deleted': True, 'datasetId': dataset_id, 'dependencies': dependencies}

    def list_sources(self, dataset_id, workspace_id):
        return self.db.all("SELECT s.*,(SELECT COUNT(*) FROM documents d WHERE d.source_id=s.id AND d.status!='deleted') document_count FROM sources s WHERE s.dataset_id=? AND s.workspace_id=? AND s.deleted_at IS NULL ORDER BY s.created_at DESC", dataset_id, workspace_id)

    def create_source(self, dataset_id, input, workspace_id, request_id=None):
        self.ensure_dataset(dataset_id, workspace_id)
        source_type = input.get('type', 'upload')
        if source_type not in ('upload', 'directory', 'archive', 'local_discovery', 'database', 'connector', 'synthetic'): raise AppError(400, 'VALIDATION_ERROR', '来源类型无效')
        source_id, timestamp = gen_id('src'), now()
        self.db.run('INSERT INTO sources(id,workspace_id,dataset_id,type,name,location_hint,config_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', source_id, workspace_id, dataset_id, source_type, required(input.get('name'), 'name'), input.get('locationHint', ''), json.dumps(input.get('config', {}), ensure_ascii=False), 'registered', timestamp, timestamp)
        self.audit.append(workspace_id=workspace_id, action='source.create', object_type='source', object_id=source_id, request_id=request_id, details={'datasetId': dataset_id, 'type': source_type})
        return self.db.one('SELECT * FROM sources WHERE id=?', source_id)

    def register_upload(self, dataset_id, source_id, filename, buffer, mime_type, workspace_id, request_id=None):
        self.ensure_dataset(dataset_id, workspace_id)
        source = self.db.one('SELECT * FROM sources WHERE id=? AND dataset_id=? AND workspace_id=? AND deleted_at IS NULL', source_id, dataset_id, workspace_id)
        if not source:
            raise AppError(404, 'NOT_FOUND', '数据来源不存在或不可访问')
        if not isinstance(buffer, (bytes, bytearray)) or not buffer:
            raise AppError(400, 'EMPTY_FILE', '上传文件为空')
        buffer = bytes(buffer)
        if len(buffer) > self.config['maxFileBytes']:
            raise AppError(413, 'FILE_TOO_LARGE', '文件超过当前大小预算', {'maxBytes': self.config['maxFileBytes']})
        filename = str(filename or '').replace('\\', '/').rsplit('/', 1)[-1]
        if not filename:
            raise AppError(400, 'FILE_REQUIRED', '请选择上传文件')
        detection = detect_file(buffer, filename)
        blob = self.blob_store.put(workspace_id, buffer, mime_type or detection['mimeType'])
        duplicate = self.db.one('''SELECT dr.*,d.id AS document_id,d.title FROM document_revisions dr
          JOIN documents d ON d.id=dr.document_id WHERE dr.workspace_id=? AND dr.content_hash=?
          AND d.dataset_id=? AND d.source_id=? AND d.status!='deleted' LIMIT 1''',
                                workspace_id, blob['sha256'], dataset_id, source_id)
        if duplicate:
            self.audit.append(workspace_id=workspace_id, action='document.duplicate_detected', object_type='document_revision', object_id=duplicate['id'], request_id=request_id, details={'sourceId': source_id, 'filename': filename})
            return {'duplicate': True, 'document': self.get_document(duplicate['document_id'], workspace_id), 'revision': duplicate}
        document_id, revision_id, timestamp = gen_id('doc'), gen_id('rev'), now()
        def persist_registration():
            self.db.run('INSERT INTO documents(id,workspace_id,dataset_id,source_id,title,logical_path,media_type,status,current_revision_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)', document_id, workspace_id, dataset_id, source_id, filename, filename, detection['mimeType'], 'queued', revision_id, timestamp, timestamp)
            self.db.run('INSERT INTO document_revisions(id,workspace_id,document_id,source_id,blob_id,revision_number,content_hash,size_bytes,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)', revision_id, workspace_id, document_id, source_id, blob['id'], 1, blob['sha256'], len(buffer), 'queued', timestamp)
            self.db.run("UPDATE sources SET status='queued',updated_at=? WHERE id=?", timestamp, source_id)
        self.db.transaction(persist_registration)
        task = self.tasks.create(workspace_id=workspace_id, task_type='document.parse', object_type='document_revision', object_id=revision_id, idempotency_key=f"parse:{revision_id}:{blob['sha256']}", input={'revisionId': revision_id})
        self.audit.append(workspace_id=workspace_id, action='document.register', object_type='document', object_id=document_id, request_id=request_id, details={'datasetId': dataset_id, 'sourceId': source_id, 'revisionId': revision_id, 'taskId': task['id'], 'filename': filename})
        return {'duplicate': False, 'document': self.get_document(document_id, workspace_id), 'revision': self.db.one('SELECT * FROM document_revisions WHERE id=?', revision_id), 'task': task}

    async def parse_revision_task(self, context):
        workspace_id = context['workspaceId']
        revision_id = context['input'].get('revisionId')
        checkpoint = context['checkpoint']
        revision = self.db.one('''SELECT dr.*,d.title,d.dataset_id,d.media_type,d.id AS document_id,d.source_id
          FROM document_revisions dr JOIN documents d ON d.id=dr.document_id WHERE dr.id=? AND dr.workspace_id=?''', revision_id, workspace_id)
        if not revision:
            raise AppError(404, 'NOT_FOUND', '待解析修订不存在')
        existing = self.db.one('SELECT * FROM parsed_artifacts WHERE document_revision_id=? AND workspace_id=?', revision['id'], workspace_id)
        if existing:
            return {'artifactId': existing['id'], 'reused': True}
        await checkpoint(10, '读取受管原件')
        blob = self.blob_store.get(revision['blob_id'], workspace_id)
        self.db.run("UPDATE document_revisions SET status='parsing' WHERE id=?", revision['id'])
        self.db.run("UPDATE documents SET status='parsing',updated_at=? WHERE id=?", now(), revision['document_id'])
        await checkpoint(25, '执行格式预检与解析路由')
        try:
            parsed = await self._parse_isolated(blob['buffer'], revision['title'])
        except AppError as error:
            state = 'needs_password' if error.code == 'NEEDS_PASSWORD' else 'unsupported' if error.code == 'UNSUPPORTED_FORMAT' else 'quarantined' if error.code == 'MIME_MISMATCH' else 'failed'
            warning = json.dumps([{'code': error.code or 'PARSE_FAILED', 'message': error.message}], ensure_ascii=False, separators=(',', ':'))
            self.db.run('UPDATE document_revisions SET status=?,warnings_json=? WHERE id=?', state, warning, revision['id'])
            self.db.run('UPDATE documents SET status=?,updated_at=? WHERE id=?', state, now(), revision['document_id'])
            self.db.run('UPDATE sources SET status=?,updated_at=? WHERE id=?', state, now(), revision['source_id'])
            raise
        await checkpoint(55, '生成标准 Markdown 与 JSON')
        artifact_id = gen_id('art')
        compact_json = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        manifest = {
            'schemaVersion': 1, 'artifactId': artifact_id, 'workspaceId': workspace_id, 'datasetId': revision['dataset_id'],
            'documentId': revision['document_id'], 'documentRevisionId': revision['id'], 'sourceId': revision['source_id'],
            'blobId': revision['blob_id'], 'originalHash': revision['content_hash'], 'mimeType': revision['media_type'],
            'sizeBytes': revision['size_bytes'], 'parser': parsed['metadata'].get('parser'), 'parserVersion': '1.0.0',
            'createdAt': now(), 'warnings': parsed['warnings'],
            'checksums': {'markdown': hash_bytes(parsed['markdown']), 'document': hash_bytes(compact_json(parsed['document'])), 'quality': hash_bytes(compact_json(parsed['quality']))},
        }
        keys = self.artifact_store.write_document(workspace_id, revision['id'], {'manifest.json': manifest, 'document.md': parsed['markdown'], 'document.json': parsed['document'], 'quality.json': parsed['quality']})
        artifact_hash = hash_bytes(compact_json(manifest['checksums']))
        await checkpoint(95, '校验标准产物与来源链')
        try:
            def persist_parsed_document():
                self.db.run('INSERT INTO parsed_artifacts(id,workspace_id,document_revision_id,schema_version,markdown_key,json_key,manifest_key,quality_key,content_hash,quality_status,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', artifact_id, workspace_id, revision['id'], 1, keys['document.md'], keys['document.json'], keys['manifest.json'], keys['quality.json'], artifact_hash, parsed['qualityStatus'], compact_json(parsed['metadata']), now())
                for index, block in enumerate(parsed['blocks'], 1):
                    logical_id, chunk_revision_id = gen_id('chunk'), gen_id('cr')
                    embedding = local_embedding(block['contentText'])
                    self.db.run('INSERT INTO chunk_logicals(id,workspace_id,dataset_id,document_id,created_at) VALUES(?,?,?,?,?)', logical_id, workspace_id, revision['dataset_id'], revision['document_id'], now())
                    self.db.run('INSERT INTO chunk_revisions(id,workspace_id,chunk_logical_id,dataset_id,document_id,document_revision_id,artifact_id,revision_number,type,content_md,content_text,source_locator_json,token_count,language,generated_by,confidence,warnings_json,sensitivity,excluded,embedding_json,embedding_model,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', chunk_revision_id, workspace_id, logical_id, revision['dataset_id'], revision['document_id'], revision['id'], artifact_id, 1, block['type'], block['contentMd'], block['contentText'], compact_json(block['locator']), block['tokenCount'], 'zh-CN', block['generatedBy'], block['confidence'], compact_json(block['warnings']), 'internal', 0, compact_json(embedding), 'ordo-hash-embedding-v1', now())
                    self.db.run('INSERT INTO chunks_fts(chunk_revision_id,workspace_id,dataset_id,title,breadcrumb,content) VALUES(?,?,?,?,?,?)', chunk_revision_id, workspace_id, revision['dataset_id'], revision['title'], f"{revision['title']} / {block['type']} {index}", block['contentText'])
                document_state = 'ready' if parsed['qualityStatus'] == 'publishable' else 'review_required'
                self.db.run('UPDATE document_revisions SET status=?,parser_id=?,parser_version=?,warnings_json=? WHERE id=?', document_state, parsed['metadata'].get('parser'), '1.0.0', compact_json(parsed['warnings']), revision['id'])
                self.db.run('UPDATE documents SET status=?,updated_at=? WHERE id=?', document_state, now(), revision['document_id'])
                self.db.run('UPDATE sources SET status=?,updated_at=? WHERE id=?', document_state, now(), revision['source_id'])
                self.audit.append(workspace_id=workspace_id, action='document.parse', object_type='document_revision', object_id=revision['id'], details={'artifactId': artifact_id, 'qualityStatus': parsed['qualityStatus'], 'blocks': len(parsed['blocks']), 'warnings': len(parsed['warnings'])})
            self.db.transaction(persist_parsed_document)
        except BaseException:
            for key in keys.values():
                try:
                    (Path(self.config['artifactRoot']) / key).unlink()
                except OSError:
                    pass
            raise
        return {'artifactId': artifact_id, 'documentId': revision['document_id'], 'revisionId': revision['id'], 'qualityStatus': parsed['qualityStatus'], 'blockCount': len(parsed['blocks']), 'warnings': parsed['warnings']}

    def get_artifact(self, artifact_id, workspace_id):
        artifact = self.db.one('SELECT * FROM parsed_artifacts WHERE id=? AND workspace_id=?', artifact_id, workspace_id)
        if not artifact:
            raise AppError(404, 'NOT_FOUND', '标准产物不存在或不可访问')
        return artifact

    def artifact_file(self, artifact_id, kind, workspace_id):
        artifact = self.get_artifact(artifact_id, workspace_id)
        keys = {'markdown': artifact['markdown_key'], 'document': artifact['json_key'], 'manifest': artifact['manifest_key'], 'quality': artifact['quality_key']}
        if kind not in keys:
            raise AppError(400, 'VALIDATION_ERROR', '标准产物类型无效')
        return self.artifact_store.read(keys[kind])

    def list_documents(self, dataset_id, workspace_id, status=None, query=None, limit=100, offset=0):
        self.ensure_dataset(dataset_id, workspace_id)
        clauses, params = ['d.dataset_id=?', 'd.workspace_id=?', "d.status!='deleted'"], [dataset_id, workspace_id]
        if status: clauses.append('d.status=?'); params.append(status)
        if query: clauses.append('(d.title LIKE ? OR d.logical_path LIKE ?)'); params += [f'%{query}%', f'%{query}%']
        where = ' AND '.join(clauses)
        total = (self.db.one(f'SELECT COUNT(*) count FROM documents d WHERE {where}', *params) or {}).get('count', 0)
        items = self.db.all(f'''SELECT d.*,dr.revision_number,dr.content_hash,dr.size_bytes,dr.parser_id,dr.warnings_json,pa.id artifact_id,pa.quality_status,
          (SELECT COUNT(*) FROM chunk_logicals cl WHERE cl.document_id=d.id) chunk_count FROM documents d LEFT JOIN document_revisions dr ON dr.id=d.current_revision_id LEFT JOIN parsed_artifacts pa ON pa.document_revision_id=dr.id WHERE {where} ORDER BY d.updated_at DESC LIMIT ? OFFSET ?''', *params, limit, offset)
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    def get_document(self, document_id, workspace_id):
        row = self.db.one("SELECT d.*,dr.revision_number,dr.content_hash,dr.size_bytes,dr.parser_id,dr.warnings_json,pa.id artifact_id,pa.quality_status,(SELECT COUNT(*) FROM chunk_logicals cl WHERE cl.document_id=d.id) chunk_count FROM documents d LEFT JOIN document_revisions dr ON dr.id=d.current_revision_id LEFT JOIN parsed_artifacts pa ON pa.document_revision_id=dr.id WHERE d.id=? AND d.workspace_id=? AND d.status!='deleted'", document_id, workspace_id)
        if not row: raise AppError(404, 'NOT_FOUND', '文档不存在或不可访问')
        row['revisions'] = self.db.all('SELECT * FROM document_revisions WHERE document_id=? AND workspace_id=? ORDER BY revision_number DESC', document_id, workspace_id)
        return row

    def delete_document(self, document_id, workspace_id, request_id=None):
        document = self.get_document(document_id, workspace_id)
        references = (self.db.one('SELECT COUNT(*) count FROM release_chunks rc JOIN chunk_revisions cr ON cr.id=rc.chunk_revision_id WHERE cr.document_id=?', document_id) or {}).get('count', 0)
        timestamp = now(); self.db.run("UPDATE documents SET status='deleted',deleted_at=?,updated_at=? WHERE id=? AND workspace_id=?", timestamp, timestamp, document_id, workspace_id)
        return {'deleted': True, 'documentId': document_id, 'releaseReferences': references, 'note': '已发布知识版本保留其不可变块引用，后续版本不会自动包含已删除文档。' if references else None}

    def list_chunks(self, dataset_id, workspace_id, query=None, document_id=None, type=None, warning=False, limit=100, offset=0):
        self.ensure_dataset(dataset_id, workspace_id)
        clauses, params = ['cr.dataset_id=?', 'cr.workspace_id=?', "d.status!='deleted'", 'cr.id=(SELECT cr2.id FROM chunk_revisions cr2 WHERE cr2.chunk_logical_id=cr.chunk_logical_id ORDER BY revision_number DESC LIMIT 1)'], [dataset_id, workspace_id]
        if document_id: clauses.append('cr.document_id=?'); params.append(document_id)
        if type: clauses.append('cr.type=?'); params.append(type)
        if warning: clauses.append("cr.warnings_json!='[]'")
        if query: clauses.append('(cr.content_text LIKE ? OR cr.id LIKE ? OR cr.chunk_logical_id LIKE ?)'); params += [f'%{query}%'] * 3
        where = ' AND '.join(clauses); join = 'FROM chunk_revisions cr JOIN documents d ON d.id=cr.document_id'
        total = (self.db.one(f'SELECT COUNT(*) count {join} WHERE {where}', *params) or {}).get('count', 0)
        items = self.db.all(f'SELECT cr.*,d.title document_title {join} WHERE {where} ORDER BY d.title,cr.created_at LIMIT ? OFFSET ?', *params, limit, offset)
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    def get_chunk(self, chunk_id, workspace_id):
        row = self.db.one("SELECT cr.*,d.title document_title FROM chunk_revisions cr JOIN documents d ON d.id=cr.document_id WHERE cr.id=? AND cr.workspace_id=? AND d.status!='deleted'", chunk_id, workspace_id)
        if not row: raise AppError(404, 'NOT_FOUND', '知识块不存在或不可访问')
        row['history'] = self.db.all('SELECT * FROM chunk_revisions WHERE chunk_logical_id=? AND workspace_id=? ORDER BY revision_number DESC', row['chunk_logical_id'], workspace_id)
        row['releases'] = self.db.all('SELECT kr.id,kr.version,kr.status,kr.created_at FROM release_chunks rc JOIN knowledge_releases kr ON kr.id=rc.release_id WHERE rc.chunk_revision_id=? AND kr.workspace_id=? ORDER BY kr.version DESC', chunk_id, workspace_id)
        return row

    def get_index_profile(self, profile_id, workspace_id):
        row = self.db.one('SELECT * FROM index_profiles WHERE id=? AND workspace_id=?', profile_id, workspace_id)
        if not row: raise AppError(404, 'NOT_FOUND', '索引配置不存在或不可访问')
        row['config'] = parse_json(row.get('config_json'), {})
        row['releaseCount'] = (self.db.one('SELECT COUNT(*) count FROM knowledge_releases WHERE index_profile_id=? AND workspace_id=?', profile_id, workspace_id) or {}).get('count', 0)
        return row

    def create_index_profile(self, kb_id, input, workspace_id, request_id=None):
        self.ensure_kb(kb_id, workspace_id)
        config = validate_index_config(deep_merge(DEFAULT_INDEX_CONFIG, input.get('config') or input.get('indexConfig') or {})); profile_id = gen_id('idxp')
        self.db.run('INSERT INTO index_profiles(id,workspace_id,knowledge_base_id,name,schema_version,config_json,config_hash,created_at) VALUES(?,?,?,?,?,?,?,?)', profile_id, workspace_id, kb_id, required(input.get('name'), 'name'), 1, json.dumps(config, ensure_ascii=False), hash_bytes(stable_json(config)), now())
        if input.get('setDefault'): self.set_default_index_profile(kb_id, profile_id, workspace_id, request_id)
        return self.get_index_profile(profile_id, workspace_id)

    def update_index_profile(self, profile_id, input, workspace_id, request_id=None):
        current = self.get_index_profile(profile_id, workspace_id)
        if current['releaseCount']: raise AppError(409, 'INDEX_PROFILE_IMMUTABLE', '已被 Release 使用的索引配置不可原地修改，请创建新配置')
        config = validate_index_config(deep_merge(current['config'], input.get('config') or {}))
        self.db.run('UPDATE index_profiles SET name=?,config_json=?,config_hash=? WHERE id=? AND workspace_id=?', required(input.get('name', current['name']), 'name'), stable_json(config), hash_bytes(stable_json(config)), profile_id, workspace_id)
        self.audit.append(workspace_id=workspace_id, action='index_profile.update', object_type='index_profile', object_id=profile_id, request_id=request_id)
        return self.get_index_profile(profile_id, workspace_id)

    def set_default_index_profile(self, kb_id, profile_id, workspace_id, request_id=None):
        kb, profile = self.ensure_kb(kb_id, workspace_id), self.get_index_profile(profile_id, workspace_id)
        if profile['knowledge_base_id'] != kb['id']: raise AppError(400, 'SCOPE_MISMATCH', '索引配置不属于所选知识库')
        self.db.run('UPDATE knowledge_bases SET default_index_profile_id=?,updated_at=? WHERE id=? AND workspace_id=?', profile_id, now(), kb_id, workspace_id)
        return self.get_knowledge_base(kb_id, workspace_id)

    def archive_index_profile(self, profile_id, workspace_id, request_id=None):
        current = self.get_index_profile(profile_id, workspace_id)
        if current['releaseCount']: raise AppError(409, 'INDEX_PROFILE_IN_USE', '已被 Release 使用的索引配置不能删除')
        if (self.db.one('SELECT COUNT(*) count FROM knowledge_bases WHERE default_index_profile_id=? AND workspace_id=?', profile_id, workspace_id) or {}).get('count', 0): raise AppError(409, 'INDEX_PROFILE_DEFAULT', '默认索引配置不能删除，请先切换默认配置')
        self.db.run('DELETE FROM index_profiles WHERE id=? AND workspace_id=?', profile_id, workspace_id); return {'deleted': True}

    def list_releases(self, dataset_id, workspace_id):
        return self.db.all('SELECT * FROM knowledge_releases WHERE dataset_id=? AND workspace_id=? ORDER BY version DESC', dataset_id, workspace_id)

    def get_release(self, release_id, workspace_id):
        row = self.db.one('SELECT * FROM knowledge_releases WHERE id=? AND workspace_id=?', release_id, workspace_id)
        if not row: raise AppError(404, 'NOT_FOUND', '知识版本不存在或不可访问')
        row['chunkCount'] = (self.db.one('SELECT COUNT(*) count FROM release_chunks WHERE release_id=?', release_id) or {}).get('count', 0); return row

    def release_content_fingerprint(self, dataset_id, workspace_id):
        chunks = self.db.all('''SELECT cr.id,cr.chunk_logical_id,cr.revision_number,cr.document_revision_id,cr.artifact_id,
          cr.content_text,cr.sensitivity,d.id AS document_id,d.title AS document_title,d.status AS document_status
          FROM chunk_revisions cr JOIN documents d ON d.id=cr.document_id
          WHERE cr.dataset_id=? AND cr.workspace_id=? AND d.status!='deleted' AND cr.excluded=0
          AND cr.id=(SELECT cr2.id FROM chunk_revisions cr2 WHERE cr2.chunk_logical_id=cr.chunk_logical_id ORDER BY cr2.revision_number DESC LIMIT 1)
          ORDER BY cr.chunk_logical_id''', dataset_id, workspace_id)
        snapshot = [{'id': chunk['id'], 'logicalId': chunk['chunk_logical_id'], 'revision': chunk['revision_number'],
                     'documentId': chunk['document_id'], 'documentRevisionId': chunk['document_revision_id'],
                     'artifactId': chunk['artifact_id'], 'documentTitle': chunk['document_title'],
                     'documentStatus': chunk['document_status'], 'sensitivity': chunk['sensitivity'],
                     'contentHash': hash_bytes(chunk['content_text'] or '')} for chunk in chunks]
        return hash_bytes(stable_json(snapshot))

    def build_release(self, dataset_id, input, workspace_id, request_id=None):
        dataset = self.ensure_dataset(dataset_id, workspace_id)
        if input.get('indexProfileId'):
            profile = self.db.one('SELECT * FROM index_profiles WHERE id=? AND knowledge_base_id=? AND workspace_id=?', input['indexProfileId'], dataset['knowledge_base_id'], workspace_id)
        else:
            profile = self.db.one('SELECT * FROM index_profiles WHERE id=(SELECT default_index_profile_id FROM knowledge_bases WHERE id=? AND workspace_id=?) OR (knowledge_base_id=? AND workspace_id=? AND NOT EXISTS (SELECT 1 FROM knowledge_bases WHERE id=? AND workspace_id=? AND default_index_profile_id IS NOT NULL)) ORDER BY created_at DESC LIMIT 1', dataset['knowledge_base_id'], workspace_id, dataset['knowledge_base_id'], workspace_id, dataset['knowledge_base_id'], workspace_id)
        if not profile:
            raise AppError(409, 'INDEX_PROFILE_REQUIRED', '知识库没有可用索引配置')
        activate = input.get('activate') is not False
        allow_review = bool(input.get('allowReviewRequired'))
        fingerprint = self.release_content_fingerprint(dataset_id, workspace_id)
        key = input.get('idempotencyKey') or f"release:{dataset_id}:{profile['id']}:{profile['config_hash']}:{fingerprint}:{'active' if activate else 'ready'}:{'review' if allow_review else 'strict'}"
        release_id = input.get('releaseId') or f"rel_{hash_bytes(key)[:32]}"
        task = self.tasks.create(workspace_id=workspace_id, task_type='release.build', object_type='dataset', object_id=dataset_id, idempotency_key=key, input={'datasetId': dataset_id, 'indexProfileId': profile['id'], 'releaseId': release_id, 'contentFingerprint': fingerprint, 'allowReviewRequired': allow_review, 'activate': activate})
        self.audit.append(workspace_id=workspace_id, action='release.build_requested', object_type='dataset', object_id=dataset_id, request_id=request_id, details={'taskId': task['id'], 'indexProfileId': profile['id']})
        return task

    async def build_release_task(self, context):
        input, workspace_id, checkpoint = context['input'], context['workspaceId'], context['checkpoint']
        dataset = self.ensure_dataset(input['datasetId'], workspace_id)
        profile = self.db.one('SELECT * FROM index_profiles WHERE id=? AND workspace_id=? AND knowledge_base_id=?', input['indexProfileId'], workspace_id, dataset['knowledge_base_id'])
        if not profile:
            raise AppError(404, 'NOT_FOUND', '索引配置不存在')
        current_fingerprint = self.release_content_fingerprint(input['datasetId'], workspace_id)
        if input.get('contentFingerprint') and input['contentFingerprint'] != current_fingerprint:
            raise AppError(409, 'RELEASE_CONTENT_CHANGED', '可发布知识块在任务排队后发生变化，请重新发起发布')
        fallback_key = f"release:{input['datasetId']}:{input['indexProfileId']}"
        release_id = input.get('releaseId') or f"rel_{hash_bytes(fallback_key)[:32]}"
        existing = self.db.one('SELECT * FROM knowledge_releases WHERE id=? AND workspace_id=?', release_id, workspace_id)
        if existing and existing['status'] in ('active', 'ready', 'superseded', 'retained'):
            manifest = parse_json(existing['manifest_json'], {})
            return {'releaseId': existing['id'], 'version': existing['version'], 'status': existing['status'], 'chunkCount': manifest.get('chunkCount', 0), 'quality': parse_json(existing['quality_json'], {})}
        version = existing['version'] if existing else (self.db.one('SELECT COALESCE(MAX(version),0)+1 version FROM knowledge_releases WHERE dataset_id=? AND workspace_id=?', input['datasetId'], workspace_id) or {})['version']
        if not existing:
            self.db.run('INSERT INTO knowledge_releases(id,workspace_id,dataset_id,knowledge_base_id,index_profile_id,version,status,manifest_json,quality_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)', release_id, workspace_id, input['datasetId'], dataset['knowledge_base_id'], profile['id'], version, 'building', '{}', '{}', 'pending', now())
        else:
            self.db.run("UPDATE knowledge_releases SET status='building',manifest_json='{}',quality_json='{}',content_hash='pending',activated_at=NULL WHERE id=? AND workspace_id=?", release_id, workspace_id)
            self.db.run('DELETE FROM release_chunks WHERE release_id=?', release_id)
        await checkpoint(10, '快照文档和知识块')
        problematic = self.db.all("SELECT id,title,status FROM documents WHERE dataset_id=? AND workspace_id=? AND status IN ('review_required','partial','failed','needs_password','quarantined')", input['datasetId'], workspace_id)
        if problematic and not input.get('allowReviewRequired'):
            raise AppError(409, 'RELEASE_QUALITY_BLOCKED', '存在未通过质量门的文档，发布已阻止', {'documents': problematic})
        chunks = self.db.all("SELECT cr.*,d.title document_title FROM chunk_revisions cr JOIN documents d ON d.id=cr.document_id WHERE cr.dataset_id=? AND cr.workspace_id=? AND d.status!='deleted' AND cr.excluded=0 AND cr.id=(SELECT cr2.id FROM chunk_revisions cr2 WHERE cr2.chunk_logical_id=cr.chunk_logical_id ORDER BY cr2.revision_number DESC LIMIT 1) ORDER BY d.title,cr.created_at", input['datasetId'], workspace_id)
        if not chunks:
            raise AppError(409, 'NO_PUBLISHABLE_CHUNKS', '数据集没有可发布知识块')
        content_hash = hash_bytes('|'.join(f"{chunk['id']}:{hash_bytes(chunk['content_text'])}" for chunk in chunks) + profile['config_hash'])
        manifest = {'schemaVersion': 1, 'releaseId': release_id, 'workspaceId': workspace_id, 'knowledgeBaseId': dataset['knowledge_base_id'], 'datasetId': input['datasetId'], 'indexProfileId': profile['id'], 'version': version, 'chunkCount': len(chunks), 'chunkRevisionIds': [chunk['id'] for chunk in chunks], 'createdAt': now(), 'contentHash': content_hash}
        await checkpoint(35, '构建隔离全文投影')
        for index, chunk in enumerate(chunks):
            if not chunk['embedding_json']:
                self.db.run('UPDATE chunk_revisions SET embedding_json=?,embedding_model=? WHERE id=?', json.dumps(local_embedding(chunk['content_text']), ensure_ascii=False, separators=(',', ':')), 'ordo-hash-embedding-v1', chunk['id'])
            if index % 50 == 0:
                await checkpoint(35 + index / len(chunks) * 35, '构建向量与全文投影', {'indexed': index, 'total': len(chunks)})
        await checkpoint(75, '验证条目、引用和投影一致性')
        self.db.run("UPDATE knowledge_releases SET status='validating' WHERE id=? AND workspace_id=?", release_id, workspace_id)
        invalid = [chunk['id'] for chunk in chunks if not chunk['document_id'] or not chunk['document_revision_id'] or not chunk['artifact_id'] or not chunk['content_text']]
        if invalid:
            raise AppError(500, 'RELEASE_VALIDATION_FAILED', 'Release 来源链验证失败', {'invalidChunkIds': invalid})
        quality = {'valid': True, 'chunkCount': len(chunks), 'invalidReferences': 0, 'reviewRequiredDocuments': len(problematic), 'checkedAt': now()}
        await checkpoint(95, '原子切换活动 Release' if input.get('activate') else 'Release 已就绪')
        def persist_release():
            status = 'active' if input.get('activate') else 'ready'
            self.db.run('UPDATE knowledge_releases SET status=?,manifest_json=?,quality_json=?,content_hash=?,activated_at=? WHERE id=? AND workspace_id=?', status, json.dumps(manifest, ensure_ascii=False, separators=(',', ':')), json.dumps(quality, ensure_ascii=False, separators=(',', ':')), content_hash, now() if input.get('activate') else None, release_id, workspace_id)
            for index, chunk in enumerate(chunks, 1):
                self.db.run('INSERT INTO release_chunks(release_id,chunk_revision_id,ordinal) VALUES(?,?,?)', release_id, chunk['id'], index)
            if input.get('activate'):
                self.db.run("UPDATE knowledge_releases SET status='superseded' WHERE dataset_id=? AND workspace_id=? AND status='active' AND id!=?", input['datasetId'], workspace_id, release_id)
                self.db.run('UPDATE datasets SET active_release_id=?,updated_at=? WHERE id=? AND workspace_id=?', release_id, now(), input['datasetId'], workspace_id)
                if dataset['id'] == (self.db.one('SELECT default_dataset_id FROM knowledge_bases WHERE id=?', dataset['knowledge_base_id']) or {}).get('default_dataset_id'):
                    self.db.run('UPDATE knowledge_bases SET active_release_id=?,updated_at=? WHERE id=? AND workspace_id=?', release_id, now(), dataset['knowledge_base_id'], workspace_id)
            self.audit.append(workspace_id=workspace_id, action='release.activate' if input.get('activate') else 'release.ready', object_type='knowledge_release', object_id=release_id, details={'datasetId': input['datasetId'], 'version': version, 'chunkCount': len(chunks), 'previousReleaseId': dataset.get('active_release_id')})
        self.db.transaction(persist_release)
        return {'releaseId': release_id, 'version': version, 'status': 'active' if input.get('activate') else 'ready', 'chunkCount': len(chunks), 'quality': quality}

    def _set_active_release(self, release, workspace_id):
        self.db.run("UPDATE knowledge_releases SET status='superseded' WHERE dataset_id=? AND workspace_id=? AND status='active' AND id!=?", release['dataset_id'], workspace_id, release['id'])
        self.db.run("UPDATE knowledge_releases SET status='active',activated_at=? WHERE id=? AND workspace_id=?", now(), release['id'], workspace_id)
        self.db.run('UPDATE datasets SET active_release_id=?,updated_at=? WHERE id=? AND workspace_id=?', release['id'], now(), release['dataset_id'], workspace_id)
        kb = self.db.one('SELECT default_dataset_id FROM knowledge_bases WHERE id=? AND workspace_id=?', release['knowledge_base_id'], workspace_id)
        if kb and kb['default_dataset_id'] == release['dataset_id']:
            self.db.run('UPDATE knowledge_bases SET active_release_id=?,updated_at=? WHERE id=? AND workspace_id=?', release['id'], now(), release['knowledge_base_id'], workspace_id)

    def activate_release(self, release_id, workspace_id, request_id=None):
        release = self.get_release(release_id, workspace_id)
        if release['status'] not in ('ready', 'active', 'superseded', 'retained'):
            raise AppError(409, 'INVALID_STATE', '当前 Release 不能激活')
        self.db.transaction(lambda: self._set_active_release(release, workspace_id))
        self.audit.append(workspace_id=workspace_id, action='release.activate', object_type='knowledge_release', object_id=release_id, request_id=request_id, details={'datasetId': release['dataset_id'], 'version': release['version']})
        return self.get_release(release_id, workspace_id)

    def rollback_release(self, release_id, workspace_id, request_id=None):
        target = self.get_release(release_id, workspace_id)
        if target['status'] not in ('ready', 'superseded', 'retained'):
            raise AppError(409, 'INVALID_STATE', '只有已验证的历史 Release 可以回滚')
        previous = self.ensure_dataset(target['dataset_id'], workspace_id).get('active_release_id')
        self.db.transaction(lambda: self._set_active_release(target, workspace_id))
        impact = self.release_impact(target['id'], workspace_id)
        self.audit.append(workspace_id=workspace_id, action='release.rollback', object_type='knowledge_release', object_id=target['id'], request_id=request_id, details={'previousReleaseId': previous, 'targetReleaseId': target['id'], 'impact': impact})
        result = self.get_release(target['id'], workspace_id)
        result['previousReleaseId'], result['impact'] = previous, impact
        return result

    def release_impact(self, release_id, workspace_id):
        release = self.get_release(release_id, workspace_id)
        count = lambda sql: (self.db.one(sql, release_id, workspace_id) or {}).get('count', 0)
        return {'releaseId': release_id, 'datasetId': release['dataset_id'], 'version': release['version'], 'status': release['status'], 'conversations': count('SELECT COUNT(*) count FROM conversations WHERE release_id=? AND workspace_id=? AND deleted_at IS NULL'), 'traces': count('SELECT COUNT(*) count FROM query_traces WHERE release_id=? AND workspace_id=?'), 'citations': count('SELECT COUNT(*) count FROM citations WHERE release_id=? AND workspace_id=?'), 'assistantReleases': count('SELECT COUNT(*) count FROM assistant_releases WHERE knowledge_release_id=? AND workspace_id=?'), 'active': release['status'] == 'active'}

    def search_release(self, release_id, query, workspace_id, limit=10, overrides=None):
        query = required(query, 'query')
        release = self.get_release(release_id, workspace_id)
        profile = self.get_index_profile(release['index_profile_id'], workspace_id)
        overrides = overrides or {}
        fusion = {**profile['config'].get('fusion', {}), **(overrides.get('fusion') or {})}
        weights = self._projection(release['dataset_id'], 'hybrid', workspace_id) or {'denseWeight': 1, 'sparseWeight': 1}
        weights = {'denseWeight': fusion.get('denseWeight', fusion.get('vectorWeight', weights['denseWeight'])), 'sparseWeight': fusion.get('sparseWeight', fusion.get('fullTextWeight', weights['sparseWeight']))}
        channels = {item['name']: item.get('enabled', False) for item in (overrides.get('route') or {}).get('routes', [])}
        fusion_k = max(1, min(1000, int(fusion.get('k', 60))))
        limit = max(1, min(50, int(limit or 10)))
        vector_limit = max(limit * 2, int(fusion.get('vectorTopK', 20)))
        full_text_limit = max(limit * 2, int(fusion.get('fullTextTopK', 20)))
        rows = self.db.all('SELECT cr.*,d.title document_title FROM release_chunks rc JOIN chunk_revisions cr ON cr.id=rc.chunk_revision_id JOIN documents d ON d.id=cr.document_id WHERE rc.release_id=? AND cr.workspace_id=? ORDER BY rc.ordinal', release_id, workspace_id)
        query_vector = local_embedding(query)
        vector = sorted(({'chunk': row, 'score': cosine(query_vector, parse_json(row.get('embedding_json'), []) or local_embedding(row['content_text']))} for row in rows), key=lambda item: item['score'], reverse=True)[:vector_limit]
        if not channels.get('vector', True):
            vector = []
        terms = [term.replace('"', '""') for term in str(query).strip().split() if term]
        full_text = []
        if terms and channels.get('full_text', channels.get('fullText', True)):
            try:
                full_text = self.db.all('''SELECT f.chunk_revision_id,bm25(chunks_fts,2.0,1.5,1.0) rank
                  FROM chunks_fts f JOIN release_chunks rc ON rc.chunk_revision_id=f.chunk_revision_id
                  WHERE chunks_fts MATCH ? AND rc.release_id=? AND f.workspace_id=? ORDER BY rank LIMIT ?''', ' OR '.join(f'"{term}"' for term in terms), release_id, workspace_id, full_text_limit)
            except Exception:
                full_text = []
        scores = {}
        for rank, item in enumerate(vector, 1):
            scores[item['chunk']['id']] = {'chunk': item['chunk'], 'vectorScore': item['score'], 'vectorRank': rank, 'fullTextScore': None, 'fullTextRank': None, 'rrf': weights['denseWeight'] / (fusion_k + rank)}
        by_id = {row['id']: row for row in rows}
        for rank, item in enumerate(full_text, 1):
            chunk = by_id.get(item['chunk_revision_id'])
            if not chunk:
                continue
            current = scores.setdefault(chunk['id'], {'chunk': chunk, 'vectorScore': None, 'vectorRank': None, 'fullTextScore': None, 'fullTextRank': None, 'rrf': 0})
            current['fullTextRank'], current['fullTextScore'] = rank, round(-float(item['rank']), 6)
            current['rrf'] += weights['sparseWeight'] / (fusion_k + rank)
        ranked = sorted(scores.values(), key=lambda item: item['rrf'] + lexical_score(query, item['chunk']['content_text']) * .01, reverse=True)[:limit]
        results = [{'rank': index, 'chunkRevisionId': item['chunk']['id'], 'documentId': item['chunk']['document_id'], 'documentRevisionId': item['chunk']['document_revision_id'], 'title': item['chunk']['document_title'], 'content': item['chunk']['content_text'], 'locator': item['chunk'].get('source_locator'), 'vectorScore': None if item['vectorScore'] is None else round(item['vectorScore'], 6), 'vectorRank': item['vectorRank'], 'fullTextScore': item['fullTextScore'], 'fullTextRank': item['fullTextRank'], 'fusionScore': round(item['rrf'], 8), 'rerankScore': lexical_score(query, item['chunk']['content_text'])} for index, item in enumerate(ranked, 1)]
        return {'release': release, 'query': query, 'routes': {'vector': channels.get('vector', True), 'fullText': channels.get('full_text', channels.get('fullText', True)), 'fusion': {'method': 'rrf', 'k': fusion_k, 'weights': weights, 'vectorTopK': vector_limit, 'fullTextTopK': full_text_limit}, 'rerank': 'local-lexical-v1'}, 'results': results}
