"""M3 reranker protocol, soft cap, and explicit degradation contracts."""

from __future__ import annotations

from types import SimpleNamespace

from app.db.database import connect, init_db
from app.retrieval import rerank


def _db():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute(
        "INSERT INTO contents(id, sha256, size) VALUES (1, 'a', 1), (2, 'b', 1)"
    )
    for chunk_id in range(1, 16):
        content_id = 1 if chunk_id <= 14 else 2
        conn.execute(
            """INSERT INTO chunks
               (id, content_id, ordinal, text, text_hash, index_version)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (chunk_id, content_id, chunk_id, f"片段 {chunk_id}", f"h{chunk_id}"),
        )
    conn.commit()
    return conn


def _candidates():
    return [
        SimpleNamespace(chunk_id=chunk_id, rrf_score=1.0 / chunk_id)
        for chunk_id in range(1, 16)
    ]


def test_rrf_fallback_preserves_exact_order(monkeypatch):
    conn = _db()
    try:
        monkeypatch.setenv("INKTABLE_RERANKER", "rrf")
        result = rerank.run_rerank(conn, "问题", _candidates())
    finally:
        conn.close()

    assert [item.chunk_id for item in result.ranked] == list(range(1, 16))
    assert result.degraded is True
    assert result.model_id == "rrf-only"
    assert result.reranked_count == 0


def test_local_failure_degrades_without_reordering(monkeypatch):
    conn = _db()
    try:
        monkeypatch.setenv("INKTABLE_RERANKER", "local")
        monkeypatch.setattr(
            rerank.LocalStaticReranker, "rerank",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fail")),
        )
        result = rerank.run_rerank(conn, "问题", _candidates())
    finally:
        conn.close()

    assert [item.chunk_id for item in result.ranked] == list(range(1, 16))
    assert result.degraded is True
    assert result.model_id == "rrf-only"


def test_soft_cap_limits_long_document_before_local_rerank(monkeypatch):
    conn = _db()
    seen = []

    def record(_self, _query, candidates):
        seen.extend(candidates)
        return [
            rerank.RerankOutput(item.chunk_id, item.rrf_score)
            for item in candidates
        ]

    try:
        monkeypatch.setenv("INKTABLE_RERANKER", "local")
        monkeypatch.setattr(rerank.LocalStaticReranker, "rerank", record)
        result = rerank.run_rerank(conn, "问题", _candidates())
    finally:
        conn.close()

    assert sum(item.content_id == 1 for item in seen) == rerank.SOFT_PER_CONTENT
    assert any(item.content_id == 2 for item in seen)
    assert result.reranked_count == len(seen)
