"""监听器与稳定性判据的对抗性测试。

这些用例来自实测中真实踩到的坑，不是想象出来的边界条件。
每个用例上方注明它挡住的是哪个具体缺陷。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db.database import connect, init_db
from app.watcher import stability
from app.watcher.service import WatchService
from app.watcher.stability import looks_like_temp, stabilize
from app.watcher.watcher import Watcher, _Handler


@pytest.fixture
def watched():
    """一个正在被监听的目录，外加一个同卷的暂存目录。"""
    root = Path(tempfile.mkdtemp())
    stage = root / "_stage"
    inbox = root / "inbox"
    stage.mkdir()
    inbox.mkdir()

    captured: list[tuple[str, bool]] = []
    w = Watcher(lambda p, m: captured.append((Path(p).name, m)))
    w.watch(str(inbox))
    time.sleep(0.8)  # 等 FSEvents 就绪

    yield inbox, stage, captured, w

    w.stop()
    shutil.rmtree(root, ignore_errors=True)


def _names(captured):
    return [n for n, _ in captured]


class TestTempDetection:
    def test_download_suffixes(self):
        for name in ["a.crdownload", "b.part", "c.tmp", "d.download", "~$e.docx"]:
            assert looks_like_temp(name), f"{name} 应被识别为临时文件"

    def test_normal_names(self):
        for name in ["合同.pdf", "report.docx", "data.xlsx"]:
            assert not looks_like_temp(name)

    def test_hidden_and_system(self):
        assert looks_like_temp(".DS_Store")
        assert looks_like_temp(".hidden")


class TestStability:
    def test_settled_file_is_stable(self, tmp_path):
        f = tmp_path / "done.txt"
        f.write_text("完整内容")
        time.sleep(1.6)  # 越过 MTIME_QUIET
        assert stabilize(str(f)).stable

    def test_temp_suffix_never_stable(self, tmp_path):
        f = tmp_path / "x.crdownload"
        f.write_bytes(b"data")
        time.sleep(1.6)
        r = stabilize(str(f))
        assert not r.stable
        assert r.reason == "temp_suffix"

    def test_vanished_file(self, tmp_path):
        assert not stabilize(str(tmp_path / "nope.txt")).stable

    def test_moved_file_cannot_pass_on_advisory_flock_alone(self, tmp_path, monkeypatch):
        """普通写句柄不持 flock；能取得 advisory lock 仍必须经过采样。"""
        f = tmp_path / "still-open.txt"
        f.write_text("正在写", encoding="utf-8")

        with open(f, "ab") as writer:
            # 句柄仍开着，但普通 writer 没主动 flock，另一个 fd 仍可取锁。
            assert stability._has_exclusive_lock(str(f))
            sampled = False

            def not_stable(_path):
                nonlocal sampled
                sampled = True
                return False

            monkeypatch.setattr(stability, "_size_stable", not_stable)
            result = stability.stabilize(str(f), moved_in=True)
            writer.write(b"more")

        assert sampled, "moved_in 因 flock 成功绕过了稳定性采样"
        assert not result.stable
        assert result.reason == "still_growing"


class TestWatcher:
    def test_move_in_captured(self, watched):
        """微信/QQ 主路径：文件在别处写好后移入监听目录。

        实测发现 macOS FSEvents 对树外移入报 created 而非 moved，
        所以判据不能依赖 moved_in。这个用例确保它照样被捕获。
        """
        inbox, stage, captured, _ = watched
        src = stage / "微信文件.docx"
        src.write_bytes(b"y" * 3000)
        os.rename(src, inbox / "微信文件.docx")

        time.sleep(8)
        assert "微信文件.docx" in _names(captured)

    def test_temp_file_not_captured(self, watched):
        """浏览器下载：.crdownload 阶段绝不能入库。"""
        inbox, _, captured, _ = watched
        part = inbox / "报告.pdf.crdownload"
        part.write_bytes(b"x" * 1000)
        time.sleep(2)
        part.rename(inbox / "报告.pdf")
        time.sleep(8)

        names = _names(captured)
        assert not any("crdownload" in n for n in names), "临时文件被误处理"
        assert "报告.pdf" in names, "改名后的最终文件被漏掉"

    def test_node_modules_not_captured_in_realtime(self, tmp_path):
        """实时监听必须与首次扫描一样排除 node_modules。"""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        captured = []
        w = Watcher(lambda p, m: captured.append((Path(p).name, m)))
        handler = _Handler(w._enqueue, w._enqueue_gone, str(inbox))
        nested = inbox / "node_modules" / "pkg"
        nested.mkdir(parents=True)
        dependency = nested / "dependency.txt"
        dependency.write_text("不应入库", encoding="utf-8")

        # 走真实 watchdog handler → enqueue 路径，但不依赖容易受并发测试
        # 影响的系统 FSEvents stream。
        handler.on_created(SimpleNamespace(is_directory=False, src_path=str(dependency)))
        assert "dependency.txt" not in _names(captured)
        assert str(dependency) not in w._pending
        assert w.stats["skipped_excluded"] > 0

    def test_streaming_write_no_false_positive(self, watched):
        """分段写入不能被误判为写完。

        实测缺陷：写入进程在两次 write 之间不持锁，只靠 flock 判据
        会在那个间隙误判稳定 —— 一个文件被回调两次，且第一次时内容不完整。
        """
        inbox, _, captured, _ = watched
        target = inbox / "下载.zip"

        def slow_write():
            with open(target, "wb") as f:
                for _ in range(5):
                    f.write(b"q" * 80000)
                    f.flush()
                    time.sleep(1.1)

        t = threading.Thread(target=slow_write)
        t.start()
        t.join()
        time.sleep(10)

        hits = [n for n in _names(captured) if n == "下载.zip"]
        assert len(hits) == 1, f"分段写入被回调 {len(hits)} 次（应为 1）"
        assert target.stat().st_size == 400000, "回调时文件内容不完整"

    def test_rewrite_triggers_again(self, watched):
        """内容改写必须重新触发 —— 增量更新的前提（§12.5）。"""
        inbox, _, captured, _ = watched
        f = inbox / "改写.md"

        f.write_text("v1")
        time.sleep(6)
        before = len([n for n in _names(captured) if n == "改写.md"])

        f.write_text("v2 内容变了")
        time.sleep(6)
        after = len([n for n in _names(captured) if n == "改写.md"])

        assert after > before, "改写后没有重新触发，索引会陈旧"

    def test_watched_paths_tracked(self, watched):
        inbox, _, _, w = watched
        assert str(inbox) in w.watched_paths
        w.unwatch(str(inbox))
        assert str(inbox) not in w.watched_paths


def test_reconcile_recovers_files_created_while_app_was_off(tmp_path, monkeypatch):
    """启动补扫必须找回应用关闭期间落入来源的文件（§19 R8）。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    late = inbox / "离线期间新增.txt"
    late.write_text("应用没运行时写入的内容", encoding="utf-8")

    conn = connect(":memory:")
    init_db(conn)
    conn.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, enabled, created_at) "
        "VALUES ('S', ?, 'manual', 'manual', 1, ?)",
        (str(inbox), time.time()),
    )
    conn.commit()

    service = WatchService(lambda: conn, threading.Lock())

    class FakeWatcher:
        watched_paths: list[str] = []
        stats: dict = {}

        def watch(self, path):
            self.watched_paths = [*self.watched_paths, path]
            return True

        def stop(self):
            return None

    service._watcher = FakeWatcher()
    monkeypatch.setattr("app.watcher.service.index_pending", lambda _c, limit: {"total": 0})

    result = service._reconcile_once()
    row = conn.execute("SELECT name, path FROM files").fetchone()
    assert result["registered"] == 1
    assert row["name"] == late.name
    assert row["path"] == str(late)
    conn.close()
