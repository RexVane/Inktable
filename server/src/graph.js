'use strict';

const { id, now, required, AppError } = require('./core');

class GraphService {
  constructor({ db, knowledge, audit, config }) {
    this.db = db;
    this.knowledge = knowledge;
    this.audit = audit;
    this.config = config;
  }

  listOntologies(knowledgeBaseId, workspaceId = this.config.localWorkspaceId) {
    this.knowledge.ensureKnowledgeBase(knowledgeBaseId, workspaceId);
    return this.db.all('SELECT * FROM ontology_versions WHERE knowledge_base_id=? AND workspace_id=? ORDER BY version DESC', knowledgeBaseId, workspaceId);
  }

  createOntology(knowledgeBaseId, input, workspaceId = this.config.localWorkspaceId, requestId) {
    this.knowledge.ensureKnowledgeBase(knowledgeBaseId, workspaceId);
    const version = (this.db.one('SELECT COALESCE(MAX(version),0)+1 AS version FROM ontology_versions WHERE knowledge_base_id=? AND workspace_id=?', knowledgeBaseId, workspaceId)?.version || 1);
    const schema = input.schema || { entityTypes: [], relationTypes: [] };
    if (!Array.isArray(schema.entityTypes) || !Array.isArray(schema.relationTypes)) throw new AppError(400, 'ONTOLOGY_SCHEMA_INVALID', '本体必须包含 entityTypes 和 relationTypes 数组');
    const ontologyId = id('onto');
    this.db.run('INSERT INTO ontology_versions(id,workspace_id,knowledge_base_id,version,name,schema_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
      ontologyId, workspaceId, knowledgeBaseId, version, required(input.name, 'name'), JSON.stringify(schema), input.publish ? 'active' : 'draft', now());
    if (input.publish) this.db.run("UPDATE ontology_versions SET status='superseded' WHERE knowledge_base_id=? AND workspace_id=? AND id!=? AND status='active'", knowledgeBaseId, workspaceId, ontologyId);
    this.audit.append({ workspaceId, action: input.publish ? 'ontology.publish' : 'ontology.create', objectType: 'ontology_version', objectId: ontologyId, requestId, details: { knowledgeBaseId, version } });
    return this.getOntology(ontologyId, workspaceId);
  }

  getOntology(ontologyId, workspaceId = this.config.localWorkspaceId) {
    const record = this.db.one('SELECT * FROM ontology_versions WHERE id=? AND workspace_id=?', ontologyId, workspaceId);
    if (!record) throw new AppError(404, 'NOT_FOUND', '本体版本不存在或不可访问');
    return record;
  }

  publishOntology(ontologyId, workspaceId = this.config.localWorkspaceId, requestId) {
    const ontology = this.getOntology(ontologyId, workspaceId);
    this.db.transaction(() => {
      this.db.run("UPDATE ontology_versions SET status='superseded' WHERE knowledge_base_id=? AND workspace_id=? AND status='active'", ontology.knowledge_base_id, workspaceId);
      this.db.run("UPDATE ontology_versions SET status='active' WHERE id=? AND workspace_id=?", ontologyId, workspaceId);
    });
    this.audit.append({ workspaceId, action: 'ontology.publish', objectType: 'ontology_version', objectId: ontologyId, requestId, details: { version: ontology.version } });
    return this.getOntology(ontologyId, workspaceId);
  }

  ensureEntityType(ontology, type) {
    const types = ontology.schema?.entityTypes || [];
    if (types.length && !types.some(item => (typeof item === 'string' ? item : item.name) === type)) throw new AppError(400, 'ENTITY_TYPE_INVALID', '实体类型不在本体定义中');
  }

  ensureRelationType(ontology, type) {
    const types = ontology.schema?.relationTypes || [];
    if (types.length && !types.some(item => (typeof item === 'string' ? item : item.name) === type)) throw new AppError(400, 'RELATION_TYPE_INVALID', '关系类型不在本体定义中');
  }

