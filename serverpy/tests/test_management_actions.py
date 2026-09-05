import pytest

from conftest import call, knowledge_fixture
from ordo.core import AppError


@pytest.mark.asyncio
async def test_conversation_rename_preserves_scope_and_history_and_is_audited(api):
    app, client = api
    kb, dataset, upload, release, conversation, answer = await knowledge_fixture(client)
    conversation_id = conversation['id']
    before = await call(client, 'GET', f'/conversations/{conversation_id}')
    renamed = await call(client, 'PATCH', f'/conversations/{conversation_id}', {'title': '  Renamed conversation  '})
    assert renamed['title'] == 'Renamed conversation'
    for key in ('id', 'workspace_id', 'knowledge_base_id', 'dataset_id', 'release_id', 'model_connection_id', 'strict_evidence', 'status', 'messages'):
        assert renamed[key] == before[key]
    assert any(row['id'] == conversation_id and row['title'] == renamed['title'] for row in await call(client, 'GET', '/conversations'))
    audit = app.state.services['db'].all("SELECT * FROM audit_events WHERE object_id=? AND action='conversation.update'", conversation_id)
    assert len(audit) == 1 and audit[0]['details']['changed'] == ['title']

    for title in ('', ' \t\n ', None, 0, False, [], {}):
        response = await client.patch(f'/api/v1/conversations/{conversation_id}', json={'title': title})
        assert response.status_code == 400 and response.json()['error']['code'] == 'VALIDATION_ERROR'
    for field in ('workspaceId', 'datasetId', 'releaseId', 'knowledgeBaseId', 'modelConnectionId', 'strictEvidence', 'status'):
        response = await client.patch(f'/api/v1/conversations/{conversation_id}', json={'title': 'Not applied', field: 'other'})
        assert response.status_code == 400
    assert (await call(client, 'GET', f'/conversations/{conversation_id}'))['title'] == renamed['title']
    assert len(app.state.services['db'].all("SELECT id FROM audit_events WHERE object_id=? AND action='conversation.update'", conversation_id)) == 1
    spec = (await client.get('/api/v1/openapi.json')).json()
    assert spec['paths']['/api/v1/conversations/{conversationId}']['patch']['operationId'] == 'updateConversation'


@pytest.mark.asyncio
async def test_management_actions_are_workspace_scoped_and_deleted_conversations_stay_deleted(api):
    app, client = api
    kb, dataset, upload, release, conversation, answer = await knowledge_fixture(client)
    services = app.state.services
    for operation in (
        lambda: services['query'].update_conversation(conversation['id'], {'title': 'Out of scope'}, 'ws_other'),
        lambda: services['query'].delete_conversation(conversation['id'], 'ws_other'),
        lambda: services['knowledge'].update_dataset(dataset, {'name': 'Out of scope'}, 'ws_other'),
        lambda: services['knowledge'].delete_dataset(dataset, 'ws_other'),
    ):
        with pytest.raises(AppError) as error:
            operation()
        assert error.value.status_code == 404 and error.value.code == 'NOT_FOUND'
    assert (await call(client, 'GET', f'/conversations/{conversation["id"]}'))['title'] == conversation['title']
    removed = await call(client, 'DELETE', f'/conversations/{conversation["id"]}')
    assert removed['deleted'] is True
    assert (await client.get(f'/api/v1/conversations/{conversation["id"]}')).status_code == 404
    assert (await client.patch(f'/api/v1/conversations/{conversation["id"]}', json={'title': 'Restore'})).status_code == 404
    assert (await client.delete(f'/api/v1/conversations/{conversation["id"]}')).status_code == 404
    assert all(row['id'] != conversation['id'] for row in await call(client, 'GET', '/conversations'))
    assert (await call(client, 'GET', f'/traces/{answer["trace"]["id"]}'))['id'] == answer['trace']['id']
    assert services['db'].one('SELECT COUNT(*) n FROM messages WHERE conversation_id=?', conversation['id'])['n'] == 2
    assert services['db'].one("SELECT COUNT(*) n FROM audit_events WHERE object_id=? AND action='conversation.delete'", conversation['id'])['n'] == 1


