import json

from .core import AppError, gen_id, now, required


class GraphService:
    def __init__(self, db, knowledge, audit, config):
        self.db = db
        self.knowledge = knowledge
        self.audit = audit
        self.config = config

    def list_ontologies(self, knowledge_base_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        self.knowledge.ensure_kb(knowledge_base_id, workspace_id)
        return self.db.all('SELECT * FROM ontology_versions WHERE knowledge_base_id=? AND workspace_id=? ORDER BY version DESC',
                           knowledge_base_id, workspace_id)

    def create_ontology(self, knowledge_base_id, input, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        self.knowledge.ensure_kb(knowledge_base_id, workspace_id)
        version = (self.db.one('SELECT COALESCE(MAX(version),0)+1 AS version FROM ontology_versions WHERE knowledge_base_id=? AND workspace_id=?',
                               knowledge_base_id, workspace_id) or {}).get('version', 1)
        schema = input.get('schema') or {'entityTypes': [], 'relationTypes': []}
        if not isinstance(schema.get('entityTypes'), list) or not isinstance(schema.get('relationTypes'), list):
            raise AppError(400, 'ONTOLOGY_SCHEMA_INVALID', '本体必须包含 entityTypes 和 relationTypes 数组')
        ontology_id = gen_id('onto')
        self.db.run('INSERT INTO ontology_versions(id,workspace_id,knowledge_base_id,version,name,schema_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    ontology_id, workspace_id, knowledge_base_id, version, required(input.get('name'), 'name'),
                    json.dumps(schema, ensure_ascii=False, separators=(',', ':')),
                    'active' if input.get('publish') else 'draft', now())
        if input.get('publish'):
            self.db.run("UPDATE ontology_versions SET status='superseded' WHERE knowledge_base_id=? AND workspace_id=? AND id!=? AND status='active'",
                        knowledge_base_id, workspace_id, ontology_id)
        self.audit.append(workspace_id=workspace_id,
                          action='ontology.publish' if input.get('publish') else 'ontology.create',
                          object_type='ontology_version', object_id=ontology_id, request_id=request_id,
                          details={'knowledgeBaseId': knowledge_base_id, 'version': version})
        return self.get_ontology(ontology_id, workspace_id)

    def get_ontology(self, ontology_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        record = self.db.one('SELECT * FROM ontology_versions WHERE id=? AND workspace_id=?', ontology_id, workspace_id)
        if not record:
            raise AppError(404, 'NOT_FOUND', '本体版本不存在或不可访问')
        return record

    def publish_ontology(self, ontology_id, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        ontology = self.get_ontology(ontology_id, workspace_id)
        self.db.transaction(lambda: (
            self.db.run("UPDATE ontology_versions SET status='superseded' WHERE knowledge_base_id=? AND workspace_id=? AND status='active'",
                        ontology['knowledge_base_id'], workspace_id),
            self.db.run("UPDATE ontology_versions SET status='active' WHERE id=? AND workspace_id=?", ontology_id, workspace_id)))
        self.audit.append(workspace_id=workspace_id, action='ontology.publish', object_type='ontology_version',
                          object_id=ontology_id, request_id=request_id, details={'version': ontology['version']})
        return self.get_ontology(ontology_id, workspace_id)

    def _ensure_entity_type(self, ontology, entity_type):
        types = (ontology.get('schema') or {}).get('entityTypes') or []
        if types and not any((item if isinstance(item, str) else item.get('name')) == entity_type for item in types):
            raise AppError(400, 'ENTITY_TYPE_INVALID', '实体类型不在本体定义中')

    def _ensure_relation_type(self, ontology, relation_type):
        types = (ontology.get('schema') or {}).get('relationTypes') or []
        if types and not any((item if isinstance(item, str) else item.get('name')) == relation_type for item in types):
            raise AppError(400, 'RELATION_TYPE_INVALID', '关系类型不在本体定义中')

    def create_entity(self, dataset_id, input, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        dataset = self.knowledge.ensure_dataset(dataset_id, workspace_id)
        ontology = self.get_ontology(required(input.get('ontologyVersionId'), 'ontologyVersionId'), workspace_id)
        if ontology['knowledge_base_id'] != dataset['knowledge_base_id']:
            raise AppError(400, 'SCOPE_MISMATCH', '本体不属于数据集所在知识库')
        self._ensure_entity_type(ontology, required(input.get('entityType'), 'entityType'))
        chunk = self.knowledge.get_chunk(required(input.get('sourceChunkId'), 'sourceChunkId'), workspace_id)
        if chunk['dataset_id'] != dataset_id:
            raise AppError(400, 'SCOPE_MISMATCH', '实体来源块不属于当前数据集')
        entity_id = gen_id('ent')
        timestamp = now()
        self.db.run('INSERT INTO graph_entities(id,workspace_id,dataset_id,ontology_version_id,entity_type,name,aliases_json,properties_json,source_chunk_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    entity_id, workspace_id, dataset_id, ontology['id'], input['entityType'], required(input.get('name'), 'name'),
                    json.dumps(input.get('aliases') or [], ensure_ascii=False),
                    json.dumps(input.get('properties') or {}, ensure_ascii=False, separators=(',', ':')),
                    chunk['id'], input.get('status') or 'confirmed', timestamp, timestamp)
        self.audit.append(workspace_id=workspace_id, action='graph_entity.create', object_type='graph_entity',
                          object_id=entity_id, request_id=request_id, details={'datasetId': dataset_id, 'sourceChunkId': chunk['id']})
        return self.get_entity(entity_id, workspace_id)

    def get_entity(self, entity_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        entity = self.db.one('SELECT * FROM graph_entities WHERE id=? AND workspace_id=?', entity_id, workspace_id)
        if not entity:
            raise AppError(404, 'NOT_FOUND', '图谱实体不存在或不可访问')
        entity['outgoing'] = self.db.all('SELECT * FROM graph_relations WHERE source_entity_id=? AND workspace_id=?', entity_id, workspace_id)
        entity['incoming'] = self.db.all('SELECT * FROM graph_relations WHERE target_entity_id=? AND workspace_id=?', entity_id, workspace_id)
        return entity

    def list_entities(self, dataset_id, workspace_id=None, query=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        query = query or {}
        self.knowledge.ensure_dataset(dataset_id, workspace_id)
        clauses, params = ['dataset_id=?', 'workspace_id=?'], [dataset_id, workspace_id]
        if query.get('type'):
            clauses.append('entity_type=?')
            params.append(query['type'])
        if query.get('q'):
            clauses.append('(name LIKE ? OR aliases_json LIKE ?)')
            params += [f"%{query['q']}%", f"%{query['q']}%"]
        return self.db.all(f"SELECT * FROM graph_entities WHERE {' AND '.join(clauses)} ORDER BY name LIMIT ? OFFSET ?",
                           *params, query.get('limit') or 100, query.get('offset') or 0)

    def create_relation(self, dataset_id, input, workspace_id=None, request_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        self.knowledge.ensure_dataset(dataset_id, workspace_id)
        ontology = self.get_ontology(required(input.get('ontologyVersionId'), 'ontologyVersionId'), workspace_id)
        self._ensure_relation_type(ontology, required(input.get('relationType'), 'relationType'))
        source = self.get_entity(required(input.get('sourceEntityId'), 'sourceEntityId'), workspace_id)
        target = self.get_entity(required(input.get('targetEntityId'), 'targetEntityId'), workspace_id)
        if source['dataset_id'] != dataset_id or target['dataset_id'] != dataset_id:
            raise AppError(400, 'SCOPE_MISMATCH', '关系两端必须属于当前数据集')
        chunk = self.knowledge.get_chunk(required(input.get('sourceChunkId'), 'sourceChunkId'), workspace_id)
        if chunk['dataset_id'] != dataset_id:
            raise AppError(400, 'SCOPE_MISMATCH', '关系来源块不属于当前数据集')
        relation_id = gen_id('relg')
        self.db.run('INSERT INTO graph_relations(id,workspace_id,dataset_id,ontology_version_id,relation_type,source_entity_id,target_entity_id,properties_json,source_chunk_id,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    relation_id, workspace_id, dataset_id, ontology['id'], input['relationType'], source['id'], target['id'],
                    json.dumps(input.get('properties') or {}, ensure_ascii=False, separators=(',', ':')),
                    chunk['id'], input.get('status') or 'confirmed', now())
        self.audit.append(workspace_id=workspace_id, action='graph_relation.create', object_type='graph_relation',
                          object_id=relation_id, request_id=request_id,
                          details={'datasetId': dataset_id, 'sourceEntityId': source['id'], 'targetEntityId': target['id'], 'sourceChunkId': chunk['id']})
        return self.db.one('SELECT * FROM graph_relations WHERE id=?', relation_id)

    def graph(self, dataset_id, workspace_id=None):
        workspace_id = workspace_id or self.config['localWorkspaceId']
        self.knowledge.ensure_dataset(dataset_id, workspace_id)
        return {
            'datasetId': dataset_id,
            'entities': self.db.all('SELECT * FROM graph_entities WHERE dataset_id=? AND workspace_id=? ORDER BY name', dataset_id, workspace_id),
            'relations': self.db.all('SELECT * FROM graph_relations WHERE dataset_id=? AND workspace_id=? ORDER BY created_at', dataset_id, workspace_id),
        }