  createEntity(datasetId, input, workspaceId = this.config.localWorkspaceId, requestId) {
    const dataset = this.knowledge.ensureDataset(datasetId, workspaceId);
    const ontology = this.getOntology(required(input.ontologyVersionId, 'ontologyVersionId'), workspaceId);
    if (ontology.knowledge_base_id !== dataset.knowledge_base_id) throw new AppError(400, 'SCOPE_MISMATCH', '本体不属于数据集所在知识库');
    this.ensureEntityType(ontology, required(input.entityType, 'entityType'));
    const chunk = this.knowledge.getChunk(required(input.sourceChunkId, 'sourceChunkId'), workspaceId);
    if (chunk.dataset_id !== datasetId) throw new AppError(400, 'SCOPE_MISMATCH', '实体来源块不属于当前数据集');
    const entityId = id('ent');
    const timestamp = now();
    this.db.run('INSERT INTO graph_entities(id,workspace_id,dataset_id,ontology_version_id,entity_type,name,aliases_json,properties_json,source_chunk_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
      entityId, workspaceId, datasetId, ontology.id, input.entityType, required(input.name, 'name'), JSON.stringify(input.aliases || []), JSON.stringify(input.properties || {}), chunk.id, input.status || 'confirmed', timestamp, timestamp);
    this.audit.append({ workspaceId, action: 'graph_entity.create', objectType: 'graph_entity', objectId: entityId, requestId, details: { datasetId, sourceChunkId: chunk.id } });
    return this.getEntity(entityId, workspaceId);
  }

  getEntity(entityId, workspaceId = this.config.localWorkspaceId) {
    const entity = this.db.one('SELECT * FROM graph_entities WHERE id=? AND workspace_id=?', entityId, workspaceId);
    if (!entity) throw new AppError(404, 'NOT_FOUND', '图谱实体不存在或不可访问');
    entity.outgoing = this.db.all('SELECT * FROM graph_relations WHERE source_entity_id=? AND workspace_id=?', entityId, workspaceId);
    entity.incoming = this.db.all('SELECT * FROM graph_relations WHERE target_entity_id=? AND workspace_id=?', entityId, workspaceId);
    return entity;
  }

  listEntities(datasetId, workspaceId = this.config.localWorkspaceId, query = {}) {
    this.knowledge.ensureDataset(datasetId, workspaceId);
    const clauses = ['dataset_id=?','workspace_id=?'];
    const params = [datasetId, workspaceId];
    if (query.type) { clauses.push('entity_type=?'); params.push(query.type); }
    if (query.q) { clauses.push('(name LIKE ? OR aliases_json LIKE ?)'); params.push(`%${query.q}%`, `%${query.q}%`); }
    return this.db.all(`SELECT * FROM graph_entities WHERE ${clauses.join(' AND ')} ORDER BY name LIMIT ? OFFSET ?`, ...params, query.limit || 100, query.offset || 0);
  }

  createRelation(datasetId, input, workspaceId = this.config.localWorkspaceId, requestId) {
    this.knowledge.ensureDataset(datasetId, workspaceId);
    const ontology = this.getOntology(required(input.ontologyVersionId, 'ontologyVersionId'), workspaceId);
    this.ensureRelationType(ontology, required(input.relationType, 'relationType'));
    const source = this.getEntity(required(input.sourceEntityId, 'sourceEntityId'), workspaceId);
    const target = this.getEntity(required(input.targetEntityId, 'targetEntityId'), workspaceId);
    if (source.dataset_id !== datasetId || target.dataset_id !== datasetId) throw new AppError(400, 'SCOPE_MISMATCH', '关系两端必须属于当前数据集');
    const chunk = this.knowledge.getChunk(required(input.sourceChunkId, 'sourceChunkId'), workspaceId);
    if (chunk.dataset_id !== datasetId) throw new AppError(400, 'SCOPE_MISMATCH', '关系来源块不属于当前数据集');
    const relationId = id('relg');
    this.db.run('INSERT INTO graph_relations(id,workspace_id,dataset_id,ontology_version_id,relation_type,source_entity_id,target_entity_id,properties_json,source_chunk_id,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
      relationId, workspaceId, datasetId, ontology.id, input.relationType, source.id, target.id, JSON.stringify(input.properties || {}), chunk.id, input.status || 'confirmed', now());
    this.audit.append({ workspaceId, action: 'graph_relation.create', objectType: 'graph_relation', objectId: relationId, requestId, details: { datasetId, sourceEntityId: source.id, targetEntityId: target.id, sourceChunkId: chunk.id } });
    return this.db.one('SELECT * FROM graph_relations WHERE id=?', relationId);
  }

  graph(datasetId, workspaceId = this.config.localWorkspaceId) {
    this.knowledge.ensureDataset(datasetId, workspaceId);
    return {
      datasetId,
      entities: this.db.all('SELECT * FROM graph_entities WHERE dataset_id=? AND workspace_id=? ORDER BY name', datasetId, workspaceId),
      relations: this.db.all('SELECT * FROM graph_relations WHERE dataset_id=? AND workspace_id=? ORDER BY created_at', datasetId, workspaceId)
    };
  }
}

module.exports = { GraphService };
