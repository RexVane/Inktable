from copy import deepcopy
import json
import math
from types import SimpleNamespace

import pytest

from conftest import call, knowledge_fixture
from ordo.knowledge import local_embedding
from ordo.models import ModelService
from ordo.trace_workbench import STAGES


@pytest.mark.asyncio
async def test_recorded_stages_keep_the_original_history_and_evidence(api):
    app, client = api
    _, _, _, _, conversation, answer = await knowledge_fixture(client)
    original = answer['trace']
    trace_id = original['id']
    second = await call(client, 'POST', f'/conversations/{conversation["id"]}/messages', {'question': 'Later question about the listening port'})
    stages = {key: await call(client, 'GET', f'/traces/{trace_id}/stages/{key}') for key in STAGES}

    assert stages['parse']['contextSource'] == 'recorded'
    assert stages['parse']['inputHistory'] == []
    assert stages['parse']['contextMessages'] == [{'role': 'user', 'content': original['query']}]
    next_parse = await call(client, 'GET', f'/traces/{second["trace"]["id"]}/stages/parse')
    assert next_parse['inputHistory'] == [
        {'role': 'user', 'content': original['query']},
        {'role': 'assistant', 'content': answer['assistantMessage']['content']},
    ]
    assert next_parse['contextMessages'][-1]['content'] == second['trace']['query']

    embedding = stages['embed']
    assert embedding['vector'] == local_embedding(original['query'])
    assert embedding['dimensions'] == len(embedding['vector'])
    assert embedding['norm'] == pytest.approx(math.sqrt(sum(value ** 2 for value in embedding['vector'])))
    assert embedding['dataSource'] == 'recorded' and not embedding['reconstructed']
    assert embedding['cacheHit'] is False and embedding['cacheStatus'] == 'disabled'
    vector = await call(client, 'GET', f'/traces/{trace_id}/stages/embed/vector')
    assert vector['model'] == embedding['model'] and vector['vector'] == embedding['vector']

    prompt, generation = stages['prompt'], stages['generation']
    assert prompt['messages'] == prompt['output']['messages']
    assert prompt['historyCount'] == 0
    assert [item['chunkRevisionId'] for item in prompt['evidence']] == [item['chunkRevisionId'] for item in stages['rerank']['selected']]
    assert generation['answer'] == answer['assistantMessage']['content']
    assert generation['messages'] == prompt['messages']
    assert generation['usage'] is None and generation['usageStatus'] == 'not_applicable'
    assert generation['promptUsedByProvider'] is False
    assert generation['status'] == next(stage['status'] for stage in original['stages'] if stage['name'] == STAGES['generation'])

    for channel in stages['recall']['channels']:
        assert channel['enabled'] == stages['recall']['output']['routes'][channel['id']]
        assert channel['count'] == len(channel['candidates'])
        assert all(channel['id'] in item['channels'] for item in channel['candidates'])
    assert stages['recall']['candidateScope'] == 'returned_results'
    assert stages['fusion']['rawCandidateCount'] is None
    assert stages['fusion']['permissionFilteredCount'] is None
    assert all(item['selected'] is True for item in stages['rerank']['candidates'][:stages['rerank']['selectedCount']])
    assert await call(client, 'GET', f'/traces/{trace_id}') == original


