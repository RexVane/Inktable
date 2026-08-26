"""数据库 schema —— PLAN §9。

核心设计：files 与 contents 分离；AI Library 是可重建的派生知识层。

    N 个 files : 1 个 contents : 1 个 library_item

真实文件系统仍是 source of truth。library_items / tags / relations 只保存索引层
知识元数据，删除后可由 contents + files 重建，不移动、不复制、不改名用户文件。
"""

from __future__ import annotations

SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    path                  TEXT NOT NULL UNIQUE,
    kind                  TEXT NOT NULL,
    discovered_by         TEXT NOT NULL,
    volatile              INTEGER NOT NULL DEFAULT 0,
    auto_preserve         INTEGER NOT NULL DEFAULT 0,
    enabled               INTEGER NOT NULL DEFAULT 0,
    permission_ok         INTEGER,
    permission_checked_at REAL,
    created_at            REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS contents (
    id                   INTEGER PRIMARY KEY,
    sha256               TEXT NOT NULL UNIQUE,
    size                 INTEGER NOT NULL,
    parse_state          TEXT NOT NULL DEFAULT 'D',
    chunk_count          INTEGER NOT NULL DEFAULT 0,
    embedding_model_id   TEXT,
    indexed_at           REAL,
    active_index_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS files (
    id                INTEGER PRIMARY KEY,
    volume_uuid       TEXT NOT NULL,
    inode             INTEGER NOT NULL,
    content_id        INTEGER REFERENCES contents(id) ON DELETE SET NULL,
    path              TEXT NOT NULL,
    name              TEXT NOT NULL,
    origin_path       TEXT,
    preserved_path    TEXT,
    source_id         INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    ext               TEXT,
    mime              TEXT,
    size              INTEGER NOT NULL,
    state             TEXT NOT NULL DEFAULT 'D',
    error_code        TEXT,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    category_id       INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    confidence        REAL,
    confirmed_by_user INTEGER NOT NULL DEFAULT 0,
    is_dataless       INTEGER NOT NULL DEFAULT 0,
    mtime             REAL,
    detected_at       REAL NOT NULL,
    indexed_at        REAL,
    missing_since     REAL,
    UNIQUE (volume_uuid, inode)
);

CREATE INDEX IF NOT EXISTS idx_files_content  ON files(content_id);
CREATE INDEX IF NOT EXISTS idx_files_state    ON files(state);
CREATE INDEX IF NOT EXISTS idx_files_source   ON files(source_id);
CREATE INDEX IF NOT EXISTS idx_files_volume   ON files(volume_uuid);
CREATE INDEX IF NOT EXISTS idx_files_category ON files(category_id);
CREATE INDEX IF NOT EXISTS idx_files_mtime    ON files(mtime);
CREATE INDEX IF NOT EXISTS idx_files_path     ON files(path);

CREATE TABLE IF NOT EXISTS categories (
    id         INTEGER PRIMARY KEY,
    parent_id  INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rules (
    id                    INTEGER PRIMARY KEY,
    priority              INTEGER NOT NULL DEFAULT 100,
    match_ext             TEXT,
    match_source_id       INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    match_name_pattern    TEXT,
    category_id           INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    confidence            REAL NOT NULL DEFAULT 0.8,
    learned_from_file_id  INTEGER
);

CREATE TABLE IF NOT EXISTS tags (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE,
    color TEXT
);

CREATE TABLE IF NOT EXISTS file_tags (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (file_id, tag_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    id                 INTEGER PRIMARY KEY,
    content_id         INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    layer              TEXT NOT NULL DEFAULT 'child',
    parent_id          INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
    page               INTEGER,
    page_end           INTEGER,
    bbox               TEXT,
    section_path       TEXT NOT NULL DEFAULT '',
    ordinal            INTEGER NOT NULL,
    text               TEXT NOT NULL,
    text_hash          TEXT NOT NULL,
    token_count        INTEGER,
    embedding_model_id TEXT,
    section_id         INTEGER REFERENCES sections(id) ON DELETE SET NULL,
    start_offset       INTEGER,
    end_offset         INTEGER,
    index_version      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_chunks_content ON chunks(content_id);
CREATE INDEX IF NOT EXISTS idx_chunks_layer   ON chunks(content_id, layer);
CREATE INDEX IF NOT EXISTS idx_chunks_hash    ON chunks(text_hash);
CREATE INDEX IF NOT EXISTS idx_chunks_parent  ON chunks(parent_id);

CREATE TABLE IF NOT EXISTS document_representations (
    id                   INTEGER PRIMARY KEY,
    content_id           INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    index_version        INTEGER NOT NULL,
    title                TEXT NOT NULL DEFAULT '',
    summary_text         TEXT NOT NULL DEFAULT '',
    abstract             TEXT,
    abstract_model       TEXT,
    full_text            TEXT NOT NULL DEFAULT '',
    text_hash            TEXT NOT NULL,
    token_count          INTEGER NOT NULL DEFAULT 0,
    structure_confidence REAL NOT NULL DEFAULT 0,
    UNIQUE(content_id, index_version)
);

CREATE TABLE IF NOT EXISTS sections (
    id                   INTEGER PRIMARY KEY,
    content_id           INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    parent_id            INTEGER REFERENCES sections(id) ON DELETE CASCADE,
    index_version        INTEGER NOT NULL,
    ordinal              INTEGER NOT NULL,
    heading_path         TEXT NOT NULL DEFAULT '',
    title                TEXT NOT NULL,
    summary_text         TEXT NOT NULL DEFAULT '',
    start_chunk_ordinal  INTEGER NOT NULL,
    end_chunk_ordinal    INTEGER NOT NULL,
    start_offset         INTEGER,
    end_offset           INTEGER,
    text_hash            TEXT NOT NULL,
    token_count          INTEGER NOT NULL DEFAULT 0,
    structure_confidence REAL NOT NULL DEFAULT 0,
    UNIQUE(content_id, index_version, heading_path)
);

CREATE TABLE IF NOT EXISTS index_versions (
    content_id    INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    status        TEXT NOT NULL,
    document_hash TEXT,
    section_count INTEGER NOT NULL DEFAULT 0,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    REAL NOT NULL,
    activated_at  REAL,
    PRIMARY KEY(content_id, version)
);

CREATE INDEX IF NOT EXISTS idx_document_representations_version
    ON document_representations(content_id, index_version);
CREATE INDEX IF NOT EXISTS idx_sections_version
    ON sections(content_id, index_version, ordinal);
CREATE INDEX IF NOT EXISTS idx_sections_parent ON sections(parent_id);

CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY,
    file_id        INTEGER REFERENCES files(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    payload        TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    attempts       INTEGER NOT NULL DEFAULT 0,
    next_run_at    REAL,
    last_error     TEXT,
    progress_done  INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_dedup
    ON tasks(kind, file_id) WHERE status IN ('pending', 'running');

CREATE TABLE IF NOT EXISTS operations (
    id            INTEGER PRIMARY KEY,
    file_id       INTEGER REFERENCES files(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    src_path      TEXT,
    dst_path      TEXT,
    method        TEXT,
    sha256_before TEXT,
    undone        INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS file_history (
    id       INTEGER PRIMARY KEY,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    field    TEXT NOT NULL,
    old      TEXT,
    new      TEXT,
    by       TEXT NOT NULL,
    batch_id TEXT,
    rule_id  INTEGER,
    at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS books (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS book_members (
    book_id  INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    added_at REAL NOT NULL,
    PRIMARY KEY (book_id, file_id)
);

CREATE TABLE IF NOT EXISTS excluded_paths (
    path       TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='', contentless_delete=1, tokenize='unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_tri USING fts5(
    text, content='', contentless_delete=1, tokenize='trigram'
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    text, content='', contentless_delete=1, tokenize='unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
    text, content='', contentless_delete=1, tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS journal (
    id          INTEGER PRIMARY KEY,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    content_ids TEXT NOT NULL DEFAULT '[]',
    book_id     INTEGER REFERENCES books(id) ON DELETE SET NULL,
    model       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_journal_created ON journal(created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS journal_fts USING fts5(
    text, content='', contentless_delete=1, tokenize='unicode61'
);
"""

# Keep the AI Library schema in one domain-owned place while making it part of
# normal database initialization.  Old v2 applications now correctly reject a
# database upgraded to v3 instead of opening it with incomplete semantics.
from app.library.core import LIBRARY_SCHEMA

SCHEMA = f"{SCHEMA}\n{LIBRARY_SCHEMA}"

DEFAULT_SETTINGS = {
    "db_schema_version": str(SCHEMA_VERSION),
    "cloud_index_placeholders": "0",
    "privacy_cloud_ai_enabled": "0",
    "confidence_threshold": "0.6",
    "ocr_enabled": "1",
    "answer_max_tokens": "auto",
}
