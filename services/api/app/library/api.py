"""FastAPI router for the Inktable AI Library.

The router is built from injected database/lock/auth dependencies so this
module stays independent from ``app.main`` and can be tested in isolation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.library.core import sync_library_items
from app.library.query import library_item_detail, library_page, library_stats


LibraryStatus = Literal["pending", "running", "ready", "failed", "stale"]


def create_library_router(
    db_provider: Callable,
    write_lock,
    auth_dependency: Callable,
) -> APIRouter:
    """Create the /library API without importing the sidecar singleton."""
    router = APIRouter(
        prefix="/library",
        tags=["library"],
        dependencies=[Depends(auth_dependency)],
    )

    @router.get("/items")
    def get_items(
        status: LibraryStatus | None = None,
        category_id: int | None = Query(None, ge=1),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0, le=1_000_000),
    ) -> dict:
        return library_page(
            db_provider(),
            status=status,
            category_id=category_id,
            limit=limit,
            offset=offset,
        )

    @router.get("/items/{item_id}")
    def get_item(item_id: int) -> dict:
        if item_id < 1:
            raise HTTPException(status_code=422, detail="item_id 必须大于 0")
        item = library_item_detail(db_provider(), item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="知识条目不存在或当前不可见")
        return item

    @router.get("/stats")
    def get_stats() -> dict:
        return library_stats(db_provider())

    @router.post("/sync")
    def post_sync() -> dict:
        # Sync is deterministic metadata maintenance. It never touches the
        # physical filesystem, but it is still a database write and therefore
        # participates in Inktable's single-writer lock.
        with write_lock:
            conn = db_provider()
            try:
                result = sync_library_items(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return result

    return router
