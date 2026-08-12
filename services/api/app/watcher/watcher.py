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
import os
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app.watcher.stability import STABILIZE_TIMEOUT, looks_like_temp, stabilize
from app.watcher.scanner import should_skip_path

log = logging.getLogger("inktable.watcher")

QUIET_PERIOD = 1.5      # 路径静默多久后才开始稳定性检测
POLL_INTERVAL = 0.5     # 工作线程扫队列的间隔
GONE_QUIET = 3.0        # deleted 事件后等这么久才确认消失 ——
                        # 原子替换（删+建）与编辑器的保存舞步都在此窗口内完成


def _default_observer():
    """创建平台监听器；测试/诊断可显式切到纯 Python polling。

    macOS 的 FSEvents 后端是原生扩展，某些 CI/共享执行环境无法创建 stream，
    甚至会在 Python 来得及捕获异常前触发进程级 Bus error。生产仍默认使用
    FSEvents；设置 ``INKTABLE_WATCH_BACKEND=polling`` 时改用安全的轮询后端。
    """
    if os.environ.get("INKTABLE_WATCH_BACKEND", "").strip().lower() == "polling":
        return PollingObserver(timeout=0.2)
    return Observer()


class _Handler(FileSystemEventHandler):
    """只做入队，绝不做重活。"""

    def __init__(self, enqueue, enqueue_gone, root: str):
        self._enqueue = enqueue
        self._enqueue_gone = enqueue_gone
        self._root = root

    def on_created(self, event):
        if not event.is_directory:
            self._enqueue(event.src_path, moved_in=False, root=self._root)

    def on_moved(self, event):
        # §11.1：微信「临时目录→移入」、浏览器「.crdownload→改名」都走这条。
        # 漏掉 on_moved 就等于漏掉两种主流下载方式。
        if not event.is_directory:
            self._enqueue(event.dest_path, moved_in=True, root=self._root)
            # 树内移动：旧路径消失了。交给 gone 队列判定 ——
            # register_file 靠 inode 命中会把新路径写回，旧路径不会误标 missing
            # （gone 判定时会先查库里该路径对应的文件是否已在别处找到）。
            self._enqueue_gone(event.src_path, root=self._root)

    def on_modified(self, event):
        # 已入库文件被改写 → 需要重新索引（§12.5 增量更新）
        if not event.is_directory:
            self._enqueue(event.src_path, moved_in=False, root=self._root)

    def on_deleted(self, event):
        # 文件消失 —— 可能是真删除，也可能是原子替换（删+建）或
        # 移到监听树外。**不能立刻动库**：静默期后确认还不在才算 gone。
        if not event.is_directory:
            self._enqueue_gone(event.src_path, root=self._root)


class Watcher:
    """管理所有已启用来源的监听。

    on_stable(path, moved_in) 由调用方注入 —— 监听器不直接碰数据库，
    避免把 SQLite 单写线程约束（§19 R9）扩散到这一层。
    """

    def __init__(self, on_stable, on_gone=None, observer_factory=None):
        self._on_stable = on_stable
        self._on_gone = on_gone          # 路径确认消失后的回调（可选）
        self._observer_factory = observer_factory or _default_observer
        self._observer: Observer | None = None
        self._pending: dict[str, tuple[float, bool]] = {}  # path -> (最后事件时间, moved_in)
        self._gone: dict[str, float] = {}                  # path -> 最后 deleted 事件时间
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._watched: dict[str, object] = {}
        self._done: dict[str, tuple[int, int]] = {}  # path -> (mtime_ns, size)，去重指纹
        self.stats = {"events": 0, "stable": 0, "deduped": 0,
                      "skipped_temp": 0, "skipped_excluded": 0,
                      "timeout": 0, "gone": 0}

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._observer is not None:
            return
        observer = self._observer_factory()
        try:
            observer.start()
        except Exception:
            try:
                observer.stop()
            except Exception:
                pass
            raise
        self._observer = observer
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
            handle = self._observer.schedule(
                _Handler(self._enqueue, self._enqueue_gone, str(p)), str(p), recursive=True
            )
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

    def _enqueue_gone(self, path: str, root: str | None = None) -> None:
        """watchdog 线程调用 —— 记录"路径可能消失了"，稍后确认。"""
        if root is not None and should_skip_path(path, root):
            return
        if looks_like_temp(Path(path).name):
            return
        with self._lock:
            self._gone[path] = time.monotonic()
            # 同一路径若还有 pending 的稳定检测，撤掉 —— 文件都没了
            self._pending.pop(path, None)

    def _enqueue(self, path: str, moved_in: bool, root: str | None = None) -> None:
        """watchdog 线程调用 —— 必须立刻返回。"""
        self.stats["events"] += 1
        if root is not None and should_skip_path(path, root):
            self.stats["skipped_excluded"] += 1
            return
        if looks_like_temp(Path(path).name):
            self.stats["skipped_temp"] += 1
            return
        with self._lock:
            # 文件又出现了 —— 撤销待确认的消失（原子替换是"删 + 建"两个事件）
            self._gone.pop(path, None)
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

            # 消失确认：deleted 后静默 GONE_QUIET 秒、且磁盘上确实不在了
            if self._on_gone is not None:
                with self._lock:
                    gone_ready = [p for p, t in self._gone.items()
                                  if now - t >= GONE_QUIET]
                    for p in gone_ready:
                        self._gone.pop(p, None)
                for path in gone_ready:
                    if Path(path).exists():
                        continue  # 又回来了（慢速原子替换），不算消失
                    self._done.pop(path, None)   # 指纹作废，重现时能再触发
                    self.stats["gone"] += 1
                    try:
                        self._on_gone(path)
                    except Exception:
                        log.exception("消失回调失败：%s", path)

            self._stop.wait(POLL_INTERVAL)
