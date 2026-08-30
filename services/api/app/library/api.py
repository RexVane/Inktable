"""FastAPI router for the Inktable AI Library.

The router is built from injected database/lock/auth dependencies so this
module stays independent from ``app.main`` and can be tested in isolation.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.library.core import sync_library_items
from app.library.enrichment import (
    cancel_enrichment_run,
    create_enrichment_run,
    enrichment_run,
    run_enrichment_batch,
    status as enrichment_status,
)
from app.library.worker import sidecar_worker, worker_snapshot
from app.library.query import (
    library_item_detail,
    library_page,
    library_stats,
    library_taxonomy,
    library_tree,
)
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
    relation_status,
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
        # category_id=-1 约定为「未分类」（左栏树的未分类节点）
        category_id: int | None = Query(None, ge=-1),
        tag_id: int | None = Query(None, ge=1),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0, le=1_000_000),
    ) -> dict:
        return library_page(
            db_provider(),
            status=status,
            category_id=category_id if category_id is not None and category_id >= 1 else None,
            tag_id=tag_id,
            uncategorized=category_id == -1,
            limit=limit,
            offset=offset,
        )

    @router.get("/taxonomy")
    def get_taxonomy() -> dict:
        return library_taxonomy(db_provider())

    @router.get("/tree")
    def get_tree() -> dict:
        return library_tree(db_provider())

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
        body = enrichment_status()
        body["worker"] = worker_snapshot()
        return body

    @router.post("/enrichment/drain")
    def post_enrichment_drain(retry_failed: bool = Query(False)) -> dict:
        """Ask the sidecar to drain pending items until none remain.

        Failed items stay out unless ``retry_failed`` is set. The 30-minute
        idle scan never retries failures.
        """
        worker = sidecar_worker()
        if worker is None:
            raise HTTPException(status_code=503, detail="整理调度尚未启动")
        return worker.request_drain(include_failed=retry_failed)

    @router.post("/enrichment/drain/cancel")
    def post_enrichment_drain_cancel() -> dict:
        worker = sidecar_worker()
        if worker is None:
            raise HTTPException(status_code=503, detail="整理调度尚未启动")
        return worker.cancel()

    @router.post("/enrichment/runs")
    def post_enrichment_run(retry_failed: bool = Query(False)) -> dict:
        """Start one bounded pass. Failed items require explicit opt-in."""
        with write_lock:
            conn = db_provider()
            try:
                result = create_enrichment_run(
                    conn, include_failed=retry_failed,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return result

    @router.get("/enrichment/runs/{run_id}")
    def get_enrichment_run(run_id: uuid.UUID) -> dict:
        try:
            return enrichment_run(db_provider(), str(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="整理任务不存在") from exc

    @router.post("/enrichment/runs/{run_id}/step")
    def post_enrichment_step(
        run_id: uuid.UUID,
        limit: int = Query(10, ge=1, le=20),
    ) -> dict:
        try:
            return run_enrichment_batch(
                db_provider,
                write_lock,
                limit=limit,
                run_id=str(run_id),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="整理任务不存在") from exc

    @router.post("/enrichment/runs/{run_id}/cancel")
    def post_enrichment_cancel(run_id: uuid.UUID) -> dict:
        with write_lock:
            conn = db_provider()
            try:
                result = cancel_enrichment_run(conn, str(run_id))
                conn.commit()
            except KeyError as exc:
                conn.rollback()
                raise HTTPException(status_code=404, detail="整理任务不存在") from exc
            except Exception:
                conn.rollback()
                raise
        return result

    @router.get("/relations/status")
    def get_relation_status() -> dict:
        """Read-only freshness/coverage state for derived relation edges."""
        return relation_status(db_provider())

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
    def post_enrich(limit: int = Query(10, ge=1, le=20)) -> dict:
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