@pytest.mark.asyncio
async def test_stage_drafts_apply_only_to_derived_runs_and_are_inherited(api):
    app, client = api
    _, _, _, _, _, answer = await knowledge_fixture(client)
    trace_id = answer['trace']['id']
    question = 'Ordo account@example.test'
    parse_config = {'rewrittenQuery': question, 'original': 'forged original',
                    'inputHistory': [{'role': 'user', 'content': 'forged future'}],
                    'contextMessages': [{'role': 'user', 'content': 'forged context'}]}
    saved = await call(client, 'PUT', f'/traces/{trace_id}/stages/parse/raw-json', parse_config)
    assert saved['saved'] and not saved['applied']
    await call(client, 'PUT', f'/traces/{trace_id}/stages/fusion/weights', {'denseWeight': 0, 'sparseWeight': 1, 'k': 31})
    await call(client, 'PUT', f'/traces/{trace_id}/stages/prompt', {'instructions': 'Trace draft instruction', 'maxEvidenceChars': 100, 'maskSensitive': True})
    assert await call(client, 'GET', f'/traces/{trace_id}') == answer['trace']

    derived = await call(client, 'POST', f'/traces/{trace_id}/stages/fusion/rerun', {'idempotencyKey': 'truth-draft-run'})
    derived_id = derived['trace']['id']
    assert derived['executionMode'] == 'full_pipeline'
    assert derived['trace']['parent'] == trace_id and derived_id != trace_id
    assert derived['trace']['query'] == question
    parse = await call(client, 'GET', f'/traces/{derived_id}/stages/parse')
    assert parse['originalQuery'] == question
    assert parse['rewrittenQuery'] == question
    assert parse['inputHistory'] == []
    assert parse['contextMessages'] == [{'role': 'user', 'content': question}]
    fusion = await call(client, 'GET', f'/traces/{derived_id}/stages/fusion')
    assert fusion['weights'] == {'denseWeight': 0, 'sparseWeight': 1}
    assert fusion['k'] == 31
    prompt = await call(client, 'GET', f'/traces/{derived_id}/stages/prompt')
    assert prompt['maxEvidenceChars'] == 100
    assert 'Trace draft instruction' in prompt['messages'][0]['content']
    assert '[EMAIL]' in prompt['messages'][-1]['content']
    assert 'account@example.test' not in prompt['prompt']

    repeated = await call(client, 'POST', f'/traces/{trace_id}/stages/fusion/rerun', {'idempotencyKey': 'truth-draft-run'})
    assert repeated['trace']['id'] == derived_id and repeated['idempotent']
    inherited = await call(client, 'POST', f'/traces/{derived_id}/stages/fusion/rerun', {})
    inherited_config = inherited['trace']['config_snapshot']['stageOverrides']
    assert inherited_config['fusion']['k'] == 31
    assert inherited_config['prompt']['maskSensitive'] is True
    assert inherited['trace']['query'] == question
    direct = await call(client, 'POST', f'/traces/{derived_id}/replay', {
        'fromStage': 'prompt', 'overrides': {'stageOverrides': {'prompt': {'instructions': 'Direct replay instruction'}}},
    })
    assert direct['trace']['config_snapshot']['stageOverrides']['fusion']['k'] == 31
    assert direct['trace']['config_snapshot']['stageOverrides']['prompt']['maskSensitive'] is True
    assert direct['trace']['query'] == question
    assert await call(client, 'GET', f'/traces/{trace_id}') == answer['trace']


@pytest.mark.asyncio
async def test_parse_scope_cannot_be_changed_by_drafts_or_direct_replay(api):
    app, client = api
    _, _, _, _, _, answer = await knowledge_fixture(client)
    trace_id = answer['trace']['id']
    for key in ('workspaceId', 'datasetId', 'releaseId'):
        config = {'filters': {key: 'outside-the-recorded-scope'}}
        response = await client.put(f'/api/v1/traces/{trace_id}/stages/parse', json=config)
        assert response.status_code == 400 and response.json()['error']['code'] == 'SCOPE_MISMATCH'
        response = await client.post(f'/api/v1/traces/{trace_id}/replay', json={'overrides': {'stageOverrides': {'parse': config}}})
        assert response.status_code == 400 and response.json()['error']['code'] == 'SCOPE_MISMATCH'
    assert await call(client, 'GET', f'/traces/{trace_id}') == answer['trace']


@pytest.mark.asyncio
async def test_unexecuted_and_failed_stages_are_not_reconstructed(api, monkeypatch):
    app, client = api
    _, _, _, _, _, answer = await knowledge_fixture(client)
    services = app.state.services
    trace = deepcopy(answer['trace'])
    monkeypatch.setattr(services['query'], 'get_trace', lambda *args: deepcopy(trace))
    ws = services['config']['localWorkspaceId']
    for status in ('failed', 'cancelled', 'unavailable', 'running'):
        trace['stages'] = [
            {'key': 'embed', 'status': status, 'output': {'model': 'ordo-hash-embedding-v1'}},
            {'key': 'prompt', 'status': status, 'output': {'templateVersion': 'strict-evidence-v1'}},
            {'key': 'rerank', 'status': 'succeeded', 'output': {'selected': []}},
        ]
        embed = services['traces'].stage(trace['id'], 'embed', ws)
        prompt = services['traces'].stage(trace['id'], 'prompt', ws)
        assert embed['vector'] == [] and embed['norm'] is None
        assert embed['status'] == status and not embed['reconstructed']
        assert prompt['messages'] == [] and prompt['prompt'] == '' and not prompt['reconstructed']
        assert prompt['status'] == status
        scatter = services['traces'].embedding_action(trace['id'], 'getEmbeddingScatter', {}, ws)
        assert scatter['points'] == [] and scatter['dataSource'] == 'unavailable'
    trace['stages'] = []
    for key in ('embed', 'prompt'):
        stage = services['traces'].stage(trace['id'], key, ws)
        assert stage['status'] == 'unavailable' and stage['dataSource'] == 'unavailable'
        assert not stage['reconstructed']


