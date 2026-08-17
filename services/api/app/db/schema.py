"""数据库 schema —— PLAN §9。

核心设计：**files 与 contents 分离**（§9 contents 表）。

    N 个 files : 1 个 contents

一份内容被存了 5 处，chunks 只建一次、向量只算一次。且任一 file 存活，
content 与其 chunks 就保留 —— 原件被微信清理掉，副本仍然可检索。
v5 之前 chunks 直接挂 file_id，导致"复用 chunks"这件事无处承载。

身份用 (volume_uuid, inode) 而非路径（§8）：用户在 Finder 里移动/改名文件后
索引自动跟上，不需要重新解析、重新嵌入。
"""

from __future__ import annotations

SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 来源（§9 sources）
CREATE TABLE IF NOT EXISTS sources (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    path                  TEXT NOT NULL UNIQUE,
    kind                  TEXT NOT NULL,              -- im / browser / system / manual
    discovered_by         TEXT NOT NULL,              -- config / heuristic / bundle / default / manual
    volatile              INTEGER NOT NULL DEFAULT 0, -- 应用会自行清理？
    auto_preserve         INTEGER NOT NULL DEFAULT 0, -- 易失来源自动保全副本，默认关
    enabled               INTEGER NOT NULL DEFAULT 0, -- 默认不启用（§1 约束 4）
    permission_ok         INTEGER,
    permission_checked_at REAL,
    created_at            REAL NOT NULL
);

-- 内容实体（§9 contents）：chunks 挂这里，不挂 files
CREATE TABLE IF NOT EXISTS contents (
    id                 INTEGER PRIMARY KEY,
    sha256             TEXT NOT NULL UNIQUE,
    size               INTEGER NOT NULL,
    parse_state        TEXT NOT NULL DEFAULT 'D',  -- B / C / D
    chunk_count        INTEGER NOT NULL DEFAULT 0,
    embedding_model_id TEXT,
    indexed_at         REAL,
    active_index_version INTEGER NOT NULL DEFAULT 1
);

-- 文件（§9 files）
CREATE TABLE IF NOT EXISTS files (
    id                INTEGER PRIMARY KEY,
    volume_uuid       TEXT NOT NULL,      -- 身份 1/2
    inode             INTEGER NOT NULL,   -- 身份 2/2
    content_id        INTEGER REFERENCES contents(id) ON DELETE SET NULL,
    path              TEXT NOT NULL,      -- 当前路径（缓存，可变）
    name              TEXT NOT NULL,
    origin_path       TEXT,               -- 首次发现时的路径
    preserved_path    TEXT,               -- 保全副本路径
    source_id         INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    ext               TEXT,
    mime              TEXT,
    size              INTEGER NOT NULL,
    state             TEXT NOT NULL DEFAULT 'D',  -- B / C / D
    error_code        TEXT,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    category_id       INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    confidence        REAL,
    confirmed_by_user INTEGER NOT NULL DEFAULT 0,
    is_dataless       INTEGER NOT NULL DEFAULT 0,  -- iCloud 占位文件（§7.8）
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

-- 分类树（§9 categories）：**纯虚拟，磁盘上不存在对应目录**
CREATE TABLE IF NOT EXISTS categories (
    id         INTEGER PRIMARY KEY,
    parent_id  INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

-- 分类规则（§9 rules / §11.4 回流学习）
-- 三条件 AND，NULL = 不限；priority 小的先匹配，首条命中即停
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

-- 分片（§9 chunks）：挂 content_id
CREATE TABLE IF NOT EXISTS chunks (
    id                 INTEGER PRIMARY KEY,
    content_id         INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    layer              TEXT NOT NULL DEFAULT 'child',   -- child（检索用）/ parent（送模型用）
    parent_id          INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
    page               INTEGER,
    page_end           INTEGER,
    bbox               TEXT,          -- JSON 数组，一片可跨多个矩形
    section_path       TEXT NOT NULL DEFAULT '',
    ordinal            INTEGER NOT NULL,
    text               TEXT NOT NULL,
    text_hash          TEXT NOT NULL, -- 增量 diff 的主键（§12.5）
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

-- Document / Section / Child 分层索引（v7 M2）。Document 与 Section 表示按
-- index_version 保存，contents.active_index_version 是唯一激活指针。
CREATE TABLE IF NOT EXISTS document_representations (
    id                   INTEGER PRIMARY KEY,
    content_id           INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    index_version        INTEGER NOT NULL,
    title                TEXT NOT NULL DEFAULT '',
    summary_text         TEXT NOT NULL DEFAULT '',
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
    status        TEXT NOT NULL, -- building / active / superseded / failed
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

-- 任务队列（§9 tasks）
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY,
    file_id        INTEGER REFERENCES files(id) ON DELETE CASCADE,  -- 非文件级任务为 NULL
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

-- 防止同一文件的同类任务重复入队（FSEvents 抖动时极易发生）
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_dedup
    ON tasks(kind, file_id) WHERE status IN ('pending', 'running');

-- 写盘操作日志（§9 operations）：只记录真正写盘的动作
CREATE TABLE IF NOT EXISTS operations (
    id            INTEGER PRIMARY KEY,
    file_id       INTEGER REFERENCES files(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,   -- preserve / archive_move
    src_path      TEXT,
    dst_path      TEXT,
    method        TEXT,            -- copy / copy_verify_delete
    sha256_before TEXT,
    undone        INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);

-- 分类变更历史（§9 file_history）：批量操作靠 batch_id 整批撤销
CREATE TABLE IF NOT EXISTS file_history (
    id       INTEGER PRIMARY KEY,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    field    TEXT NOT NULL,
    old      TEXT,
    new      TEXT,
    by       TEXT NOT NULL,   -- rule / llm / user
    batch_id TEXT,
    rule_id  INTEGER,
    at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 文件书（§9 books / B7）：虚拟集合，一个文件可属多本书。
-- 与分类互补：分类单选表达"是什么"，书多选表达"为哪件事收集"。
-- 书内可限定检索与问答范围（filters.book_id）。
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

-- 中文全文检索双索引（§9.1，M0 实测确认必须双路）
--
-- FTS5 默认 unicode61 对中文零命中：整段汉字被当作一个 token。
-- jieba 主索引负责成词查询，trigram 副索引兜底子串、编号、错别字。
-- 两个都是 content='' 的外部内容表，rowid 对齐 chunks.id。
--
-- contentless_delete=1 不可省：content='' 的表默认**不支持 DELETE**
-- （SQLite 报 "cannot DELETE from contentless fts5 table"）。缺了它，
-- 删除文件或来源时索引清不掉，搜索会命中已消失的分片 —— 库只能进不能出。
-- 替代方案 INSERT INTO t(t,rowid,text) VALUES('delete',...) 要求提供
-- 被删行的原文，而删除时原文往往已不存在，不可行。
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
"""

DEFAULT_SETTINGS = {
    "db_schema_version": str(SCHEMA_VERSION),
    "cloud_index_placeholders": "0",   # iCloud 占位文件默认不索引（§7.8）
    "privacy_cloud_ai_enabled": "0",   # 云端 AI 默认关闭（§1 约束 3）
    "confidence_threshold": "0.6",
    "ocr_enabled": "1",                # 扫描件 OCR（macOS Vision，纯本地）默认开
    "answer_max_tokens": "auto",       # 问答输出上限："auto"=跟随所选模型
}
