from __future__ import annotations

import sqlite3

from app.library.core import (
    compute_input_hash,
    ensure_library_schema,
    get_library_item,
    replace_library_item_tags,
    set_related_items,
    sync_library_items,
    update_enrichment,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE contents (
            id INTEGER PRIMARY KEY,
            sha256 TEXT NOT NULL UNIQUE,
            size INTEGER NOT NULL,
            active_index_version INTEGER NOT NULL DEFAULT 1,
            chunk_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE tags (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            color TEXT
        );
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE files (
            id INTEGER PRIMARY KEY,
            content_id INTEGER REFERENCES contents(id) ON DELETE SET NULL,
            source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
            name TEXT NOT NULL,
            ext TEXT,
            state TEXT NOT NULL,
            preserved_path TEXT,
            mtime REAL
        );
        CREATE TABLE document_representations (
            id INTEGER PRIMARY KEY,
            content_id INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
            index_version INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            abstract TEXT,
            abstract_model TEXT,
            text_hash TEXT
        );
        """
    )
    ensure_library_schema(conn)
    return conn


def _content(conn: sqlite3.Connection, *, cid: int, sha: str, name: str, ext: str) -> None:
    conn.execute(
        "INSERT INTO contents(id, sha256, size, active_index_version) VALUES (?, ?, 1, 1)",
        (cid, sha),
    )
    conn.execute(
        """
        INSERT INTO files(id, content_id, source_id, name, ext, state, preserved_path, mtime)
        VALUES (?, ?, NULL, ?, ?, 'registered', NULL, ?)
        """,
        (cid, cid, name, ext, float(cid)),
    )


def test_sync_creates_pending_item_without_exposing_retrieval_abstract() -> None:
    conn = _db()
    sha = "a" * 64
    _content(conn, cid=1, sha=sha, name="操作系统.pdf", ext="pdf")
    conn.execute(
        """
        INSERT INTO document_representations
            (content_id, index_version, title, abstract, abstract_model, text_hash)
        VALUES (1, 1, '操作系统', '进程、内存与文件系统', 'ollama:bge-summary', 'doc-v1')
        """
    )

    first = sync_library_items(conn, now=10)
    second = sync_library_items(conn, now=20)

    assert first == {"created": 1, "refreshed": 0, "stale": 0}
    assert second == {"created": 0, "refreshed": 1, "stale": 0}
    row = conn.execute("SELECT * FROM library_items").fetchone()
    assert row["title"] == "操作系统"
    assert row["item_type"] == "pdf"
    assert row["summary"] == ""
    assert row["enrichment_status"] == "pending"
    assert row["enrichment_model"] is None
    assert row["prompt_version"] is None
    assert row["input_hash"] == compute_input_hash(sha, 1, "doc-v1")
    assert "abstract" not in row.keys()


def test_retrieval_abstract_changes_do_not_invalidate_library_summary() -> None:
    conn = _db()
    sha = "a" * 64
    _content(conn, cid=1, sha=sha, name="主题.pdf", ext="pdf")
    conn.execute(
        """INSERT INTO document_representations
           (content_id, index_version, title, abstract, abstract_model, text_hash)
           VALUES (1, 1, '主题', '旧检索词', 'model-v1', 'doc-v1')"""
    )
    sync_library_items(conn, now=10)
    item = conn.execute("SELECT * FROM library_items").fetchone()
    assert update_enrichment(
        conn,
        item["id"],
        summary="真正面向用户的知识卡片摘要",
        category_id=None,
        model="qwen3:8b",
        prompt_version="library-enrichment-v1",
        input_hash=item["input_hash"],
        now=20,
    )

    conn.execute(
        "UPDATE document_representations SET abstract='新检索词' WHERE content_id=1"
    )
    result = sync_library_items(conn, now=30)
    current = conn.execute("SELECT * FROM library_items").fetchone()

    assert result["stale"] == 0
    assert current["enrichment_status"] == "ready"
    assert current["summary"] == "真正面向用户的知识卡片摘要"


def test_sync_cleans_legacy_abstract_bootstrap_from_v3_development_build() -> None:
    conn = _db()
    sha = "a" * 64
    _content(conn, cid=1, sha=sha, name="legacy.pdf", ext="pdf")
    conn.execute(
        "INSERT INTO document_representations(content_id,index_version,title,text_hash) "
        "VALUES (1,1,'legacy','doc-v1')"
    )
    sync_library_items(conn, now=10)
    item_id = conn.execute("SELECT id FROM library_items").fetchone()[0]
    conn.execute(
        """UPDATE library_items
           SET summary='检索关键词摘要', enrichment_status='ready',
               enrichment_model='qwen3:8b', enriched_at=11
           WHERE id=?""",
        (item_id,),
    )

    result = sync_library_items(conn, now=20)
    row = conn.execute("SELECT * FROM library_items WHERE id=?", (item_id,)).fetchone()

    assert result["refreshed"] == 1
    assert row["summary"] == ""
    assert row["enrichment_status"] == "pending"
    assert row["enrichment_model"] is None
    assert row["enriched_at"] is None


def test_reindex_same_file_marks_existing_enrichment_stale() -> None:
    conn = _db()
    sha = "a" * 64
    _content(conn, cid=1, sha=sha, name="scan.pdf", ext="pdf")
    conn.execute(
        "INSERT INTO document_representations(content_id,index_version,title,text_hash) "
        "VALUES (1,1,'扫描件','ocr-v1')"
    )
    sync_library_items(conn, now=10)
    item = conn.execute("SELECT * FROM library_items").fetchone()
    old_hash = item["input_hash"]
    assert update_enrichment(
        conn,
        item["id"],
        summary="第一版 OCR 摘要",
        category_id=None,
        model="qwen3:8b",
        prompt_version="library-enrichment-v1",
        input_hash=old_hash,
        now=15,
    )

    # File bytes are identical; only the active parsed/OCR representation changed.
    conn.execute(
        "INSERT INTO document_representations(content_id,index_version,title,text_hash) "
        "VALUES (1,2,'扫描件','ocr-v2')"
    )
    conn.execute("UPDATE contents SET active_index_version=2 WHERE id=1")

    result = sync_library_items(conn, now=20)
    current = conn.execute("SELECT * FROM library_items").fetchone()
    assert result["stale"] == 1
    assert current["enrichment_status"] == "stale"
    assert current["input_hash"] == compute_input_hash(sha, 2, "ocr-v2")
    assert current["input_hash"] != old_hash


def test_ignored_only_content_is_not_promoted_to_library() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO contents(id, sha256, size, active_index_version) VALUES (1, ?, 1, 1)",
        ("a" * 64,),
    )
    conn.execute(
        """
        INSERT INTO files(id, content_id, source_id, name, ext, state, preserved_path, mtime)
        VALUES (1, 1, NULL, 'noise.md', 'md', 'ignored', NULL, 1)
        """
    )

    result = sync_library_items(conn, now=10)

    assert result["created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 0


def test_disabled_source_is_not_promoted_to_library() -> None:
    conn = _db()
    conn.execute("INSERT INTO sources(id, enabled) VALUES (1, 0)")
    conn.execute(
        "INSERT INTO contents(id, sha256, size, active_index_version) VALUES (1, ?, 1, 1)",
        ("a" * 64,),
    )
    conn.execute(
        """
        INSERT INTO files(id, content_id, source_id, name, ext, state, preserved_path, mtime)
        VALUES (1, 1, 1, 'hidden.pdf', 'pdf', 'registered', NULL, 1)
        """
    )

    sync_library_items(conn)

    assert conn.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 0


def test_missing_file_requires_preservation_copy() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO contents(id, sha256, size, active_index_version) VALUES (1, ?, 1, 1)",
        ("a" * 64,),
    )
    conn.execute(
        """
        INSERT INTO files(id, content_id, source_id, name, ext, state, preserved_path, mtime)
        VALUES (1, 1, NULL, 'gone.pdf', 'pdf', 'missing', NULL, 1)
        """
    )
    sync_library_items(conn)
    assert conn.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 0

    conn.execute("UPDATE files SET preserved_path = '/preserved/gone.pdf' WHERE id = 1")
    sync_library_items(conn)
    assert conn.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 1


def test_enrichment_rejects_result_when_active_document_changed_without_sync() -> None:
    conn = _db()
    sha = "a" * 64
    _content(conn, cid=1, sha=sha, name="notes.md", ext="md")
    conn.execute(
        "INSERT INTO document_representations(content_id,index_version,title,text_hash) "
        "VALUES (1,1,'notes','doc-v1')"
    )
    sync_library_items(conn, now=10)
    item = conn.execute("SELECT * FROM library_items").fetchone()
    old_hash = item["input_hash"]

    # The parser/OCR activates a new representation before the asynchronous
    # model call returns, and Library sync has not run yet.
    conn.execute(
        "INSERT INTO document_representations(content_id,index_version,title,text_hash) "
        "VALUES (1,2,'notes','doc-v2')"
    )
    conn.execute("UPDATE contents SET active_index_version=2 WHERE id=1")

    accepted = update_enrichment(
        conn,
        item["id"],
        summary="old answer",
        category_id=None,
        model="qwen3:8b",
        prompt_version="library-enrichment-v1",
        input_hash=old_hash,
        now=20,
    )

    assert accepted is False
    current = get_library_item(conn, item["id"])
    assert current["summary"] == ""


def test_tags_use_existing_controlled_vocabulary() -> None:
    conn = _db()
    _content(conn, cid=1, sha="a" * 64, name="deadlock.md", ext="md")
    conn.execute("INSERT INTO tags(id, name) VALUES (1, '死锁'), (2, '操作系统')")
    sync_library_items(conn)
    item_id = conn.execute("SELECT id FROM library_items").fetchone()[0]

    replace_library_item_tags(
        conn,
        item_id,
        [(1, "ai", 0.95), (2, "rule", 1.0)],
    )

    rows = conn.execute(
        "SELECT tag_id, source, confidence FROM library_item_tags ORDER BY tag_id"
    ).fetchall()
    assert [(r["tag_id"], r["source"]) for r in rows] == [(1, "ai"), (2, "rule")]


def test_related_to_is_stored_once_in_canonical_order() -> None:
    conn = _db()
    _content(conn, cid=1, sha="a" * 64, name="a.pdf", ext="pdf")
    _content(conn, cid=2, sha="b" * 64, name="b.pdf", ext="pdf")
    sync_library_items(conn)
    ids = [r[0] for r in conn.execute("SELECT id FROM library_items ORDER BY id")]

    set_related_items(conn, ids[1], [(ids[0], 0.91), (ids[0], 0.90)], now=30)

    row = conn.execute("SELECT * FROM library_relations").fetchone()
    assert row["source_item_id"] == min(ids)
    assert row["target_item_id"] == max(ids)
    assert row["score"] == 0.91
