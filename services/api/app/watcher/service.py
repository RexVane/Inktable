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
import os
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

from app.index.pipeline import (
    ORPHAN_CLEANUP_BATCH,
    cleanup_orphan_contents,
    count_readable_pending,
    embed_backfill,
    index_pending,
)
from app.watcher.policy import is_drive_root
from app.watcher.scanner import (
    ScanStats,
    path_is_within,
    register_file,
    scan_source,
    should_skip_path,
)
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
INITIAL_RECONCILE_DELAY = 2.0
RECONCILE_INTERVAL = 6 * 60 * 60
RECONCILE_INDEX_BUDGET = 2000
RECONCILE_ORPHAN_BUDGET = 500


class WatchService:
    """维护所有已启用来源的实时监听。"""

    def __init__(self, conn_factory, db_lock: threading.Lock):
        self._conn_factory = conn_factory
        self._lock = db_lock
        self._watcher = Watcher(self._on_stable, on_gone=self._on_gone)
        self._activity: deque[dict] = deque(maxlen=ACTIVITY_LIMIT)
        self._counters = {"detected": 0, "registered": 0, "indexed": 0,
                          "skipped": 0, "missing": 0, "preserved": 0}
        self._reconcile_stop = threading.Event()
        self._reconcile_thread: threading.Thread | None = None
        self._scan_thread: threading.Thread | None = None
        self._scan_wake = threading.Event()
        self._scan_stop = threading.Event()
        self._scan_queue: deque[tuple[str, int, str, bool]] = deque()
        self._scan_jobs: dict[str, dict] = {}
        self._scan_cancel: dict[str, threading.Event] = {}
        self._scan_lock = threading.Lock()
        self._started = False
        self._reconcile = {
            "runs": 0, "last_at": None, "last_error": "",
            "scanned": 0, "registered": 0, "indexed": 0,
            "phase": "idle", "current_source": None,
        }

    # ------------------------------------------------------------ 后台来源扫描

    def queue_scan(self, source_id: int, root: str, *, prune_projects: bool = False) -> str:
        """Queue a non-blocking source scan and return its job id."""
        import uuid

        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "source_id": source_id,
            "root": root,
            "state": "queued",
            "phase": "queued",
            "scanned": 0,
            "registered": 0,
            "unchanged": 0,
            "errors": 0,
            "indexed": 0,
            "embedded": 0,
            "embedding_remaining": None,
            "embedding_available": None,
            "embedding_error": "",
            "started_at": None,
            "updated_at": time.time(),
            "finished_at": None,
            "error": "",
        }
        cancel_event = threading.Event()
        with self._scan_lock:
            self._scan_jobs[job_id] = job
            self._scan_cancel[job_id] = cancel_event
            self._scan_queue.append((job_id, source_id, root, prune_projects))
            if self._scan_thread is None or not self._scan_thread.is_alive():
                self._scan_stop.clear()
                self._scan_thread = threading.Thread(
                    target=self._scan_loop,
                    name="inktable-source-scan",
                    daemon=True,
                )
                self._scan_thread.start()
        self._scan_wake.set()
        return job_id

    def _update_scan_job(self, job_id: str, **updates) -> None:
        with self._scan_lock:
            job = self._scan_jobs.get(job_id)
            if job is not None:
                job.update(updates)
                job["updated_at"] = time.time()

    def _scan_loop(self) -> None:
        while not self._scan_stop.is_set():
            self._scan_wake.wait(0.5)
            self._scan_wake.clear()
            while not self._scan_stop.is_set():
                with self._scan_lock:
                    if not self._scan_queue:
                        break
                    job_id, source_id, root, prune_projects = self._scan_queue.popleft()
                    cancel_event = self._scan_cancel.get(job_id)
                self._run_scan_job(
                    job_id, source_id, root, prune_projects,
                    cancel_event or threading.Event(),
                )

    def _run_scan_job(
        self, job_id: str, source_id: int, root: str,
        prune_projects: bool, cancel_event: threading.Event,
    ) -> None:
        with self._scan_lock:
            job = self._scan_jobs.get(job_id)
            if not job or job["state"] == "cancelled":
                return
            job.update(state="scanning", phase="scanning", started_at=time.time())
        conn = None

        def active() -> bool:
            return not self._scan_stop.is_set() and not cancel_event.is_set()

        try:
            conn = self._conn_factory()

            def progress(stats: ScanStats) -> None:
                self._update_scan_job(
                    job_id,
                    scanned=stats.scanned,
                    registered=stats.registered + stats.content_updated,
                    unchanged=stats.unchanged + stats.path_updated,
                    errors=stats.errors,
                )

            stats = scan_source(
                conn, source_id, Path(root), progress=progress,
                lock=self._lock, prune_projects=prune_projects,
            )
            if not active():
                self._update_scan_job(job_id, state="cancelled", phase="cancelled",
                                      finished_at=time.time())
                return

            content_ids = set(stats.content_ids)
            self._update_scan_job(
                job_id,
                scanned=stats.scanned,
                registered=stats.registered + stats.content_updated,
                unchanged=stats.unchanged + stats.path_updated,
                errors=stats.errors,
                phase="indexing", state="indexing",
            )
            indexed_total = 0
            while active():
                with self._lock:
                    result = index_pending(
                        conn, limit=24, embed=False,
                        content_ids=content_ids,
                    )
                    conn.commit()
                indexed_total += result.get("indexed", 0)
                self._update_scan_job(job_id, indexed=indexed_total)
                if result.get("total", 0) <= 0:
                    break
                if result.get("indexed", 0) <= 0 and result.get("failed", 0) <= 0:
                    break
                time.sleep(0.05)

            if not active():
                self._update_scan_job(job_id, state="cancelled", phase="cancelled",
                                      finished_at=time.time())
                return

            self._update_scan_job(job_id, phase="embedding", state="embedding")
            embedded_total = 0
            while active():
                try:
                    with self._lock:
                        backfill = embed_backfill(
                            conn, limit=128, content_ids=content_ids,
                        )
                        conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                available = bool(backfill.get("available"))
                remaining = backfill.get("remaining")
                embedded = int(backfill.get("embedded", 0) or 0)
                embedded_total += embedded
                self._update_scan_job(
                    job_id,
                    embedded=embedded_total,
                    embedding_remaining=remaining,
                    embedding_available=available,
                    embedding_error="" if available else "embedding unavailable",
                )
                if not available:
                    self._update_scan_job(
                        job_id, state="blocked", phase="embedding",
                        finished_at=time.time(),
                    )
                    return
                if remaining == 0:
                    break
                if embedded == 0:
                    self._update_scan_job(
                        job_id, state="blocked", phase="embedding",
                        embedding_error="embedding made no progress",
                        finished_at=time.time(),
                    )
                    return
                time.sleep(0.05)

            self._update_scan_job(
                job_id,
                state="done" if active() else "cancelled",
                phase="idle" if active() else "cancelled",
                indexed=indexed_total,
                finished_at=time.time(),
            )
        except Exception as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            log.exception("后台来源扫描失败 %s: %s", root, exc)
            self._update_scan_job(
                job_id, state="failed", phase="error", error=str(exc),
                embedding_error=str(exc), finished_at=time.time(),
            )
        finally:
            # main.db() returns a thread-local connection owned by the app;
            # do not close it here and leave a closed handle in that thread.
            with self._scan_lock:
                self._scan_cancel.pop(job_id, None)

    def scan_jobs(self) -> list[dict]:
        with self._scan_lock:
            return [dict(job) for job in self._scan_jobs.values()]

    def active_scan_source_ids(self) -> set[int]:
        with self._scan_lock:
            return {
                job["source_id"] for job in self._scan_jobs.values()
                if job["state"] in {"queued", "scanning", "indexing", "embedding"}
            }

    def cancel_scans_for_source(self, source_id: int) -> None:
        with self._scan_lock:
            for job_id, job in self._scan_jobs.items():
                if job["source_id"] == source_id and job["state"] in {
                    "queued", "scanning", "indexing", "embedding",
                }:
                    event = self._scan_cancel.get(job_id)
                    if event:
                        event.set()
                    job["state"] = "cancelled"
                    job["phase"] = "cancelled"
                    job["finished_at"] = time.time()
            self._scan_queue = deque(
                item for item in self._scan_queue if item[1] != source_id
            )

    # ------------------------------------------------------------ 生命周期

    def start(self) -> dict:
        """从数据库读取已启用来源并全部挂上监听。"""
        conn = self._conn_factory()
        rows = conn.execute(
            "SELECT id, name, path FROM sources WHERE enabled = 1"
        ).fetchall()

        ok, failed = [], []
        watched = set(self._watcher.watched_paths)
        for r in rows:
            if not Path(r["path"]).is_dir():
                failed.append({"name": r["name"], "reason": "目录不存在"})
                continue
            try:
                if r["path"] in watched or self._watcher.watch(r["path"]):
                    ok.append(r["name"])
                    watched.add(r["path"])
                else:
                    failed.append({"name": r["name"], "reason": "监听器未能挂载目录"})
            except Exception as e:
                # 权限不足是最常见的原因（未授予完全磁盘访问）
                failed.append({"name": r["name"], "reason": str(e)})

        self._started = True
        if rows:
            # 先挂监听再补扫：应用关闭期间新增的文件不会漏；补扫期间新事件
            # 也会进入 watcher 队列。延迟两秒让首屏先完成加载。
            self._start_reconciler(INITIAL_RECONCILE_DELAY)
        log.info("监听已启动：%d 个来源，%d 个失败", len(ok), len(failed))
        return {"watching": ok, "failed": failed}

    def sync_watched_paths(self) -> None:
        """Make watcher paths match the currently enabled source roots."""
        conn = self._conn_factory()
        enabled = {row["path"] for row in conn.execute(
            "SELECT path FROM sources WHERE enabled = 1"
        ).fetchall()}
        watched = set(self._watcher.watched_paths)
        for path in watched - enabled:
            self._watcher.unwatch(path)
        for path in enabled - watched:
            try:
                self._watcher.watch(path)
            except Exception:
                log.exception("无法同步监听来源：%s", path)

    def watch(self, path: str) -> bool:
        watched = self._watcher.watch(path)
        # 新来源在 enable API 中已经同步扫描过；这里只需启动后续周期兜底。
        # start() 前启用来源时不能先创建一个等待 6 小时的线程，否则随后
        # start() 无法安排首次 2 秒补扫。
        if self._started:
            self._start_reconciler(RECONCILE_INTERVAL)
        return watched

    def unwatch(self, path: str) -> bool:
        conn = self._conn_factory()
        row = conn.execute("SELECT id FROM sources WHERE path = ?", (path,)).fetchone()
        if row:
            self.cancel_scans_for_source(row["id"])
        return self._watcher.unwatch(path)

    def stop(self) -> None:
        self._started = False
        self._reconcile_stop.set()
        self._scan_stop.set()
        self._scan_wake.set()
        if self._scan_thread:
            self._scan_thread.join(timeout=10)
            if not self._scan_thread.is_alive():
                self._scan_thread = None
        if self._reconcile_thread:
            self._reconcile_thread.join(timeout=10)
            if not self._reconcile_thread.is_alive():
                self._reconcile_thread = None
        self._watcher.stop()

    def _start_reconciler(self, initial_delay: float) -> None:
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            return
        self._reconcile_stop.clear()
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop,
            args=(initial_delay,),
            name="inktable-reconcile",
            daemon=True,
        )
        self._reconcile_thread.start()

    def _reconcile_loop(self, initial_delay: float) -> None:
        if self._reconcile_stop.wait(initial_delay):
            return
        while not self._reconcile_stop.is_set():
            try:
                self._reconcile_once()
            except Exception as e:
                self._reconcile["last_error"] = str(e)
                self._reconcile["phase"] = "error"
                log.exception("来源补扫失败：%s", e)
            if self._reconcile_stop.wait(RECONCILE_INTERVAL):
                return

    def _reconcile_once(self) -> dict:
        """补扫全部已启用来源，并处理本轮新产生的待索引内容。

        FSEvents 不是持久队列：应用没运行、网络卷断续或事件缓冲溢出时都会
        漏事件。周期全量扫描是实时监听的正确性兜底，不是性能优化。
        """
        result = {
            "scanned": 0,
            "registered": 0,
            "indexed": 0,
            "orphans_cleaned": 0,
            "failed": 0,
        }
        # 锁的粒度：逐来源、逐索引批次进出，而不是整个补扫霸占一把锁 ——
        # 否则 API 写请求与实时入库会被整盘首扫饿死几十分钟。
        with self._lock:
            conn = self._conn_factory()
            rows = conn.execute(
                "SELECT id, name, path FROM sources WHERE enabled = 1 ORDER BY id"
            ).fetchall()
        watched = set(self._watcher.watched_paths)
        active_scan_ids = self.active_scan_source_ids()

        for row in rows:
            if row["id"] in active_scan_ids:
                continue
            self._reconcile.update({"phase": "scan", "current_source": row["name"]})
            root = Path(row["path"])
            if not root.is_dir():
                result["failed"] += 1
                continue
            if row["path"] not in watched:
                try:
                    if self._watcher.watch(row["path"]):
                        watched.add(row["path"])
                except Exception as e:
                    log.warning("补扫时重新挂载监听失败 %s：%s", row["path"], e)
            try:
                # 锁交给 scan_source 按小批次进出 —— 整来源一把锁会让
                # /ask 等持锁请求排队几十分钟（实测问答被饿数分钟）
                stats = scan_source(
                    conn,
                    row["id"],
                    root,
                    lock=self._lock,
                    prune_projects=not is_drive_root(row["path"]),
                )
                time.sleep(0.1)
            except Exception as e:
                conn.rollback()
                result["failed"] += 1
                log.exception("补扫来源失败 %s：%s", row["name"], e)
                continue
            result["scanned"] += stats.scanned
            result["registered"] += stats.registered + stats.content_updated
            self._reconcile.update({
                "scanned": result["scanned"],
                "registered": result["registered"],
            })

        self._reconcile.update({"phase": "index", "current_source": None})
        budget = RECONCILE_INDEX_BUDGET
        while budget > 0:
            with self._lock:
                pending_before = count_readable_pending(conn)
                # 批量索引不内联嵌入 —— 向量走下面的 backfill 小批补齐
                indexed = index_pending(conn, limit=min(24, budget), embed=False)
                pending_after = count_readable_pending(conn)
            time.sleep(0.1)  # 批间让锁（无公平锁的让路窗口）
            result["indexed"] += indexed.get("indexed", 0)
            consumed = indexed.get("total", 0)
            if consumed <= 0:
                break
            budget -= consumed
            if pending_after >= pending_before:
                # 解析器持续返回 unsupported 等终态时，同一 content 仍可
                # 被 index_pending 再次选中；没有进展就停止本轮，避免周期
                # 补扫在一个坏文件上空转 2000 次。
                break

        # index_pending only removes one bounded orphan batch. Continue with a
        # finite maintenance budget while releasing both the Python lock and
        # SQLite write transaction between batches. A one-time large cleanup
        # is handled by scripts/cleanup_ingestion_noise.py with the same helper.
        self._reconcile["phase"] = "cleanup"
        orphan_budget = RECONCILE_ORPHAN_BUDGET
        while orphan_budget > 0:
            batch = min(ORPHAN_CLEANUP_BATCH, orphan_budget)
            with self._lock:
                cleaned = cleanup_orphan_contents(conn, limit=batch)
                conn.commit()
            result["orphans_cleaned"] += cleaned
            orphan_budget -= cleaned
            time.sleep(0.1)
            if cleaned < batch:
                break

        # 向量补课：小批次进出锁，慢慢磨（嵌入是分钟级慢操作，绝不能
        # 长期霸占写锁）。前端的 embed_backfill 循环也在驱动，双方幂等。
        self._reconcile["phase"] = "embed"
        while True:
            try:
                with self._lock:
                    bf = embed_backfill(conn, limit=128)
                    conn.commit()
            except Exception:
                conn.rollback()
                raise
            time.sleep(0.1)  # 批间让锁
            if not bf.get("available") or bf.get("embedded", 0) <= 0:
                break

        self._reconcile.update({
            "runs": self._reconcile["runs"] + 1,
            "last_at": time.time(),
            "last_error": "" if result["failed"] == 0 else f"{result['failed']} 个来源失败",
            "scanned": result["scanned"],
            "registered": result["registered"],
            "indexed": result["indexed"],
            "phase": "idle",
            "current_source": None,
        })
        if result["registered"] or result["indexed"]:
            log.info(
                "来源补扫完成：扫描 %d，登记/更新 %d，索引 %d",
                result["scanned"], result["registered"], result["indexed"],
            )
        return result

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

                # Watcher 已在入队时复用扫描排除规则；这里再做一次防御性
                # 校验，保证直接调用回调或平台事件异常时也不会把
                # node_modules / package / 超深目录写入库。
                source = conn.execute(
                    "SELECT path FROM sources WHERE id = ?", (source_id,)
                ).fetchone()
                if source is None or should_skip_path(path, source["path"]):
                    self._counters["skipped"] += 1
                    self._log_activity(name, path, "excluded", 0)
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
            base = self._real(r["path"])
            # commonpath + normcase 同时解决分隔符、路径边界和 Windows
            # 大小写问题；字符串 startswith 会把 C:\foo 误当成 C:\foobar。
            if path_is_within(target, base):
                normalized_len = len(os.path.normcase(os.path.normpath(base)))
                if normalized_len > best_len:
                    best, best_len = r["id"], normalized_len
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
            "reconcile": dict(self._reconcile),
            "activity": list(self._activity),
        }
