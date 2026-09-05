"""Editable chunk revisions and rebuildable index projections."""
import asyncio
import difflib
import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .core import AppError, gen_id, now, required, stable_json, bounded_int


class KnowledgeWorkbench:
    async def _parse_isolated(self, content, filename):
        with tempfile.TemporaryDirectory(prefix='parse-', dir=self.config['taskRoot']) as temp:
            source, output = Path(temp) / 'input', Path(temp) / 'result.json'
            source.write_bytes(content)
            command = [sys.executable, '-m', 'ordo.parser_worker', str(source), filename, str(output), str(self.config['maxParserOutputBytes'])]
            try:
                result = await asyncio.to_thread(subprocess.run, command, cwd=self.config['projectRoot'] / 'serverpy',
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                                 timeout=self.config['parserTimeoutMs'] / 1000, creationflags=0x08000000 if sys.platform == 'win32' else 0)
            except subprocess.TimeoutExpired as error:
                raise AppError(408, 'PARSER_TIMEOUT', '文档解析超过时间预算') from error
            if result.returncode or not output.exists():
                raise AppError(422, 'PARSE_FAILED', '解析进程未能完成文档处理')
            if output.stat().st_size > self.config['maxParserOutputBytes']:
                raise AppError(413, 'PARSER_OUTPUT_TOO_LARGE', '解析产物超过大小预算')
            envelope = json.loads(output.read_text('utf-8'))
            if 'error' in envelope:
                error = envelope['error']
                raise AppError(error['status'], error['code'], error['message'])
            return envelope['data']

    def _new_chunk_revision(self, current, input, ws):
        from .knowledge import estimate_tokens, local_embedding
        latest = self.db.one('SELECT id,revision_number FROM chunk_revisions WHERE chunk_logical_id=? ORDER BY revision_number DESC LIMIT 1', current['chunk_logical_id'])
        if current.get('id') and (not latest or latest['id'] != current['id']):
            raise AppError(409, 'CHUNK_REVISION_CONFLICT', '知识块已更新，请重新载入最新版本', {'currentRevisionId': latest['id'] if latest else None})
        md = required(input.get('contentMd', input.get('contentText', current['content_md'])), 'contentMd')
        text = required(input.get('contentText', md), 'contentText')
        chunk_id = gen_id('cr')
        values = [chunk_id, ws, current['chunk_logical_id'], current['dataset_id'], current['document_id'],
                  current['document_revision_id'], current['artifact_id'], (latest['revision_number'] if latest else 0) + 1,
                  input.get('parentChunkId', current.get('parent_chunk_id')), input.get('type', current['type']), md, text,
                  stable_json(input.get('sourceLocator', current.get('source_locator') or {})), estimate_tokens(text), current['language'],
                  input.get('generatedBy', 'human'), current['confidence'], stable_json(current.get('warnings') or []),
                  input.get('sensitivity', current['sensitivity']), int(bool(input.get('excluded', current['excluded']))),
                  current.get('id'), stable_json(local_embedding(text)), 'ordo-hash-embedding-v1', now()]
        self.db.run('INSERT INTO chunk_revisions(id,workspace_id,chunk_logical_id,dataset_id,document_id,document_revision_id,artifact_id,revision_number,parent_chunk_id,type,content_md,content_text,source_locator_json,token_count,language,generated_by,confidence,warnings_json,sensitivity,excluded,supersedes_id,embedding_json,embedding_model,created_at) VALUES(' + ','.join('?' for _ in values) + ')', *values)
        self.db.run('INSERT INTO chunks_fts(chunk_revision_id,workspace_id,dataset_id,title,breadcrumb,content) VALUES(?,?,?,?,?,?)', chunk_id, ws, current['dataset_id'], current['document_title'], current['document_title'] + ' / ' + input.get('type', current['type']), text)
        return self.get_chunk(chunk_id, ws)

    def edit_chunk(self, chunk_id, input, workspace_id, request_id=None):
        current = self.get_chunk(chunk_id, workspace_id)
        result = self.db.transaction(lambda: self._new_chunk_revision(current, input, workspace_id))
        self.audit.append(workspace_id=workspace_id, action='chunk.revise', object_type='chunk_revision', object_id=result['id'], request_id=request_id, details={'supersedes': chunk_id})
        return result

    def toggle_chunk_disabled(self, chunk_id, input, workspace_id, request_id=None):
        chunk = self.get_chunk(chunk_id, workspace_id)
        return self.edit_chunk(chunk_id, {'excluded': input.get('disabled', input.get('excluded', not chunk['excluded']))}, workspace_id, request_id)

    def vectorize_chunk(self, chunk_id, workspace_id, request_id=None):
        result = self.edit_chunk(chunk_id, {'generatedBy': 'vector-recompute'}, workspace_id, request_id)
        return dict(result, status='success', dimensions=len(result.get('embedding') or []))

    def restore_chunk(self, chunk_id, input, workspace_id, request_id=None):
        current = self.get_chunk(chunk_id, workspace_id)
        revision_id = input.get('revisionId') or input.get('targetRevisionId')
        if not revision_id and input.get('revisionNumber'):
            revision_id = next((row['id'] for row in current['history'] if row['revision_number'] == int(input['revisionNumber'])), None)
        target = self.get_chunk(required(revision_id, 'revisionId'), workspace_id)
        if target['chunk_logical_id'] != current['chunk_logical_id']:
            raise AppError(400, 'SCOPE_MISMATCH', '只能恢复同一逻辑块的历史修订')
        return self.edit_chunk(chunk_id, {'contentMd': target['content_md'], 'contentText': target['content_text'], 'type': target['type'], 'excluded': target['excluded'], 'sensitivity': target['sensitivity'], 'sourceLocator': target['source_locator']}, workspace_id, request_id)

    def diff_chunk(self, chunk_id, input, workspace_id):
        chunk = self.get_chunk(chunk_id, workspace_id)
        target_id = input.get('otherId') or input.get('revisionId') or input.get('compareTo') or chunk.get('supersedes_id')
        other = self.get_chunk(target_id, workspace_id) if target_id else None
        if other and other['chunk_logical_id'] != chunk['chunk_logical_id']:
            raise AppError(400, 'SCOPE_MISMATCH', '只能比较同一逻辑块')
        before, after = (other or {}).get('content_md', ''), chunk['content_md']
        return {'chunkId': chunk_id, 'previousId': target_id, 'before': before, 'after': after,
                'diff': '\n'.join(difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile=target_id or 'empty', tofile=chunk_id, lineterm=''))}

    def split_chunk(self, chunk_id, input, workspace_id, request_id=None):
        current = self.get_chunk(chunk_id, workspace_id)
        parts = input.get('parts') or input.get('segments')
        if parts is None:
            position = bounded_int(input.get('splitAt', input.get('position')), len(current['content_text']) // 2, 1, max(1, len(current['content_text']) - 1), 'splitAt')
            parts = [current['content_text'][:position], current['content_text'][position:]]
        if not isinstance(parts, list) or not 2 <= len(parts) <= 50:
            raise AppError(400, 'VALIDATION_ERROR', 'parts 必须包含 2 到 50 个子块')
        def persist():
            excluded = self._new_chunk_revision(current, {'excluded': True}, workspace_id)
            children = []
            for part in parts:
                logical = gen_id('chunk')
                self.db.run('INSERT INTO chunk_logicals(id,workspace_id,dataset_id,document_id,created_at) VALUES(?,?,?,?,?)', logical, workspace_id, current['dataset_id'], current['document_id'], now())
                payload = part if isinstance(part, dict) else {'contentText': part}
                children.append(self._new_chunk_revision(dict(current, id=None, chunk_logical_id=logical), dict(payload, excluded=False, parentChunkId=chunk_id), workspace_id))
            return {'parent': excluded, 'children': children, 'chunks': children}
        result = self.db.transaction(persist)
        self.audit.append(workspace_id=workspace_id, action='chunk.split', object_type='chunk_revision', object_id=chunk_id, request_id=request_id)
        return result

    def merge_chunks(self, input, workspace_id, request_id=None):
        ids = input.get('chunkIds') or input.get('ids') or []
        if not isinstance(ids, list) or not 2 <= len(set(ids)) == len(ids) <= 50:
            raise AppError(400, 'VALIDATION_ERROR', '合并需要 2 到 50 个不同知识块')
        chunks = [self.get_chunk(chunk_id, workspace_id) for chunk_id in ids]
        if len({item['document_revision_id'] for item in chunks}) != 1:
            raise AppError(400, 'SCOPE_MISMATCH', '只能合并同一文档修订的知识块')
        def persist():
            first, logical = chunks[0], gen_id('chunk')
            self.db.run('INSERT INTO chunk_logicals(id,workspace_id,dataset_id,document_id,created_at) VALUES(?,?,?,?,?)', logical, workspace_id, first['dataset_id'], first['document_id'], now())
            merged = self._new_chunk_revision(dict(first, id=None, chunk_logical_id=logical), {'contentMd': input.get('contentMd') or '\n\n'.join(item['content_md'] for item in chunks), 'excluded': False, 'parentChunkId': first['id']}, workspace_id)
            return {'merged': merged, 'excluded': [self._new_chunk_revision(item, {'excluded': True}, workspace_id) for item in chunks]}
        result = self.db.transaction(persist)
        self.audit.append(workspace_id=workspace_id, action='chunk.merge', object_type='chunk_revision', object_id=result['merged']['id'], request_id=request_id)
        return result

    def _current_chunks(self, dataset_id, ws):
        self.ensure_dataset(dataset_id, ws)
        return self.db.all("SELECT cr.*,d.title document_title FROM chunk_revisions cr JOIN documents d ON d.id=cr.document_id WHERE cr.dataset_id=? AND cr.workspace_id=? AND d.status!='deleted' AND cr.revision_number=(SELECT MAX(revision_number) FROM chunk_revisions WHERE chunk_logical_id=cr.chunk_logical_id)", dataset_id, ws)

    def _projection(self, dataset_id, kind, ws, value=None):
        if value is not None:
            self.db.run('INSERT INTO index_projections(workspace_id,dataset_id,kind,content_json,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(dataset_id,kind) DO UPDATE SET content_json=excluded.content_json,updated_at=excluded.updated_at', ws, dataset_id, kind, stable_json(value), now())
        row = self.db.one('SELECT * FROM index_projections WHERE dataset_id=? AND workspace_id=? AND kind=?', dataset_id, ws, kind)
        return row['content'] if row else None

    def get_indexing_stats(self, dataset_id, workspace_id):
        chunks = self._current_chunks(dataset_id, workspace_id)
        vectorized = sum(bool(item.get('embedding')) for item in chunks)
        active = self.db.one("SELECT id,version,status,created_at FROM knowledge_releases WHERE dataset_id=? AND workspace_id=? AND status='active'", dataset_id, workspace_id)
        return {'totalChunks': len(chunks), 'vectorizedChunks': vectorized, 'pendingChunks': len(chunks) - vectorized,
                'disabledChunks': sum(bool(item['excluded']) for item in chunks),
                'activeRelease': {'id': active['id'] if active else None, 'version': 'v' + str(active['version'] if active else 0), 'status': active['status'] if active else 'none', 'createdAt': active['created_at'] if active else None}}

    def get_indexing_pipeline(self, dataset_id, workspace_id):
        stats = self.get_indexing_stats(dataset_id, workspace_id)
        graph = self._projection(dataset_id, 'hnsw', workspace_id)
        weights = self._projection(dataset_id, 'hybrid', workspace_id) or {'denseWeight': .5, 'sparseWeight': .5}
        return {'currentStep': 2 if stats['pendingChunks'] else 4, 'steps': [
            {'step': 1, 'name': '切块治理', 'status': 'completed', 'progress': 100, 'completedCount': stats['totalChunks'], 'totalCount': stats['totalChunks']},
            {'step': 2, 'name': '向量化计算', 'status': 'processing' if stats['pendingChunks'] else 'completed', 'progress': 100 * stats['vectorizedChunks'] / max(stats['totalChunks'], 1), 'completedCount': stats['vectorizedChunks'], 'totalCount': stats['totalChunks']},
            {'step': 3, 'name': '向量索引 (HNSW)', 'status': 'ready' if graph else 'pending', 'progress': 100 if graph else 0, 'algorithm': 'HNSW', 'queryProvider': 'exact-cosine'},
            {'step': 4, 'name': '全文索引 (BM25)', 'status': 'ready', 'progress': 100, 'rrfK': 60, **weights}]}

    def get_chapters(self, dataset_id, workspace_id):
        documents = {}
        for chunk in self._current_chunks(dataset_id, workspace_id):
            document = documents.setdefault(chunk['document_id'], {'documentId': chunk['document_id'], 'title': chunk['document_title'], 'chapters': []})
            document['chapters'].append({'id': chunk['id'], 'title': chunk['content_text'].split('\n')[0][:120], 'type': chunk['type'], 'locator': chunk['source_locator']})
        return list(documents.values())

    def get_chunk_lineage(self, chunk_id, workspace_id):
        chunk = self.get_chunk(chunk_id, workspace_id)
        stats = self.get_indexing_stats(chunk['dataset_id'], workspace_id)
        dimensions = len(chunk.get('embedding') or [])
        return {'node1_chunk': {'id': chunk_id, 'logicalId': chunk['chunk_logical_id'], 'documentTitle': chunk['document_title'], 'status': 'synced', 'label': '数据块'},
                'node2_vector': {'id': chunk_id, 'tokenCount': chunk['token_count'], 'dimensions': dimensions, 'status': 'synced' if dimensions else 'pending', 'statusText': f'{dimensions} 维', 'label': '向量记录'},
                'node3_collection': {'name': chunk['dataset_id'], 'vectorCount': stats['vectorizedChunks'], 'status': 'active', 'label': '集合 (Collection)'},
                'node4_index': {'name': 'exact-cosine', 'type': 'exact-cosine', 'status': 'ready', 'label': '向量索引'}}

    def batch_vectorize_pending(self, dataset_id, workspace_id, request_id=None):
        chunks = [item for item in self._current_chunks(dataset_id, workspace_id) if not item.get('embedding')]
        result = self.db.transaction(lambda: [self.vectorize_chunk(item['id'], workspace_id, request_id)['id'] for item in chunks])
        return {'status': 'success', 'vectorizedCount': len(result), 'chunkIds': result, 'progress': 100}

    def rebuild_hnsw_index(self, dataset_id, workspace_id, request_id=None):
        from .vector_index import build
        start = time.monotonic()
        chunks = [item for item in self._current_chunks(dataset_id, workspace_id) if not item['excluded']]
        graph = build(chunks)
        self._projection(dataset_id, 'hnsw', workspace_id, graph)
        return {'status': 'success', 'algorithm': 'HNSW', 'totalNodes': len(chunks), 'latencyMs': round((time.monotonic() - start) * 1000), 'maxLevel': graph['maxLevel'], 'message': 'HNSW 图投影已重建；发布版本默认使用精确余弦检索'}

    def optimize_vector_index(self, dataset_id, workspace_id, request_id=None):
        self.ensure_dataset(dataset_id, workspace_id)
        before = self.config['dbPath'].stat().st_size
        self.db.run("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        self.db.run('PRAGMA optimize')
        self.db.run('PRAGMA wal_checkpoint(PASSIVE)')
        freed = max(0, before - self.config['dbPath'].stat().st_size)
        return {'status': 'success', 'freedBytes': freed, 'freedMb': round(freed / 1048576, 2), 'message': '完成 FTS 合并和 SQLite 查询规划优化'}

    def rebuild_bm25_index(self, dataset_id, workspace_id, request_id=None):
        self.ensure_dataset(dataset_id, workspace_id)
        # Include historical revisions: immutable releases still reference them.
        chunks = self.db.all('SELECT cr.*,d.title document_title FROM chunk_revisions cr JOIN documents d ON d.id=cr.document_id WHERE cr.dataset_id=? AND cr.workspace_id=?', dataset_id, workspace_id)
        def persist():
            self.db.run('DELETE FROM chunks_fts WHERE dataset_id=? AND workspace_id=?', dataset_id, workspace_id)
            for item in chunks:
                self.db.run('INSERT INTO chunks_fts(chunk_revision_id,workspace_id,dataset_id,title,breadcrumb,content) VALUES(?,?,?,?,?,?)', item['id'], workspace_id, dataset_id, item['document_title'], item['type'], item['content_text'])
        self.db.transaction(persist)
        return {'status': 'success', 'indexedChunks': len(chunks), 'provider': 'sqlite-fts5', 'message': '全文索引已重建'}

    def set_hybrid_weights(self, dataset_id, input, workspace_id, request_id=None):
        self.ensure_dataset(dataset_id, workspace_id)
        try:
            dense, sparse = float(input.get('denseWeight', .5)), float(input.get('sparseWeight', .5))
            if not all(math.isfinite(item) and item >= 0 for item in (dense, sparse)) or dense + sparse <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise AppError(400, 'VALIDATION_ERROR', '检索权重必须为非负有限数且总和大于零')
        weights = {'denseWeight': dense / (dense + sparse), 'sparseWeight': sparse / (dense + sparse)}
        self._projection(dataset_id, 'hybrid', workspace_id, weights)
        self.audit.append(workspace_id=workspace_id, action='index.hybrid_weights', object_type='dataset', object_id=dataset_id, request_id=request_id, details=weights)
        return dict(weights, status='success')
