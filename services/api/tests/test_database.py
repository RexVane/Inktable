"""Database startup safety gates."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.db import database
from app.db.schema import SCHEMA_VERSION


def test_init_rejects_database_from_newer_app() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES ('db_schema_version', ?)",
        (str(SCHEMA_VERSION + 1),),
    )

    with pytest.raises(RuntimeError, match="高于当前程序支持"):
        database.init_db(conn)

    # Rejection happens before current schema SQL is applied.
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'"
    ).fetchone() is None
    conn.close()


def test_init_rebuilds_legacy_contentless_fts_for_deletion() -> None:
    conn = database.connect(":memory:")
    database.init_db(conn)
    conn.execute(
        "INSERT INTO contents(id, sha256, size) VALUES (1, 'legacy-fts', 10)"
    )
    conn.execute(
        """INSERT INTO chunks(id, content_id, ordinal, text, text_hash,
                              section_path)
           VALUES (1, 1, 0, '银行家算法正文', 'hash', '操作系统')"""
    )
    conn.execute("DROP TABLE chunks_fts")
    conn.execute("DROP TABLE chunks_fts_tri")
    conn.execute(
        "CREATE VIRTUAL TABLE chunks_fts USING fts5("
        "text, content='', tokenize='unicode61')"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE chunks_fts_tri USING fts5("
        "text, content='', tokenize='trigram')"
    )
    conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES (1, '旧索引')")
    conn.execute("INSERT INTO chunks_fts_tri(rowid, text) VALUES (1, '旧索引')")
    conn.commit()

    database.init_db(conn)

    for table in ("chunks_fts", "chunks_fts_tri"):
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone()[0]
        assert "contentless_delete=1" in "".join(sql.split())
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
        conn.execute(f"DELETE FROM {table} WHERE rowid = 1")
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    conn.close()


def test_connect_readonly_cannot_write_and_sets_no_journal_mode(tmp_path) -> None:
    """只读旁路必须真的只读，而且不能碰 journal_mode。

    背景：向量矩阵预热线程原先走普通 connect()，它会执行
    `PRAGMA journal_mode = WAL` —— 那需要短暂排他锁，于是启动时预热与正常
    写入抢锁，TestClient 用例开始**间歇性**报 database is locked
    （三轮全量挂两次、单独跑却通过）。只读连接不设 journal_mode，
    从根上没有这种竞争。

    这条用例守的是一般化的教训：只读旁路（预热、审计、统计）不该走读写连接。
    """
    path = tmp_path / "library.db"
    writer = database.connect(path)
    database.init_db(writer)
    writer.close()

    reader = database.connect_readonly(path)
    try:
        assert reader.execute("SELECT count(*) FROM files").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO settings(key, value) VALUES ('x', 'y')")
    finally:
        reader.close()


def test_default_data_directory_is_native_and_separate_from_control_root(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.delenv("ORDO_DATA_DIR", raising=False)
    monkeypatch.delenv("INKTABLE_DATA_DIR", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(database.Path, "home", lambda: tmp_path)

    monkeypatch.setattr(database.sys, "platform", "darwin")
    assert database._resolve_app_dir() == (
        tmp_path / "Library" / "Application Support" / "Ordo" / "data"
    )
    monkeypatch.setattr(database.sys, "platform", "win32")
    assert database._resolve_app_dir() == tmp_path / "AppData" / "Roaming" / "Ordo" / "data"
    monkeypatch.setattr(database.sys, "platform", "linux")
    assert database._resolve_app_dir() == tmp_path / ".local" / "share" / "Ordo"

    legacy = tmp_path / "Library" / "Application Support" / "Ordo"
    legacy.mkdir(parents=True)
    (legacy / "library.db").write_bytes(b"existing")
    assert database._resolve_app_dir() == legacy

    ink = tmp_path / "Library" / "Application Support" / "Inktable"
    ink.mkdir(parents=True)
    (ink / "library.db").write_bytes(b"old-install")
    # Ordo library still wins when both exist.
    assert database._resolve_app_dir() == legacy
    (legacy / "library.db").unlink()
    assert database._resolve_app_dir() == ink


def test_self_referencing_fk_columns_are_indexed() -> None:
    """外键指向的列必须有索引，尤其是 chunks.parent_id 这个**自引用**外键。

    chunks.parent_id REFERENCES chunks(id) ON DELETE SET NULL。开着
    PRAGMA foreign_keys 时，删一个 chunk 就要找出所有 parent_id 指向它的行来
    执行 SET NULL —— 没有索引就是每删一行全表扫一遍。

    实测代价：真实库 164,180 个分片、删 16,753 个（清理 670 个孤儿 content）
    时，`DELETE FROM contents WHERE NOT EXISTS(...)` 的级联跑了 30 分钟以上
    仍未完成（约 27 亿次行访问）；补上索引后同一步几秒完成。
    sections.parent_id 与 chunks.section_id 本来就有索引，唯独这一列漏了，
    所以这条用例守的是"别再漏"。
    """
    conn = database.connect(":memory:")
    try:
        database.init_db(conn)
        indexed_columns = set()
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'chunks'"
        ):
            for info in conn.execute(f"PRAGMA index_info({row[0]!r})"):
                indexed_columns.add(info[2])
        assert "parent_id" in indexed_columns, "chunks.parent_id 缺索引，级联删除会退化成全表扫"
        assert "content_id" in indexed_columns
        assert "section_id" in indexed_columns
    finally:
        conn.close()


def test_single_instance_lock_blocks_second_holder(tmp_path, monkeypatch) -> None:
    lock_path = tmp_path / "library.db.lock"
    monkeypatch.setenv("ORDO_DB", str(tmp_path / "library.db"))
    database.release_single_instance_lock()
    database.acquire_single_instance_lock()

    contender = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            contender.seek(0)
            with pytest.raises(OSError):
                msvcrt.locking(contender.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            with pytest.raises(OSError):
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        contender.close()
        database.release_single_instance_lock()


def test_single_instance_lock_can_be_reacquired_after_release(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORDO_DB", str(tmp_path / "library.db"))
    database.release_single_instance_lock()

    database.acquire_single_instance_lock()
    database.release_single_instance_lock()
    database.acquire_single_instance_lock()
    database.release_single_instance_lock()


def _file_db(path):
    conn = database.connect(path)
    database.init_db(conn)
    return conn


def test_integrity_check_and_daily_backup_are_restorable(tmp_path) -> None:
    db_path = tmp_path / "library.db"
    conn = _file_db(db_path)
    conn.execute(
        "INSERT INTO categories (name, sort_order) VALUES ('重要资料', 0)"
    )
    conn.commit()

    assert database.integrity_check(conn) == ["ok"]
    backup = database.create_daily_backup(
        conn, backup_dir=tmp_path / "backups", today=date(2026, 8, 12)
    )
    assert backup.name == "library-2026-08-12.db"
    assert database.backup_is_restorable(backup)

    restored = sqlite3.connect(backup)
    try:
        assert restored.execute(
            "SELECT name FROM categories WHERE name = '重要资料'"
        ).fetchone()[0] == "重要资料"
    finally:
        restored.close()
        conn.close()


def test_daily_backup_is_created_once_and_does_not_capture_later_writes(tmp_path) -> None:
    conn = _file_db(tmp_path / "library.db")
    backups = tmp_path / "backups"
    day = date(2026, 8, 12)
    first = database.create_daily_backup(conn, backup_dir=backups, today=day)

    conn.execute("INSERT INTO categories (name, sort_order) VALUES ('稍后写入', 0)")
    conn.commit()
    second = database.create_daily_backup(conn, backup_dir=backups, today=day)
    assert second == first

    snap = sqlite3.connect(first)
    try:
        assert snap.execute(
            "SELECT count(*) FROM categories WHERE name = '稍后写入'"
        ).fetchone()[0] == 0
    finally:
        snap.close()
        conn.close()


def test_concurrent_daily_backup_reuses_one_restorable_snapshot(tmp_path) -> None:
    db_path = tmp_path / "library.db"
    seed = _file_db(db_path)
    seed.execute(
        "INSERT INTO categories (name, sort_order) VALUES ('并发备份标记', 0)"
    )
    seed.commit()
    seed.close()

    backup_dir = tmp_path / "backups"

    def create() -> Path:
        conn = database.connect(db_path)
        try:
            return database.create_daily_backup(
                conn, backup_dir=backup_dir, today=date(2026, 8, 12),
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        backups = list(executor.map(lambda _item: create(), range(2)))

    assert backups[0] == backups[1]
    assert database.backup_is_restorable(backups[0])


def test_backup_rotation_keeps_latest_seven(tmp_path) -> None:
    conn = _file_db(tmp_path / "library.db")
    backups = tmp_path / "backups"
    start = date(2026, 8, 1)
    for offset in range(9):
        database.create_daily_backup(
            conn, backup_dir=backups, today=start + timedelta(days=offset)
        )

    names = sorted(p.name for p in backups.glob("library-*.db"))
    assert names == [f"library-2026-08-{day:02d}.db" for day in range(3, 10)]
    conn.close()


def test_restore_writes_new_file_and_never_overwrites_existing_database(tmp_path) -> None:
    conn = _file_db(tmp_path / "library.db")
    conn.execute("INSERT INTO categories (name, sort_order) VALUES ('备份内容', 0)")
    conn.commit()
    backup = database.create_daily_backup(conn, backup_dir=tmp_path / "backups")

    candidate = tmp_path / "restore-candidate.db"
    assert database.restore_backup_to(backup, candidate) == candidate
    assert database.backup_is_restorable(candidate)

    existing = tmp_path / "must-not-overwrite.db"
    existing.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        database.restore_backup_to(backup, existing)
    assert existing.read_bytes() == b"keep me"
    conn.close()


def test_corrupt_backup_is_rejected(tmp_path) -> None:
    corrupt = tmp_path / "broken.db"
    corrupt.write_bytes(b"not a sqlite database")
    assert not database.backup_is_restorable(corrupt)

    with pytest.raises(database.BackupError, match="不可恢复"):
        database.restore_backup_to(corrupt, tmp_path / "candidate.db")


def test_backup_refuses_uncommitted_transaction(tmp_path) -> None:
    conn = _file_db(tmp_path / "library.db")
    conn.execute("INSERT INTO categories (name, sort_order) VALUES ('未提交', 0)")
    with pytest.raises(database.BackupError, match="未提交事务"):
        database.create_daily_backup(conn, backup_dir=tmp_path / "backups")
    conn.rollback()
    conn.close()
