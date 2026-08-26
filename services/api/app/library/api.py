"""FastAPI router for the Inktable AI Library.

The router is built from injected database/lock/auth dependencies so this
module stays independent from ``app.main`` and can be tested in isolation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.library.core import sync_library_items
from app.library.enrichment import run_enrichment_batch, status as enrichment_status
from app.library.query import library_item_detail, library_page, library_stats
from app.library.relations import (
    DEFAULT_CHUNKS_PER_ITEM,
    DEFAULT_LIMIT as DEFAULT_RELATION_LIMIT,
    DEFAULT_MIN_SCORE,
    DEFAULT_TOP_K,
    MAX_CHUNKS_PER_ITEM,
    MAX_LIMIT as MAX_RELATION_LIMIT,
    MAX_TOP_K,
    apply_relation_plan,
    build_relation_plan,
)


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

    @router.get("/enrichment/status")
    def get_enrichment_status() -> dict:
        """Local-only model capability; no document content is sent here."""
        return enrichment_status()

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

    @router.post("/enrich")
    def post_enrich(limit: int = Query(3, ge=1, le=10)) -> dict:
        """Explicitly run a bounded local-Ollama enrichment batch.

        This route never falls through to the cloud QA provider. The request is
        the user's explicit action authorizing local model processing; indexing
        by itself does not send document excerpts anywhere.
        """
        return run_enrichment_batch(
            db_provider,
            write_lock,
            limit=limit,
        )

    @router.post("/relations/rebuild")
    def rebuild_relations(
        limit: int = Query(DEFAULT_RELATION_LIMIT, ge=2, le=MAX_RELATION_LIMIT),
        top_k: int = Query(DEFAULT_TOP_K, ge=1, le=MAX_TOP_K),
        min_score: float = Query(DEFAULT_MIN_SCORE, ge=-1.0, le=1.0),
        chunks_per_item: int = Query(
            DEFAULT_CHUNKS_PER_ITEM, ge=1, le=MAX_CHUNKS_PER_ITEM,
        ),
    ) -> dict:
        """Rebuild conservative related-document edges from existing vectors.

        Only the short Library sync and final edge apply hold the global write
        lock. Sampling vectors and computing document-centroid similarities run
        outside it, so relation rebuilding cannot freeze watcher/index writes.
        """
        with write_lock:
            conn = db_provider()
            try:
                sync_result = sync_library_items(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        plan = build_relation_plan(
            db_provider(),
            limit=limit,
            top_k=top_k,
            min_score=min_score,
            chunks_per_item=chunks_per_item,
        )

        with write_lock:
            conn = db_provider()
            try:
                result = apply_relation_plan(conn, plan)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            **result,
            "planned_relations": len(plan.edges),
            "library_sync": sync_result,
        }

    return router
