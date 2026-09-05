import base64
import hashlib
import hmac
import io
import json
import re
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest

from conftest import call, knowledge_fixture


@pytest.mark.asyncio
async def test_contract_and_auth(api):
    app, client = api
    spec = (await client.get('/api/v1/openapi.json')).json()
    catalog = json.loads(Path(__file__).resolve().parents[1].joinpath('ordo/api_contract.json').read_text())
    frontend = Path(__file__).resolve().parents[2].joinpath('web/api.js').read_text('utf-8')
    definitions = {name: {'method': method, 'path': '/api/v1' + path} for name, method, path in re.findall(r"^    (\w+): \['(GET|POST|PATCH|PUT|DELETE)', '([^']+)'\]", frontend, re.M)}
    assert catalog == definitions
    for name, operation in catalog.items():
        route = re.sub(r':(\w+)', r'{\1}', operation['path'])
        assert spec['paths'][route][operation['method'].lower()]['operationId'] == name
    assert len(catalog) == 224
    response = await client.post('/api/v1/knowledge-bases', json={'name': 'x'}, headers={'x-ordo-csrf': 'wrong'})
    assert response.status_code == 403 and response.json()['error']['code'] == 'CSRF_INVALID'
    response = await client.post('/api/v1/knowledge-bases', json=[])
    assert response.status_code == 400
    assert (await client.get('/api/v1/absent')).status_code == 404
    assert (await client.get('/')).status_code == 200
    assert (await client.get('/api.js')).status_code == 200


@pytest.mark.asyncio
async def test_every_contract_resolves_to_a_handler(api):
    app, client = api
    catalog = json.loads(Path(__file__).resolve().parents[1].joinpath('ordo/api_contract.json').read_text())
    failures = []
    for name, operation in catalog.items():
        path = re.sub(r':\w+', 'missing', operation['path'])
        response = await client.request(operation['method'], path, **({'json': {}} if operation['method'] != 'GET' else {}))
        if response.status_code >= 500:
            failures.append((name, response.status_code, response.text[:400]))
        elif response.status_code in (404, 405) and response.json().get('error', {}).get('code') == 'ROUTE_NOT_FOUND':
            failures.append((name, 'unregistered route'))
    assert not failures, failures


@pytest.mark.asyncio
async def test_knowledge_query_revisions_diagnostics_and_backup(api, tmp_path):
    app, client = api
    kb, dataset, upload, release, conversation, answer = await knowledge_fixture(client)
    trace_id = answer['trace']['id']
    citation = answer['assistantMessage']['citations'][0]
    opened = await call(client, 'GET', f'/citations/{citation["id"]}')
    assert opened
    original = await call(client, 'GET', f'/chunks/{citation["chunk_revision_id"]}')
    revised = await call(client, 'POST', f'/chunks/{original["id"]}/revisions', {'contentMd': 'Python FastAPI 修改后的安装说明。'})
    assert revised['id'] != original['id']
    assert (await call(client, 'GET', f'/chunks/{original["id"]}'))['content_md'] == original['content_md']
    response = await client.post('/api/v1/chunks/' + original['id'] + '/revisions', json={'contentMd': 'stale'})
    assert response.status_code == 409
    assert original['id'] in [row['chunkRevisionId'] for row in (await call(client, 'POST', f'/releases/{release}/search', {'query': 'Python'}))['results']]
    for stage in ('parse', 'embed', 'route', 'recall', 'fusion', 'rerank', 'prompt', 'generation'):
        result = await call(client, 'GET', f'/traces/{trace_id}/stages/{stage}')
        assert result['traceId'] == trace_id
    fusion = await call(client, 'GET', f'/traces/{trace_id}/stages/fusion')
    assert fusion['totalCandidates'] == answer['trace']['metrics']['candidateCount']
    assert (await client.get(f'/api/v1/traces/{trace_id}/stages/rerank/chunks/absent')).status_code == 404
    await call(client, 'PUT', f'/traces/{trace_id}/stages/fusion/weights', {'denseWeight': 0, 'sparseWeight': 1})
    replay = await call(client, 'POST', f'/traces/{trace_id}/stages/fusion/rerun', {'idempotencyKey': 'replay-one'})
    assert replay['trace']['parent'] == trace_id
    assert replay['trace']['id'] != trace_id
    again = await call(client, 'POST', f'/traces/{trace_id}/stages/fusion/rerun', {'idempotencyKey': 'replay-one'})
    assert again['trace']['id'] == replay['trace']['id'] and again['idempotent']
    await call(client, 'GET', f'/traces/{trace_id}/compare/{replay["trace"]["id"]}')
    wiki = await call(client, 'POST', f'/wiki/from-message/{answer["assistantMessage"]["id"]}', {'title': '安装说明'})
    assert wiki['revisions'][0]['sources']
    await call(client, 'POST', f'/wiki/{wiki["id"]}', {'contentMd': '修订说明', 'publish': True})
    backup = await call(client, 'POST', '/backups', {'idempotencyKey': 'backup-one'})
    done = await call(client, 'GET', f'/tasks/{backup["id"]}/wait?timeoutMs=30000')
    assert done['status'] == 'succeeded', done
    backup_id = done['result']['backupId']
    restore = await call(client, 'POST', f'/backups/{backup_id}/restore', {'targetRoot': str(tmp_path / 'restored')})
    restored = await call(client, 'GET', f'/tasks/{restore["id"]}/wait?timeoutMs=30000')
    assert restored['status'] == 'succeeded', restored.get('error_message')
    with sqlite3.connect(tmp_path / 'restored/metadata/ordo.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM citations').fetchone()[0] > 0
    assert (await call(client, 'GET', '/audit/verify'))['valid']


