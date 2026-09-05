import json
import sqlite3
import threading

from .core import gen_id, now, row_to_object

MIGRATIONS = [
    {
        'version': 1,
        'name': 'initial_product_schema',
        'sql': """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
) STRICT;
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK(type IN ('local','personal','enterprise')),
  owner_id TEXT NOT NULL REFERENCES users(id),
  retention_days INTEGER NOT NULL DEFAULT 365,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE memberships (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  role TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,user_id)
) STRICT;
CREATE TABLE settings (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(workspace_id,key)
) STRICT;
CREATE TABLE feature_flags (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  key TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  config_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY(workspace_id,key)
) STRICT;
CREATE TABLE knowledge_bases (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','deleted')),
  default_dataset_id TEXT,
  active_release_id TEXT,
  config_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT,
  UNIQUE(workspace_id,name)
) STRICT;
CREATE INDEX idx_knowledge_bases_workspace ON knowledge_bases(workspace_id,status);
CREATE TABLE datasets (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT 'zh-CN',
  labels_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','deleted')),
  active_release_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT,
  UNIQUE(knowledge_base_id,name)
) STRICT;
CREATE INDEX idx_datasets_kb ON datasets(workspace_id,knowledge_base_id,status);
CREATE TABLE index_profiles (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
  name TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  config_json TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(knowledge_base_id,config_hash)
) STRICT;
CREATE TABLE blobs (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  mime_type TEXT NOT NULL,
  storage_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(workspace_id,sha256)
) STRICT;
CREATE TABLE sources (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  dataset_id TEXT NOT NULL REFERENCES datasets(id),
  type TEXT NOT NULL CHECK(type IN ('upload','directory','archive','local_discovery','database','connector','synthetic')),
  name TEXT NOT NULL,
  location_hint TEXT NOT NULL DEFAULT '',
  config_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'registered',
  last_watermark TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT
) STRICT;
CREATE INDEX idx_sources_dataset ON sources(workspace_id,dataset_id,status);
CREATE TABLE documents (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  dataset_id TEXT NOT NULL REFERENCES datasets(id),
  source_id TEXT NOT NULL REFERENCES sources(id),
  title TEXT NOT NULL,
  logical_path TEXT NOT NULL DEFAULT '',
  media_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'registered',
  current_revision_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT
) STRICT;
CREATE INDEX idx_documents_dataset ON documents(workspace_id,dataset_id,status);
CREATE TABLE document_revisions (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  document_id TEXT NOT NULL REFERENCES documents(id),
  source_id TEXT NOT NULL REFERENCES sources(id),
  blob_id TEXT REFERENCES blobs(id),
  revision_number INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'registered',
  parser_id TEXT,
  parser_version TEXT,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(document_id,revision_number),
  UNIQUE(document_id,content_hash)
) STRICT;
CREATE INDEX idx_document_revisions_document ON document_revisions(workspace_id,document_id,revision_number DESC);
CREATE TABLE parsed_artifacts (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  document_revision_id TEXT NOT NULL REFERENCES document_revisions(id),
  schema_version INTEGER NOT NULL DEFAULT 1,
  markdown_key TEXT NOT NULL,
  json_key TEXT NOT NULL,
  manifest_key TEXT NOT NULL,
  quality_key TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  quality_status TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(document_revision_id,content_hash)
) STRICT;
CREATE TABLE chunk_logicals (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  dataset_id TEXT NOT NULL REFERENCES datasets(id),
  document_id TEXT NOT NULL REFERENCES documents(id),
  created_at TEXT NOT NULL
) STRICT;
CREATE TABLE chunk_revisions (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  chunk_logical_id TEXT NOT NULL REFERENCES chunk_logicals(id),
  dataset_id TEXT NOT NULL REFERENCES datasets(id),
  document_id TEXT NOT NULL REFERENCES documents(id),
  document_revision_id TEXT NOT NULL REFERENCES document_revisions(id),
  artifact_id TEXT NOT NULL REFERENCES parsed_artifacts(id),
  revision_number INTEGER NOT NULL,
  parent_chunk_id TEXT,
  type TEXT NOT NULL,
  content_md TEXT NOT NULL,
  content_text TEXT NOT NULL,
  source_locator_json TEXT NOT NULL DEFAULT '{}',
  token_count INTEGER NOT NULL,
  language TEXT NOT NULL DEFAULT 'zh-CN',
  generated_by TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  sensitivity TEXT NOT NULL DEFAULT 'internal',
  excluded INTEGER NOT NULL DEFAULT 0,
  supersedes_id TEXT,
  embedding_json TEXT,
  embedding_model TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(chunk_logical_id,revision_number)
) STRICT;
CREATE INDEX idx_chunks_dataset ON chunk_revisions(workspace_id,dataset_id,created_at);
CREATE INDEX idx_chunks_document ON chunk_revisions(workspace_id,document_id,created_at);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  chunk_revision_id UNINDEXED,
  workspace_id UNINDEXED,
  dataset_id UNINDEXED,
  title,
  breadcrumb,
  content,
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE knowledge_releases (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  dataset_id TEXT NOT NULL REFERENCES datasets(id),
  knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
  index_profile_id TEXT NOT NULL REFERENCES index_profiles(id),
  version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','building','validating','ready','active','superseded','retained','failed')),
  manifest_json TEXT NOT NULL DEFAULT '{}',
  quality_json TEXT NOT NULL DEFAULT '{}',
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  activated_at TEXT,
  UNIQUE(dataset_id,version)
) STRICT;
CREATE INDEX idx_releases_dataset ON knowledge_releases(workspace_id,dataset_id,version DESC);
CREATE TABLE release_chunks (
  release_id TEXT NOT NULL REFERENCES knowledge_releases(id),
  chunk_revision_id TEXT NOT NULL REFERENCES chunk_revisions(id),
  ordinal INTEGER NOT NULL,
  PRIMARY KEY(release_id,chunk_revision_id)
) STRICT;
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  type TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','paused','succeeded','partial','failed','cancelled')),
  progress INTEGER NOT NULL DEFAULT 0,
  input_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}',
  error_code TEXT,
  error_message TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  attempt INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,idempotency_key)
) STRICT;
CREATE INDEX idx_tasks_status ON tasks(workspace_id,status,created_at DESC);
CREATE TABLE task_events (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  task_id TEXT NOT NULL REFERENCES tasks(id),
  sequence INTEGER NOT NULL,
  level TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  data_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(task_id,sequence)
) STRICT;
CREATE TABLE conversations (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
  dataset_id TEXT NOT NULL REFERENCES datasets(id),
  release_id TEXT NOT NULL REFERENCES knowledge_releases(id),
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  model_connection_id TEXT,
  strict_evidence INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT
) STRICT;
CREATE INDEX idx_conversations_workspace ON conversations(workspace_id,created_at DESC);
CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
  content TEXT NOT NULL,
  evidence_status TEXT,
  trace_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX idx_messages_conversation ON messages(workspace_id,conversation_id,created_at);
CREATE TABLE query_traces (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  message_id TEXT REFERENCES messages(id),
  release_id TEXT NOT NULL REFERENCES knowledge_releases(id),
  query TEXT NOT NULL,
  status TEXT NOT NULL,
  evidence_status TEXT NOT NULL,
  stages_json TEXT NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
) STRICT;
CREATE TABLE citations (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  trace_id TEXT NOT NULL REFERENCES query_traces(id),
  message_id TEXT NOT NULL REFERENCES messages(id),
  release_id TEXT NOT NULL REFERENCES knowledge_releases(id),
  document_id TEXT NOT NULL REFERENCES documents(id),
  document_revision_id TEXT NOT NULL REFERENCES document_revisions(id),
  chunk_revision_id TEXT NOT NULL REFERENCES chunk_revisions(id),
  title TEXT NOT NULL,
  locator_json TEXT NOT NULL,
  excerpt TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(message_id,ordinal)
) STRICT;
CREATE TABLE feedback (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  message_id TEXT NOT NULL REFERENCES messages(id),
  rating INTEGER NOT NULL CHECK(rating IN (-1,1)),
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(workspace_id,message_id)
) STRICT;
CREATE TABLE model_connections (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL,
  provider TEXT NOT NULL,
  purpose TEXT NOT NULL,
  base_url TEXT,
  model_id TEXT NOT NULL,
  secret_ref TEXT,
  config_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'unverified',
  last_checked_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,name)
) STRICT;
CREATE TABLE secrets (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  purpose TEXT NOT NULL,
  encrypted_value TEXT NOT NULL,
  mask TEXT NOT NULL,
  created_at TEXT NOT NULL,
  rotated_at TEXT
) STRICT;
CREATE TABLE wiki_pages (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
  parent_id TEXT,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  current_revision_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE wiki_revisions (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  page_id TEXT NOT NULL REFERENCES wiki_pages(id),
  revision_number INTEGER NOT NULL,
  content_md TEXT NOT NULL,
  sources_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL,
  UNIQUE(page_id,revision_number)
) STRICT;
CREATE TABLE assistants (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL,
  dataset_id TEXT NOT NULL REFERENCES datasets(id),
  status TEXT NOT NULL DEFAULT 'draft',
  draft_config_json TEXT NOT NULL DEFAULT '{}',
  active_release_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE assistant_releases (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  assistant_id TEXT NOT NULL REFERENCES assistants(id),
  knowledge_release_id TEXT NOT NULL REFERENCES knowledge_releases(id),
  version INTEGER NOT NULL,
  config_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(assistant_id,version)
) STRICT;
CREATE TABLE audit_events (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  result TEXT NOT NULL,
  request_id TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  previous_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX idx_audit_workspace ON audit_events(workspace_id,created_at DESC);
CREATE TABLE backup_manifests (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  status TEXT NOT NULL,
  storage_key TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  checksum TEXT NOT NULL,
  created_at TEXT NOT NULL,
  verified_at TEXT
) STRICT;
""",
    },
    {
        'version': 2,
        'name': 'connectors_graph_widget_and_pause',
        'sql': """
ALTER TABLE tasks ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0;
CREATE TABLE connectors (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK(type IN ('sqlite','postgresql')),
  config_json TEXT NOT NULL DEFAULT '{}',
  secret_ref TEXT,
  status TEXT NOT NULL DEFAULT 'unverified',
  last_checked_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(workspace_id,name)
) STRICT;
CREATE TABLE database_query_templates (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  connector_id TEXT NOT NULL REFERENCES connectors(id),
  name TEXT NOT NULL,
  sql_text TEXT NOT NULL,
  params_json TEXT NOT NULL DEFAULT '[]',
  row_limit INTEGER NOT NULL DEFAULT 1000,
  timeout_ms INTEGER NOT NULL DEFAULT 10000,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(connector_id,name)
) STRICT;
CREATE TABLE database_snapshots (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  connector_id TEXT NOT NULL REFERENCES connectors(id),
  template_id TEXT NOT NULL REFERENCES database_query_templates(id),
  dataset_id TEXT NOT NULL REFERENCES datasets(id),
  source_id TEXT NOT NULL REFERENCES sources(id),
  row_count INTEGER NOT NULL,
  schema_hash TEXT NOT NULL,
  watermark TEXT,
  artifact_key TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
CREATE TABLE ontology_versions (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
  version INTEGER NOT NULL,
  name TEXT NOT NULL,
  schema_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL,
  UNIQUE(knowledge_base_id,version)
) STRICT;
CREATE TABLE graph_entities (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  dataset_id TEXT NOT NULL REFERENCES datasets(id),
  ontology_version_id TEXT NOT NULL REFERENCES ontology_versions(id),
  entity_type TEXT NOT NULL,
  name TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  properties_json TEXT NOT NULL DEFAULT '{}',
  source_chunk_id TEXT NOT NULL REFERENCES chunk_revisions(id),
  status TEXT NOT NULL DEFAULT 'confirmed',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE INDEX idx_graph_entities_dataset ON graph_entities(workspace_id,dataset_id,entity_type,name);
CREATE TABLE graph_relations (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  dataset_id TEXT NOT NULL REFERENCES datasets(id),
  ontology_version_id TEXT NOT NULL REFERENCES ontology_versions(id),
  relation_type TEXT NOT NULL,
  source_entity_id TEXT NOT NULL REFERENCES graph_entities(id),
  target_entity_id TEXT NOT NULL REFERENCES graph_entities(id),
  properties_json TEXT NOT NULL DEFAULT '{}',
  source_chunk_id TEXT NOT NULL REFERENCES chunk_revisions(id),
  status TEXT NOT NULL DEFAULT 'confirmed',
  created_at TEXT NOT NULL
) STRICT;
CREATE TABLE widget_clients (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  assistant_id TEXT NOT NULL REFERENCES assistants(id),
  client_id TEXT NOT NULL UNIQUE,
  secret_ref TEXT NOT NULL,
  allowed_origins_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  rotated_at TEXT
) STRICT;
CREATE TABLE widget_nonces (
  client_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(client_id,nonce)
) STRICT;
CREATE TABLE visitor_sessions (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  assistant_id TEXT NOT NULL REFERENCES assistants(id),
  assistant_release_id TEXT NOT NULL REFERENCES assistant_releases(id),
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  pseudonym TEXT NOT NULL,
  origin TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT
) STRICT;
CREATE TABLE handoff_requests (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  visitor_session_id TEXT NOT NULL REFERENCES visitor_sessions(id),
  status TEXT NOT NULL DEFAULT 'queued',
  priority TEXT NOT NULL DEFAULT 'normal',
  summary TEXT NOT NULL,
  contact_json TEXT NOT NULL DEFAULT '{}',
  assigned_to TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
""",
    },
    {
        'version': 3,
        'name': 'knowledge_base_default_index_profile',
        'sql': """
ALTER TABLE knowledge_bases ADD COLUMN default_index_profile_id TEXT;
""",
    },
    {
        'version': 4,
        'name': 'trace_replay_metadata',
        'sql': """
ALTER TABLE query_traces ADD COLUMN parent TEXT REFERENCES query_traces(id);
ALTER TABLE query_traces ADD COLUMN root TEXT REFERENCES query_traces(id);
ALTER TABLE query_traces ADD COLUMN trace_type TEXT NOT NULL DEFAULT 'original';
ALTER TABLE query_traces ADD COLUMN replay_from_stage TEXT;
ALTER TABLE query_traces ADD COLUMN config_snapshot TEXT NOT NULL DEFAULT '{}';
ALTER TABLE query_traces ADD COLUMN input_snapshot TEXT NOT NULL DEFAULT '{}';
ALTER TABLE query_traces ADD COLUMN permission_snapshot TEXT NOT NULL DEFAULT '{}';
ALTER TABLE query_traces ADD COLUMN retention TEXT NOT NULL DEFAULT 'standard';
UPDATE query_traces SET root=id WHERE root IS NULL;
CREATE INDEX idx_query_traces_parent ON query_traces(workspace_id,parent,created_at);
CREATE INDEX idx_query_traces_root ON query_traces(workspace_id,root,created_at);
""",
    },
]


