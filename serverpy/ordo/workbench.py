"""Dataset file management and parsing workbench backed by persisted records."""
import asyncio
import base64
import difflib
import json
import os
import time
from pathlib import Path

from .core import AppError, bounded_int, gen_id, now, required, stable_json
from .parsers import detect_file, normalize_text


class WorkbenchService:
    def __init__(self, db, knowledge, tasks, product, config):
        self.db, self.knowledge, self.tasks, self.product, self.config = db, knowledge, tasks, product, config

    def datasets(self, ws, kb_id=None):
        rows = self.db.all("SELECT d.*,(SELECT COUNT(*) FROM documents doc WHERE doc.dataset_id=d.id AND doc.status!='deleted') documentCount,k.default_dataset_id FROM datasets d JOIN knowledge_bases k ON k.id=d.knowledge_base_id WHERE d.workspace_id=? AND d.status='active'" + (' AND d.knowledge_base_id=?' if kb_id else '') + ' ORDER BY d.created_at', *([ws, kb_id] if kb_id else [ws]))
        return [dict(row, isDefault=row['id'] == row['default_dataset_id'], updatedAt=row['updated_at']) for row in rows]

    def create_dataset(self, input, ws, request_id):
        kb_id = input.get('knowledgeBaseId') or input.get('kbId')
        if not kb_id:
            kbs = self.knowledge.list_knowledge_bases(ws)
            if len(kbs) == 1:
                kb_id = kbs[0]['id']
            elif not kbs:
                kb = self.knowledge.create_knowledge_base({'name': input.get('knowledgeBaseName') or '默认知识库'}, ws, request_id)
                kb_id = kb['id']
            else:
                raise AppError(400, 'KNOWLEDGE_BASE_REQUIRED', '存在多个知识库，请指定 knowledgeBaseId')
        return self.knowledge.create_dataset(kb_id, input, ws, request_id)

    def register_file(self, filename, content, mime, input, ws, request_id):
        dataset_id = input.get('datasetId')
        if dataset_id:
            source_id = input.get('sourceId') or self.knowledge.create_source(dataset_id, {'name': filename, 'type': 'upload'}, ws, request_id)['id']
            result = self.knowledge.register_upload(dataset_id, source_id, filename, content, mime, ws, request_id)
            if input.get('folderId'):
                self.move_file(dataset_id, result['document']['id'], {'folderId': input['folderId']}, ws)
            return result
        if not content:
            raise AppError(400, 'EMPTY_FILE', '上传文件为空')
        if len(content) > self.config['maxFileBytes']:
            raise AppError(413, 'FILE_TOO_LARGE', '文件超过大小预算')
        filename = str(filename).replace('\\', '/').rsplit('/', 1)[-1]
        detection = detect_file(content, filename)
        blob = self.knowledge.blob_store.put(ws, content, mime or detection['mimeType'])
        file_id, timestamp = gen_id('reg'), now()
        self.db.run('INSERT INTO registered_files(id,workspace_id,blob_id,name,media_type,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', file_id, ws, blob['id'], filename, detection['mimeType'], timestamp, timestamp)
        self.product.audit.append(workspace_id=ws, action='source.register', object_type='registered_file', object_id=file_id, request_id=request_id)
        return {'id': file_id, 'sourceId': file_id, 'fileId': file_id, 'name': filename, 'status': 'unassigned', 'sizeBytes': len(content), 'datasetId': None}

    def sources(self, ws, query=None, dataset_id=None, status=None):
        rows = self.db.all("SELECT s.*,d.name dataset_name,(SELECT COUNT(*) FROM documents doc WHERE doc.source_id=s.id AND doc.status!='deleted') document_count FROM sources s JOIN datasets d ON d.id=s.dataset_id WHERE s.workspace_id=? AND s.deleted_at IS NULL", ws)
        rows += self.db.all("SELECT r.*,NULL dataset_id,NULL dataset_name,'upload' type,1 document_count,b.size_bytes FROM registered_files r JOIN blobs b ON b.id=r.blob_id WHERE r.workspace_id=? AND r.status='unassigned'", ws)
        rows = [dict(row, datasetId=row.get('dataset_id'), datasetName=row.get('dataset_name'), updatedAt=row['updated_at']) for row in rows]
        return sorted([row for row in rows if (not query or query.lower() in row['name'].lower()) and (not dataset_id or row.get('dataset_id') == dataset_id) and (not status or row['status'] == status)], key=lambda row: row['created_at'], reverse=True)

    def assign_source(self, source_id, input, ws, request_id):
        target = self.knowledge.ensure_dataset(required(input.get('datasetId'), 'datasetId'), ws)
        registration = self.db.one("SELECT * FROM registered_files WHERE id=? AND workspace_id=? AND status!='deleted'", source_id, ws)
        if registration:
            if registration['status'] == 'assigned':
                return self.assign_source(registration['source_id'], input, ws, request_id)
            blob = self.knowledge.blob_store.get(registration['blob_id'], ws)
            source = self.knowledge.create_source(target['id'], {'name': registration['name'], 'type': 'upload'}, ws, request_id)
            result = self.knowledge.register_upload(target['id'], source['id'], registration['name'], blob['buffer'], registration['media_type'], ws, request_id)
            self.db.run("UPDATE registered_files SET status='assigned',source_id=?,document_id=?,updated_at=? WHERE id=? AND workspace_id=?", source['id'], result['document']['id'], now(), source_id, ws)
            return dict(result, source=source, datasetId=target['id'])
        source = self.db.one('SELECT * FROM sources WHERE id=? AND workspace_id=? AND deleted_at IS NULL', source_id, ws)
        if not source:
            raise AppError(404, 'NOT_FOUND', '数据来源不存在')
        if source['dataset_id'] == target['id']:
            return dict(source, datasetId=target['id'])
        documents = self.db.all("SELECT id FROM documents WHERE source_id=? AND workspace_id=? AND status!='deleted'", source_id, ws)
        results = [self.move_file(source['dataset_id'], row['id'], {'datasetId': target['id']}, ws) for row in documents]
        self.delete_source(source_id, ws, request_id)
        return {'sourceId': source_id, 'datasetId': target['id'], 'files': results}

    def delete_source(self, source_id, ws, request_id):
        registration = self.db.one("SELECT * FROM registered_files WHERE id=? AND workspace_id=? AND status!='deleted'", source_id, ws)
        if registration and registration['status'] == 'unassigned':
            self.db.run("UPDATE registered_files SET status='deleted',updated_at=? WHERE id=? AND workspace_id=?", now(), source_id, ws)
        else:
            source_id = registration['source_id'] if registration else source_id
            if not self.db.one('SELECT id FROM sources WHERE id=? AND workspace_id=? AND deleted_at IS NULL', source_id, ws):
                raise AppError(404, 'NOT_FOUND', '数据来源不存在')
            for row in self.db.all("SELECT id FROM documents WHERE source_id=? AND workspace_id=? AND status!='deleted'", source_id, ws):
                self.knowledge.delete_document(row['id'], ws, request_id)
            self.db.run("UPDATE sources SET status='deleted',deleted_at=?,updated_at=? WHERE id=? AND workspace_id=?", now(), now(), source_id, ws)
        self.product.audit.append(workspace_id=ws, action='source.delete', object_type='source', object_id=source_id, request_id=request_id)
        return {'deleted': True, 'sourceId': source_id}

    def folder(self, folder_id, dataset_id, ws):
        if not folder_id or folder_id == 'root':
            return None
        row = self.db.one('SELECT * FROM dataset_folders WHERE id=? AND dataset_id=? AND workspace_id=?', folder_id, dataset_id, ws)
        if not row:
            raise AppError(404, 'NOT_FOUND', '文件夹不存在或不属于当前数据集')
        return row

    def create_folder(self, dataset_id, input, ws):
        self.knowledge.ensure_dataset(dataset_id, ws)
        name = required(input.get('name'), 'name')
        if name in ('.', '..') or any(c in name for c in '/\\\x00'):
            raise AppError(400, 'VALIDATION_ERROR', '文件夹名称不能包含路径分隔符')
        parent = self.folder(input.get('parentId'), dataset_id, ws)
        path = (parent['path'] + '/' if parent else '') + name
        if self.db.one('SELECT id FROM dataset_folders WHERE dataset_id=? AND path=?', dataset_id, path):
            raise AppError(409, 'FOLDER_EXISTS', '同名文件夹已存在')
        folder_id = gen_id('fld')
        self.db.run('INSERT INTO dataset_folders(id,workspace_id,dataset_id,parent_id,name,path,created_at) VALUES(?,?,?,?,?,?,?)', folder_id, ws, dataset_id, parent['id'] if parent else None, name, path, now())
        return self.folder(folder_id, dataset_id, ws)

    def tree(self, dataset_id, ws):
        self.knowledge.ensure_dataset(dataset_id, ws)
        rows = self.db.all("SELECT f.*,(SELECT COUNT(*) FROM documents d WHERE d.folder_id=f.id AND d.status!='deleted') fileCount FROM dataset_folders f WHERE f.dataset_id=? AND f.workspace_id=? ORDER BY path", dataset_id, ws)
        nodes = {row['id']: dict(row, children=[]) for row in rows}
        roots = []
        for node in nodes.values():
            (nodes[node['parent_id']]['children'] if node['parent_id'] in nodes else roots).append(node)
        return roots

    def files(self, dataset_id, ws, params):
        self.knowledge.ensure_dataset(dataset_id, ws)
        folder = self.folder(params.get('folderId'), dataset_id, ws)
        clauses, args = ["d.dataset_id=?", 'd.workspace_id=?', "d.status!='deleted'", 'd.folder_id IS ?'], [dataset_id, ws, folder['id'] if folder else None]
        if params.get('query') or params.get('q'):
            clauses.append('d.title LIKE ?')
            args.append('%' + (params.get('query') or params['q']) + '%')
        if params.get('status'):
            clauses.append('d.status=?')
            args.append({'completed': 'ready', 'pending': 'queued'}.get(params['status'], params['status']))
        limit = bounded_int(params.get('limit'), 100, 1, 500, 'limit')
        offset = bounded_int(params.get('offset'), (bounded_int(params.get('page'), 1, 1, 100000, 'page') - 1) * limit, 0, 100000000, 'offset')
        where = ' AND '.join(clauses)
        total = self.db.one(f'SELECT COUNT(*) n FROM documents d WHERE {where}', *args)['n']
        rows = self.db.all(f'SELECT d.id FROM documents d WHERE {where} ORDER BY d.updated_at DESC LIMIT ? OFFSET ?', *args, limit, offset)
        return {'items': [self.inspect_file(row['id'], ws) for row in rows], 'total': total, 'limit': limit, 'offset': offset}

    def inspect_file(self, file_id, ws):
        row = self.db.one("SELECT r.*,b.size_bytes FROM registered_files r JOIN blobs b ON b.id=r.blob_id WHERE r.id=? AND r.workspace_id=? AND r.status!='deleted'", file_id, ws)
        if row and row['status'] == 'unassigned':
            return dict(row, fileType=Path(row['name']).suffix.lstrip('.'), sizeBytes=row['size_bytes'], datasetId=None)
        document = self.knowledge.get_document(row['document_id'] if row else file_id, ws)
        source = self.db.one('SELECT name,type FROM sources WHERE id=? AND workspace_id=?', document['source_id'], ws)
        folder = self.folder(document.get('folder_id'), document['dataset_id'], ws)
        size = document.get('size_bytes') or 0
        return dict(document, name=document['title'], fileType=Path(document['title']).suffix.lstrip('.'),
                    processStatus={'ready': 'completed', 'queued': 'pending'}.get(document['status'], document['status']),
                    chunkCount=document['chunk_count'], sizeBytes=size, sizeFormatted=f'{size / 1048576:.2f} MB' if size >= 1048576 else f'{size / 1024:.2f} KB',
                    folderPath=folder['path'] if folder else '', source=source['name'], sourceType=source['type'], updatedAt=document['updated_at'])

    def move_file(self, dataset_id, file_id, input, ws):
        document = self.knowledge.get_document(file_id, ws)
        if document['dataset_id'] != dataset_id:
            raise AppError(404, 'NOT_FOUND', '文件不属于当前数据集')
        target_id = input.get('targetDatasetId') or input.get('datasetId') or dataset_id
        self.knowledge.ensure_dataset(target_id, ws)
        folder = self.folder(input.get('targetFolderId', input.get('folderId')), target_id, ws)
        if target_id != dataset_id:
            revision = document['revisions'][0]
            blob = self.knowledge.blob_store.get(revision['blob_id'], ws)
            result = self.register_file(document['title'], blob['buffer'], document['media_type'], {'datasetId': target_id}, ws, None)
            file_id = result['document']['id']
            self.knowledge.delete_document(document['id'], ws)
        self.db.run('UPDATE documents SET folder_id=?,logical_path=?,updated_at=? WHERE id=? AND workspace_id=?', folder['id'] if folder else None, (folder['path'] + '/' if folder else '') + document['title'], now(), file_id, ws)
        return self.inspect_file(file_id, ws)

    def delete_file(self, dataset_id, file_id, ws):
        if self.knowledge.get_document(file_id, ws)['dataset_id'] != dataset_id:
            raise AppError(404, 'NOT_FOUND', '文件不属于当前数据集')
        return self.knowledge.delete_document(file_id, ws)

    def parsing_settings(self, ws, input=None):
        current = {**self.product.get_settings(ws).get('ingestion', {}), 'updatedAt': None}
        settings = {'autoParsingEnabled': current.get('autoParsingEnabled', True), 'concurrency': current.get('concurrency', 4)}
        if input is not None:
            if 'autoParsingEnabled' in input and not isinstance(input['autoParsingEnabled'], bool):
                raise AppError(400, 'VALIDATION_ERROR', 'autoParsingEnabled 必须是布尔值')
            if 'concurrency' in input and input['concurrency'] not in (2, 4, 8):
                raise AppError(400, 'VALIDATION_ERROR', '并发数仅支持 2、4、8')
            settings.update({key: input[key] for key in settings if key in input})
            self.product.update_setting('ingestion', {**current, **settings}, ws)
            if settings['autoParsingEnabled']:
                self.tasks.resume_queued()
        return settings

    def parsing_tasks(self, ws, params):
        kb_id = params.get('knowledgeBaseId') or params.get('kbId')
        dataset_id = params.get('datasetId')
        clauses, args = ['t.workspace_id=?', "t.type='document.parse'"], [ws]
        if kb_id:
            self.knowledge.ensure_kb(kb_id, ws)
            clauses.append('ds.knowledge_base_id=?'); args.append(kb_id)
        if dataset_id:
            self.knowledge.ensure_dataset(dataset_id, ws)
            clauses.append('ds.id=?'); args.append(dataset_id)
        rows = self.db.all('SELECT t.*,d.id documentId,d.title name,ds.id datasetId FROM tasks t JOIN document_revisions dr ON dr.id=t.object_id JOIN documents d ON d.id=dr.document_id JOIN datasets ds ON ds.id=d.dataset_id WHERE ' + ' AND '.join(clauses) + ' ORDER BY t.created_at DESC', *args)
        return [dict(row, progressPercent=row['progress']) for row in rows]

    def control_parsing(self, action, input, ws):
        if input.get('profileId') not in (None, 'profile_default', 'profile_fast_text'):
            raise AppError(422, 'PARSER_PROVIDER_UNAVAILABLE', '当前安装支持原生文字解析，未配置 OCR/VLM 引擎')
        rows = self.parsing_tasks(ws, input)
        changed = []
        if action in ('start', 'resume'):
            self.tasks.parsing_manual.add(ws)
        for task in rows:
            if action in ('start', 'resume') and task['status'] == 'paused':
                self.tasks.resume(task['id'], ws); changed.append(task['id'])
            elif action in ('start', 'resume') and task['status'] == 'queued':
                self.tasks._schedule(task['id']); changed.append(task['id'])
            elif action == 'pause' and task['status'] in ('queued', 'running'):
                self.tasks.pause(task['id'], ws); changed.append(task['id'])
            elif action == 'retry-failed' and task['status'] == 'failed':
                self.tasks.retry(task['id'], ws); changed.append(task['id'])
            elif action == 'clear-pending' and task['status'] == 'queued':
                self.tasks.cancel(task['id'], ws); changed.append(task['id'])
        return {'status': 'paused' if action == 'pause' else 'running', 'changedCount': len(changed), 'taskIds': changed,
                'activeWorkers': sum(task['status'] == 'running' for task in self.parsing_tasks(ws, input)), 'startedAt': now()}

    def pipeline_stats(self, ws, params):
        rows = self.parsing_tasks(ws, params)
        total = len(rows)
        stages = {}
        for key, name, threshold in [('detection', '检测与路由', 25), ('parsing', '解析', 55), ('cleaning', '清理', 95), ('markdownJson', 'Markdown / JSON', 100)]:
            count = sum(row['progress'] >= threshold for row in rows)
            stages[key] = {'name': name, 'completed': count, 'total': total, 'status': 'completed' if count == total else 'processing'}
        return {'totalDocuments': total, 'stages': stages}

    async def document_page(self, document_id, page_num, ws, mode='page'):
        # PDF 渲染与 base64 编码是 CPU/IO 密集操作，放到工作线程避免阻塞事件循环。
        return await asyncio.to_thread(self._document_page, document_id, page_num, ws, mode)

    def _document_page(self, document_id, page_num, ws, mode='page'):
        started = time.monotonic()
        document = self.knowledge.get_document(document_id, ws)
        revision = next(row for row in document['revisions'] if row['id'] == document['current_revision_id'])
        blob = self.knowledge.blob_store.get(revision['blob_id'], ws)
        parsed = json.loads(self.knowledge.artifact_file(document['artifact_id'], 'document', ws)) if document.get('artifact_id') else {'blocks': [], 'metadata': {}, 'warnings': []}
        count = int(parsed.get('metadata', {}).get('pages') or 1)
        pdf = None
        if document['media_type'] == 'application/pdf':
            import pymupdf
            try:
                pdf = pymupdf.open(stream=blob['buffer'], filetype='pdf')
                if pdf.needs_pass:
                    raise AppError(422, 'NEEDS_PASSWORD', 'PDF 已加密')
                count = len(pdf)
            except AppError:
                raise
            except Exception as error:
                raise AppError(422, 'PREVIEW_FAILED', 'PDF 无法读取') from error
        try:
            if mode == 'pages':
                return {'documentId': document_id, 'totalPages': count, 'pages': [{'pageNum': num, 'page': num, 'url': f'/api/v1/documents/{document_id}/pages/{num}', 'width': pdf[num-1].rect.width if pdf else None, 'height': pdf[num-1].rect.height if pdf else None} for num in range(1, count + 1)]}
            number = bounded_int(page_num, 1, 1, count, 'pageNum')
            blocks = [block for block in parsed.get('blocks', []) if int(block.get('locator', {}).get('page') or 1) == number]
            cleaned = '\n\n'.join(block['contentText'] for block in blocks)
            raw = pdf[number-1].get_text('text') if pdf else (blob['buffer'].decode('utf-8', errors='replace') if document['media_type'].startswith('text/') else cleaned)
            warnings = [item for item in parsed.get('warnings', []) if item.get('page', number) == number]
            if mode == 'diff':
                return {'documentId': document_id, 'pageNum': number, 'before': raw, 'after': cleaned, 'originalText': raw, 'cleanedText': cleaned,
                        'diff': [{'type': tag, 'beforeStart': a, 'beforeEnd': b, 'afterStart': c, 'afterEnd': d} for tag, a, b, c, d in difflib.SequenceMatcher(a=raw, b=cleaned).get_opcodes()]}
            if mode == 'inspect':
                return {'documentId': document_id, 'pageNum': number, 'parser': revision.get('parser_id'), 'engine': parsed.get('metadata', {}).get('parser'), 'routeReason': '原生 PDF 文字层' if pdf and raw.strip() else '标准解析产物', 'textCharacters': len(raw), 'qualityStatus': document.get('quality_status'), 'warnings': warnings, 'inspectionMs': round((time.monotonic()-started)*1000)}
            image, boxes, width, height = None, [], None, None
            if pdf:
                page = pdf[number-1]
                scale = min(1.5, 1800 / max(page.rect.width, page.rect.height, 1))
                image = 'data:image/png;base64,' + base64.b64encode(page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes('png')).decode()
                width, height = page.rect.width, page.rect.height
                boxes = [{'bbox': list(block[:4]), 'text': block[4], 'engine': 'pymupdf', 'type': 'text'} for block in page.get_text('blocks') if len(block) > 6 and block[6] == 0]
            return {'documentId': document_id, 'pageNum': number, 'totalPages': count, 'imageUrl': image, 'width': width, 'height': height, 'bboxes': boxes, 'blocks': blocks, 'text': cleaned or raw, 'renderMode': 'image' if image else 'text'}
        finally:
            if pdf:
                pdf.close()

    def resources(self, ws):
        import psutil
        process = psutil.Process()
        memory = psutil.virtual_memory()
        counts = {row['status']: row['n'] for row in self.db.all('SELECT status,COUNT(*) n FROM tasks WHERE workspace_id=? GROUP BY status', ws)}
        completed = self.db.all("SELECT substr(finished_at,1,16) minute,COUNT(*) count FROM tasks WHERE workspace_id=? AND type='document.parse' AND status='succeeded' AND finished_at>=datetime('now','-10 minutes') GROUP BY minute ORDER BY minute", ws)
        return {'checkedAt': now(), 'cpu': {'percent': psutil.cpu_percent(interval=None), 'cores': os.cpu_count()},
                'memory': {'totalBytes': memory.total, 'usedBytes': memory.used, 'percent': memory.percent, 'processBytes': process.memory_info().rss},
                'gpu': {'available': False, 'devices': [], 'reason': '当前解析器使用 CPU；未配置 GPU 监测'},
                'queueLength': counts.get('queued', 0), 'activeTasks': counts.get('running', 0), 'throughput': completed, 'throughputUnit': 'documents/minute'}
