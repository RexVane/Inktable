"""Background drain for 知识馆整理.

The renderer used to loop ``/step`` itself. Leaving the knowledge-library view,
reloading, or a single failed refresh would stop the pass mid-corpus. The
sidecar now owns the loop:

- User click: drain every pending/stale item (failed only when asked).
- Idle: every 30 minutes, if leftovers exist, process them one-by-one.

Failed items are never auto-retried. Cloud calls still require the library
slot to be pointed at a cloud endpoint; indexing alone never starts a drain.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from app.library.enrichment import (
    DEFAULT_BATCH,
    cancel_enrichment_run,
    count_claimable,
    create_enrichment_run,
    model_available,
    run_enrichment_batch,
)

log = logging.getLogger("inktable.library.worker")

DEFAULT_SCAN_SECONDS = 30 * 60
IDLE_BATCH = 1
IDLE_BATCH_PAUSE = 1.0

_sidecar_worker: "EnrichmentDaemon | None" = None
_sidecar_lock = threading.Lock()


def scan_interval_seconds() -> float:
    raw = (os.environ.get("INKTABLE_LIBRARY_ENRICH_SCAN_SECONDS") or "").strip()
    if not raw:
        return float(DEFAULT_SCAN_SECONDS)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(DEFAULT_SCAN_SECONDS)


class EnrichmentDaemon:
    """One drain at a time; user requests preempt the idle scan."""

    def __init__(
        self,
        db_provider: Callable,
        write_lock,
        *,
        scan_interval: float | None = None,
        user_batch: int = DEFAULT_BATCH,
        idle_batch: int = IDLE_BATCH,
        idle_pause: float = IDLE_BATCH_PAUSE,
    ) -> None:
        self._db = db_provider
        self._write_lock = write_lock
        self._scan_interval = (
            scan_interval_seconds() if scan_interval is None else max(0.0, float(scan_interval))
        )
        self._user_batch = max(1, int(user_batch))
        self._idle_batch = max(1, int(idle_batch))
        self._idle_pause = max(0.0, float(idle_pause))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._mode: str | None = None
        self._run_id: str | None = None
        self._ready = 0
        self._failed = 0
        self._error: str | None = None
        self._cancel = False
        self._pending_user = False
        self._pending_include_failed = False
        self._last_mode: str | None = None
        self._next_idle_mono: float | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        if self._scan_interval > 0:
            self._next_idle_mono = time.monotonic() + self._scan_interval
        else:
            self._next_idle_mono = None
        self._thread = threading.Thread(
            target=self._loop, name="inktable-library-enrich", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=8)
        self._thread = None

    def snapshot(self) -> dict:
        with self._lock:
            next_scan_at = None
            if self._next_idle_mono is not None:
                next_scan_at = time.time() + max(0.0, self._next_idle_mono - time.monotonic())
            return {
                "running": self._running,
                "mode": self._mode,
                "last_mode": self._last_mode,
                "run_id": self._run_id,
                "ready": self._ready,
                "failed": self._failed,
                "error": self._error,
                "stopping": self._cancel,
                "scan_seconds": self._scan_interval,
                "next_scan_at": next_scan_at,
            }

    def request_drain(self, *, include_failed: bool = False) -> dict:
        """Queue a user drain. An in-flight idle pass is cancelled first."""
        self.start()
        idle_run: str | None = None
        with self._lock:
            if self._running and self._mode == "user" and not self._cancel:
                return self.snapshot()
            previous_mode = self._mode
            previous_run = self._run_id
            self._pending_user = True
            self._pending_include_failed = include_failed
            self._running = True
            self._mode = "user"
            self._last_mode = "user"
            self._ready = 0
            self._failed = 0
            self._error = None
            if previous_mode == "idle":
                self._cancel = True
                idle_run = previous_run
            else:
                self._cancel = False
        if idle_run:
            self._cancel_run(idle_run)
        self._wake.set()
        return self.snapshot()

    def cancel(self) -> dict:
        with self._lock:
            self._cancel = True
            self._pending_user = False
            run_id = self._run_id
        if run_id:
            self._cancel_run(run_id)
        return self.snapshot()

    def _finish_locked(self, mode: str) -> None:
        """Clear the active pass unless a user drain is already queued."""
        self._last_mode = mode
        self._run_id = None
        if self._pending_user:
            self._running = True
            self._mode = "user"
            return
        self._running = False
        self._mode = None

    def _cancel_run(self, run_id: str) -> None:
        with self._write_lock:
            conn = self._db()
            try:
                cancel_enrichment_run(conn, run_id)
                conn.commit()
            except KeyError:
                conn.rollback()
            except Exception:
                conn.rollback()
                raise

    def _loop(self) -> None:
        interval = self._scan_interval
        while not self._stop.is_set():
            timeout = None
            if self._next_idle_mono is not None:
                timeout = max(0.05, self._next_idle_mono - time.monotonic())
            self._wake.wait(timeout=timeout)
            self._wake.clear()
            if self._stop.is_set():
                break
            user = False
            include_failed = False
            with self._lock:
                if self._pending_user:
                    user = True
                    include_failed = self._pending_include_failed
                    self._pending_user = False
            if user:
                self._drain(
                    include_failed=include_failed,
                    batch=self._user_batch,
                    mode="user",
                )
                if interval > 0:
                    self._next_idle_mono = time.monotonic() + interval
                continue
            if self._next_idle_mono is not None and time.monotonic() >= self._next_idle_mono - 0.02:
                self._drain(include_failed=False, batch=self._idle_batch, mode="idle")
                self._next_idle_mono = time.monotonic() + interval

    def _drain(self, *, include_failed: bool, batch: int, mode: str) -> None:
        if not model_available():
            with self._lock:
                self._error = "整理模型未配置或不可用"
                self._finish_locked(mode)
            log.info("跳过知识馆整理（%s）：模型不可用", mode)
            return
        with self._write_lock:
            conn = self._db()
            try:
                pending = count_claimable(conn, include_failed=include_failed)
                if pending <= 0:
                    conn.commit()
                    with self._lock:
                        self._finish_locked(mode)
                    return
                run = create_enrichment_run(conn, include_failed=include_failed)
                conn.commit()
                run_id = str(run["id"])
            except Exception:
                conn.rollback()
                raise
        with self._lock:
            self._running = True
            self._mode = mode
            self._run_id = run_id
            self._ready = 0
            self._failed = 0
            self._error = None
        log.info("知识馆整理开始（%s）run=%s pending≈%s", mode, run_id, pending)
        try:
            while not self._stop.is_set():
                with self._lock:
                    if self._cancel:
                        break
                result = run_enrichment_batch(
                    self._db,
                    self._write_lock,
                    limit=batch,
                    run_id=run_id,
                )
                if result.get("available") is False:
                    with self._lock:
                        self._error = str(result.get("error") or "整理模型不可用")
                    break
                with self._lock:
                    self._ready += int(result.get("ready") or 0)
                    self._failed += int(result.get("failed") or 0)
                claimed = int(result.get("claimed") or 0)
                status = result.get("run_status")
                if not claimed or status in {"completed", "cancelled"}:
                    break
                if mode == "idle" and self._idle_pause:
                    if self._stop.wait(self._idle_pause):
                        break
        finally:
            with self._lock:
                should_cancel = self._cancel
                current = self._run_id
                ready = self._ready
                failed = self._failed
                self._finish_locked(mode)
            if should_cancel and current:
                try:
                    self._cancel_run(current)
                except Exception:  # noqa: BLE001 — shutdown path
                    log.debug("取消整理任务失败", exc_info=True)
            log.info("知识馆整理结束（%s）ready=%s failed=%s", mode, ready, failed)


def start_sidecar_worker(db_provider: Callable, write_lock) -> EnrichmentDaemon:
    global _sidecar_worker
    with _sidecar_lock:
        if _sidecar_worker is not None:
            _sidecar_worker.stop()
        _sidecar_worker = EnrichmentDaemon(db_provider, write_lock)
        _sidecar_worker.start()
        return _sidecar_worker


def stop_sidecar_worker() -> None:
    global _sidecar_worker
    with _sidecar_lock:
        worker = _sidecar_worker
        _sidecar_worker = None
    if worker is not None:
        worker.stop()


def sidecar_worker() -> EnrichmentDaemon | None:
    return _sidecar_worker


def worker_snapshot() -> dict:
    worker = sidecar_worker()
    if worker is None:
        return {
            "running": False,
            "mode": None,
            "run_id": None,
            "ready": 0,
            "failed": 0,
            "error": None,
            "stopping": False,
            "last_mode": None,
            "scan_seconds": scan_interval_seconds(),
            "next_scan_at": None,
        }
    return worker.snapshot()
