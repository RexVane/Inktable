from __future__ import annotations

import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import database
from app.library.api import create_library_router
from app.library.core import sync_library_items


def _app():
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
    return app, conn


def test_library_list_detail_and_stats_routes() -> None:
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
