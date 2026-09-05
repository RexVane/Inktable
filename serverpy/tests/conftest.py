import sys
from pathlib import Path

import httpx
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ordo.app import create_app


@pytest_asyncio.fixture
async def api(tmp_path):
    app = create_app({'dataRoot': tmp_path / 'data', 'allowLocalModelEndpoints': True})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://localhost') as client:
            session = (await client.get('/api/v1/session/bootstrap')).json()['data']
            client.headers['x-ordo-csrf'] = session['csrfToken']
            yield app, client


async def call(client, method, path, body=None, **kwargs):
    response = await client.request(method, '/api/v1' + path, **({'json': body} if body is not None else {}), **kwargs)
    assert response.status_code < 400, (method, path, response.status_code, response.text)
    content = response.json()
    return content.get('data', content)


async def knowledge_fixture(client):
    kb = await call(client, 'POST', '/knowledge-bases', {'name': '测试知识库'})
    dataset_id = kb['default_dataset_id']
    upload = await call(client, 'POST', f'/datasets/{dataset_id}/files', files={'file': ('guide.md', '# 安装指南\n\n安装 Ordo 需要 Python 3.11 或以上版本。\n\n## 使用方法\n\n上传文档，解析后构建知识版本，再开始问答。\n\n## 配置说明\n\nOrdo 默认监听 127.0.0.1 的 8790 端口。'.encode(), 'text/markdown')})
    task = await call(client, 'GET', f'/tasks/{upload["task"]["id"]}/wait?timeoutMs=30000')
    assert task['status'] == 'succeeded', task
    release_task = await call(client, 'POST', f'/datasets/{dataset_id}/releases', {'activate': True})
    built = await call(client, 'GET', f'/tasks/{release_task["id"]}/wait?timeoutMs=30000')
    assert built['status'] == 'succeeded', built
    conversation = await call(client, 'POST', '/conversations', {'title': '测试', 'knowledgeBaseId': kb['id'], 'datasetId': dataset_id})
    answer = await call(client, 'POST', f'/conversations/{conversation["id"]}/messages', {'question': '如何安装 Ordo？'})
    assert answer['assistantMessage']['citations']
    return kb, dataset_id, upload, built['result']['releaseId'], conversation, answer
