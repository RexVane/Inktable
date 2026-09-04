import re
import time

from .core import AppError, gen_id, hash_bytes, now, parse_json, required, stable_json
from .models import format_locator, local_evidence_answer

_EVIDENCE_REFUSAL_RE = re.compile(r'(无法回答|不能回答|未找到|没有找到|证据不足|无法确认|无法确定|不知道)')
_REPLAY_SUPPORTED = {'问题解析', 'question_parse', 'parse', 'start', 'all'}


def parse_question(question, conversation):
    normalized = re.sub(r'\s+', ' ', str(question).strip())
    entities = list(dict.fromkeys(re.findall(r'[A-Za-z]+[-_.]?[A-Za-z0-9]*|[\u4e00-\u9fff]{2,8}', normalized) or []))[:12]
    needs_clarification = len(normalized) < 2
    return {
        'original': question, 'normalized': normalized,
        'language': 'zh-CN' if re.search(r'[\u4e00-\u9fff]', normalized) else 'und',
        'intent': 'procedure' if re.search(r'怎么|如何|步骤|安装|配置', normalized)
        else 'fact' if re.search(r'多少|几|统计', normalized) else 'knowledge_query',
        'entities': entities,
        'filters': {'workspaceId': conversation['workspace_id'], 'datasetId': conversation['dataset_id'],
                    'releaseId': conversation['release_id']},
        'needsClarification': needs_clarification,
        'policy': 'strict_evidence' if conversation.get('strict_evidence') else 'evidence_preferred',
    }


def route_query(question):
    exact = bool(re.search(r'[A-Z]{2,}[-_]?\d+|\b\d{3,}\b', question))
    table = bool(re.search(r'表格|字段|列|行|统计|数量', question))
    return {
        'routes': [
            {'name': 'full_text', 'enabled': True, 'weight': 1.4 if exact else 1},
            {'name': 'vector', 'enabled': True, 'weight': 0.9 if table else 1},
            {'name': 'graph', 'enabled': False, 'reason': 'R2 图谱检索未启用'},
            {'name': 'database', 'enabled': False, 'reason': 'R2 受控数据库模板未启用'},
        ],
        'reason': '包含精确型号或编号，提高全文权重' if exact
        else '表格问题保留结构化块优先级' if table else '使用向量与全文混合检索',
    }


def _is_evidence_refusal(content):
    return bool(_EVIDENCE_REFUSAL_RE.search(str(content or '')))


