"""Idempotent sample content for a newly installed local workspace."""
from .app import create_app


async def seed():
    app = create_app()
    async with app.router.lifespan_context(app):
        services = app.state.services
        db, k, config = services['db'], services['knowledge'], services['config']
        ws = config['localWorkspaceId']
        existing = db.one("SELECT id FROM knowledge_bases WHERE workspace_id=? AND name='Ordo 使用指南' AND status='active'", ws)
        if existing:
            print('Sample knowledge base already exists; existing data preserved.')
            return
        kb = k.create_knowledge_base({'name': 'Ordo 使用指南', 'description': '本地示例知识库'}, ws)
        dataset_id = kb['default_dataset_id']
        source = k.create_source(dataset_id, {'name': '示例使用指南', 'type': 'synthetic'}, ws)
        upload = k.register_upload(dataset_id, source['id'], 'Ordo 使用指南.md', '# Ordo 使用指南\n\nOrdo 使用 Python 和 FastAPI 提供本地知识服务。\n\n## 导入知识\n\n上传文档后完成解析，构建并激活知识版本，然后开始问答。\n\n## 引用溯源\n\n每个回答中的引用都指向不可变知识块及原始文档修订。'.encode('utf-8'), 'text/markdown', ws)
        task = await services['tasks'].wait(upload['task']['id'], ws, 120000)
        if task['status'] != 'succeeded':
            raise RuntimeError(task.get('error_message'))
        task = k.build_release(dataset_id, {'activate': True}, ws)
        task = await services['tasks'].wait(task['id'], ws, 120000)
        if task['status'] != 'succeeded':
            raise RuntimeError(task.get('error_message'))
        print('Sample knowledge base created:', kb['id'])
