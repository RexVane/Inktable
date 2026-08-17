"""missing 处理与保全副本的对抗性测试（A4 / A7）。

核心承诺：
  · 文件消失 → 标 missing，**索引保留**，仍可搜到
  · 文件重现 → 自动复活
  · 保全 = 复制，原文件与库里其他东西一根汗毛都不能动
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.db.database import connect, init_db
from app.organize import preserve as P
from app.watcher.scanner import ScanStats, register_file, scan_source


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def source(db, tmp_path):
    db.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, volatile, created_at) "
        "VALUES ('测试来源', ?, 'manual', 'manual', 1, ?)",
        (str(tmp_path), time.time()),
    )
    db.commit()
    return 1


@pytest.fixture
def preserve_root(tmp_path, monkeypatch):
    """把保全目录指到临时位置，别污染真实的 App Support。"""
    root = tmp_path / "_preserved"
    monkeypatch.setattr(P, "PRESERVE_ROOT", root)
    return root


class TestMissing:
    def test_rescan_marks_missing_keeps_index(self, db, source, tmp_path):
        """文件被删后重扫：标 missing，但 contents/chunks 必须原样保留。

        这是索引模式的核心承诺 —— 知识不随文件消失而消失（§12.6）。
        """
        f = tmp_path / "a.txt"
        f.write_text("汝窑天青釉工艺档案")
        scan_source(db, source, tmp_path)
        n_contents = db.execute("SELECT count(*) c FROM contents").fetchone()["c"]

        f.unlink()
        stats = scan_source(db, source, tmp_path)

        row = db.execute("SELECT state, missing_since FROM files").fetchone()
        assert row["state"] == "missing"
        assert row["missing_since"] is not None
        assert stats.marked_missing == 1
        # 索引一片不少
        assert db.execute("SELECT count(*) c FROM contents").fetchone()["c"] == n_contents

    def test_reappear_revives(self, db, source, tmp_path):
        """同一文件（同 inode）回来了 → 自动复活，不重新解析。"""
        f = tmp_path / "b.txt"
        f.write_text("内容")
        scan_source(db, source, tmp_path)

        # 移出去（模拟外置盘场景里的暂时不可见）
        outside = tmp_path.parent / f"outside-{tmp_path.name}"
        outside.mkdir(exist_ok=True)
        moved = outside / "b.txt"
        f.rename(moved)
        scan_source(db, source, tmp_path)
        assert db.execute("SELECT state FROM files").fetchone()["state"] == "missing"

        # 移回来 —— inode 不变，身份命中
        moved.rename(f)
        scan_source(db, source, tmp_path)
        row = db.execute("SELECT state, missing_since FROM files").fetchone()
        assert row["state"] == "registered", "重现后没有自动复活"
        assert row["missing_since"] is None

    def test_move_within_source_not_missing(self, db, source, tmp_path):
        """来源内移动（inode 不变）绝不能误标 missing。"""
        (tmp_path / "sub").mkdir()
        f = tmp_path / "c.txt"
        f.write_text("内容")
        scan_source(db, source, tmp_path)

        f.rename(tmp_path / "sub" / "c.txt")
        stats = scan_source(db, source, tmp_path)

        row = db.execute("SELECT state, path FROM files").fetchone()
        assert row["state"] != "missing"
        assert Path(row["path"]).parts[-2:] == ("sub", "c.txt")
        assert stats.marked_missing == 0


class TestPreserve:
    def test_preserve_copies_and_logs(self, db, source, tmp_path, preserve_root):
        """保全 = 复制 + 哈希校验 + operations 日志。原文件不动。"""
        f = tmp_path / "合同.txt"
        f.write_text("重要合同内容")
        before = (f.stat().st_size, f.stat().st_mtime_ns)
        stats = ScanStats()
        fid = register_file(db, f, source, stats)

        result = P.preserve_file(db, fid)
        dest = Path(result["preserved_path"])

        assert dest.is_file()
        assert dest.read_text() == "重要合同内容"
        assert preserve_root in dest.parents
        # 原文件分毫未动（§1 约束 1）
        assert (f.stat().st_size, f.stat().st_mtime_ns) == before
        # 日志可审计
        op = db.execute("SELECT kind, method FROM operations").fetchone()
        assert op["kind"] == "preserve" and op["method"] == "copy"
        # 库里记了副本路径
        assert db.execute(
            "SELECT preserved_path FROM files WHERE id = ?", (fid,)
        ).fetchone()["preserved_path"] == str(dest)

    def test_preserve_idempotent(self, db, source, tmp_path, preserve_root):
        """重复保全不产生第二个副本。"""
        f = tmp_path / "d.txt"
        f.write_text("内容")
        stats = ScanStats()
        fid = register_file(db, f, source, stats)

        r1 = P.preserve_file(db, fid)
        r2 = P.preserve_file(db, fid)
        assert r2["already"] is True
        assert r1["preserved_path"] == r2["preserved_path"]
        copies = list(Path(r1["preserved_path"]).parent.iterdir())
        assert len([c for c in copies if not c.name.startswith(".")]) == 1

    def test_preserve_missing_file_fails_cleanly(self, db, source, tmp_path, preserve_root):
        """原文件已消失 → 明确报错，不留残余。"""
        f = tmp_path / "e.txt"
        f.write_text("内容")
        stats = ScanStats()
        fid = register_file(db, f, source, stats)
        f.unlink()

        with pytest.raises(P.PreserveError):
            P.preserve_file(db, fid)
        # 没有半成品副本
        if preserve_root.exists():
            leftovers = [p for p in preserve_root.rglob("*") if p.is_file()]
            assert leftovers == []

    def test_effective_path_falls_back_to_copy(self, db, source, tmp_path, preserve_root):
        """原件被清理后，打开文件应落到副本 —— 保全存在的意义。"""
        f = tmp_path / "微信收的.txt"
        f.write_text("微信文件内容")
        stats = ScanStats()
        fid = register_file(db, f, source, stats)
        P.preserve_file(db, fid)

        f.unlink()  # 模拟微信清缓存
        row = db.execute(
            "SELECT path, preserved_path FROM files WHERE id = ?", (fid,)
        ).fetchone()
        eff = P.effective_path(row)
        assert eff is not None
        assert Path(eff).read_text() == "微信文件内容"

    def test_preserve_source_bulk(self, db, source, tmp_path, preserve_root):
        """批量保全：单个失败不中断其余。"""
        for i in range(3):
            (tmp_path / f"f{i}.txt").write_text(f"内容{i}")
        scan_source(db, source, tmp_path)
        # 删掉一个制造失败
        (tmp_path / "f1.txt").unlink()
        db.execute("UPDATE files SET state='registered'")  # 绕过 missing 过滤，逼它失败
        db.commit()

        result = P.preserve_source(db, source)
        assert result["preserved"] == 2
        assert result["failed"] == 1

    def test_unique_dest_never_overwrites(self, db, source, tmp_path, preserve_root):
        """两个不同来源目录下的同名文件 → 副本自动编号，绝不覆盖。"""
        d1, d2 = tmp_path / "x", tmp_path / "y"
        d1.mkdir(); d2.mkdir()
        (d1 / "同名.txt").write_text("第一份")
        (d2 / "同名.txt").write_text("第二份")
        scan_source(db, source, tmp_path)
        ids = [r["id"] for r in db.execute("SELECT id FROM files ORDER BY id")]

        p1 = Path(P.preserve_file(db, ids[0])["preserved_path"])
        p2 = Path(P.preserve_file(db, ids[1])["preserved_path"])
        assert p1 != p2
        assert {p1.read_text(), p2.read_text()} == {"第一份", "第二份"}
