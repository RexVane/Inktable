from __future__ import annotations

import threading
import time

import app.library.worker as worker_mod
from app.library.core import sync_library_items
from app.library.enrichment import count_claimable, run_enrichment_batch
from app.library.worker import EnrichmentDaemon

from tests.test_library_enrichment import _seed, _valid_result


def _wait_until(predicate, *, timeout: float = 4.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.02)
    raise AssertionError(f"condition not met: {last!r}")


def _patched_batch(monkeypatch):
    def batch(*args, **kwargs):
        kwargs["generate_fn"] = lambda _prompt: _valid_result()
        kwargs["model"] = "fake-local-model"
        return run_enrichment_batch(*args, **kwargs)

    monkeypatch.setattr(worker_mod, "run_enrichment_batch", batch)
    monkeypatch.setattr(worker_mod, "model_available", lambda **_kwargs: True)


def test_count_claimable_sees_pending_but_not_failed_by_default() -> None:
    conn = _seed()
    assert count_claimable(conn) == 1
    item_id = conn.execute("SELECT id FROM library_items").fetchone()[0]
    conn.execute(
        "UPDATE library_items SET enrichment_status='failed' WHERE id=?",
        (item_id,),
    )
    conn.commit()
    assert count_claimable(conn) == 0
    assert count_claimable(conn, include_failed=True) == 1
    conn.close()


def test_user_drain_processes_every_pending_item(monkeypatch) -> None:
    conn = _seed()
    _patched_batch(monkeypatch)
    lock = threading.Lock()
    daemon = EnrichmentDaemon(
        lambda: conn, lock, scan_interval=0, idle_pause=0, user_batch=1,
    )
    try:
        daemon.start()
        snap = daemon.request_drain()
        assert snap["running"] is True
        assert snap["mode"] == "user"
        done = _wait_until(lambda: (not daemon.snapshot()["running"]) or None)
        assert done is True
        final = daemon.snapshot()
        assert final["ready"] == 1
        assert final["failed"] == 0
        row = conn.execute("SELECT enrichment_status FROM library_items").fetchone()
        assert row[0] == "ready"
    finally:
        daemon.stop()
        conn.close()


def test_idle_scan_drains_leftovers_and_skips_failed(monkeypatch) -> None:
    conn = _seed()
    _patched_batch(monkeypatch)
    item_id = conn.execute("SELECT id FROM library_items").fetchone()[0]
    conn.execute(
        "UPDATE library_items SET enrichment_status='failed' WHERE id=?",
        (item_id,),
    )
    conn.commit()
    lock = threading.Lock()
    daemon = EnrichmentDaemon(
        lambda: conn, lock, scan_interval=0.05, idle_pause=0, idle_batch=1,
    )
    try:
        daemon.start()
        time.sleep(0.2)
        snap = daemon.snapshot()
        assert snap["running"] is False
        assert conn.execute(
            "SELECT enrichment_status FROM library_items"
        ).fetchone()[0] == "failed"
    finally:
        daemon.stop()
        conn.close()


def test_idle_scan_processes_pending_after_interval(monkeypatch) -> None:
    conn = _seed()
    _patched_batch(monkeypatch)
    lock = threading.Lock()
    daemon = EnrichmentDaemon(
        lambda: conn, lock, scan_interval=0.05, idle_pause=0, idle_batch=1,
    )
    try:
        daemon.start()
        _wait_until(lambda: conn.execute(
            "SELECT enrichment_status FROM library_items"
        ).fetchone()[0] == "ready")
        snap = daemon.snapshot()
        assert snap["last_mode"] == "idle"
        assert snap["ready"] == 1
    finally:
        daemon.stop()
        conn.close()


def test_cancel_stops_before_the_next_batch(monkeypatch) -> None:
    conn = _seed()
    started = threading.Event()
    release = threading.Event()

    def batch(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2)
        kwargs["generate_fn"] = lambda _prompt: _valid_result()
        kwargs["model"] = "fake-local-model"
        return run_enrichment_batch(*args, **kwargs)

    monkeypatch.setattr(worker_mod, "run_enrichment_batch", batch)
    monkeypatch.setattr(worker_mod, "model_available", lambda **_kwargs: True)
    lock = threading.Lock()
    daemon = EnrichmentDaemon(
        lambda: conn, lock, scan_interval=0, idle_pause=0, user_batch=1,
    )
    try:
        daemon.start()
        daemon.request_drain()
        assert started.wait(timeout=2)
        daemon.cancel()
        release.set()
        _wait_until(lambda: (not daemon.snapshot()["running"]) or None)
        assert daemon.snapshot()["running"] is False
    finally:
        release.set()
        daemon.stop()
        conn.close()


def test_sync_library_items_still_required_for_new_rows() -> None:
    conn = _seed()
    # count_claimable itself syncs; this just keeps the helper honest.
    sync_library_items(conn, now=20)
    assert count_claimable(conn) == 1
    conn.close()