MIGRATIONS.append({
    'version': 5, 'name': 'python_workbench_state', 'sql': '''
CREATE TABLE registered_files (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 blob_id TEXT NOT NULL REFERENCES blobs(id), name TEXT NOT NULL, media_type TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'unassigned', source_id TEXT REFERENCES sources(id),
 document_id TEXT REFERENCES documents(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE dataset_folders (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 dataset_id TEXT NOT NULL REFERENCES datasets(id), parent_id TEXT REFERENCES dataset_folders(id),
 name TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(dataset_id,path)
) STRICT;
ALTER TABLE documents ADD COLUMN folder_id TEXT REFERENCES dataset_folders(id);
CREATE TABLE trace_stage_drafts (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 trace_id TEXT NOT NULL REFERENCES query_traces(id), stage TEXT NOT NULL,
 version INTEGER NOT NULL, config_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(trace_id,stage,version)
) STRICT;
CREATE TABLE index_projections (
 workspace_id TEXT NOT NULL REFERENCES workspaces(id), dataset_id TEXT NOT NULL REFERENCES datasets(id),
 kind TEXT NOT NULL, content_json TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(dataset_id,kind)
) STRICT;
'''})

MIGRATIONS.append({
    'version': 6, 'name': 'hot_path_indexes', 'sql': '''
CREATE INDEX IF NOT EXISTS idx_citations_trace ON citations(trace_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_query_traces_conversation ON query_traces(workspace_id, conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_document_revisions_dedupe ON document_revisions(workspace_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_graph_relations_source ON graph_relations(workspace_id, source_entity_id);
CREATE INDEX IF NOT EXISTS idx_graph_relations_target ON graph_relations(workspace_id, target_entity_id);
'''})


