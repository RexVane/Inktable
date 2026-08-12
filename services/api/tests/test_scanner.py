"""扫描器对抗性测试。

不只验证"能跑通"，而是主动构造会打破设计约束的场景。
每个测试对应方案里的一条硬约束或一个性能关键项。
"""

from __future__ import annotations

import mimetypes
import time

import pytest

from app.db.database import connect, init_db
from app.watcher import scanner
from app.watcher.scanner import scan_source, should_skip_path


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


def test_rename_keeps_identity(db, source, tmp_path, monkeypatch):
    """文件改名后必须复用原记录，且不重新哈希（PLAN §8、§12.1）。

    这是索引模式的核心性能保证：用户整理一次 Downloads 目录，
    若身份追踪失效就要全库重新解析 + 重新嵌入。
    """
    (tmp_path / "a.txt").write_text("内容")
    scan_source(db, source, tmp_path)
    before = db.execute("SELECT id, content_id FROM files").fetchone()

    (tmp_path / "a.txt").rename(tmp_path / "b.txt")
    monkeypatch.setattr(scanner, "hash_file", lambda _p: pytest.fail("纯改名不应重哈希"))
    stats = scan_source(db, source, tmp_path)
    after = db.execute("SELECT id, content_id, name FROM files").fetchone()

    assert after["id"] == before["id"], "改名后 file_id 变了，索引会断"
    assert after["content_id"] == before["content_id"], "改名触发了重新哈希"
    assert after["name"] == "b.txt"
    assert stats.path_updated == 1
    assert stats.registered == 0, "改名被误判为新文件"


def test_same_size_subsecond_edit_is_rehashed(db, source, tmp_path, monkeypatch):
    """同大小编辑即使 mtime 只变化不足一秒，也不能走 unchanged 快路径。"""
    f = tmp_path / "quick.txt"
    f.write_text("AAAA", encoding="utf-8")
    scan_source(db, source, tmp_path)
    before = db.execute("SELECT content_id, mtime FROM files").fetchone()

    f.write_text("BBBB", encoding="utf-8")  # 同字节数、同 inode
    # 用显式时间戳消除调度抖动，可靠复现旧实现的 1 秒容差缺陷。
    edited_mtime = before["mtime"] + 0.25
    import os
    os.utime(f, (edited_mtime, edited_mtime))

    original_hash = scanner.hash_file
    calls = 0

    def counted_hash(path):
        nonlocal calls
        calls += 1
        return original_hash(path)

    monkeypatch.setattr(scanner, "hash_file", counted_hash)
    stats = scan_source(db, source, tmp_path)
    after = db.execute("SELECT content_id, mtime FROM files").fetchone()

    assert calls == 1
    assert stats.content_updated == 1
    assert stats.unchanged == 0
    assert after["content_id"] != before["content_id"]
    assert 0 < after["mtime"] - before["mtime"] < 1


def test_rename_or_atomic_replace_to_ignored_extension_detaches_old_content(
    db, source, tmp_path, monkeypatch
):
    """改名或随后原子替换到 ignore 扩展后，旧全文不能继续挂在该文件上。"""
    original = tmp_path / "searchable.txt"
    original.write_text("曾经可以全文检索的内容", encoding="utf-8")
    scan_source(db, source, tmp_path)
    before = db.execute("SELECT id, content_id FROM files").fetchone()
    assert before["content_id"] is not None

    ignored = tmp_path / "searchable.unknown"
    original.rename(ignored)
    monkeypatch.setattr(scanner, "hash_file", lambda _p: pytest.fail("ignore 扩展不应读取内容"))
    stats = scan_source(db, source, tmp_path)
    after = db.execute(
        "SELECT id, content_id, path, name, ext, mime, state, inode FROM files"
    ).fetchone()

    assert after["id"] == before["id"]
    assert after["content_id"] is None
    assert after["path"] == str(ignored)
    assert after["name"] == ignored.name
    assert after["ext"] == ".unknown"
    assert after["state"] == "ignored"
    assert stats.skipped_ext == 1

    # 同一路径再被新 inode 原子替换，路径兜底也必须继续保持 ignored，
    # 不能因为 identity 变化又把旧全文重新挂回文件。
    old_inode = after["inode"]
    replacement = tmp_path / ".incoming"
    replacement.write_text("新的未知类型内容", encoding="utf-8")
    replacement.replace(ignored)
    scan_source(db, source, tmp_path)
    replaced = db.execute(
        "SELECT id, content_id, inode, state, ext FROM files"
    ).fetchone()
    assert replaced["id"] == before["id"]
    assert replaced["inode"] != old_inode
    assert replaced["content_id"] is None
    assert replaced["state"] == "ignored"
    assert replaced["ext"] == ".unknown"


