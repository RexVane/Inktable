from __future__ import annotations

from app.db import database
from app.library.core import (
    list_library_items,
    replace_library_item_tags,
    set_related_items,
    sync_library_items,
    update_enrichment,
)
from app.library.query import library_item_detail, library_stats


def _seed() -> tuple[object, int, int]:
    conn = database.connect(":memory:")
    database.init_db(conn)
    conn.executemany(
        "INSERT INTO sources(id, name, path, kind, discovered_by, enabled, created_at) "
        "VALUES (?, ?, ?, 'manual', 'manual', 1, 1)",
        [(1, "课程", "/course"), (2, "论文", "/papers")],
    )
    conn.executemany(
        "INSERT INTO contents(id, sha256, size, parse_state) VALUES (?, ?, 100, 'indexed')",
        [(1, "sha-os"), (2, "sha-rag")],
    )
    conn.executemany(
        """INSERT INTO files
           (id, volume_uuid, inode, content_id, path, name, source_id, ext,
            size, state, detected_at, mtime)
           VALUES (?, 'vol', ?, ?, ?, ?, ?, ?, 100, 'registered', 1, ?)""",
        [
            (1, 101, 1, "/course/os.pdf", "操作系统.pdf", 1, "pdf", 10),
            (2, 102, 2, "/papers/rag.md", "RAG笔记.md", 2, "md", 20),
        ],
    )
    sync_library_items(conn, now=30)
    conn.commit()
    rows = conn.execute(
        "SELECT id, content_id FROM library_items ORDER BY content_id"
    ).fetchall()
    return conn, int(rows[0]["id"]), int(rows[1]["id"])


def test_disabled_source_hides_item_without_destroying_ai_metadata() -> None:
    conn, os_item, _ = _seed()
    try:
        assert [r["id"] for r in list_library_items(conn)]
        assert update_enrichment(
            conn,
            os_item,
            summary="操作系统课程资料",
            category_id=None,
            language="zh-CN",
            model="test-model",
            prompt_version="v1",
            input_hash="sha-os",
            now=40,
        )
        conn.commit()

        conn.execute("UPDATE sources SET enabled=0 WHERE id=1")
        conn.commit()
        visible_ids = {int(row["id"]) for row in list_library_items(conn)}
        assert os_item not in visible_ids
        assert library_item_detail(conn, os_item) is None

        # The row is retained rather than deleted, so re-enabling the physical
        # source restores the user's previous AI metadata.
        stored = conn.execute(
            "SELECT summary FROM library_items WHERE id=?", (os_item,)
        ).fetchone()
        assert stored["summary"] == "操作系统课程资料"

        conn.execute("UPDATE sources SET enabled=1 WHERE id=1")
        conn.commit()
        restored = library_item_detail(conn, os_item)
        assert restored is not None
        assert restored["summary"] == "操作系统课程资料"
    finally:
        conn.close()


def test_missing_file_requires_preservation_copy_to_remain_visible() -> None:
    conn, os_item, _ = _seed()
    try:
        conn.execute("UPDATE files SET state='missing', preserved_path=NULL WHERE id=1")
        conn.commit()
        assert library_item_detail(conn, os_item) is None

        conn.execute(
            "UPDATE files SET preserved_path='/app/preserved/os.pdf' WHERE id=1"
        )
        conn.commit()
        detail = library_item_detail(conn, os_item)
        assert detail is not None
        assert detail["source_files"][0]["preserved_path"] == "/app/preserved/os.pdf"
    finally:
        conn.close()


def test_library_detail_exposes_controlled_tags_sources_and_relations() -> None:
    conn, os_item, rag_item = _seed()
    try:
        conn.executemany(
            "INSERT INTO tags(id, name) VALUES (?, ?)",
            [(1, "操作系统"), (2, "死锁")],
        )
        replace_library_item_tags(
            conn,
            os_item,
            [(1, "ai", 0.95), (2, "ai", 0.88)],
        )
        set_related_items(conn, os_item, [(rag_item, 0.82)], now=50)
        conn.commit()

        detail = library_item_detail(conn, os_item)
        assert detail is not None
        assert [tag["name"] for tag in detail["tags"]] == ["操作系统", "死锁"]
        assert detail["source_files"][0]["path"] == "/course/os.pdf"
        assert detail["related"][0]["id"] == rag_item
        assert detail["related"][0]["score"] == 0.82

        stats = library_stats(conn)
        assert stats["total"] == 2
        assert stats["tagged"] == 1
        assert stats["relations"] == 1
    finally:
        conn.close()