def assert_fts5_available(connection):
    try:
        connection.execute("CREATE VIRTUAL TABLE temp.__fts5_probe USING fts5(x)")
        connection.execute("DROP TABLE temp.__fts5_probe")
    except sqlite3.OperationalError as error:
        raise RuntimeError(f'当前 Python 构建未启用 SQLite FTS5，无法运行 Ordo: {error}')


class OrdoDatabase:
    def __init__(self, config):
        self.config = config
        config['dbPath'].parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        probe = self.connection()
        assert_fts5_available(probe)
        self.migrate()
        self.seed_local_identity()

    def connection(self):
        connection = getattr(self._local, 'connection', None)
        if connection is None:
            connection = sqlite3.connect(str(self.config['dbPath']), timeout=5.0, isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA busy_timeout=5000')
            self._local.connection = connection
        return connection

    def migrate(self):
        connection = self.connection()
        connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL) STRICT;')
        applied = {row['version'] for row in connection.execute('SELECT version FROM schema_migrations').fetchall()}
        for migration in MIGRATIONS:
            if migration['version'] in applied:
                continue
            try:
                connection.executescript(f"BEGIN IMMEDIATE;\n{migration['sql']}")
                connection.execute(
                    'INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)',
                    (migration['version'], migration['name'], now()),
                )
                connection.execute('COMMIT')
            except BaseException:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    def seed_local_identity(self):
        timestamp = now()
        workspace_id = self.config['localWorkspaceId']
        owner_id = self.config['localOwnerId']
        connection = self.connection()
        connection.execute('INSERT OR IGNORE INTO users(id,display_name,status,created_at,updated_at) VALUES(?,?,?,?,?)',
                           (owner_id, '本机所有者', 'active', timestamp, timestamp))
        connection.execute('INSERT OR IGNORE INTO workspaces(id,name,type,owner_id,retention_days,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                           (workspace_id, 'Ordo', 'local', owner_id, 365, timestamp, timestamp))
        connection.execute('INSERT OR IGNORE INTO memberships(id,workspace_id,user_id,role,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                           (gen_id('mem'), workspace_id, owner_id, 'owner', 'active', timestamp, timestamp))
        defaults = {
            'general': {'language': 'zh-CN', 'theme': 'silver', 'telemetry': False, 'retentionDays': 365},
            'query': {'strictEvidence': True, 'topK': 8, 'minScore': 0.08, 'allowPublicKnowledge': False},
            'ingestion': {'maxFileBytes': self.config['maxFileBytes'], 'archiveDepth': 1},
            'backup': {'enabled': True, 'retention': 7},
        }
        for key, value in defaults.items():
            connection.execute('INSERT OR IGNORE INTO settings(workspace_id,key,value_json,updated_at) VALUES(?,?,?,?)',
                               (workspace_id, key, json.dumps(value, ensure_ascii=False, separators=(',', ':')), timestamp))
        for key, enabled in {'wiki': True, 'assistants': True, 'externalModels': True, 'graph': True, 'databaseConnectors': True, 'websiteAssistant': True}.items():
            connection.execute('INSERT OR IGNORE INTO feature_flags(workspace_id,key,enabled,config_json,updated_at) VALUES(?,?,?,?,?)',
                               (workspace_id, key, 1 if enabled else 0, '{}', timestamp))

    def transaction(self, fn):
        connection = self.connection()
        if getattr(self._local, 'in_transaction', False):
            return fn()
        self._local.in_transaction = True
        try:
            connection.execute('BEGIN IMMEDIATE')
            result = fn()
            connection.execute('COMMIT')
            return result
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        finally:
            self._local.in_transaction = False

    def one(self, sql, *params):
        row = self.connection().execute(sql, params).fetchone()
        return row_to_object(row)

    def all(self, sql, *params):
        return [row_to_object(row) for row in self.connection().execute(sql, params).fetchall()]

    def run(self, sql, *params):
        return self.connection().execute(sql, params)

    def exec(self, sql):
        return self.connection().executescript(sql)

    def close(self):
        connection = getattr(self._local, 'connection', None)
        if connection is not None:
            connection.close()
            self._local.connection = None
