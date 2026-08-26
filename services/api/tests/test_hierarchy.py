"""M2 Document / Section / Child hierarchy and version activation contracts."""

from __future__ import annotations

import sqlite3

import pytest

from app.db.database import connect, init_db
from app.db.schema import SCHEMA_VERSION
from app.index import embedding as emb
from app.index import pipeline
from app.index import vector as vec
from app.index.hierarchy import hierarchy_routes
from app.index.search import search


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute(
        "INSERT INTO contents(id, sha256, size) VALUES (1, 'hierarchy', 1)"
    )
    conn.commit()
    yield conn
    conn.close()


def test_v1_database_is_migrated_and_existing_chunks_remain_active():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
           INSERT INTO settings VALUES ('db_schema_version', '1');
           CREATE TABLE contents (
             id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
             size INTEGER NOT NULL, parse_state TEXT NOT NULL DEFAULT 'pending',
             chunk_count INTEGER NOT NULL DEFAULT 0,
             embedding_model_id TEXT, indexed_at REAL
           );
           CREATE TABLE files (
             id INTEGER PRIMARY KEY, volume_uuid TEXT NOT NULL, inode INTEGER NOT NULL,
             content_id INTEGER, path TEXT NOT NULL, name TEXT NOT NULL,
             source_id INTEGER, category_id INTEGER, mtime REAL,
             size INTEGER NOT NULL, state TEXT NOT NULL, detected_at REAL NOT NULL,
             UNIQUE(volume_uuid, inode)
           );
           CREATE TABLE chunks (
             id INTEGER PRIMARY KEY, content_id INTEGER NOT NULL,
             layer TEXT NOT NULL DEFAULT 'child', parent_id INTEGER, page INTEGER,
             page_end INTEGER, bbox TEXT, section_path TEXT NOT NULL DEFAULT '',
             ordinal INTEGER NOT NULL, text TEXT NOT NULL, text_hash TEXT NOT NULL,
             token_count INTEGER, embedding_model_id TEXT
           );
           INSERT INTO contents(id, sha256, size, parse_state, chunk_count)
             VALUES (1, 'legacy', 20, 'indexed', 1);
           INSERT INTO files(id, volume_uuid, inode, content_id, path, name, size,
                             state, detected_at)
             VALUES (1, 'v', 1, 1, '/tmp/legacy.md', 'legacy.md', 20, 'active', 1);
           INSERT INTO chunks(id, content_id, ordinal, text, text_hash, section_path)
             VALUES (1, 1, 0, '旧版分片仍可检索', 'old', '旧章节');"""
    )

    init_db(conn)

    assert conn.execute(
        "SELECT value FROM settings WHERE key = 'db_schema_version'"
    ).fetchone()[0] == str(SCHEMA_VERSION)
    chunk = conn.execute(
        "SELECT section_id, start_offset, end_offset, index_version FROM chunks"
    ).fetchone()
    assert chunk["section_id"] is not None
    assert (chunk["start_offset"], chunk["end_offset"], chunk["index_version"]) == (
        0, len("旧版分片仍可检索"), 1,
    )
    assert conn.execute(
        "SELECT status FROM index_versions WHERE content_id = 1"
    ).fetchone()[0] == "active"
    library_item = conn.execute(
        "SELECT content_id, title, summary, enrichment_status FROM library_items"
    ).fetchone()
    assert dict(library_item) == {
        "content_id": 1,
        "title": "legacy.md",
        "summary": "",
        "enrichment_status": "pending",
    }
    # Migration only derives metadata; the registered source path is untouched.
    assert conn.execute("SELECT path FROM files WHERE id=1").fetchone()[0] == "/tmp/legacy.md"
    conn.close()


def test_markdown_builds_nested_sections_offsets_and_active_version(
    db, tmp_path, monkeypatch,
):
    path = tmp_path / "knowledge.md"
    path.write_text(
        "# 知识架构\n\n" + "总览内容。" * 80
        + "\n\n## 混合检索\n\n" + "混合检索结合词法与语义。" * 70,
        encoding="utf-8",
    )
    monkeypatch.setattr(emb, "is_available", lambda: False)

    result = pipeline.index_content(db, 1, path)

    assert result["state"] == "indexed"
    content = db.execute(
        "SELECT active_index_version, chunk_count FROM contents WHERE id = 1"
    ).fetchone()
    assert content["active_index_version"] == 1
    sections = db.execute(
        """SELECT id, parent_id, heading_path, start_chunk_ordinal,
                  end_chunk_ordinal FROM sections ORDER BY ordinal"""
    ).fetchall()
    assert [row["heading_path"] for row in sections] == [
        "知识架构", "知识架构 › 混合检索",
    ]
    assert sections[1]["parent_id"] == sections[0]["id"]
    document = db.execute(
        "SELECT full_text FROM document_representations WHERE content_id = 1"
    ).fetchone()["full_text"]
    for chunk in db.execute(
        "SELECT text, start_offset, end_offset, section_id FROM chunks ORDER BY ordinal"
    ):
        assert document[chunk["start_offset"]:chunk["end_offset"]] == chunk["text"]
        assert chunk["section_id"] is not None
    assert db.execute(
        "SELECT status FROM index_versions WHERE content_id = 1 AND version = 1"
    ).fetchone()[0] == "active"


def test_failed_shadow_build_keeps_previous_active_version(
    db, tmp_path, monkeypatch,
):
    path = tmp_path / "knowledge.md"
    path.write_text("# 第一版\n\n" + "旧知识可继续使用。" * 80, encoding="utf-8")
    monkeypatch.setattr(emb, "is_available", lambda: False)
    pipeline.index_content(db, 1, path)
    db.commit()
    old_ids = [row["id"] for row in db.execute(
        "SELECT id FROM chunks WHERE content_id = 1 AND index_version = 1"
    )]

    path.write_text("# 第二版\n\n" + "新知识尚未激活。" * 80, encoding="utf-8")
    original = pipeline.index_chunk

    def fail_index(conn, chunk_id, text, section_path=""):
        original(conn, chunk_id, text, section_path)
        raise RuntimeError("shadow failure")

    monkeypatch.setattr(pipeline, "index_chunk", fail_index)
    with pytest.raises(RuntimeError, match="shadow failure"):
        pipeline.index_content(db, 1, path)

    assert db.execute(
        "SELECT active_index_version FROM contents WHERE id = 1"
    ).fetchone()[0] == 1
    assert [row["id"] for row in db.execute(
        "SELECT id FROM chunks WHERE content_id = 1 ORDER BY id"
    )] == old_ids
    failed = db.execute(
        """SELECT status, error FROM index_versions
           WHERE content_id = 1 AND version = 2"""
    ).fetchone()
    assert failed["status"] == "failed"
    assert "shadow failure" in failed["error"]
    routes = search(db, "旧知识", limit=20)
    assert any(chunk_id in old_ids for hits in routes.values() for chunk_id, _ in hits)


def test_hierarchy_routes_return_active_section_children(db, tmp_path, monkeypatch):
    path = tmp_path / "knowledge.md"
    path.write_text(
        "# 检索系统\n\n## 证据压缩\n\n" + "证据压缩保留原文偏移。" * 80,
        encoding="utf-8",
    )
    monkeypatch.setattr(emb, "is_available", lambda: False)
    pipeline.index_content(db, 1, path)

    routes = hierarchy_routes(db, "证据压缩", limit=20)

    assert routes["document"]
    assert routes["section"]
    active_ids = {row["id"] for row in db.execute(
        """SELECT ch.id FROM chunks ch JOIN contents c ON c.id = ch.content_id
           WHERE ch.index_version = c.active_index_version"""
    )}
    assert {cid for hits in routes.values() for cid, _ in hits} <= active_ids


def test_completed_version_can_be_atomically_rolled_back(
    db, tmp_path, monkeypatch,
):
    path = tmp_path / "knowledge.md"
    monkeypatch.setattr(emb, "is_available", lambda: False)
    path.write_text("# 第一版\n\n" + "旧版本独有知识。" * 80, encoding="utf-8")
    pipeline.index_content(db, 1, path)
    db.commit()
    old_ids = {row["id"] for row in db.execute(
        "SELECT id FROM chunks WHERE content_id = 1 AND index_version = 1"
    )}

    path.write_text("# 第二版\n\n" + "新版本独有知识。" * 80, encoding="utf-8")
    pipeline.index_content(db, 1, path)
    db.commit()
    assert db.execute(
        "SELECT active_index_version FROM contents WHERE id = 1"
    ).fetchone()[0] == 2

    result = pipeline.activate_index_version(db, 1, 1)

    assert result["previous_version"] == 2
    assert result["active_version"] == 1
    assert db.execute(
        "SELECT status FROM index_versions WHERE content_id = 1 AND version = 1"
    ).fetchone()[0] == "active"
    assert db.execute(
        "SELECT status FROM index_versions WHERE content_id = 1 AND version = 2"
    ).fetchone()[0] == "superseded"
    routes = search(db, "旧版本独有知识", limit=20)
    assert any(chunk_id in old_ids for hits in routes.values() for chunk_id, _ in hits)


def test_failed_or_incomplete_version_cannot_be_activated(db):
    db.execute(
        """INSERT INTO index_versions
           (content_id, version, status, section_count, chunk_count, created_at)
           VALUES (1, 2, 'failed', 0, 0, 1),
                  (1, 3, 'superseded', 0, 1, 1)"""
    )

    with pytest.raises(ValueError, match="已完成"):
        pipeline.activate_index_version(db, 1, 2)
    with pytest.raises(RuntimeError, match="不完整"):
        pipeline.activate_index_version(db, 1, 3)
    assert db.execute(
        "SELECT active_index_version FROM contents WHERE id = 1"
    ).fetchone()[0] == 1


def test_vector_route_never_returns_superseded_chunks(db, monkeypatch):
    if not pipeline._vector_table_exists(db):
        pytest.skip("sqlite-vec 不可用")
    import numpy as np

    from app.index.embedding import DIM

    db.execute("UPDATE contents SET active_index_version = 2 WHERE id = 1")
    chunk_id = db.execute(
        """INSERT INTO chunks
           (content_id, ordinal, text, text_hash, index_version)
           VALUES (1, 0, '只存在旧版本', 'old-vector', 1)"""
    ).lastrowid
    vector = np.ones(DIM, dtype=np.float32)
    vector /= np.linalg.norm(vector)
    vec.upsert(db, [(chunk_id, vector)])

    class FakeEmbedder:
        def encode_one(self, _text):
            return vector

    monkeypatch.setattr(emb, "is_available", lambda: True)
    monkeypatch.setattr(emb, "get_embedder", lambda: FakeEmbedder())

    assert search(db, "旧版本", limit=20)["vector"] == []