def test_cross_source_move_refreshes_ownership_and_metadata(db, tmp_path, monkeypatch):
    """跨来源移动仍复用 inode，并把归属及派生字段一起刷新。"""
    source_a_root = tmp_path / "first"
    source_b_root = tmp_path / "second"
    source_a_root.mkdir()
    source_b_root.mkdir()
    source_a = db.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, created_at) "
        "VALUES ('first', ?, 'manual', 'manual', ?)",
        (str(source_a_root), time.time()),
    ).lastrowid
    source_b = db.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, created_at) "
        "VALUES ('second', ?, 'manual', 'manual', ?)",
        (str(source_b_root), time.time()),
    ).lastrowid
    db.commit()

    original = source_a_root / "before.txt"
    original.write_text("同一份内容", encoding="utf-8")
    scan_source(db, source_a, source_a_root)
    before = db.execute(
        "SELECT id, content_id FROM files WHERE path = ?", (str(original),)
    ).fetchone()

    moved = source_b_root / "after.md"
    original.rename(moved)
    # inode 命中不应重新读取/哈希内容；若调用便说明退化为重新入库。
    monkeypatch.setattr(scanner, "hash_file", lambda _p: pytest.fail("跨来源移动不应重哈希"))
    stats = scan_source(db, source_b, source_b_root)
    after = db.execute(
        "SELECT id, content_id, source_id, path, name, ext, mime FROM files"
    ).fetchone()

    assert after["id"] == before["id"]
    assert after["content_id"] == before["content_id"]
    assert after["source_id"] == source_b
    assert after["path"] == str(moved)
    assert after["name"] == "after.md"
    assert after["ext"] == ".md"
    assert after["mime"] == mimetypes.guess_type(moved.name)[0]
    assert stats.path_updated == 1

    # 旧来源随后扫描时不得把已转属的记录误标 missing。
    scan_source(db, source_a, source_a_root)
    final = db.execute(
        "SELECT source_id, state FROM files WHERE id = ?", (after["id"],)
    ).fetchone()
    assert final["source_id"] == source_b
    assert final["state"] == "registered"


def test_hash_failure_keeps_old_content_and_retries(db, source, tmp_path, monkeypatch):
    """临时读取失败不得解除旧索引，下一次扫描必须重新尝试哈希。"""
    f = tmp_path / "doc.txt"
    f.write_text("稳定的旧内容", encoding="utf-8")
    scan_source(db, source, tmp_path)
    old = db.execute("SELECT id, content_id FROM files").fetchone()

    # 改为不同大小，确保首次扫描进入内容更新路径；随后保持相同 metadata，
    # 用来验证 retry 标记不会被 fast path 当成 unchanged。
    f.write_text("这是需要重新读取并重新建立内容哈希的新版本", encoding="utf-8")
    original_hash = scanner.hash_file
    calls = 0

    def flaky_hash(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("临时拒绝读取")
        return original_hash(path)

    monkeypatch.setattr(scanner, "hash_file", flaky_hash)
    first = scan_source(db, source, tmp_path)
    failed = db.execute(
        "SELECT content_id, state, error_code, retry_count FROM files WHERE id = ?", (old["id"],)
    ).fetchone()
    assert first.errors == 1
    assert first.unchanged == 0
    assert failed["content_id"] == old["content_id"], "读取失败不应错误解除旧 content"
    assert failed["state"] == "failed"
    assert failed["error_code"] == "hash_failed"
    assert failed["retry_count"] == 1

    second = scan_source(db, source, tmp_path)
    retried = db.execute(
        "SELECT content_id, state, error_code, retry_count FROM files WHERE id = ?", (old["id"],)
    ).fetchone()
    assert calls == 2, "失败文件被当成 unchanged，未在下一次扫描重试"
    assert second.content_updated == 1
    assert retried["content_id"] != old["content_id"]
    assert retried["state"] == "registered"
    assert retried["error_code"] is None
    assert retried["retry_count"] == 0


def test_path_fallback_reuses_row_after_atomic_replace(db, source, tmp_path):
    """同一路径被原子替换（新 inode）时复用原 file_id，而不是插入重复路径。"""
    target = tmp_path / "atomic.txt"
    target.write_text("旧版本", encoding="utf-8")
    scan_source(db, source, tmp_path)
    before = db.execute(
        "SELECT id, inode, content_id FROM files WHERE path = ?", (str(target),)
    ).fetchone()

    replacement = tmp_path / ".replacement"
    replacement.write_text("原子替换后的全新内容，长度也不同", encoding="utf-8")
    replacement.replace(target)
    stats = scan_source(db, source, tmp_path)
    after = db.execute(
        "SELECT id, inode, content_id FROM files WHERE path = ?", (str(target),)
    ).fetchone()

    assert db.execute("SELECT count(*) c FROM files").fetchone()["c"] == 1
    assert after["id"] == before["id"]
    assert after["inode"] != before["inode"]
    assert after["content_id"] != before["content_id"]
    assert stats.content_updated == 1


def test_live_filter_uses_same_package_and_depth_rules(tmp_path):
    """实时监听调用的路径过滤与扫描器的 package / 深度规则一致。"""
    assert should_skip_path(tmp_path / "note.pages" / "index.xml", tmp_path)
    deep = tmp_path
    for i in range(12):
        deep /= f"d{i}"
    assert should_skip_path(deep / "too-deep.txt", tmp_path)


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
