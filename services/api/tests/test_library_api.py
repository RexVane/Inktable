from __future__ import annotations

import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import database
import app.library.api as library_api
from app.library.api import create_library_router
from app.library.core import sync_library_items


def _app(*, with_lock: bool = False):
    conn = database.connect(":memory:")
    database.init_db(conn)
    conn.execute(
        "INSERT INTO sources(id, name, path, kind, discovered_by, enabled, created_at) "
        "VALUES (1, '资料', '/docs', 'manual', 'manual', 1, 1)"
    )
    conn.execute(
        "INSERT INTO contents(id, sha256, size, parse_state) "
        "VALUES (1, 'sha-api', 10, 'indexed')"
    )
    conn.execute(
        """INSERT INTO files
           (id, volume_uuid, inode, content_id, path, name, source_id, ext,
            size, state, detected_at, mtime)
           VALUES (1, 'vol', 1, 1, '/docs/note.md', 'note.md', 1, 'md',
                   10, 'registered', 1, 1)"""
    )
    sync_library_items(conn, now=10)
    conn.commit()

    app = FastAPI()

    def auth() -> None:
        return None

    lock = threading.Lock()
    app.include_router(create_library_router(lambda: conn, lock, auth))
    if with_lock:
        return app, conn, lock
    return app, conn


def test_library_list_detail_stats_and_relation_status_routes() -> None:
    app, conn = _app()
    try:
        client = TestClient(app)
        listed = client.get('/library/items')
        assert listed.status_code == 200
        body = listed.json()
        assert body['total'] == 1
        assert body['items'][0]['title'] == 'note.md'
        item_id = body['items'][0]['id']

        detail = client.get(f'/library/items/{item_id}')
        assert detail.status_code == 200
        assert detail.json()['source_files'][0]['path'] == '/docs/note.md'

        stats = client.get('/library/stats')
        assert stats.status_code == 200
        assert stats.json()['total'] == 1

        relation_state = client.get('/library/relations/status')
        assert relation_state.status_code == 200
        assert relation_state.json()['total_visible'] == 1
        assert relation_state.json()['relations'] == 0
        assert relation_state.json()['stale_relations'] == 0
        assert relation_state.json()['needs_rebuild'] is False
    finally:
        conn.close()


def test_library_sync_route_is_idempotent_and_read_routes_hide_disabled_source() -> None:
    app, conn = _app()
    try:
        client = TestClient(app)
        synced = client.post('/library/sync')
        assert synced.status_code == 200
        assert synced.json()['created'] == 0
        assert synced.json()['refreshed'] == 1

        conn.execute('UPDATE sources SET enabled=0 WHERE id=1')
        conn.commit()
        hidden = client.get('/library/items')
        assert hidden.status_code == 200
        assert hidden.json()['total'] == 0

        raw_count = conn.execute('SELECT COUNT(*) FROM library_items').fetchone()[0]
        assert raw_count == 1, 'hiding a source must not destroy AI metadata'
    finally:
        conn.close()


def test_library_api_validates_status_and_missing_item() -> None:
    app, conn = _app()
    try:
        client = TestClient(app)
        invalid = client.get('/library/items?status=unknown')
        assert invalid.status_code == 422
        missing = client.get('/library/items/999')
        assert missing.status_code == 404
    finally:
        conn.close()


def test_library_enrich_route_delegates_bounded_batch(monkeypatch) -> None:
    seen: dict[str, int] = {}

    def fake_run(db_provider, write_lock, *, limit: int):
        assert db_provider() is conn
        assert write_lock is not None
        seen['limit'] = limit
        return {
            'available': True,
            'provider': 'local_ollama',
            'model': 'fake',
            'prompt_version': 'library-enrichment-v1',
            'claimed': 2,
            'ready': 2,
            'failed': 0,
            'stale': 0,
        }

    monkeypatch.setattr(library_api, 'run_enrichment_batch', fake_run)
    app, conn = _app()
    try:
        client = TestClient(app)
        response = client.post('/library/enrich?limit=4')
        assert response.status_code == 200
        assert response.json()['ready'] == 2
        assert seen['limit'] == 4

        too_large = client.post('/library/enrich?limit=11')
        assert too_large.status_code == 422
    finally:
        conn.close()


def test_relation_rebuild_computes_outside_write_lock(monkeypatch) -> None:
    seen: dict[str, object] = {}
    app, conn, lock = _app(with_lock=True)

    def fake_build(_conn, *, limit, top_k, min_score, chunks_per_item):
        # The expensive vector/CPU phase must not hold the global writer lock.
        acquired = lock.acquire(blocking=False)
        assert acquired, 'relation planning unexpectedly holds the write lock'
        lock.release()
        seen.update({
            'limit': limit,
            'top_k': top_k,
            'min_score': min_score,
            'chunks_per_item': chunks_per_item,
        })
        return SimpleNamespace(edges=((1, 2, 0.9),))

    def fake_apply(_conn, plan):
        # The short database apply phase must be serialized.
        assert lock.acquire(blocking=False) is False
        assert len(plan.edges) == 1
        return {
            'processed': 2,
            'vectorized': 2,
            'relations': 1,
            'stale_skipped': 0,
            'total_visible': 2,
            'truncated': False,
            'top_k': 2,
            'min_score': 0.7,
            'chunks_per_item': 4,
            'source': 'embedding-centroid-v1',
        }

    monkeypatch.setattr(library_api, 'build_relation_plan', fake_build)
    monkeypatch.setattr(library_api, 'apply_relation_plan', fake_apply)
    try:
        client = TestClient(app)
        response = client.post(
            '/library/relations/rebuild?limit=10&top_k=2&min_score=0.7&chunks_per_item=4'
        )
        assert response.status_code == 200
        body = response.json()
        assert body['relations'] == 1
        assert body['planned_relations'] == 1
        assert seen == {
            'limit': 10,
            'top_k': 2,
            'min_score': 0.7,
            'chunks_per_item': 4,
        }

        invalid = client.post('/library/relations/rebuild?top_k=25')
        assert invalid.status_code == 422
    finally:
        conn.close()
