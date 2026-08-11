"""扫描器对抗性测试。

不只验证"能跑通"，而是主动构造会打破设计约束的场景。
每个测试对应方案里的一条硬约束或一个性能关键项。
"""

from __future__ import annotations

import time

import pytest

from app.db.database import connect, init_db
from app.watcher.scanner import scan_source


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def source(db, tmp_path):
    db.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, created_at) "
        "VALUES ('test', ?, 'manual', 'manual', ?)",
        (str(tmp_path), time.time()),
    )
    db.commit()
    return 1


def test_rename_keeps_identity(db, source, tmp_path):
    """文件改名后必须复用原记录，且不重新哈希（PLAN §8、§12.1）。

    这是索引模式的核心性能保证：用户整理一次 Downloads 目录，
    若身份追踪失效就要全库重新解析 + 重新嵌入。
    """
    (tmp_path / "a.txt").write_text("内容")
    scan_source(db, source, tmp_path)
    before = db.execute("SELECT id, content_id FROM files").fetchone()

    (tmp_path / "a.txt").rename(tmp_path / "b.txt")
    stats = scan_source(db, source, tmp_path)
    after = db.execute("SELECT id, content_id, name FROM files").fetchone()

    assert after["id"] == before["id"], "改名后 file_id 变了，索引会断"
    assert after["content_id"] == before["content_id"], "改名触发了重新哈希"
    assert after["name"] == "b.txt"
    assert stats.path_updated == 1
    assert stats.registered == 0, "改名被误判为新文件"


def test_identical_content_shares_row(db, source, tmp_path):
    """内容相同的文件共享一条 contents（PLAN §9 contents 表）。

    这决定了重复文件不会重复解析、重复向量化。
    """
    (tmp_path / "x.txt").write_text("一样的内容")
    (tmp_path / "y.txt").write_text("一样的内容")
    scan_source(db, source, tmp_path)

    files = db.execute("SELECT count(*) c FROM files").fetchone()["c"]
    contents = db.execute("SELECT count(*) c FROM contents").fetchone()["c"]
    assert files == 2
    assert contents == 1, "重复内容没有去重，会重复嵌入"


def test_content_change_detected(db, source, tmp_path):
    """内容变化必须被检测到并重新登记（PLAN §12.5 增量更新的前提）。"""
    f = tmp_path / "doc.txt"
    f.write_text("原内容")
    scan_source(db, source, tmp_path)
    old = db.execute("SELECT content_id FROM files").fetchone()["content_id"]

    time.sleep(1.1)  # 跨过 mtime 的 1 秒容差
    f.write_text("改过的内容")
    stats = scan_source(db, source, tmp_path)
    new = db.execute("SELECT content_id FROM files").fetchone()["content_id"]

    assert new != old, "内容变了但没检测到，索引会陈旧"
    assert stats.content_updated == 1


def test_excluded_dirs_not_descended(db, source, tmp_path):
    """node_modules 等目录必须不下钻（PLAN §7.7 ①）。

    开发者的 ~/Documents 里一个 node_modules 就是几万个文件，
    没有这道闸，文件库会被彻底淹没。
    """
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    for i in range(30):
        (nm / f"m{i}.txt").write_text("x")
    (tmp_path / "real.txt").write_text("真实文件")

    scan_source(db, source, tmp_path)
    leaked = db.execute(
        "SELECT count(*) c FROM files WHERE path LIKE '%node_modules%'"
    ).fetchone()["c"]
    assert leaked == 0, "node_modules 泄漏进库"
    assert db.execute("SELECT count(*) c FROM files").fetchone()["c"] == 1


def test_package_dirs_not_descended(db, source, tmp_path):
    """.app / .pages 等 package 目录整体当作一个文件（PLAN §7.7 ②）。"""
    pkg = tmp_path / "note.pages"
    pkg.mkdir()
    (pkg / "index.xml").write_text("<xml/>")
    (pkg / "data.bin").write_bytes(b"\x00")

    scan_source(db, source, tmp_path)
    inside = db.execute(
        "SELECT count(*) c FROM files WHERE path LIKE '%note.pages/%'"
    ).fetchone()["c"]
    assert inside == 0, "package 内部文件泄漏，用户会看到一堆 XML 碎片"


def test_scan_never_modifies_files(db, source, tmp_path):
    """扫描绝不改动原文件 —— PLAN §1 第一条不可协商约束。"""
    paths = []
    for i in range(5):
        p = tmp_path / f"f{i}.txt"
        p.write_text(f"内容{i}")
        paths.append(p)
    before = {p: (p.stat().st_size, p.stat().st_mtime) for p in paths}

    scan_source(db, source, tmp_path)

    for p in paths:
        assert p.exists(), f"{p.name} 被删除了"
        assert (p.stat().st_size, p.stat().st_mtime) == before[p], f"{p.name} 被改动了"


def test_stats_reconcile(db, source, tmp_path):
    """统计必须对账：scanned == 各分项之和。

    对不上意味着有文件被静默丢弃 —— 这类 bug 在生产里极难发现。
    """
    (tmp_path / "a.txt").write_text("1")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "c.o").write_bytes(b"obj")  # 白名单外，应被忽略

    stats = scan_source(db, source, tmp_path)
    assert stats.scanned == stats.accounted, (
        f"统计对不上：scanned={stats.scanned} accounted={stats.accounted}"
    )


def test_rescan_is_idempotent(db, source, tmp_path):
    """重复扫描不产生重复记录。"""
    (tmp_path / "a.txt").write_text("内容")
    scan_source(db, source, tmp_path)
    n1 = db.execute("SELECT count(*) c FROM files").fetchone()["c"]

    stats = scan_source(db, source, tmp_path)
    n2 = db.execute("SELECT count(*) c FROM files").fetchone()["c"]

    assert n1 == n2 == 1
    assert stats.unchanged == 1
    assert stats.registered == 0
