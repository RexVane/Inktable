"""Trace inspection uses recorded evidence; edits are versioned drafts for derived runs."""
import csv
import io
import math
import re

from .core import AppError, bounded_int, gen_id, now, stable_json
from .knowledge import local_embedding, cosine
from .models import build_prompt
from .query import parse_question

STAGES = {'parse': '问题解析', 'embed': '问题向量化', 'route': '检索路由', 'recall': '多路召回',
          'fusion': '结果融合', 'rerank': '重排', 'prompt': '构建提示词', 'generation': '回答生成'}


class TraceWorkbench:
    def __init__(self, db, query, knowledge, product):
        self.db, self.query, self.knowledge, self.product = db, query, knowledge, product

    def draft(self, trace_id, stage, ws):
        row = self.db.one('SELECT * FROM trace_stage_drafts WHERE trace_id=? AND stage=? AND workspace_id=? ORDER BY version DESC LIMIT 1', trace_id, stage, ws)
        return row['config'] if row else {}

    def save(self, trace_id, stage, input, ws, request_id=None, replace=False):
        trace = self.query.get_trace(trace_id, ws)
        config = dict(input) if replace else {**self.draft(trace_id, stage, ws), **input}
        if stage == 'parse':
            filters = config.get('filters') or {}
            release = self.knowledge.get_release(trace['release_id'], ws)
            for key in ('workspaceId', 'releaseId', 'datasetId'):
                expected = trace.get('permission_snapshot', {}).get(key) or (ws if key == 'workspaceId' else trace['release_id'] if key == 'releaseId' else release['dataset_id'])
                if key in filters and expected and filters[key] != expected:
                    raise AppError(400, 'SCOPE_MISMATCH', '解析配置不能扩大 Trace 的权限范围')
        if stage == 'route':
            for channel in config.get('routes', []):
                if channel.get('enabled') and channel.get('name') not in ('vector', 'full_text', 'fullText'):
                    raise AppError(422, 'RETRIEVAL_PROVIDER_UNAVAILABLE', '该通道未配置问答检索 Provider')
        if stage == 'fusion':
            for key in ('denseWeight', 'sparseWeight', 'vectorWeight', 'fullTextWeight'):
                if key in config and (not isinstance(config[key], (int, float)) or not math.isfinite(config[key]) or config[key] < 0):
                    raise AppError(400, 'VALIDATION_ERROR', '融合权重必须为非负有限数')
            if 'k' in config:
                config['k'] = bounded_int(config['k'], 60, 1, 1000, 'k')
        if stage == 'rerank':
            if config.get('provider', config.get('model', 'local-lexical-v1')) not in ('local-lexical-v1', 'ordo-local-lexical-v1'):
                raise AppError(422, 'RERANK_PROVIDER_UNAVAILABLE', '当前仅配置本地词项重排 Provider')
            for key in ('topK', 'topN'):
                if key in config:
                    config[key] = bounded_int(config[key], 6, 1, 50, key)
            if 'threshold' in config and (not isinstance(config['threshold'], (int, float)) or not 0 <= config['threshold'] <= 1):
                raise AppError(400, 'VALIDATION_ERROR', '重排阈值必须在 0 到 1 之间')
        def persist():
            version = self.db.one('SELECT COALESCE(MAX(version),0)+1 n FROM trace_stage_drafts WHERE trace_id=? AND stage=?', trace_id, stage)['n']
            self.db.run('INSERT INTO trace_stage_drafts(id,workspace_id,trace_id,stage,version,config_json,created_at) VALUES(?,?,?,?,?,?,?)', gen_id('draft'), ws, trace_id, stage, version, stable_json(config), now())
            return version
        version = self.db.transaction(persist)
        self.product.audit.append(workspace_id=ws, action='trace.draft.update', object_type='trace', object_id=trace_id, request_id=request_id, details={'stage': stage, 'version': version})
        return {'traceId': trace_id, 'stage': stage, 'version': version, 'config': config, 'saved': True, 'applied': False}

    def pipeline(self, trace_id, ws):
        trace = self.query.get_trace(trace_id, ws)
        stages, offset = [], 0
        for index, (key, name) in enumerate(STAGES.items(), 1):
            recorded = next((item for item in (trace.get('stages') or []) if item.get('name') == name or item.get('key') == key), {})
            duration = recorded.get('durationMs')
            end = offset + duration if offset is not None and isinstance(duration, (int, float)) else None
            stages.append({**recorded, 'id': index, 'key': key, 'name': name, 'status': recorded.get('status', 'unavailable'), 'durationMs': duration, 'startMs': offset if end is not None else None, 'endMs': end})
            offset = end
        return {'traceId': trace_id, 'query': trace['query'], 'status': trace['status'], 'stages': stages,
                'metrics': trace.get('metrics') or {}, 'totalMs': (trace.get('metrics') or {}).get('totalMs', offset), 'releaseId': trace['release_id']}

    def stage(self, trace_id, key, ws):
        trace = self.query.get_trace(trace_id, ws)
        recorded = next((item for item in (trace.get('stages') or []) if item.get('name') == STAGES[key] or item.get('key') == key), {})
        output = recorded.get('output') if isinstance(recorded.get('output'), dict) else {}
        completed = recorded.get('status') in ('succeeded', 'degraded')
        result = {**output, 'traceId': trace_id, 'query': trace['query'], 'releaseId': trace['release_id'],
                  'status': recorded.get('status', 'unavailable'), 'durationMs': recorded.get('durationMs'),
                  'dataSource': 'recorded' if output else 'unavailable', 'reconstructed': False,
                  'output': output, 'draft': self.draft(trace_id, key, ws), 'pipeline': self.pipeline(trace_id, ws)['stages']}
        if key == 'parse':
            history = (trace.get('input_snapshot') or {}).get('history', output.get('inputHistory'))
            history_recorded = isinstance(history, list)
            history = history if history_recorded else []
            normalized = output.get('normalizedQuery', output.get('normalized'))
            result.update(originalQuery=output.get('original', trace['query']), normalizedQuery=normalized,
                          rewrittenQuery=output.get('rewrittenQuery', output.get('rewritten', normalized)),
                          keywords=output.get('keywords', output.get('entities', [])), inputHistory=history,
                          contextMessages=[*history, {'role': 'user', 'content': trace['query']}],
                          contextSource='recorded' if history_recorded else 'unavailable')
        elif key == 'embed':
            vector = output.get('vector')
            if vector is None and 'vector' not in output and completed and output.get('model') == 'ordo-hash-embedding-v1':
                vector = local_embedding(trace['query'])
                result.update(reconstructed=True, dataSource='reconstructed', reconstructionReason='legacy_vector_missing')
            vector = vector if isinstance(vector, list) else []
            result.update(vector=vector, norm=math.sqrt(sum(item * item for item in vector)) if vector else None,
                          dimensions=len(vector) if vector else output.get('dimensions'),
                          cacheHit=output.get('cacheHit'), cacheStatus=output.get('cacheStatus', 'unrecorded'))
        elif key == 'route':
            result.update(channels=output.get('routes', []), routingBasis={
                'reason': output.get('reason'), 'strategy': output.get('retrievalStrategy'),
                'permissionScope': output.get('permissionScope')})
        elif key in ('recall', 'fusion', 'rerank'):
            candidates = output.get('candidates', output.get('fusion'))
            if candidates is None:
                candidates = [dict(row, selected=True) for row in output.get('selected', [])] + [dict(row, selected=False) for row in output.get('rejected', [])]
            result['candidates'] = [self.candidate(row, fusion_score=key != 'rerank' or row.get('selected') is False) for row in candidates]
            result['totalCandidates'] = len(candidates)
            result['summary'] = {'candidateCount': len(candidates), 'vectorCount': len(output.get('vector', [])), 'fullTextCount': len(output.get('fullText', [])), 'selectedCount': len(output.get('selected', []))}
            if key == 'recall':
                result['channels'] = [{'id': channel, 'name': name, 'enabled': (output.get('routes') or {}).get(channel), 'count': len(output.get(channel, [])), 'candidates': [self.candidate(item, fusion_score=False) for item in output.get(channel, [])]} for channel, name in [('vector', '向量检索'), ('fullText', '全文检索')]]
        elif key == 'prompt':
            result['messages'] = output.get('messages') or []
            history = (trace.get('input_snapshot') or {}).get('history')
            rerank = next((item for item in (trace.get('stages') or []) if item.get('name') == STAGES['rerank'] or item.get('key') == 'rerank'), {})
            selected = (rerank.get('output') or {}).get('selected')
            if ('messages' not in output and completed and output.get('templateVersion') == 'strict-evidence-v1'
                    and isinstance(history, list) and isinstance(selected, list) and rerank.get('status') in ('succeeded', 'degraded')):
                config = ((trace.get('config_snapshot') or {}).get('stageOverrides') or {}).get('prompt') or {}
                result['messages'] = build_prompt(trace['query'], selected, bool(output.get('strictEvidence', True)), history, config)
                result.update(reconstructed=True, dataSource='reconstructed', reconstructionReason='legacy_prompt_messages_missing')
            result['prompt'] = '\n\n'.join(item['content'] for item in result.get('messages', []))
        elif key == 'generation':
            message = self.db.one("SELECT * FROM messages WHERE id=? AND workspace_id=? AND role='assistant'", trace.get('message_id'), ws)
            result.update(answer=output.get('answer', message['content'] if message else ''), message=message,
                          answerSource='recorded_stage' if 'answer' in output else 'recorded_message' if message else 'unavailable',
                          citations=trace['citations'], evidenceStatus=trace['evidence_status'], usage=output.get('usage'),
                          usageStatus=output.get('usageStatus', 'recorded' if output.get('usage') is not None else 'unrecorded'))
        return result

    @staticmethod
    def candidate(item, fusion_score=True):
        return {**item, 'id': item.get('chunkRevisionId', item.get('id')), 'chunkId': item.get('chunkRevisionId', item.get('id')),
                'documentTitle': item.get('title', ''), 'contentText': item.get('content', ''), 'fusionScore': item.get('fusionScore', item.get('score') if fusion_score else None),
                'channels': [channel for channel, field in [('vector', 'vectorRank'), ('fullText', 'fullTextRank')] if item.get(field) is not None]}

    def candidates(self, trace_id, stage, params, ws):
        items = self.stage(trace_id, stage, ws)['candidates']
        query, channel = params.get('query') or params.get('q'), params.get('channel')
        if query:
            items = [item for item in items if query.lower() in (item['contentText'] + item['documentTitle']).lower()]
        if channel:
            channel = 'fullText' if channel == 'full_text' else channel
            items = [item for item in items if channel in item['channels']]
        if params.get('minScore') is not None:
            try:
                threshold = float(params['minScore'])
            except (ValueError, TypeError):
                raise AppError(400, 'VALIDATION_ERROR', 'minScore 无效')
            if not math.isfinite(threshold):
                raise AppError(400, 'VALIDATION_ERROR', 'minScore 无效')
            items = [item for item in items if (item.get('score') or 0) >= threshold]
        limit = bounded_int(params.get('limit'), 100, 1, 500, 'limit')
        offset = bounded_int(params.get('offset'), 0, 0, 100000, 'offset')
        return {'items': items[offset:offset + limit], 'total': len(items), 'limit': limit, 'offset': offset}

    def chunk(self, trace_id, stage, candidate_id, ws):
        item = next((item for item in self.stage(trace_id, stage, ws)['candidates'] if item['id'] == candidate_id), None)
        if not item:
            raise AppError(404, 'NOT_FOUND', '该知识块不在此次 Trace 的候选集合中')
        return item

    def calculation(self, trace_id, candidate_id, ws):
        stage = self.stage(trace_id, 'fusion', ws)
        item = self.chunk(trace_id, 'fusion', candidate_id, ws)
        k = stage.get('k', 60)
        weights = stage.get('weights') or {'denseWeight': 1, 'sparseWeight': 1}
        contributions = [{'channel': channel, 'rank': item.get(field), 'weight': weight,
                          'contribution': weight / (k + item[field]) if item.get(field) else 0}
                         for channel, field, weight in [('vector', 'vectorRank', weights['denseWeight']), ('fullText', 'fullTextRank', weights['sparseWeight'])]]
        return {'traceId': trace_id, 'candidateId': candidate_id, 'method': 'rrf', 'k': k, 'contributions': contributions, 'score': sum(row['contribution'] for row in contributions)}

    def logs(self, trace_id, stage, ws):
        current = self.stage(trace_id, stage, ws)
        trace = self.query.get_trace(trace_id, ws)
        rows = [{'timestamp': trace['created_at'], 'level': 'info', 'stage': stage,
                 'message': '已记录阶段输出' if current['output'] else '未记录阶段输出',
                 'status': current['status'], 'durationMs': current['durationMs'], 'dataSource': current['dataSource']}]
        rows.extend({'timestamp': row['created_at'], 'level': 'info', 'stage': stage, 'message': '已保存配置草稿', 'version': row['version']} for row in self.db.all('SELECT version,created_at FROM trace_stage_drafts WHERE trace_id=? AND workspace_id=? AND stage=? ORDER BY version', trace_id, ws, stage))
        return rows

    async def rerun(self, trace_id, stage, input, ws, request_id=None, on_event=None):
        trace = self.query.get_trace(trace_id, ws)
        source_config = (trace.get('config_snapshot') or {}).get('stageOverrides') or {}
        drafts = {key: {**(source_config.get(key) or {}), **self.draft(trace_id, key, ws)} for key in STAGES}
        overrides = dict(input.get('overrides') or {})
        for key, config in (overrides.get('stageOverrides') or {}).items():
            drafts[key] = {**(drafts.get(key) or {}), **config}
        for key in ('question', 'topK', 'modelConnectionId', 'strictEvidence'):
            if key in input:
                overrides[key] = input[key]
        parse = drafts['parse']
        overrides.setdefault('question', overrides.get('query') or parse.get('rewrittenQuery') or parse.get('rewritten') or parse.get('normalizedQuery') or parse.get('normalized') or parse.get('query') or trace['query'])
        overrides['stageOverrides'] = drafts
        result = await self.query.replay_trace(trace_id, {'fromStage': stage, 'overrides': overrides}, ws, request_id, input.get('idempotencyKey'), on_event=on_event)
        return dict(result, derivedTraceId=result['trace']['id'], executionMode='full_pipeline', requestedFromStage=stage)

    def parse_action(self, trace_id, action, input, ws, request_id=None):
        trace = self.query.get_trace(trace_id, ws)
        release = self.knowledge.get_release(trace['release_id'], ws)
        conversation = {'workspace_id': ws, 'dataset_id': release['dataset_id'], 'release_id': trace['release_id'],
                        'strict_evidence': (trace.get('config_snapshot') or {}).get('strictEvidence', True)}
        parsed = parse_question(input.get('question') or input.get('query') or trace['query'], conversation)
        if action == 'classifyTraceIntent':
            return {'traceId': trace_id, 'intent': parsed['intent'], 'provider': 'local-rules-v1'}
        if action == 'extractTraceEntities':
            return {'traceId': trace_id, 'entities': parsed['entities'], 'provider': 'local-rules-v1'}
        if action == 'rewriteTraceQuery':
            return {'traceId': trace_id, 'rewrittenQuery': parsed['normalized'], 'provider': 'local-normalization-v1'}
        return self.save(trace_id, 'parse', input, ws, request_id, replace=action == 'updateTraceRawJson')

    def embedding_action(self, trace_id, action, input, ws):
        stage = self.stage(trace_id, 'embed', ws)
        if action == 'getEmbeddingVector':
            return {key: stage.get(key) for key in ('traceId', 'model', 'dimensions', 'vector', 'norm', 'status', 'dataSource', 'reconstructed', 'cacheHit', 'cacheStatus')}
        if action == 'getEmbeddingScatter':
            # A labeled deterministic projection, never a fabricated t-SNE distribution.
            if not stage['vector'] or stage.get('model') != 'ordo-hash-embedding-v1':
                return {'method': 'fixed-linear-projection', 'points': [], 'dataSource': 'unavailable', 'reconstructed': False}
            items = self.stage(trace_id, 'recall', ws)['candidates']
            points = []
            axis_x, axis_y = local_embedding('projection axis x'), local_embedding('projection axis y')
            for item in [{'id': trace_id, 'contentText': stage['query'], 'documentTitle': 'query'}] + items:
                vector = local_embedding(item['contentText'])
                points.append({'id': item['id'], 'label': item['documentTitle'], 'x': cosine(vector, axis_x), 'y': cosine(vector, axis_y)})
            return {'method': 'fixed-linear-projection', 'points': points, 'dataSource': 'reconstructed', 'reconstructed': True}
        models = input.get('models') or input.get('modelIds') or [input.get('model') or 'ordo-hash-embedding-v1']
        if any(model not in ('local-hash-v1', 'ordo-hash-embedding-v1') for model in models):
            raise AppError(422, 'EMBEDDING_PROVIDER_UNAVAILABLE', '当前仅配置本地哈希向量模型')
        vector = local_embedding(input.get('query') or stage['query'])
        return {'traceId': trace_id, 'model': models[0], 'vector': vector, 'dimensions': len(vector),
                'norm': math.sqrt(sum(item * item for item in vector)),
                'results': [{'model': model, 'vector': vector, 'dimensions': len(vector)} for model in models], 'persisted': False}

    def sensitive_scan(self, trace_id, ws):
        text = self.stage(trace_id, 'prompt', ws)['prompt']
        matches = []
        for kind, pattern in [('email', r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}'), ('phone', r'(?<!\d)1[3-9]\d{9}(?!\d)'), ('credential', r'(?i)(?:api[_-]?key|password|token)\s*[:=]\s*\S+')]:
            matches.extend({'type': kind, 'start': match.start(), 'end': match.end(), 'replacement': '[' + kind.upper() + ']'} for match in re.finditer(pattern, text))
        return {'traceId': trace_id, 'matches': matches, 'count': len(matches), 'method': 'local-patterns-v1'}

    def mask_prompt(self, trace_id, ws, request_id):
        scan = self.sensitive_scan(trace_id, ws)
        messages = self.stage(trace_id, 'prompt', ws).get('messages', [])
        masked = []
        for message in messages:
            text = message['content']
            text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)
            text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[PHONE]', text)
            text = re.sub(r'(?i)(?:api[_-]?key|password|token)\s*[:=]\s*\S+', '[CREDENTIAL]', text)
            masked.append(dict(message, content=text))
        result = self.save(trace_id, 'prompt', {'maskSensitive': True}, ws, request_id)
        return dict(result, messages=masked, maskedCount=scan['count'])

    def export(self, trace_id, stage, format, ws):
        items = self.stage(trace_id, stage, ws)['candidates']
        if format == 'csv':
            stream = io.StringIO(newline='')
            writer = csv.DictWriter(stream, fieldnames=['id', 'documentTitle', 'contentText', 'rank', 'fusionScore'], extrasaction='ignore')
            writer.writeheader()
            writer.writerows(items)
            return stream.getvalue(), 'text/csv; charset=utf-8'
        return stable_json({'traceId': trace_id, 'stage': stage, 'candidates': items}), 'application/json'
