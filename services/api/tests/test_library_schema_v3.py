from __future__ import annotations

import sqlite3

from app.db import database
from app.db.schema import SCHEMA_VERSION
from app.library.core import compute_input_hash, sync_library_items


def test_schema_v3_creates_ai_library_tables() -> None:
    assert SCHEMA_VERSION == 3
    conn = database.connect(":memory:")
    try:
        database.init_db(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"library_items", "library_item_tags", "library_relations"}.issubset(tables)
        assert database.get_setting(conn, "db_schema_version") == "3"
    finally:
        conn.close()


def test_v2_database_upgrades_to_v3_without_touching_physical_files() -> None:
    conn = database.connect(":memory:")
    try:
        # Build the current SQL surface once, then emulate a database last
        # opened by the v2 application. The upgrade contract is additive:
        # physical file rows remain intact and the derived library layer is
        # rebuilt from them.
        database.init_db(conn)
        conn.execute(
            "UPDATE settings SET value='2' WHERE key='db_schema_version'"
        )
        conn.execute(
            "INSERT INTO sources(id, name, path, kind, discovered_by, enabled, created_at) "
            "VALUES (1, '测试来源', '/vault', 'manual', 'manual', 1, 1)"
        )
        conn.execute(
            "INSERT INTO contents(id, sha256, size, parse_state) "
            "VALUES (1, 'sha-v2-upgrade', 123, 'indexed')"
        )
        conn.execute(
            "INSERT INTO files(id, volume_uuid, inode, content_id, path, name, source_id, "
            "ext, size, state, detected_at) "
            "VALUES (1, 'vol', 1, 1, '/vault/os.pdf', 'os.pdf', 1, 'pdf', 123, "
            "'registered', 1)"
        )
        conn.commit()

        database.init_db(conn)

        # v3 bootstrap happens during normal init_db(), before any explicit
        # Library sync or LLM work. Its input_hash is only an identity seed;
        # the first sync upgrades it to the active-document-aware hash.
        bootstrapped = conn.execute(
            "SELECT content_id, title, item_type, input_hash FROM library_items"
        ).fetchone()
        assert dict(bootstrapped) == {
            "content_id": 1,
            "title": "os.pdf",
            "item_type": "document",
            "input_hash": "sha-v2-upgrade",
        }

        result = sync_library_items(conn, now=10)
        conn.commit()
        assert database.get_setting(conn, "db_schema_version") == "3"
        assert result["created"] == 0
        assert result["refreshed"] == 1
        item = conn.execute(
            "SELECT content_id, title, item_type, input_hash FROM library_items"
        ).fetchone()
        assert dict(item) == {
            "content_id": 1,
            "title": "os.pdf",
            "item_type": "pdf",
            "input_hash": compute_input_hash("sha-v2-upgrade", 1, None),
        }
        physical = conn.execute(
            "SELECT path, name, content_id FROM files WHERE id=1"
        ).fetchone()
        assert dict(physical) == {
            "path": "/vault/os.pdf",
            "name": "os.pdf",
            "content_id": 1,
        }
    finally:
        conn.close()


def test_future_schema_is_rejected_before_ai_library_sql_runs() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES ('db_schema_version', ?)",
        (str(SCHEMA_VERSION + 1),),
    )
    try:
        try:
            database.init_db(conn)
        except RuntimeError as exc:
            assert "高于当前程序支持" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("newer schema must be rejected")
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_items'"
        ).fetchone() is None
    finally:
        conn.close()
