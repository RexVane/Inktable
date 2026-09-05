"""The checked-in frontend contract is registered as real FastAPI operations."""
import asyncio
import inspect
import json
import re
import sys
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .core import AppError, bounded_int, required, stable_json
from .query import parse_question, route_query
from .trace_workbench import STAGES


def envelope(value):
    if isinstance(value, dict) and all(key in value for key in ('items', 'total', 'limit', 'offset')):
        return {'data': value['items'], 'meta': {key: value[key] for key in ('total', 'limit', 'offset')}}
    return {'data': value}


def bindings():
    mapping = {}
    def bind(service, values, signature):
        for operation, method in values.items():
            mapping[operation] = (service, method, signature)
    bind('knowledge', {'getKnowledgeBases': 'list_knowledge_bases'}, ['ws'])
    bind('knowledge', {'createKnowledgeBase': 'create_knowledge_base'}, ['body', 'ws', 'rid'])
    for identifier, read, write, remove in [
        ('kbId', {'getKnowledgeBase': 'get_knowledge_base', 'getKnowledgeBaseImpact': 'knowledge_base_impact', 'getDatasets': 'list_datasets'}, {'updateKnowledgeBase': 'update_knowledge_base', 'createDataset': 'create_dataset', 'createIndexProfile': 'create_index_profile'}, {'deleteKnowledgeBase': 'delete_knowledge_base'}),
        ('datasetId', {'getDataset': 'get_dataset', 'getSources': 'list_sources', 'getIndexingStats': 'get_indexing_stats', 'getIndexingPipeline': 'get_indexing_pipeline', 'getChapters': 'get_chapters', 'getReleases': 'list_releases'}, {'updateDataset': 'update_dataset', 'createSource': 'create_source', 'buildRelease': 'build_release', 'setHybridWeights': 'set_hybrid_weights'}, {'deleteDataset': 'delete_dataset', 'vectorizePending': 'batch_vectorize_pending', 'rebuildHnswIndex': 'rebuild_hnsw_index', 'optimizeVectorIndex': 'optimize_vector_index', 'rebuildBm25Index': 'rebuild_bm25_index'}),
        ('profileId', {'getIndexProfile': 'get_index_profile'}, {'updateIndexProfile': 'update_index_profile'}, {'deleteIndexProfile': 'archive_index_profile'}),
        ('documentId', {'getDocument': 'get_document'}, {}, {'deleteDocument': 'delete_document'}),
        ('chunkId', {'getChunk': 'get_chunk', 'getChunkLineage': 'get_chunk_lineage'}, {'editChunk': 'edit_chunk', 'toggleChunkDisabled': 'toggle_chunk_disabled', 'restoreChunk': 'restore_chunk', 'splitChunk': 'split_chunk'}, {'vectorizeChunk': 'vectorize_chunk'}),
        ('releaseId', {'getRelease': 'get_release', 'getReleaseImpact': 'release_impact'}, {}, {'activateRelease': 'activate_release', 'rollbackRelease': 'rollback_release'})]:
        bind('knowledge', read, [identifier, 'ws'])
        bind('knowledge', write, [identifier, 'body', 'ws', 'rid'])
        bind('knowledge', remove, [identifier, 'ws', 'rid'])
    bind('knowledge', {'mergeChunks': 'merge_chunks'}, ['body', 'ws', 'rid'])
    bind('tasks', {'getTask': 'get', 'cancelTask': 'cancel', 'pauseTask': 'pause', 'resumeTask': 'resume', 'retryTask': 'retry'}, ['taskId', 'ws'])
    bind('connectors', {'getConnectors': 'list'}, ['ws'])
    bind('connectors', {'createConnector': 'create'}, ['body', 'ws', 'rid'])
    bind('connectors', {'getConnector': 'get', 'getConnectorSchema': 'schema', 'getQueryTemplates': 'list_templates'}, ['connectorId', 'ws'])
    bind('connectors', {'testConnector': 'test'}, ['connectorId', 'ws', 'rid'])
    bind('connectors', {'createQueryTemplate': 'create_template'}, ['connectorId', 'body', 'ws', 'rid'])
    bind('connectors', {'snapshotQueryTemplate': 'snapshot'}, ['templateId', 'body', 'ws', 'rid'])
    bind('graph', {'getOntologies': 'list_ontologies'}, ['kbId', 'ws'])
    bind('graph', {'createOntology': 'create_ontology'}, ['kbId', 'body', 'ws', 'rid'])
    bind('graph', {'publishOntology': 'publish_ontology'}, ['ontologyId', 'ws', 'rid'])
    bind('graph', {'getGraph': 'graph'}, ['datasetId', 'ws'])
    bind('graph', {'createGraphEntity': 'create_entity', 'createGraphRelation': 'create_relation'}, ['datasetId', 'body', 'ws', 'rid'])
    bind('query', {'getConversation': 'get_conversation'}, ['conversationId', 'ws'])
    bind('query', {'createConversation': 'create_conversation'}, ['body', 'ws', 'rid'])
    bind('query', {'updateConversation': 'update_conversation'}, ['conversationId', 'body', 'ws', 'rid'])
    bind('query', {'deleteConversation': 'delete_conversation'}, ['conversationId', 'ws', 'rid'])
    bind('query', {'sendFeedback': 'feedback'}, ['messageId', 'body', 'ws', 'rid'])
    bind('query', {'openCitation': 'open_citation'}, ['citationId', 'ws'])
    bind('query', {'getTrace': 'get_trace'}, ['traceId', 'ws'])
    bind('query', {'compareTraces': 'compare_traces'}, ['traceId', 'otherId', 'ws'])
    bind('models', {'getModels': 'list'}, ['ws'])
    bind('models', {'createModel': 'create'}, ['body', 'ws', 'rid'])
    bind('models', {'getModel': 'get'}, ['modelId', 'ws'])
    bind('models', {'patchModel': 'update'}, ['modelId', 'body', 'ws', 'rid'])
    bind('models', {'testModel': 'test', 'deleteModel': 'remove'}, ['modelId', 'ws', 'rid'])
    bind('product', {'getDashboard': 'dashboard', 'getHealth': 'health', 'getDiagnostics': 'diagnostics', 'getSettings': 'get_settings', 'getFeatureFlags': 'feature_flags', 'getAssistants': 'list_assistants', 'getBackups': 'list_backups'}, ['ws'])
    bind('product', {'createWikiPage': 'create_wiki', 'createAssistant': 'create_assistant', 'createBackup': 'request_backup'}, ['body', 'ws', 'rid'])
    bind('product', {'updateSetting': 'update_setting', 'putFeatureFlag': 'set_feature_flag'}, ['key', 'body', 'ws', 'rid'])
    bind('product', {'getWikiPage': 'get_wiki'}, ['pageId', 'ws'])
    bind('product', {'reviseWikiPage': 'revise_wiki'}, ['pageId', 'body', 'ws', 'rid'])
    bind('product', {'wikiFromMessage': 'wiki_from_message'}, ['messageId', 'body', 'ws', 'rid'])
    bind('product', {'getAssistant': 'get_assistant'}, ['assistantId', 'ws'])
    bind('product', {'updateAssistant': 'update_assistant', 'publishAssistant': 'publish_assistant'}, ['assistantId', 'body', 'ws', 'rid'])
    bind('product', {'pauseAssistant': 'pause_assistant', 'deleteAssistant': 'delete_assistant'}, ['assistantId', 'ws', 'rid'])
    bind('product', {'restoreBackup': 'request_restore'}, ['backupId', 'body', 'ws', 'rid'])
    bind('widget', {'getAssistantClients': 'list_clients'}, ['assistantId', 'ws'])
    bind('widget', {'createAssistantClient': 'create_client'}, ['assistantId', 'body', 'ws', 'rid'])
    bind('widget', {'rotateWidgetClient': 'rotate_client', 'revokeWidgetClient': 'revoke_client'}, ['clientId', 'ws', 'rid'])
    bind('widget', {'updateHandoff': 'update_handoff'}, ['handoffId', 'body', 'ws', 'rid'])
    bind('audit', {'verifyAudit': 'verify'}, ['ws'])
    bind('workbench', {'createUnscopedDataset': 'create_dataset'}, ['body', 'ws', 'rid'])
    bind('workbench', {'assignSourceDataset': 'assign_source'}, ['sourceId', 'body', 'ws', 'rid'])
    bind('workbench', {'deleteSource': 'delete_source'}, ['sourceId', 'ws', 'rid'])
    bind('workbench', {'getDatasetTree': 'tree'}, ['datasetId', 'ws'])
    bind('workbench', {'createDatasetFolder': 'create_folder'}, ['datasetId', 'body', 'ws'])
    bind('workbench', {'deleteDatasetFile': 'delete_file'}, ['datasetId', 'fileId', 'ws'])
    bind('workbench', {'moveDatasetFile': 'move_file'}, ['datasetId', 'fileId', 'body', 'ws'])
    bind('workbench', {'inspectFile': 'inspect_file'}, ['fileId', 'ws'])
    bind('workbench', {'getParsingSettings': 'parsing_settings', 'getSystemResources': 'resources'}, ['ws'])
    bind('workbench', {'updateParsingSettings': 'parsing_settings'}, ['ws', 'body'])
    return mapping


