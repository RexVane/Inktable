"""监听服务 —— 把 Watcher 的稳定回调接到「登记 + 索引」。

职责边界：
- Watcher（§11.1）只负责判断"文件写完了"，不碰数据库
- 本模块负责把稳定文件登记进库并立即索引，串行化写入（§19 R9）
- main.py 只负责生命周期与 API，不含业务逻辑

**为什么单独一层**：Watcher 的回调跑在它自己的工作线程里。数据库写入
必须与 API 请求共用同一把锁，否则 SQLite 会 database is locked。
这一层就是那把锁的持有者。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

from app.index.pipeline import index_pending
from app.watcher.scanner import ScanStats, register_file
from app.watcher.watcher import Watcher


def _register_status(s: ScanStats) -> str:
    """把 ScanStats 的计数还原成"这一个文件发生了什么"。

    scan_source 用 ScanStats 累计批量结果；这里每次只登记一个文件，
    所以哪个计数器被 +1 就是这个文件的结局。
    """
    for field in ("registered", "content_updated", "path_updated",
                  "duplicates", "unchanged"):
        if getattr(s, field):
            return field
    return "unknown"


def _skip_reason(s: ScanStats) -> str:
    for field in ("skipped_ext", "skipped_excluded", "skipped_dataless",
                  "skipped_too_large", "errors"):
        if getattr(s, field):
            return field
    return "skipped"

log = logging.getLogger("inktable.watch_service")

# 最近活动日志保留条数 —— 给界面"实时动态"用
ACTIVITY_LIMIT = 50


class WatchService:
    """维护所有已启用来源的实时监听。"""

    def __init__(self, conn_factory, db_lock: threading.Lock):
        self._conn_factory = conn_factory
        self._lock = db_lock
        self._watcher = Watcher(self._on_stable, on_gone=self._on_gone)
        self._activity: deque[dict] = deque(maxlen=ACTIVITY_LIMIT)
        self._counters = {"detected": 0, "registered": 0, "indexed": 0,
                          "skipped": 0, "missing": 0, "preserved": 0}

    # ------------------------------------------------------------ 生命周期

    def start(self) -> dict:
        """从数据库读取已启用来源并全部挂上监听。"""
        conn = self._conn_factory()
        rows = conn.execute(
            "SELECT id, name, path FROM sources WHERE enabled = 1"
        ).fetchall()

        ok, failed = [], []
        for r in rows:
            if not Path(r["path"]).is_dir():
                failed.append({"name": r["name"], "reason": "目录不存在"})
                continue
            try:
                self._watcher.watch(r["path"])
                ok.append(r["name"])
            except OSError as e:
                # 权限不足是最常见的原因（未授予完全磁盘访问）
                failed.append({"name": r["name"], "reason": str(e)})

        log.info("监听已启动：%d 个来源，%d 个失败", len(ok), len(failed))
        return {"watching": ok, "failed": failed}

    def watch(self, path: str) -> None:
        self._watcher.watch(path)

    def unwatch(self, path: str) -> None:
        self._watcher.unwatch(path)

    def stop(self) -> None:
        self._watcher.stop()

    # ------------------------------------------------------------ 回调

    def _on_stable(self, path: str, moved_in: bool) -> None:
        """文件写完了 —— 登记并索引。

        整段在 db_lock 内：与 API 的写操作串行，避免 SQLite 锁冲突。
        单个文件失败只记日志，绝不让监听线程死掉 —— 监听一旦死了，
        用户不会收到任何错误提示，只会觉得"新文件没进来"。
        """
        self._counters["detected"] += 1
        name = Path(path).name
        chunks = 0

        try:
            with self._lock:
                conn = self._conn_factory()
                source_id = self._source_for(conn, path)
                if source_id is None:
                    # 用 warning 而非 debug：这条一旦出现就意味着文件被静默丢弃，
                    # 是"检测到了却没入库"这类最难排查问题的唯一线索。
                    log.warning("文件不属于任何已启用来源，跳过：%s", path)
                    self._counters["skipped"] += 1
                    self._log_activity(name, path, "no_source", 0)
                    return

                stats = ScanStats()
                file_id = register_file(conn, Path(path), source_id, stats)
                conn.commit()

                if file_id is None:
                    self._counters["skipped"] += 1
                    self._log_activity(name, path, _skip_reason(stats), 0)
                    return

                self._counters["registered"] += 1

                # 立即索引 —— 用户刚存的文件应当马上能搜到，
                # 而不是等下一次手动"重新索引"
                idx = index_pending(conn, limit=5)
                conn.commit()

            chunks = idx.get("chunks", 0)
            if idx.get("indexed", 0):
                self._counters["indexed"] += 1
                # 成功路径也要留痕：排查"文件没进来"时，需要能区分
                # 「没检测到」「检测到被跳过」「入库了但搜不到」三种情况
                log.info("自动入库 %s → %d 片", name, chunks)
            self._log_activity(name, path, _register_status(stats), chunks)
            log.info("自动入库：%s（%d 片）", name, chunks)

            # 易失来源开了自动保全 → 立刻复制一份（§2.5）。
            # 微信清缓存不挑时候，等用户手动点就晚了。
            self._auto_preserve(file_id, source_id)

        except sqlite3.Error as e:
            log.error("自动入库失败（数据库）：%s — %s", path, e)
        except Exception as e:
            log.exception("自动入库失败：%s — %s", path, e)

    def _source_for(self, conn, path: str) -> int | None:
        """找出这个文件属于哪个来源。

        两个必须处理的细节：

        **① 符号链接**。FSEvents 回调给的是**解析后**的真实路径，而来源
        路径按用户输入原样存库。macOS 上 `/var` → `/private/var`、
        `/tmp` → `/private/tmp`，外置盘也常有链接。不规范化就会前缀匹配
        失败 —— 文件被检测到却"不属于任何来源"，静默丢弃。

        **② 最长匹配**。来源可能嵌套（`~/Documents` 与 `~/Documents/项目`），
        文件应归属更具体的那个。
        """
        rows = conn.execute(
            "SELECT id, path FROM sources WHERE enabled = 1"
        ).fetchall()

        target = self._real(path)
        best, best_len = None, -1
        for r in rows:
            base = self._real(r["path"]).rstrip("/") + "/"
            if target.startswith(base) and len(base) > best_len:
                best, best_len = r["id"], len(base)
        return best

    @staticmethod
    def _real(path: str) -> str:
        """解析符号链接。失败时退回原路径 —— 宁可匹配不上也不抛异常。"""
        try:
            return str(Path(path).resolve())
        except OSError:
            return path

    def _log_activity(self, name: str, path: str, status: str, chunks: int) -> None:
        self._activity.appendleft({
            "name": name,
            "path": path,
            "status": status,
            "chunks": chunks,
            "at": time.time(),
        })

    def _on_gone(self, path: str) -> None:
        """路径确认消失 —— 标 missing，**保留全部索引**（§8.3）。

        树内移动的旧路径不会误伤：register_file 靠 inode 已把新路径
        写回同一行，按旧路径查不到记录，这里自然 no-op。
        外置盘整卷拔出是另一回事（卷级批量 gone），missing 状态可自动
        恢复 —— 文件重现时 register_file 的身份命中会清掉它。
        """
        try:
            with self._lock:
                conn = self._conn_factory()
                cur = conn.execute(
                    """UPDATE files SET state = 'missing', missing_since = ?
                       WHERE path = ? AND state != 'missing'""",
                    (time.time(), path),
                )
                conn.commit()
            if cur.rowcount:
                self._counters["missing"] += 1
                name = Path(path).name
                self._log_activity(name, path, "missing", 0)
                log.info("文件已消失，索引保留：%s", name)
        except Exception:
            log.exception("标记消失失败：%s", path)

    def _auto_preserve(self, file_id: int | None, source_id: int | None) -> None:
        if file_id is None or source_id is None:
            return
        try:
            with self._lock:
                conn = self._conn_factory()
                src = conn.execute(
                    "SELECT volatile, auto_preserve FROM sources WHERE id = ?",
                    (source_id,),
                ).fetchone()
                if not src or not (src["volatile"] and src["auto_preserve"]):
                    return
                from app.organize.preserve import preserve_file

                preserve_file(conn, file_id)
                conn.commit()
            self._counters["preserved"] += 1
        except Exception as e:
            log.warning("自动保全失败 file_id=%s：%s", file_id, e)

    # ------------------------------------------------------------ 状态查询

    @property
    def status(self) -> dict:
        watched = self._watcher.watched_paths
        return {
            "running": bool(watched),
            "watched": watched,
            "counters": dict(self._counters),
            "watcher": dict(self._watcher.stats),
            "activity": list(self._activity),
        }
