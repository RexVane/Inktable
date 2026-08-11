"""目录实时监听 —— PLAN §11.1 / §19 R6。

架构：三层，各自单一职责

  watchdog 事件线程  →  去抖队列  →  工作线程（稳定性检测 + 入库 + 索引）
     绝不阻塞            合并抖动         允许慢（6 秒轮询）

**为什么必须分层**：稳定性检测要采样 6 秒。若在 watchdog 回调里同步做，
事件线程被占住，期间到达的事件会积压甚至丢失（FSEvents 队列有上限）。
所以回调只做一件事：把路径丢进队列，立刻返回。

**去抖**：一次文件写入会触发多个 created/modified 事件。队列按路径去重，
并记录"最后一次事件时间"，工作线程只处理静默超过 QUIET_PERIOD 的路径。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.watcher.stability import STABILIZE_TIMEOUT, looks_like_temp, stabilize

log = logging.getLogger("inktable.watcher")

QUIET_PERIOD = 1.5      # 路径静默多久后才开始稳定性检测
POLL_INTERVAL = 0.5     # 工作线程扫队列的间隔


class _Handler(FileSystemEventHandler):
    """只做入队，绝不做重活。"""

    def __init__(self, enqueue):
        self._enqueue = enqueue

    def on_created(self, event):
        if not event.is_directory:
            self._enqueue(event.src_path, moved_in=False)

    def on_moved(self, event):
        # §11.1：微信「临时目录→移入」、浏览器「.crdownload→改名」都走这条。
        # 漏掉 on_moved 就等于漏掉两种主流下载方式。
        if not event.is_directory:
            self._enqueue(event.dest_path, moved_in=True)

    def on_modified(self, event):
        # 已入库文件被改写 → 需要重新索引（§12.5 增量更新）
        if not event.is_directory:
            self._enqueue(event.src_path, moved_in=False)


class Watcher:
    """管理所有已启用来源的监听。

    on_stable(path, moved_in) 由调用方注入 —— 监听器不直接碰数据库，
    避免把 SQLite 单写线程约束（§19 R9）扩散到这一层。
    """

    def __init__(self, on_stable):
        self._on_stable = on_stable
        self._observer: Observer | None = None
        self._pending: dict[str, tuple[float, bool]] = {}  # path -> (最后事件时间, moved_in)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._watched: dict[str, object] = {}
        self._done: dict[str, tuple[int, int]] = {}  # path -> (mtime_ns, size)，去重指纹
        self.stats = {"events": 0, "stable": 0, "deduped": 0, "skipped_temp": 0, "timeout": 0}

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._observer is not None:
            return
        self._observer = Observer()
        self._observer.start()
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="inktable-stabilizer", daemon=True)
        self._worker.start()
        log.info("watcher started")

    def stop(self) -> None:
        self._stop.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._worker:
            self._worker.join(timeout=8)
            self._worker = None
        self._watched.clear()

    def watch(self, path: str) -> bool:
        """加一个监听目录。已在监听则忽略。"""
        if self._observer is None:
            self.start()
        p = Path(path)
        if not p.is_dir() or path in self._watched:
            return False
        try:
            handle = self._observer.schedule(_Handler(self._enqueue), path, recursive=True)
        except OSError as e:
            log.warning("无法监听 %s：%s", path, e)
            return False
        self._watched[path] = handle
        return True

    def unwatch(self, path: str) -> bool:
        handle = self._watched.pop(path, None)
        if handle is None or self._observer is None:
            return False
        self._observer.unschedule(handle)
        return True

    @property
    def watched_paths(self) -> list[str]:
        return sorted(self._watched)

    # ---------------------------------------------------------------- 内部

    def _enqueue(self, path: str, moved_in: bool) -> None:
        """watchdog 线程调用 —— 必须立刻返回。"""
        self.stats["events"] += 1
        if looks_like_temp(Path(path).name):
            self.stats["skipped_temp"] += 1
            return
        with self._lock:
            # moved_in 一旦为真就保持为真：同一路径可能先 created 再 moved
            prev = self._pending.get(path)
            self._pending[path] = (time.monotonic(), moved_in or (prev[1] if prev else False))

    def _run(self) -> None:
        """工作线程：取静默够久的路径，做稳定性检测，稳定则回调。"""
        first_seen: dict[str, float] = {}

        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                ready = [(p, m) for p, (t, m) in self._pending.items()
                         if now - t >= QUIET_PERIOD]
                for p, _ in ready:
                    self._pending.pop(p, None)

            for path, moved_in in ready:
                first_seen.setdefault(path, now)
                try:
                    result = stabilize(path, moved_in=moved_in)
                except Exception as e:
                    log.warning("稳定性检测出错 %s：%s", path, e)
                    continue

                if result.stable:
                    first_seen.pop(path, None)

                    # 去重：一次写入结束会同时触发 modified 与我们自己的
                    # 稳定判定，实测同一文件回调两次。用 (路径, mtime, 大小)
                    # 做指纹 —— 内容没变就不重复回调；真被改写了 mtime 会变，
                    # 仍能触发重新索引（§12.5）。
                    try:
                        st = Path(path).stat()
                        fp = (int(st.st_mtime_ns), st.st_size)
                    except OSError:
                        continue
                    if self._done.get(path) == fp:
                        self.stats["deduped"] += 1
                        continue
                    self._done[path] = fp
                    if len(self._done) > 20000:      # 防无界增长
                        self._done.clear()

                    self.stats["stable"] += 1
                    try:
                        self._on_stable(path, moved_in)
                    except Exception:
                        log.exception("入库回调失败：%s", path)
                elif result.reason == "still_growing":
                    # 还在写 —— 重新入队，但不能无限等（§11.1）
                    if now - first_seen[path] > STABILIZE_TIMEOUT:
                        self.stats["timeout"] += 1
                        first_seen.pop(path, None)
                        log.info("超过 %ds 仍在增长，放弃实时索引：%s", STABILIZE_TIMEOUT, path)
                    else:
                        with self._lock:
                            self._pending[path] = (time.monotonic(), moved_in)

            self._stop.wait(POLL_INTERVAL)