async def stream_result(run, request_id):
    queue = asyncio.Queue()
    def event(name, value):
        queue.put_nowait((name, value))
    async def producer():
        try:
            result = await run(event)
            # ask_stream emits done itself, idempotent replay returns without emitting events.
            if not getattr(producer, 'done', False):
                event('done', result)
        except AppError as error:
            event('error', {'code': error.code, 'message': error.message, 'requestId': request_id})
        except Exception:
            import logging
            logging.getLogger('ordo').exception('Streaming request %s failed', request_id)
            event('error', {'code': 'INTERNAL_ERROR', 'message': '问答处理失败', 'requestId': request_id})
        finally:
            queue.put_nowait(None)
    task = asyncio.create_task(producer())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), 15)
            except asyncio.TimeoutError:
                yield ': keepalive\n\n'
                continue
            if item is None:
                break
            name, value = item
            yield f'event: {name}\ndata: {json.dumps(value, ensure_ascii=False, separators=(",", ":"))}\n\n'
            if name in ('done', 'error'):
                break
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def mount_routes(app, services, bootstrap):
    catalog = json.loads(Path(__file__).with_name('api_contract.json').read_text('utf-8'))
    direct = bindings()
    k, q, p, w, t = (services[key] for key in ('knowledge', 'query', 'product', 'workbench', 'traces'))
    db, config, tasks, widget = (services[key] for key in ('db', 'config', 'tasks', 'widget'))
    async def dispatch(operation, request):
        ws, rid = request.state.workspace_id, request.state.request_id
        params, path = dict(request.query_params), request.path_params
        body, raw = {}, b''
        multipart = request.headers.get('content-type', '').startswith('multipart/form-data')
        if request.method not in ('GET', 'HEAD') and not multipart:
            raw = await request.body()
            if raw:
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise AppError(400, 'JSON_INVALID', '请求体不是有效 JSON')
                if not isinstance(body, dict):
                    raise AppError(400, 'VALIDATION_ERROR', '请求体必须是 JSON 对象')
        if body.get('idempotencyKey') is None and request.headers.get('idempotency-key'):
            body['idempotencyKey'] = request.headers['idempotency-key']
        feature = 'wiki' if 'Wiki' in operation or operation in ('wikiFromMessage', 'saveTraceQa') else 'websiteAssistant' if 'Widget' in operation or 'Handoff' in operation or operation in ('getAssistantClients', 'createAssistantClient') else 'assistants' if 'Assistant' in operation else 'graph' if 'Graph' in operation or 'Ontolog' in operation else 'databaseConnectors' if 'Connector' in operation or 'QueryTemplate' in operation else None
        if feature:
            p.require_feature(feature, ws)
        if operation in direct:
            service, method, signature = direct[operation]
            values = {**path, 'ws': ws, 'rid': rid, 'body': body}
            result = getattr(services[service], method)(*[values[key] for key in signature])
            return await result if inspect.isawaitable(result) else result
        limit = bounded_int(params.get('limit'), 100, 1, 500, 'limit')
        offset = bounded_int(params.get('offset'), 0, 0, 1000000, 'offset')
        if operation == 'bootstrapSession':
            return await bootstrap(request)
        if operation == 'getVersion':
            import fastapi
            return {'appVersion': config['appVersion'], 'schemaVersion': p.schema_version(), 'deploymentProfile': config['deploymentProfile'], 'platform': config['platform'], 'runtime': 'Python/FastAPI', 'python': sys.version.split()[0], 'fastapi': fastapi.__version__, 'node': 'Python ' + sys.version.split()[0]}
        if operation == 'getOpenApi':
            return JSONResponse(app.openapi())
        if operation == 'search':
            return p.global_search(params.get('q') or params.get('query'), ws, limit)
        if operation == 'getIndexProfiles':
            k.ensure_kb(path['kbId'], ws)
            return db.all('SELECT * FROM index_profiles WHERE knowledge_base_id=? AND workspace_id=? ORDER BY created_at DESC', path['kbId'], ws)
        if operation == 'setDefaultIndexProfile':
            profile = k.get_index_profile(path['profileId'], ws)
            return k.set_default_index_profile(profile['knowledge_base_id'], profile['id'], ws, rid)
        if operation in ('uploadDocument', 'uploadFile', 'uploadArchive'):
            async with request.form(max_files=1, max_fields=30, max_part_size=config['bodyLimit']) as form:
                file = form.get('file')
                if not file or not hasattr(file, 'read'):
                    raise AppError(400, 'FILE_REQUIRED', '请选择上传文件')
                content = await file.read(config['maxFileBytes'] + 1)
                if len(content) > config['maxFileBytes']:
                    raise AppError(413, 'FILE_TOO_LARGE', '文件超过大小预算')
                input = {**params, **{key: value for key, value in form.items() if isinstance(value, str)}, **({'datasetId': path['datasetId']} if 'datasetId' in path else {})}
                if operation == 'uploadArchive':
                    return services['ingest'].archive_import(path['datasetId'], file.filename, content, input, ws, rid)
                return w.register_file(file.filename, content, file.content_type, input, ws, rid)
        if operation == 'directoryPreview':
            k.ensure_dataset(path['datasetId'], ws)
            return services['ingest'].directory_preview(required(body.get('directory'), 'directory'), body.get('rules'))
        if operation == 'directoryImport':
            return services['ingest'].directory_import(path['datasetId'], body, ws, rid)
        if operation == 'getDocuments':
            return k.list_documents(path['datasetId'], ws, params.get('status'), params.get('query'), limit, offset)
        if operation == 'getArtifact':
            kind = path['kind']
            return Response(k.artifact_file(path['artifactId'], kind, ws), media_type='text/markdown' if kind == 'markdown' else 'application/json')
        if operation == 'getChunks':
            return k.list_chunks(path['datasetId'], ws, params.get('query'), params.get('documentId'), params.get('type'), params.get('warning') in ('true', '1'), limit, offset)
        if operation == 'getChunkDiff':
            return k.diff_chunk(path['chunkId'], params, ws)
        if operation == 'searchRelease':
            return k.search_release(path['releaseId'], body.get('query') or body.get('question'), ws, bounded_int(body.get('limit', body.get('topK')), 10, 1, 50, 'limit'))
        if operation == 'getTasks':
            return tasks.list(ws, params.get('status'), params.get('type'), limit, offset)
        if operation == 'waitTask':
            return await tasks.wait(path['taskId'], ws, bounded_int(params.get('timeoutMs'), 10000, 1, 120000, 'timeoutMs'))
        if operation == 'executeQueryTemplate':
            return await services['connectors'].execute_template(path['templateId'], body.get('params', body.get('values', {})), ws, rid)
        if operation == 'testConnectorConfig':
            return await services['connectors'].test_config(body, ws)
        if operation == 'getGraphEntities':
            return services['graph'].list_entities(path['datasetId'], ws, {**params, 'limit': limit, 'offset': offset})
        if operation == 'getConversations':
            return q.list_conversations(ws, limit, offset)
        if operation == 'getTraces':
            return q.list_traces(ws, params.get('conversationId'), limit, offset)
        if operation == 'sendMessage':
            q.get_conversation(path['conversationId'], ws)
            required(body.get('question', body.get('query')), 'question')
            body['topK'] = bounded_int(body.get('topK'), 8, 1, 50, 'topK')
            if body.get('stream') or 'text/event-stream' in request.headers.get('accept', ''):
                return StreamingResponse(stream_result(lambda event: q.ask_stream(path['conversationId'], body, ws, rid, event), rid), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})
            return await q.ask(path['conversationId'], body, ws, rid)
        if operation == 'getWikiPages':
            return p.list_wiki(ws, params.get('knowledgeBaseId'))
        if operation == 'getHandoffs':
            return widget.list_handoffs(ws, params.get('status'))
        if operation == 'getAudit':
            return services['audit'].list(ws, limit, offset)
        if operation == 'issueWidgetToken':
            return widget.issue_token(body, dict(request.headers), raw.decode('utf-8'))
        if operation in ('createWidgetSession', 'sendWidgetMessage', 'requestWidgetHandoff', 'deleteWidgetSession'):
            token, origin = request.headers.get('authorization', '').removeprefix('Bearer '), request.headers.get('origin', '')
            if operation == 'createWidgetSession':
                return widget.create_visitor_session(token or body.get('token'), origin)
            if operation == 'sendWidgetMessage':
                return await widget.ask(path['sessionId'], origin, token, body, rid)
            if operation == 'requestWidgetHandoff':
                return widget.request_handoff(path['sessionId'], origin, token, body)
            return widget.delete_visitor(path['sessionId'], origin, token)
        if operation == 'getAllDatasets':
            return w.datasets(ws, params.get('knowledgeBaseId') or params.get('kbId'))
        if operation in ('getRegisteredSources', 'getRecentSources', 'getSourceAttentionItems', 'getSourceTree'):
            rows = w.sources(ws, params.get('query') or params.get('q'), params.get('datasetId'), params.get('status'))
            if operation == 'getSourceAttentionItems':
                return [row for row in rows if row['status'] in ('failed', 'review_required', 'unsupported', 'quarantined', 'needs_password')]
            if operation == 'getSourceTree':
                return [{'id': kind, 'name': kind, 'count': sum(row['type'] == kind for row in rows), 'children': [row for row in rows if row['type'] == kind]} for kind in sorted({row['type'] for row in rows})]
            return {'items': rows[offset:offset+limit], 'total': len(rows), 'limit': limit, 'offset': offset} if operation == 'getRegisteredSources' else rows[:limit]
        if operation == 'getSourceActivities':
            return db.all("SELECT * FROM audit_events WHERE workspace_id=? AND (action LIKE 'source.%' OR action LIKE 'document.%') ORDER BY rowid DESC LIMIT ?", ws, limit)
        if operation == 'getDatasetFiles':
            return w.files(path['datasetId'], ws, params)
        if operation in ('batchDeleteDatasetFiles', 'batchMoveDatasetFiles'):
            ids = body.get('fileIds') or body.get('ids') or []
            if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
                raise AppError(400, 'VALIDATION_ERROR', 'fileIds 必须包含 1 到 500 个文件 ID')
            for file_id in ids:
                if k.get_document(file_id, ws)['dataset_id'] != path['datasetId']:
                    raise AppError(404, 'NOT_FOUND', '文件不属于当前数据集')
            results = [w.delete_file(path['datasetId'], file_id, ws) if operation == 'batchDeleteDatasetFiles' else w.move_file(path['datasetId'], file_id, body, ws) for file_id in ids]
            return {'count': len(results), 'results': results}
        if operation == 'getParsingProfiles':
            return [{'id': 'profile_default', 'name': '默认解析运行', 'isDefault': True, 'available': True, 'description': '原生文字解析；扫描件标记人工复核'}, {'id': 'profile_fast_text', 'name': '纯文本快速运行', 'available': True}, {'id': 'profile_ocr', 'name': '深度 OCR 运行', 'available': False, 'reason': '未配置 OCR Provider'}]
        controls = {'startParsing': 'start', 'pauseParsing': 'pause', 'resumeParsing': 'resume', 'retryFailedParsing': 'retry-failed', 'clearPendingParsingTasks': 'clear-pending'}
        if operation in controls:
            return w.control_parsing(controls[operation], body, ws)
        if operation == 'getParsingPipelineStats':
            return w.pipeline_stats(ws, params)
        if operation in ('getParsingTasks', 'exportParsingLogs'):
            rows = w.parsing_tasks(ws, params)
            if operation == 'exportParsingLogs':
                return Response(stable_json({'tasks': [tasks.get(row['id'], ws) for row in rows], 'resources': w.resources(ws)}), media_type='application/json', headers={'Content-Disposition': 'attachment; filename="parsing_audit_log.json"'})
            filtered = [row for row in rows if not params.get('status') or row['status'] == params['status']]
            return {'items': filtered[offset:offset+limit], 'total': len(filtered), 'limit': limit, 'offset': offset}
        page_modes = {'getDocumentPreviewPages': 'pages', 'getDocumentPage': 'page', 'getDocumentPageInspect': 'inspect', 'getDocumentPageDiff': 'diff'}
        if operation in page_modes:
            return w.document_page(path['documentId'], path.get('pageNum', 1), ws, page_modes[operation])
        if operation == 'quickParse':
            conversation = q.get_conversation(body['conversationId'], ws) if body.get('conversationId') else {'workspace_id': ws, 'dataset_id': body.get('datasetId'), 'release_id': None, 'strict_evidence': True}
            return parse_question(required(body.get('query', body.get('question')), 'query'), conversation)
        if operation == 'getConversationContext':
            conversation = q.get_conversation(path['conversationId'], ws)
            return {'conversationId': conversation['id'], 'messages': conversation['messages'][-20:], 'releaseId': conversation['release_id']}
        trace_id = path.get('traceId')
        stage_getters = {'getTraceParseStage': 'parse', 'getTraceEmbedStage': 'embed', 'getTraceRouteStage': 'route', 'getTraceRecallStage': 'recall', 'getTraceFusionStage': 'fusion', 'getTraceRerankStage': 'rerank', 'getTracePromptStage': 'prompt', 'getTraceGenerationStage': 'generation'}
        if operation in stage_getters:
            return t.stage(trace_id, stage_getters[operation], ws)
        if operation in ('getTracePipeline', 'getTraceWaterfall'):
            return t.pipeline(trace_id, ws)
        log_getters = {'getTraceParseLogs': 'parse', 'getEmbeddingLogs': 'embed', 'getTraceRouteLogs': 'route', 'getFusionLogs': 'fusion', 'getRerankLogs': 'rerank'}
        if operation in log_getters:
            return t.logs(trace_id, log_getters[operation], ws)
        reruns = {'replayTrace': body.get('fromStage') or 'parse', 'replayAll': 'parse', 'reparseTrace': 'parse', 'rerunFusionStage': 'fusion', 'retryRecallChannel': 'recall', 'regenerateAnswer': 'generation'}
        if operation in reruns:
            q.get_trace(trace_id, ws)
            if operation == 'regenerateAnswer' and (body.get('stream') or 'text/event-stream' in request.headers.get('accept', '')):
                return StreamingResponse(stream_result(lambda event: t.rerun(trace_id, reruns[operation], body, ws, rid, event), rid), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})
            return await t.rerun(trace_id, reruns[operation], body, ws, rid)
        if operation in ('updateTraceParse', 'classifyTraceIntent', 'extractTraceEntities', 'updateTraceKeywords', 'updateNormalizedQuery', 'rewriteTraceQuery', 'updateRewrittenQuery', 'updateTraceFilters', 'updateTraceRawJson'):
            return t.parse_action(trace_id, operation, body, ws, rid)
        if operation in ('recomputeEmbedding', 'getEmbeddingScatter', 'compareEmbeddingModels', 'getEmbeddingVector'):
            return t.embedding_action(trace_id, operation, body or params, ws)
        updates = {'updateTraceRoute': 'route', 'updateFusionWeights': 'fusion', 'updateRerankConfig': 'rerank', 'updateTracePrompt': 'prompt'}
        if operation in updates:
            return t.save(trace_id, updates[operation], body, ws, rid)
        if operation == 'resetFusionWeights':
            return t.save(trace_id, 'fusion', {'denseWeight': 1, 'sparseWeight': 1, 'k': 60}, ws, rid, replace=True)
        if operation == 'simulateTraceRoute':
            trace = q.get_trace(trace_id, ws)
            return {'traceId': trace_id, 'simulation': True, **route_query(body.get('query') or trace['query'])}
        if operation == 'getTraceRouteIndexes':
            trace = q.get_trace(trace_id, ws)
            release = k.get_release(trace['release_id'], ws)
            return {'releaseId': release['id'], 'profile': k.get_index_profile(release['index_profile_id'], ws), 'chunkCount': release['chunkCount']}
        if operation == 'getTraceRouteIntent':
            return t.stage(trace_id, 'parse', ws)['output']
        if operation == 'getRecallChannel':
            channel = next((item for item in t.stage(trace_id, 'recall', ws)['channels'] if item['id'] == path['channelId']), None)
            if not channel:
                raise AppError(404, 'NOT_FOUND', '召回通道不存在')
            return channel
        if operation in ('getFusionCandidates', 'filterRecall'):
            return t.candidates(trace_id, 'fusion' if operation == 'getFusionCandidates' else 'recall', params if operation == 'getFusionCandidates' else body, ws)
        chunk_getters = {'getRecallChunk': ('recall', 'chunkId'), 'getFusionChunk': ('fusion', 'candidateId'), 'getRerankChunk': ('rerank', 'chunkId')}
        if operation in chunk_getters:
            stage, identifier = chunk_getters[operation]
            return t.chunk(trace_id, stage, path[identifier], ws)
        if operation == 'getFusionCalculation':
            return t.calculation(trace_id, path['candidateId'], ws)
        if operation in ('exportRecall', 'exportFusion'):
            content, media_type = t.export(trace_id, 'recall' if operation == 'exportRecall' else 'fusion', params.get('format', 'json'), ws)
            return Response(content, media_type=media_type, headers={'Content-Disposition': 'attachment; filename="trace-export.' + ('csv' if params.get('format') == 'csv' else 'json') + '"'})
        if operation == 'compareRerank':
            trace = q.get_trace(trace_id, ws)
            before = t.stage(trace_id, 'rerank', ws)['candidates']
            if body.get('config'):
                t.save(trace_id, 'rerank', body['config'], ws, rid)
            result = await t.rerun(trace_id, 'rerank', body, ws, rid)
            return {'traceId': trace_id, 'derivedTraceId': result['trace']['id'], 'before': before, 'after': t.stage(result['trace']['id'], 'rerank', ws)['candidates']}
        if operation == 'getPromptVersions':
            q.get_trace(trace_id, ws)
            return db.all("SELECT * FROM trace_stage_drafts WHERE trace_id=? AND workspace_id=? AND stage='prompt' ORDER BY version DESC", trace_id, ws)
        if operation == 'scanPromptSensitiveData':
            return t.sensitive_scan(trace_id, ws)
        if operation == 'maskPrompt':
            return t.mask_prompt(trace_id, ws, rid)
        if operation in ('sendTraceFeedback', 'saveTraceQa'):
            trace = q.get_trace(trace_id, ws)
            return q.feedback(trace['message_id'], body, ws, rid) if operation == 'sendTraceFeedback' else p.wiki_from_message(trace['message_id'], body, ws, rid)
        raise RuntimeError('Unimplemented operation: ' + operation)

    def endpoint_for(operation):
        async def endpoint(request: Request):
            result = await dispatch(operation, request)
            return result if isinstance(result, Response) else envelope(result)
        endpoint.__name__ = operation
        return endpoint
    # Static segments must precede path parameters, e.g. /sources/recent and /traces/quick-parse.
    for operation, spec in sorted(catalog.items(), key=lambda pair: (pair[1]['path'].count(':'), -len(pair[1]['path']))):
        path = re.sub(r':(\w+)', r'{\1}', spec['path'])
        parameters = [{'name': name, 'in': 'path', 'required': True, 'schema': {'type': 'string'}} for name in re.findall(r':(\w+)', spec['path'])]
        extra = {'parameters': parameters}
        if spec['method'] not in ('GET', 'DELETE'):
            if operation in ('uploadDocument', 'uploadFile', 'uploadArchive'):
                extra['requestBody'] = {'required': True, 'content': {'multipart/form-data': {'schema': {'type': 'object', 'required': ['file'], 'properties': {'file': {'type': 'string', 'format': 'binary'}, 'datasetId': {'type': 'string'}, 'sourceId': {'type': 'string'}, 'folderId': {'type': 'string'}}}}}}
            else:
                extra['requestBody'] = {'content': {'application/json': {'schema': {'type': 'object', 'additionalProperties': True}}}}
        app.add_api_route(path, endpoint_for(operation), methods=[spec['method']], operation_id=operation, tags=[spec['path'].split('/')[3]], openapi_extra=extra)
    app.add_api_route('/api/v1/messages/{messageId}/wiki', endpoint_for('wikiFromMessage'), methods=['POST'], operation_id='wikiFromMessageLegacy')
