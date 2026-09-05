import asyncio
import base64
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import zipfile

import httpx
import pytest

from conftest import call, knowledge_fixture
from ordo.app import create_app
from ordo.core import AppError, now
from ordo.config import resolve_config
from ordo.db import MIGRATIONS
from ordo.models import timed_fetch
from ordo.storage import ensure_data_layout


@pytest.mark.asyncio
async def test_directory_snapshot_pause_and_archive_traversal(api, tmp_path):
    app, client = api
    kb = await call(client, 'POST', '/knowledge-bases', {'name': 'Import boundaries'})
    dataset, services = kb['default_dataset_id'], app.state.services
    folder = tmp_path / 'authorized'
    folder.mkdir()
    (folder / 'file.txt').write_text('Authorized content', 'utf-8')
    preview = await call(client, 'POST', f'/datasets/{dataset}/directory/preview', {'directory': str(folder)})
    assert preview['count'] == 1
    imported = await call(client, 'POST', f'/datasets/{dataset}/directory/import', {'directory': str(folder)})
    assert (await call(client, 'GET', f'/tasks/{imported["task"]["id"]}/wait?timeoutMs=30000'))['status'] == 'succeeded'
    # Exercise the persisted authorized snapshot after the source is replaced.
    original = services['tasks'].get(imported['task']['id'])['input']
    (folder / 'file.txt').write_text('Changed file after authorization', 'utf-8')
    async def checkpoint(*args):
        pass
    result = await services['ingest']._directory_task({'input': original, 'workspaceId': 'ws_local', 'checkpoint': checkpoint})
    assert result['status'] == 'partial' and result['manifest'][0]['code'] == 'DIRECTORY_FILE_CHANGED'
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, 'w') as archive:
        archive.writestr('../outside.txt', 'forbidden')
    imported = await call(client, 'POST', f'/datasets/{dataset}/archives', files={'file': ('unsafe.zip', memory.getvalue(), 'application/zip')})
    task = await call(client, 'GET', f'/tasks/{imported["task"]["id"]}/wait?timeoutMs=30000')
    assert task['status'] == 'failed'
    assert not (tmp_path / 'outside.txt').exists()
    await call(client, 'PATCH', '/parsing/settings', {'autoParsingEnabled': False})
    pending = await call(client, 'POST', f'/datasets/{dataset}/files', files={'file': ('paused.txt', b'Paused work', 'text/plain')})
    paused = await call(client, 'POST', '/parsing/pause', {'datasetId': dataset})
    assert paused['changedCount'] >= 1
    assert (await call(client, 'GET', f'/tasks/{pending["task"]["id"]}'))['status'] == 'paused'
    await call(client, 'POST', '/parsing/resume', {'datasetId': dataset})
    assert (await call(client, 'GET', f'/tasks/{pending["task"]["id"]}/wait?timeoutMs=30000'))['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_pdf_preview_and_encrypted_pdf_status(api):
    import pymupdf
    app, client = api
    kb = await call(client, 'POST', '/knowledge-bases', {'name': 'PDF tests'})
    dataset = kb['default_dataset_id']
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), 'Python FastAPI test document')
        original = pdf.tobytes()
        encrypted = pdf.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='password')
    uploaded = await call(client, 'POST', f'/datasets/{dataset}/files', files={'file': ('page.pdf', original, 'application/pdf')})
    assert (await call(client, 'GET', f'/tasks/{uploaded["task"]["id"]}/wait?timeoutMs=30000'))['status'] == 'succeeded'
    page = await call(client, 'GET', f'/documents/{uploaded["document"]["id"]}/pages/1')
    assert page['imageUrl'].startswith('data:image/png;base64,') and page['bboxes']
    uploaded = await call(client, 'POST', f'/datasets/{dataset}/files', files={'file': ('locked.pdf', encrypted, 'application/pdf')})
    task = await call(client, 'GET', f'/tasks/{uploaded["task"]["id"]}/wait?timeoutMs=30000')
    assert task['status'] == 'failed' and task['error_code'] == 'NEEDS_PASSWORD'
    document = await call(client, 'GET', f'/documents/{uploaded["document"]["id"]}')
    assert document['status'] == 'needs_password'


@pytest.mark.asyncio
async def test_model_transport_streams_before_response_ends_and_rejects_redirects():
    release = asyncio.Event()
    seen = asyncio.Event()
    requests = []
    async def handler(reader, writer):
        raw = await reader.readuntil(b'\r\n\r\n')
        requests.append(raw)
        if b'/redirect ' in raw:
            writer.write(b'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 0\r\n\r\n')
        else:
            first, last = b'data: {"delta":"first"}\n\n', b'data: [DONE]\n\n'
            writer.write(f'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {len(first)+len(last)}\r\n\r\n'.encode() + first)
            await writer.drain()
            await release.wait()
            writer.write(last)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    async with server:
        with pytest.raises(AppError) as blocked:
            await timed_fetch(url)
        assert blocked.value.code == 'ENDPOINT_PRIVATE_BLOCKED'
        fetch = asyncio.create_task(timed_fetch(url, allow_local=True, on_line=lambda line: seen.set() if 'first' in line else None))
        try:
            await asyncio.wait_for(seen.wait(), 3)
            assert not fetch.done(), 'Token callback must run before the upstream body finishes'
        finally:
            release.set()
        response = await fetch
        assert response.ok
        with pytest.raises(AppError) as redirect:
            await timed_fetch(url + '/redirect', allow_local=True)
        assert redirect.value.code == 'MODEL_REDIRECT_REJECTED'