class QueryService:
    def __init__(self, db, knowledge, models, audit, config):
        self.db = db
        self.knowledge = knowledge
        self.models = models
        self.audit = audit
        self.config = config

    def create_conversation(self, raw_input, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        input = dict(raw_input or {})
        if not input.get('knowledgeBaseId') and input.get('knowledge_base_id'):
            input['knowledgeBaseId'] = input['knowledge_base_id']
        kb = self.knowledge.ensure_kb(required(input.get('knowledgeBaseId'), 'knowledgeBaseId'), workspace_id)
        dataset = self.knowledge.ensure_dataset(input.get('datasetId') or kb.get('default_dataset_id'), workspace_id)
        if dataset['knowledge_base_id'] != kb['id']:
            raise AppError(400, 'SCOPE_MISMATCH', '数据集不属于所选知识库')
        release_id = input.get('releaseId') or dataset.get('active_release_id')
        if not release_id:
            raise AppError(409, 'ACTIVE_RELEASE_REQUIRED', '知识库还没有活动知识版本')
        release = self.knowledge.get_release(release_id, workspace_id)
        if release['dataset_id'] != dataset['id'] or release['status'] not in ('active', 'superseded', 'retained', 'ready'):
            raise AppError(409, 'RELEASE_INVALID', '知识版本与会话范围不兼容')
        if input.get('modelConnectionId'):
            self.models.get(input['modelConnectionId'], workspace_id)
        conversation_id = gen_id('conv')
        timestamp = now()
        self.db.run('INSERT INTO conversations(id,workspace_id,knowledge_base_id,dataset_id,release_id,title,status,model_connection_id,strict_evidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    conversation_id, workspace_id, kb['id'], dataset['id'], release['id'], input.get('title') or '新对话', 'active',
                    input.get('modelConnectionId'), 0 if input.get('strictEvidence') is False else 1, timestamp, timestamp)
        self.audit.append(workspace_id=workspace_id, action='conversation.create', object_type='conversation',
                          object_id=conversation_id, request_id=request_id,
                          details={'knowledgeBaseId': kb['id'], 'datasetId': dataset['id'], 'releaseId': release['id']})
        return self.get_conversation(conversation_id, workspace_id)

    def list_conversations(self, workspace_id=None, limit=100, offset=0):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        from_clause = ('FROM conversations c JOIN knowledge_bases kb ON kb.id=c.knowledge_base_id '
                       'JOIN datasets d ON d.id=c.dataset_id JOIN knowledge_releases kr ON kr.id=c.release_id')
        total = (self.db.one(f'SELECT COUNT(*) AS count {from_clause} WHERE c.workspace_id=? AND c.deleted_at IS NULL', workspace_id) or {}).get('count', 0)
        items = self.db.all(f'''SELECT c.*,kb.name AS knowledge_base_name,d.name AS dataset_name,kr.version AS release_version,
          (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count {from_clause}
          WHERE c.workspace_id=? AND c.deleted_at IS NULL ORDER BY c.updated_at DESC LIMIT ? OFFSET ?''', workspace_id, limit, offset)
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    def get_conversation(self, conversation_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        conversation = self.db.one(
            '''SELECT c.*,kb.name AS knowledge_base_name,d.name AS dataset_name,kr.version AS release_version
            FROM conversations c JOIN knowledge_bases kb ON kb.id=c.knowledge_base_id JOIN datasets d ON d.id=c.dataset_id
            JOIN knowledge_releases kr ON kr.id=c.release_id WHERE c.id=? AND c.workspace_id=? AND c.deleted_at IS NULL''',
            conversation_id, workspace_id)
        if not conversation:
            raise AppError(404, 'NOT_FOUND', '会话不存在或不可访问')
        messages = self.db.all('SELECT * FROM messages WHERE conversation_id=? AND workspace_id=? ORDER BY created_at,id', conversation_id, workspace_id)
        for message in messages:
            message['citations'] = self.db.all(
                'SELECT id,title,locator_json,excerpt,ordinal,document_id,document_revision_id,chunk_revision_id,release_id FROM citations WHERE message_id=? AND workspace_id=? ORDER BY ordinal',
                message['id'], workspace_id)
        conversation['messages'] = messages
        return conversation

    def delete_conversation(self, conversation_id, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        self.get_conversation(conversation_id, workspace_id)
        self.db.run("UPDATE conversations SET status='deleted',deleted_at=?,updated_at=? WHERE id=? AND workspace_id=?",
                    now(), now(), conversation_id, workspace_id)
        self.audit.append(workspace_id=workspace_id, action='conversation.delete', object_type='conversation',
                          object_id=conversation_id, request_id=request_id)
        return {'deleted': True}

    async def ask(self, conversation_id, input, workspace_id=None, request_id=None, trace_metadata=None):
        return await self.ask_stream(conversation_id, input, workspace_id, request_id, None, trace_metadata)

    async def ask_stream(self, conversation_id, input, workspace_id=None, request_id=None, on_event=None, trace_metadata=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        trace_metadata = trace_metadata or {}
        conversation = self.get_conversation(conversation_id, workspace_id)
        if conversation['status'] != 'active':
            raise AppError(409, 'INVALID_STATE', '当前会话不可继续问答')
        metadata_config = trace_metadata.get('configSnapshot') or {}
        if metadata_config.get('modelConnectionId') is not None:
            self.models.get(metadata_config['modelConnectionId'], workspace_id)
            conversation['model_connection_id'] = metadata_config['modelConnectionId']
        if metadata_config.get('strictEvidence') is not None:
            conversation['strict_evidence'] = 1 if metadata_config['strictEvidence'] else 0
        question = required(input.get('question') if input.get('question') is not None else input.get('query'), 'question')
        user_message_id = gen_id('msg')
        trace_id = gen_id('trace')
        started = time.monotonic()
        stages = []

        def stage(name, started_at, status, output):
            duration_ms = max(0, round((time.monotonic() - started_at) * 1000))
            stages.append({'name': name, 'status': status, 'durationMs': duration_ms, 'output': output})
            if on_event:
                on_event('stage', {'name': name, 'status': status, 'durationMs': duration_ms})

        stage_start = time.monotonic()
        query_plan = parse_question(question, conversation)
        stage('问题解析', stage_start, 'succeeded', query_plan)

        stage_start = time.monotonic()
        embedding_summary = {'provider': 'local-hash-v1', 'model': 'ordo-hash-embedding-v1', 'dimensions': 128,
                             'inputHash': hash_bytes(question.encode('utf-8')), 'degraded': False}
        stage('问题向量化', stage_start, 'succeeded', embedding_summary)

        stage_start = time.monotonic()
        route = route_query(question)
        stage('检索路由', stage_start, 'succeeded', route)

        stage_start = time.monotonic()
        retrieval = self.knowledge.search_release(conversation['release_id'], question, workspace_id, input.get('topK') or 8)
        candidates = retrieval['results']

        def candidate_summary(item):
            return {'chunkRevisionId': item['chunkRevisionId'], 'documentId': item['documentId'],
                    'documentRevisionId': item['documentRevisionId'], 'title': item['title'], 'content': item['content'],
                    'locator': item.get('locator'), 'rank': item.get('rank'), 'score': item.get('fusionScore'),
                    'vectorRank': item.get('vectorRank'), 'vectorScore': item.get('vectorScore'),
                    'fullTextRank': item.get('fullTextRank'), 'fullTextScore': item.get('fullTextScore'),
                    'rerankScore': item.get('rerankScore')}

        retrieval_output = {
            'routes': retrieval['routes'], 'candidateCount': len(candidates),
            'vector': [dict(candidate_summary(item), rank=item['vectorRank'], score=item['vectorScore'])
                       for item in candidates if item.get('vectorRank') is not None],
            'fullText': [dict(candidate_summary(item), rank=item['fullTextRank'], score=item['fullTextScore'])
                         for item in candidates if item.get('fullTextRank') is not None],
            'fusion': [candidate_summary(item) for item in candidates],
        }
        stage('多路召回', stage_start, 'succeeded', retrieval_output)

        stage_start = time.monotonic()
        stage('结果融合', stage_start, 'succeeded', {
            'method': (retrieval.get('routes') or {}).get('fusion', {}).get('method', 'rrf'),
            'k': (retrieval.get('routes') or {}).get('fusion', {}).get('k', 60),
            'candidateCount': len(candidates), 'rawCandidateCount': len(retrieval_output['vector']) + len(retrieval_output['fullText']),
            'deduplicatedCount': len(candidates), 'permissionFilteredCount': len(candidates),
            'vector': retrieval_output['vector'], 'fullText': retrieval_output['fullText'], 'candidates': retrieval_output['fusion'],
        })

        stage_start = time.monotonic()
        selected = [item for item in candidates
                    if (item.get('rerankScore') is not None and item['rerankScore'] > 0)
                    or (item.get('fullTextScore') is not None and item['fullTextScore'] > 0)
                    or (item.get('vectorScore') is not None and item['vectorScore'] > 0.35)][:6]
        selected_ids = {item['chunkRevisionId'] for item in selected}
        stage('重排', stage_start, 'succeeded', {
            'provider': 'local-lexical-v1', 'threshold': 0.35, 'inputCount': len(candidates), 'selectedCount': len(selected),
            'selected': [dict(candidate_summary(item), rank=item['rank'], score=item['rerankScore']) for item in selected],
            'rejected': [dict(candidate_summary(item), reason='未达到保留阈值') for item in candidates if item['chunkRevisionId'] not in selected_ids],
        })

        evidence_status = 'sufficient' if selected else 'insufficient'
        stage_start = time.monotonic()
        prompt_summary = {'templateVersion': 'strict-evidence-v1', 'strictEvidence': bool(conversation['strict_evidence']),
                          'evidenceCount': len(selected), 'maxEvidenceChars': 12000,
                          'security': {'evidenceTreatedAsUntrusted': True, 'hiddenReasoningStored': False, 'secretsIncluded': False}}
        stage('构建提示词', stage_start, 'succeeded', prompt_summary)

        stage_start = time.monotonic()
        generated = None
        degraded = False
        tokens_streamed = False
        history = [{'role': m['role'], 'content': m['content']} for m in (conversation.get('messages') or [])
                   if m['role'] in ('user', 'assistant')][-6:]

        def on_model_token(delta):
            nonlocal tokens_streamed
            tokens_streamed = True
            if on_event:
                on_event('token', {'delta': delta})

        strict_evidence = bool(conversation['strict_evidence'])
        if strict_evidence and not selected:
            generated = dict(local_evidence_answer(question, selected))
            degraded = True
            generated['degradationReason'] = 'NO_SUPPORTING_EVIDENCE'
        else:
            try:
                generated = await self.models.generate(connection_id=conversation.get('model_connection_id'),
                                                       workspace_id=workspace_id, question=question, evidence=selected,
                                                       strict_evidence=strict_evidence, history=history,
                                                       on_token=on_model_token if on_event else None)
            except AppError as error:
                if not selected and getattr(error, 'code', None) != 'FEATURE_DISABLED':
                    raise
                generated = dict(local_evidence_answer(question, selected))
                degraded = True
                generated['degradationReason'] = getattr(error, 'code', None) or 'MODEL_UNAVAILABLE'
        valid_ordinals = [ordinal for ordinal in (generated.get('citationOrdinals') or [])
                          if isinstance(ordinal, int) and 1 <= ordinal <= len(selected)]
        if len(valid_ordinals) != len(generated.get('citationOrdinals') or []):
            raise AppError(502, 'CITATION_INVALID', '回答包含无效引用')
        if strict_evidence and selected and not valid_ordinals and not _is_evidence_refusal(generated.get('content')):
            raise AppError(502, 'EVIDENCE_CITATION_REQUIRED', '严格证据模式要求回答包含有效引用或明确拒答')
        stage('回答生成', stage_start, 'degraded' if degraded else 'succeeded', {
            'provider': generated.get('provider'), 'modelId': generated.get('modelId'), 'evidenceStatus': evidence_status,
            'citationCount': len(valid_ordinals), 'degraded': degraded,
            'degradationReason': generated.get('degradationReason'), 'usage': generated.get('usage')})

        assistant_message_id = gen_id('msg')
        finished = now()
        config_snapshot = trace_metadata.get('configSnapshot') or {
            'modelConnectionId': conversation.get('model_connection_id'), 'strictEvidence': bool(conversation['strict_evidence']),
            'topK': input.get('topK') or 8}
        input_snapshot = trace_metadata.get('inputSnapshot') or {
            'question': question, 'topK': input.get('topK') or 8,
            **({'idempotencyKey': trace_metadata['idempotencyKey']} if trace_metadata.get('idempotencyKey') else {})}
        permission_snapshot = trace_metadata.get('permissionSnapshot') or {
            'workspaceId': workspace_id, 'conversationId': conversation_id, 'datasetId': conversation['dataset_id'],
            'releaseId': conversation['release_id']}
        staged = {'stages_json': stable_json(stages)}
        self.db.transaction(lambda: (
            self.db.run('INSERT INTO messages(id,workspace_id,conversation_id,role,content,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)',
                        user_message_id, workspace_id, conversation_id, 'user', question, '{}', finished),
            self.db.run('INSERT INTO messages(id,workspace_id,conversation_id,role,content,evidence_status,trace_id,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                        assistant_message_id, workspace_id, conversation_id, 'assistant', generated['content'], evidence_status, trace_id,
                        stable_json({'provider': generated.get('provider'), 'modelId': generated.get('modelId'), 'degraded': degraded}), finished),
            self.db.run('''INSERT INTO query_traces(id,workspace_id,conversation_id,message_id,release_id,query,status,evidence_status,stages_json,metrics_json,created_at,parent,root,trace_type,replay_from_stage,config_snapshot,input_snapshot,permission_snapshot,retention)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        trace_id, workspace_id, conversation_id, assistant_message_id, conversation['release_id'], question,
                        'degraded' if degraded else 'succeeded', evidence_status, staged['stages_json'],
                        stable_json({'totalMs': round((time.monotonic() - started) * 1000), 'candidateCount': len(candidates),
                                     'selectedEvidence': len(selected)}), finished,
                        trace_metadata.get('parentTraceId'), trace_metadata.get('rootTraceId') or trace_id,
                        trace_metadata.get('traceType') or 'original', trace_metadata.get('replayFromStage'),
                        stable_json(config_snapshot), stable_json(input_snapshot), stable_json(permission_snapshot),
                        trace_metadata.get('retention') or 'standard')))
        for ordinal, item in enumerate(selected, 1):
            if ordinal not in valid_ordinals:
                continue
            self.db.run('INSERT INTO citations(id,workspace_id,trace_id,message_id,release_id,document_id,document_revision_id,chunk_revision_id,title,locator_json,excerpt,ordinal,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        gen_id('cite'), workspace_id, trace_id, assistant_message_id, conversation['release_id'],
                        item['documentId'], item['documentRevisionId'], item['chunkRevisionId'], item['title'],
                        stable_json(item.get('locator') or {}), str(item['content'])[:500], ordinal, finished)
        if conversation['title'] == '新对话':
            self.db.run('UPDATE conversations SET title=?,updated_at=? WHERE id=? AND workspace_id=?', question[:60], finished, conversation_id, workspace_id)
        else:
            self.db.run('UPDATE conversations SET updated_at=? WHERE id=? AND workspace_id=?', finished, conversation_id, workspace_id)

        if on_event and not tokens_streamed:
            text = str(generated.get('content') or '')
            for i in range(0, len(text), 4):
                on_event('token', {'delta': text[i:i + 4]})
                import asyncio
                await asyncio.sleep(0.01)
        final_result = {
            'userMessage': self.db.one('SELECT * FROM messages WHERE id=?', user_message_id),
            'assistantMessage': dict(self.db.one('SELECT * FROM messages WHERE id=?', assistant_message_id),
                                     citations=self.db.all('SELECT * FROM citations WHERE message_id=? ORDER BY ordinal', assistant_message_id)),
            'trace': self.get_trace(trace_id, workspace_id),
        }
        if on_event:
            on_event('done', final_result)
        return final_result

    def get_trace(self, trace_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        trace = self.db.one('SELECT * FROM query_traces WHERE id=? AND workspace_id=?', trace_id, workspace_id)
        if not trace:
            raise AppError(404, 'NOT_FOUND', '问答 Trace 不存在或不可访问')
        for field in ('config_snapshot', 'input_snapshot', 'permission_snapshot'):
            trace[field] = parse_json(trace.get(field), {})
        if not trace.get('root'):
            trace['root'] = trace['id']
        trace['citations'] = self.db.all(
            'SELECT id,title,locator_json,excerpt,ordinal,document_id,document_revision_id,chunk_revision_id,release_id FROM citations WHERE trace_id=? AND workspace_id=? ORDER BY ordinal',
            trace_id, workspace_id)
        return trace

    def list_traces(self, workspace_id=None, conversation_id=None, limit=100, offset=0):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        clauses, params = ['workspace_id=?'], [workspace_id]
        if conversation_id:
            clauses.append('conversation_id=?')
            params.append(conversation_id)
        total = (self.db.one(f'SELECT COUNT(*) AS count FROM query_traces WHERE {" AND ".join(clauses)}', *params) or {}).get('count', 0)
        items = self.db.all(f'SELECT * FROM query_traces WHERE {" AND ".join(clauses)} ORDER BY created_at DESC LIMIT ? OFFSET ?', *params, limit, offset)
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    async def replay_trace(self, trace_id, input=None, workspace_id=None, request_id=None, idempotency_key=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        source = self.get_trace(trace_id, workspace_id)
        body = input if isinstance(input, dict) else {}
        from_stage = None if body.get('fromStage') in (None, '', ) else str(body['fromStage'])
        if from_stage and from_stage not in _REPLAY_SUPPORTED:
            raise AppError(422, 'REPLAY_UNSUPPORTED', f'不支持从阶段“{from_stage}”重放：当前仅支持从问题解析阶段重新执行完整问答流水线',
                           {'fromStage': from_stage, 'supportedFromStages': sorted(_REPLAY_SUPPORTED)})
        overrides = body.get('overrides') if isinstance(body.get('overrides'), dict) else {}
        allowed = ('question', 'query', 'topK', 'modelConnectionId', 'strictEvidence')
        unknown = [key for key in overrides if key not in allowed]
        if unknown:
            raise AppError(400, 'VALIDATION_ERROR', 'replay overrides 包含不支持的字段', {'fields': unknown})
        replay_input = {'question': overrides.get('question', overrides.get('query', source['query']))}
        if 'topK' in overrides:
            replay_input['topK'] = overrides['topK']
        if 'modelConnectionId' in overrides:
            replay_input['modelConnectionId'] = overrides['modelConnectionId']
        if 'strictEvidence' in overrides:
            replay_input['strictEvidence'] = overrides['strictEvidence']
        request_fingerprint = stable_json({'sourceTraceId': trace_id, 'fromStage': from_stage, 'overrides': replay_input})
        if idempotency_key:
            existing = self.db.one("SELECT * FROM query_traces WHERE workspace_id=? AND trace_type='replay' AND json_extract(input_snapshot,'$.idempotencyKey')=? ORDER BY created_at LIMIT 1",
                                   workspace_id, str(idempotency_key))
            if existing:
                existing_input = parse_json(existing['input_snapshot'], {})
                if existing_input.get('requestFingerprint') != request_fingerprint:
                    raise AppError(409, 'IDEMPOTENCY_CONFLICT', '相同幂等键对应了不同的 Trace 重放输入')
                existing_trace = self.get_trace(existing['id'], workspace_id)
                return {'trace': existing_trace,
                        'assistantMessage': self.db.one('SELECT * FROM messages WHERE id=? AND workspace_id=?', existing_trace['message_id'], workspace_id) if existing_trace.get('message_id') else None,
                        'replayed': False, 'idempotent': True}
        config_snapshot = {
            'modelConnectionId': overrides.get('modelConnectionId') if 'modelConnectionId' in overrides else (source['config_snapshot'] or {}).get('modelConnectionId'),
            'strictEvidence': bool(overrides['strictEvidence']) if 'strictEvidence' in overrides else (source['config_snapshot'] or {}).get('strictEvidence', True),
            'topK': overrides.get('topK') if 'topK' in overrides else (source['config_snapshot'] or {}).get('topK', 8),
        }
        result = await self.ask(source['conversation_id'], replay_input, workspace_id, request_id, {
            'parentTraceId': trace_id, 'rootTraceId': source.get('root') or source['id'], 'traceType': 'replay',
            'replayFromStage': from_stage or '问题解析', 'configSnapshot': config_snapshot,
            'inputSnapshot': {'sourceTraceId': trace_id, 'fromStage': from_stage or '问题解析', 'overrides': replay_input,
                              'idempotencyKey': str(idempotency_key) if idempotency_key else None,
                              'requestFingerprint': request_fingerprint},
            'permissionSnapshot': source.get('permission_snapshot') or {'workspaceId': workspace_id,
                                                                        'conversationId': source['conversation_id'], 'releaseId': source['release_id']},
            'retention': source.get('retention') or 'standard', 'idempotencyKey': str(idempotency_key) if idempotency_key else None,
        })
        return {'trace': result['trace'], 'userMessage': result['userMessage'], 'assistantMessage': result['assistantMessage'],
                'replayed': True, 'idempotent': False}

    def compare_traces(self, trace_id, other_trace_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        left = self.get_trace(trace_id, workspace_id)
        right = self.get_trace(other_trace_id, workspace_id)

        def parse_stages(trace):
            value = trace.get('stages')
            return value if isinstance(value, list) else parse_json(trace.get('stages_json'), [])

        left_stages = parse_stages(left)
        right_stages = parse_stages(right)
        stage_names = list(dict.fromkeys([stage['name'] for stage in left_stages + right_stages]))
        stages = []
        for name in stage_names:
            a = next((stage for stage in left_stages if stage['name'] == name), None)
            b = next((stage for stage in right_stages if stage['name'] == name), None)
            stages.append({'name': name, 'left': a, 'right': b,
                           'durationDiffMs': ((b or {}).get('durationMs') or 0) - ((a or {}).get('durationMs') or 0),
                           'statusChanged': (a or {}).get('status') != (b or {}).get('status')})

        def candidates_from(trace):
            retrieval_stage = next((stage for stage in parse_stages(trace) if stage['name'] == '多路召回'), None)
            output = (retrieval_stage or {}).get('output') or {}
            return output.get('fusion') if isinstance(output.get('fusion'), list) else []

        left_candidates = candidates_from(left)
        right_candidates = candidates_from(right)

        def key(candidate):
            return candidate.get('chunkRevisionId') or candidate.get('documentId') or candidate.get('title')

        left_keys = set()
        for candidate in left_candidates:
            left_keys.add(key(candidate))
        right_keys = set()
        for candidate in right_candidates:
            right_keys.add(key(candidate))
        candidates = {'left': left_candidates, 'right': right_candidates,
                      'added': [item for item in right_candidates if key(item) not in left_keys],
                      'removed': [item for item in left_candidates if key(item) not in right_keys],
                      'common': [item for item in right_candidates if key(item) in left_keys]}

        def answer_from(trace):
            if not trace.get('message_id'):
                return None
            return self.db.one("SELECT id,content,evidence_status FROM messages WHERE id=? AND workspace_id=? AND role='assistant'", trace['message_id'], workspace_id)

        left_answer = answer_from(left)
        right_answer = answer_from(right)
        left_ms = Number_default((left.get('metrics') or {}).get('totalMs'))
        right_ms = Number_default((right.get('metrics') or {}).get('totalMs'))
        answer = {'left': left_answer, 'right': right_answer,
                  'changed': (left_answer or {}).get('content') != (right_answer or {}).get('content'),
                  'contentChanged': (left_answer or {}).get('content') != (right_answer or {}).get('content')}
        timing = {'leftMs': left_ms, 'rightMs': right_ms, 'deltaMs': right_ms - left_ms}
        return {'traceId': trace_id, 'otherTraceId': other_trace_id, 'stages': stages, 'stageDiffs': stages,
                'candidates': candidates, 'candidateDiff': candidates, 'answer': answer, 'answers': answer,
                'timing': timing, 'durationDiffMs': timing['deltaMs']}

    def open_citation(self, citation_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        citation = self.db.one('''SELECT c.*,d.status AS document_status,cr.content_md,cr.content_text
          FROM citations c JOIN documents d ON d.id=c.document_id JOIN chunk_revisions cr ON cr.id=c.chunk_revision_id
          WHERE c.id=? AND c.workspace_id=?''', citation_id, workspace_id)
        if not citation:
            raise AppError(404, 'NOT_FOUND', '引用不存在或不可访问')
        release_link = self.db.one('''SELECT 1 AS allowed FROM release_chunks rc JOIN knowledge_releases kr ON kr.id=rc.release_id
          WHERE rc.release_id=? AND rc.chunk_revision_id=? AND kr.workspace_id=?''',
                                   citation['release_id'], citation['chunk_revision_id'], workspace_id)
        if not release_link:
            raise AppError(410, 'CITATION_INVALID', '引用不再属于固定知识版本')
        return {
            'id': citation['id'], 'title': citation['title'], 'locator': citation['locator'], 'excerpt': citation['excerpt'],
            'documentStatus': citation['document_status'], 'documentId': citation['document_id'],
            'documentRevisionId': citation['document_revision_id'], 'chunkRevisionId': citation['chunk_revision_id'],
            'releaseId': citation['release_id'], 'contentMd': citation['content_md'], 'contentText': citation['content_text'],
            'locationLabel': format_locator(citation['locator']),
        }

    def feedback(self, message_id, input, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        message = self.db.one("SELECT * FROM messages WHERE id=? AND workspace_id=? AND role='assistant'", message_id, workspace_id)
        if not message:
            raise AppError(404, 'NOT_FOUND', '回答消息不存在或不可访问')
        try:
            rating = int(input.get('rating'))
        except (TypeError, ValueError):
            rating = None
        if rating not in (1, -1):
            raise AppError(400, 'VALIDATION_ERROR', 'rating 只能是 1 或 -1')
        feedback_id = gen_id('fb')
        self.db.run('''INSERT INTO feedback(id,workspace_id,message_id,rating,reason,created_at) VALUES(?,?,?,?,?,?)
          ON CONFLICT(workspace_id,message_id) DO UPDATE SET rating=excluded.rating,reason=excluded.reason,created_at=excluded.created_at''',
                    feedback_id, workspace_id, message_id, rating, input.get('reason') or '', now())
        self.audit.append(workspace_id=workspace_id, action='feedback.save', object_type='message', object_id=message_id,
                          request_id=request_id, details={'rating': rating})
        return self.db.one('SELECT * FROM feedback WHERE workspace_id=? AND message_id=?', workspace_id, message_id)


def Number_default(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0