@pytest.mark.asyncio
async def test_registered_files_folders_parsing_and_indexes(api):
    app, client = api
    registration = await call(client, 'POST', '/files', files={'file': ('unassigned.txt', b'Python FastAPI source text.', 'text/plain')})
    assert registration['status'] == 'unassigned'
    kb = await call(client, 'POST', '/knowledge-bases', {'name': '文件测试'})
    dataset = kb['default_dataset_id']
    await call(client, 'PATCH', '/parsing/settings', {'autoParsingEnabled': False, 'concurrency': 2})
    assigned = await call(client, 'PATCH', f'/sources/{registration["id"]}/dataset', {'datasetId': dataset})
    task_id = assigned['task']['id']
    assert (await call(client, 'GET', f'/tasks/{task_id}'))['status'] == 'queued'
    await call(client, 'POST', '/parsing/start', {'knowledgeBaseId': kb['id'], 'profileId': 'profile_fast_text'})
    assert (await call(client, 'GET', f'/tasks/{task_id}/wait?timeoutMs=30000'))['status'] == 'succeeded'
    doc_id = assigned['document']['id']
    folder = await call(client, 'POST', f'/datasets/{dataset}/folders', {'name': 'docs'})
    await call(client, 'PATCH', f'/datasets/{dataset}/files/{doc_id}/move', {'folderId': folder['id']})
    files = await call(client, 'GET', f'/datasets/{dataset}/files?folderId={folder["id"]}')
    assert files[0]['id'] == doc_id
    assert (await call(client, 'GET', f'/datasets/{dataset}/tree'))[0]['fileCount'] == 1
    for suffix in ('preview/pages', 'pages/1', 'pages/1/inspect', 'pages/1/diff'):
        await call(client, 'GET', f'/documents/{doc_id}/{suffix}')
    for suffix in ('vectorize-pending', 'rebuild-hnsw', 'optimize-index', 'rebuild-bm25'):
        result = await call(client, 'POST', f'/datasets/{dataset}/indexing/{suffix}', {})
        assert result['status'] == 'success'
    chunks = await call(client, 'GET', f'/datasets/{dataset}/chunks')
    split = await call(client, 'POST', f'/chunks/{chunks[0]["id"]}/split', {'parts': ['Python source', 'FastAPI source']})
    merged = await call(client, 'POST', '/chunks/merge', {'chunkIds': [item['id'] for item in split['children']]})
    assert 'Python source' in merged['merged']['content_text']
    await call(client, 'GET', '/system/resources')
    assert (await client.post('/api/v1/parsing/start', json={'profileId': 'profile_ocr'})).status_code == 422