@pytest.mark.asyncio
async def test_graph_scope_flags_and_replay_idempotency(api):
    app, client = api
    kb, dataset, upload, release, conversation, answer = await knowledge_fixture(client)
    ontology = await call(client, 'POST', f'/knowledge-bases/{kb["id"]}/ontologies', {'name': 'Products', 'schema': {'entityTypes': ['Product'], 'relationTypes': ['uses']}, 'publish': True})
    chunk_id = answer['assistantMessage']['citations'][0]['chunk_revision_id']
    def entity(name):
        return {'name': name, 'ontologyVersionId': ontology['id'], 'entityType': 'Product', 'sourceChunkId': chunk_id}
    first = await call(client, 'POST', f'/datasets/{dataset}/graph/entities', entity('Ordo'))
    second = await call(client, 'POST', f'/datasets/{dataset}/graph/entities', entity('Python'))
    await call(client, 'POST', f'/datasets/{dataset}/graph/relations', {'ontologyVersionId': ontology['id'], 'relationType': 'uses', 'sourceEntityId': first['id'], 'targetEntityId': second['id'], 'sourceChunkId': chunk_id})
    assert len((await call(client, 'GET', f'/datasets/{dataset}/graph'))['relations']) == 1
    assert len(await call(client, 'GET', f'/datasets/{dataset}/graph/entities?q=Ordo')) == 1
    await call(client, 'PUT', '/feature-flags/graph', {'enabled': False})
    assert (await client.get(f'/api/v1/datasets/{dataset}/graph')).status_code == 403
    trace = answer['trace']['id']
    left, right = await asyncio.gather(*[call(client, 'POST', f'/traces/{trace}/replay', {'idempotencyKey': 'parallel'}) for _ in range(2)])
    assert left['trace']['id'] == right['trace']['id']
    assert left['idempotent'] != right['idempotent']
    conflict = await client.post(f'/api/v1/traces/{trace}/replay', json={'idempotencyKey': 'parallel', 'overrides': {'question': 'Changed input'}})
    assert conflict.status_code == 409
    assert (await call(client, 'GET', '/audit/verify'))['valid']


@pytest.mark.asyncio
async def test_schema_four_data_and_secrets_survive_python_upgrade(tmp_path):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    config = resolve_config({'dataRoot': tmp_path / 'legacy'})
    ensure_data_layout(config)
    with closing(sqlite3.connect(config['dbPath'])) as db:
        db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL) STRICT')
        for migration in MIGRATIONS[:4]:
            db.executescript(migration['sql'])
            db.execute('INSERT INTO schema_migrations VALUES(?,?,?)', (migration['version'], migration['name'], now()))
            db.commit()
    master = bytes(range(32))
    config['keyPath'].write_bytes(master)
    app = create_app({'dataRoot': config['dataRoot']})
    async with app.router.lifespan_context(app):
        service = app.state.services
        assert service['product'].schema_version() == 5
        # Node packs iv + tag + ciphertext rather than cryptography's ciphertext + tag.
        iv = bytes(range(12))
        encrypted = AESGCM(master).encrypt(iv, b'legacy-api-key', None)
        payload = base64.b64encode(iv + encrypted[-16:] + encrypted[:-16]).decode()
        assert service['secretStore'].decrypt(payload) == 'legacy-api-key'
        kb = service['knowledge'].create_knowledge_base({'name': 'Preserve on restart'}, 'ws_local')
    restarted = create_app({'dataRoot': config['dataRoot']})
    async with restarted.router.lifespan_context(restarted):
        assert restarted.state.services['knowledge'].get_knowledge_base(kb['id'], 'ws_local')['name'] == 'Preserve on restart'
        assert restarted.state.services['audit'].verify('ws_local')['valid']


@pytest.mark.asyncio
async def test_backup_tampering_and_existing_target_do_not_change_live_data(api, tmp_path):
    app, client = api
    kb = await call(client, 'POST', '/knowledge-bases', {'name': 'Backup safety'})
    task = await call(client, 'POST', '/backups', {'idempotencyKey': 'safe-backup'})
    task = await call(client, 'GET', f'/tasks/{task["id"]}/wait?timeoutMs=30000')
    assert task['status'] == 'succeeded'
    backup_id = task['result']['backupId']
    existing = await client.post(f'/api/v1/backups/{backup_id}/restore', json={'targetRoot': str(tmp_path)})
    assert existing.status_code == 400
    backup_file = app.state.services['config']['backupRoot'] / task['result']['storageKey']
    with backup_file.open('r+b') as stream:
        stream.seek(24)
        stream.write(b'TAMPERED')
    restore = await call(client, 'POST', f'/backups/{backup_id}/restore', {'targetRoot': str(tmp_path / 'restore')})
    restore = await call(client, 'GET', f'/tasks/{restore["id"]}/wait?timeoutMs=30000')
    assert restore['status'] == 'failed' and restore['error_code'] == 'BACKUP_CHECKSUM_INVALID'
    assert not (tmp_path / 'restore').exists()
    assert (await call(client, 'GET', f'/knowledge-bases/{kb["id"]}'))['name'] == 'Backup safety'