@pytest.mark.asyncio
async def test_legacy_reconstruction_is_labeled_and_never_reads_new_history(api, monkeypatch):
    app, client = api
    _, _, _, _, _, answer = await knowledge_fixture(client)
    services = app.state.services
    trace = deepcopy(answer['trace'])
    for stage in trace['stages']:
        if stage['name'] == STAGES['embed']:
            for key in ('vector', 'cacheHit', 'cacheStatus'):
                stage['output'].pop(key, None)
        if stage['name'] == STAGES['prompt']:
            stage['output'].pop('messages')
        if stage['name'] == STAGES['parse']:
            for key in ('inputHistory', 'contextMessages', 'contextSource'):
                stage['output'].pop(key, None)
    monkeypatch.setattr(services['query'], 'get_trace', lambda *args: deepcopy(trace))
    ws = services['config']['localWorkspaceId']
    embed = services['traces'].stage(trace['id'], 'embed', ws)
    assert embed['dataSource'] == 'reconstructed' and embed['reconstructed']
    assert embed['vector'] == local_embedding(trace['query'])
    assert embed['cacheHit'] is None and embed['cacheStatus'] == 'unrecorded'
    prompt = services['traces'].stage(trace['id'], 'prompt', ws)
    assert prompt['dataSource'] == 'reconstructed' and prompt['reconstructed']
    trace['input_snapshot'] = {}
    missing_context = services['traces'].stage(trace['id'], 'parse', ws)
    assert missing_context['contextSource'] == 'unavailable'
    assert missing_context['contextMessages'] == [{'role': 'user', 'content': trace['query']}]
    missing_prompt = services['traces'].stage(trace['id'], 'prompt', ws)
    assert missing_prompt['messages'] == [] and not missing_prompt['reconstructed']


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['openai-compatible', 'ollama'])
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('has_usage', [False, True])
async def test_model_usage_and_finish_reason_are_only_recorded_when_returned(monkeypatch, provider, streaming, has_usage):
    record = {'provider': provider, 'model_id': 'fixture-model', 'base_url': 'http://127.0.0.1:9', 'config': {}}
    service = ModelService(None, None, None, {'localWorkspaceId': 'fixture', 'allowLocalModelEndpoints': True})
    monkeypatch.setattr(service, 'get', lambda *args, **kwargs: record)
    answer = 'Recorded provider response'
    usage = {'prompt_tokens': 17, 'completion_tokens': 5, 'total_tokens': 22}
    if provider == 'ollama':
        payload = {'message': {'content': answer}, 'done': True}
        if has_usage:
            payload.update(prompt_eval_count=17, eval_count=5, done_reason='stop')
    else:
        payload = {'choices': [{'message': {'content': answer}, 'delta': {'content': answer}}]}
        if has_usage:
            payload['usage'] = usage
            payload['choices'][0]['finish_reason'] = 'stop'

    async def fake_fetch(url, request, timeout, allow_local, on_line=None):
        body = json.loads(request['body'])
        assert body['model'] == 'fixture-model' and body['stream'] is streaming
        if on_line:
            on_line(json.dumps(payload) if provider == 'ollama' else 'data: ' + json.dumps(payload))
            if provider != 'ollama':
                on_line('data: [DONE]')
        return SimpleNamespace(ok=True, status=200, json=lambda: payload)

    monkeypatch.setattr('ordo.models.timed_fetch', fake_fetch)
    tokens = []
    result = await service.generate(connection_id='fixture', question='Question', evidence=[], strict_evidence=False,
                                    on_token=tokens.append if streaming else None)
    assert result['content'] == answer
    assert result['usage'] == (usage if has_usage else None)
    assert result['finishReason'] == ('stop' if has_usage else None)
    if streaming:
        assert ''.join(tokens) == answer