@pytest.mark.asyncio
async def test_connectors_archive_graph_and_widget(api, tmp_path):
    app, client = api
    kb, dataset, upload, release, conversation, answer = await knowledge_fixture(client)
    external = tmp_path / 'external.sqlite3'
    with sqlite3.connect(external) as db:
        db.execute('CREATE TABLE products(name TEXT)')
        db.execute("INSERT INTO products VALUES('Ordo')")
    probe = await call(client, 'POST', '/connectors/test', {'type': 'sqlite', 'path': str(external)})
    assert probe['available'] and not probe['persisted']
    connector = await call(client, 'POST', '/connectors', {'name': 'sqlite test', 'type': 'sqlite', 'path': str(external)})
    await call(client, 'POST', f'/connectors/{connector["id"]}/test', {})
    await call(client, 'GET', f'/connectors/{connector["id"]}/schema')
    template = await call(client, 'POST', f'/connectors/{connector["id"]}/templates', {'name': 'products', 'sql': 'SELECT name FROM products', 'params': []})
    results = await call(client, 'POST', f'/query-templates/{template["id"]}/execute', {'values': []})
    assert results['rows'][0]['name'] == 'Ordo'
    snapshot = await call(client, 'POST', f'/query-templates/{template["id"]}/snapshot', {'datasetId': dataset})
    assert (await call(client, 'GET', f'/tasks/{snapshot["task"]["id"]}/wait?timeoutMs=30000'))['status'] == 'succeeded'
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, 'w') as archive:
        archive.writestr('extra.txt', 'Additional knowledge for Ordo.')
    imported = await call(client, 'POST', f'/datasets/{dataset}/archives', files={'file': ('docs.zip', memory.getvalue(), 'application/zip')})
    assert (await call(client, 'GET', f'/tasks/{imported["task"]["id"]}/wait?timeoutMs=30000'))['status'] == 'succeeded'
    assistant = await call(client, 'POST', '/assistants', {'name': '网站助手', 'datasetId': dataset})
    await call(client, 'POST', f'/assistants/{assistant["id"]}/publish', {})
    widget = await call(client, 'POST', f'/assistants/{assistant["id"]}/clients', {'name': 'website', 'allowedOrigins': ['https://example.com']})
    timestamp, nonce, origin, raw = str(int(time.time()*1000)), 'nonce-test-one-123456', 'https://example.com', '{}'
    canonical = '\n'.join(['POST', '/api/v1/public/widget/token', timestamp, nonce, origin, hashlib.sha256(raw.encode()).hexdigest()])
    signature = hmac.new(widget['clientSecret'].encode(), canonical.encode(), hashlib.sha256).hexdigest()
    headers = {'origin': origin, 'content-type': 'application/json', 'x-ordo-client': widget['clientId'], 'x-ordo-timestamp': timestamp, 'x-ordo-nonce': nonce, 'x-ordo-signature': signature}
    token = await call(client, 'POST', '/public/widget/token', content=raw, headers=headers)
    assert (await client.post('/api/v1/public/widget/token', content=raw, headers=headers)).status_code == 409
    visitor_headers = {'origin': origin, 'authorization': 'Bearer ' + token['token']}
    visitor = await call(client, 'POST', '/public/widget/sessions', {}, headers=visitor_headers)
    visitor_id = visitor.get('id') or visitor.get('sessionId')
    result = await call(client, 'POST', f'/public/widget/sessions/{visitor_id}/messages', {'question': '如何安装 Ordo？'}, headers=visitor_headers)
    assert result['citations']
    handoff = await call(client, 'POST', f'/public/widget/sessions/{visitor_id}/handoff', {'summary': '需要帮助'}, headers=visitor_headers)
    assert handoff
    await call(client, 'DELETE', f'/assistants/{assistant["id"]}')
    assert (await client.post(f'/api/v1/public/widget/sessions/{visitor_id}/messages', json={'question': 'Ordo'}, headers=visitor_headers)).status_code >= 400