@pytest.mark.asyncio
async def test_dataset_rename_trims_names_rejects_invalid_input_and_returns_name_conflicts(api):
    app, client = api
    kb = await call(client, 'POST', '/knowledge-bases', {'name': 'Dataset management'})
    dataset = kb['default_dataset_id']
    other = await call(client, 'POST', f'/knowledge-bases/{kb["id"]}/datasets', {'name': 'Existing name'})
    renamed = await call(client, 'PATCH', f'/datasets/{dataset}', {'name': '  Renamed dataset  '})
    assert renamed['name'] == 'Renamed dataset'
    assert renamed['knowledge_base_id'] == kb['id']
    for name in ('', ' \t\n ', None, 0, False, [], {}):
        response = await client.patch(f'/api/v1/datasets/{dataset}', json={'name': name})
        assert response.status_code == 400 and response.json()['error']['code'] == 'VALIDATION_ERROR'
    conflict = await client.patch(f'/api/v1/datasets/{dataset}', json={'name': '  Existing name  '})
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'NAME_CONFLICT'
    assert (await call(client, 'GET', f'/datasets/{dataset}'))['name'] == 'Renamed dataset'
    unchanged = await call(client, 'PATCH', f'/datasets/{dataset}', {'name': ' Renamed dataset '})
    assert unchanged['name'] == 'Renamed dataset'
    another_kb = await call(client, 'POST', '/knowledge-bases', {'name': 'Other collection', 'defaultDatasetName': 'Independent name'})
    independent = await call(client, 'PATCH', f'/datasets/{dataset}', {'name': 'Independent name'})
    assert independent['knowledge_base_id'] == kb['id'] != another_kb['id']
    audit = app.state.services['db'].all("SELECT * FROM audit_events WHERE object_id=? AND action='dataset.update'", dataset)
    assert len(audit) == 3 and all(row['details']['changed'] == ['name'] for row in audit)


@pytest.mark.asyncio
async def test_deleted_assistants_do_not_block_dataset_deletion(api):
    app, client = api
    kb = await call(client, 'POST', '/knowledge-bases', {'name': 'Assistant deletion'})
    dataset = kb['default_dataset_id']
    source = await call(client, 'POST', f'/datasets/{dataset}/sources', {'name': 'Source to archive', 'type': 'upload'})
    assistant = await call(client, 'POST', '/assistants', {'name': 'Dependent assistant', 'datasetId': dataset})
    blocked = await client.delete(f'/api/v1/datasets/{dataset}')
    assert blocked.status_code == 409 and blocked.json()['error']['code'] == 'DEPENDENCY_CONFLICT'
    assert blocked.json()['error']['details']['assistants'] == 1
    assert app.state.services['db'].one("SELECT COUNT(*) n FROM audit_events WHERE object_id=? AND action='dataset.delete'", dataset)['n'] == 0
    await call(client, 'DELETE', f'/assistants/{assistant["id"]}')
    result = await call(client, 'DELETE', f'/datasets/{dataset}')
    assert result['deleted'] and result['dependencies']['assistants'] == 0
    assert (await client.get(f'/api/v1/datasets/{dataset}')).status_code == 404
    assert (await client.patch(f'/api/v1/datasets/{dataset}', json={'name': 'Restore'})).status_code == 404
    assert all(row['id'] != dataset for row in await call(client, 'GET', f'/knowledge-bases/{kb["id"]}/datasets'))
    assert app.state.services['db'].one('SELECT * FROM sources WHERE id=?', source['id'])['deleted_at'] is not None
    audit = app.state.services['db'].one("SELECT * FROM audit_events WHERE object_id=? AND action='dataset.delete'", dataset)
    assert audit and audit['details']['assistants'] == 0
    assert (await call(client, 'GET', '/audit/verify'))['valid']


@pytest.mark.asyncio
async def test_live_conversations_still_block_dataset_deletion_until_soft_deleted(api):
    app, client = api
    kb, dataset, upload, release, conversation, answer = await knowledge_fixture(client)
    blocked = await client.delete(f'/api/v1/datasets/{dataset}')
    assert blocked.status_code == 409 and blocked.json()['error']['code'] == 'DEPENDENCY_CONFLICT'
    assert blocked.json()['error']['details']['conversations'] == 1
    assert (await call(client, 'GET', f'/datasets/{dataset}'))['status'] == 'active'
    await call(client, 'DELETE', f'/conversations/{conversation["id"]}')
    result = await call(client, 'DELETE', f'/datasets/{dataset}')
    assert result['deleted'] is True and result['dependencies']['conversations'] == 0
    assert app.state.services['db'].one('SELECT id FROM knowledge_releases WHERE id=?', release)
    assert app.state.services['db'].one('SELECT id FROM query_traces WHERE id=?', answer['trace']['id'])
    assert (await call(client, 'GET', '/audit/verify'))['valid']
