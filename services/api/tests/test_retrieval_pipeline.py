"""Shared retrieval pipeline and privacy-safe trace contracts."""

from __future__ import annotations

from app.db.database import connect, init_db
from app.index.search import index_chunk
from app.retrieval.pipeline import (
    Candidate,
    ContextCandidate,
    QueryPlan,
    RetrievalResult,
    RetrievalTrace,
    assemble_context,
    compress_evidence,
    expand_neighbors,
    load_context_candidates,
    run,
)


def _db():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute(
        "INSERT INTO contents (id, sha256, size) VALUES (1, 'a', 10)"
    )
    conn.execute(
        """INSERT INTO files
           (id, volume_uuid, inode, content_id, path, name, ext, size, state,
            mtime, detected_at)
           VALUES (1, 'vol', 1, 1, '/tmp/knowledge.md', 'knowledge.md', '.md',
                   10, 'active', 1, 1)"""
    )
    conn.execute(
        """INSERT INTO chunks
           (id, content_id, ordinal, text, text_hash, section_path)
           VALUES (1, 1, 0, '分层索引和混合检索组成知识检索底座', 'h1', '架构')"""
    )
    index_chunk(conn, 1, "分层索引和混合检索组成知识检索底座", "架构")
    conn.commit()
    return conn


def test_trace_has_stable_stages_without_query_text():
    conn = _db()
    try:
        result = run(conn, "知识检索底座", route_limit=20)
    finally:
        conn.close()

    assert [stage["name"] for stage in result.trace.stages] == [
        "hierarchy_routing", "deep_retrieval", "decompose", "scope", "rrf",
        "rerank",
    ]
    assert result.trace.trace_id
    assert result.trace.query_hash
    assert "知识检索底座" not in str(result.trace.to_dict())
    assert result.trace.candidates == [{
        "chunk_id": 1,
        "rrf_score": result.candidates[0].rrf_score,
        "rerank_score": result.candidates[0].rerank_score,
        "route_ranks": result.candidates[0].route_ranks,
    }]
    assert result.candidates[0].chunk_id == 1
    assert result.candidates[0].rrf_score > 0
    assert result.candidates[0].final_score == result.candidates[0].rerank_score


def test_empty_book_scope_removes_candidates():
    conn = _db()
    try:
        conn.execute("INSERT INTO books (id, name, created_at) VALUES (1, '空书', 1)")
        conn.commit()
        result = run(conn, "知识检索底座", route_limit=20, book_id=1)
    finally:
        conn.close()

    assert result.candidates == []
    assert result.trace.stages[3]["book_id"] == 1


def _retrieval(*chunk_ids: int) -> RetrievalResult:
    return RetrievalResult(
        plan=QueryPlan(query="test"),
        routes={},
        candidates=[
            Candidate(chunk_id=chunk_id, rrf_score=1.0 / rank)
            for rank, chunk_id in enumerate(chunk_ids, start=1)
        ],
        trace=RetrievalTrace(trace_id="trace", query_hash="hash"),
    )


def test_context_diversity_caps_chunks_per_content():
    conn = _db()
    try:
        conn.executemany(
            """INSERT INTO chunks
               (id, content_id, ordinal, text, text_hash, section_path)
               VALUES (?, 1, ?, ?, ?, '架构')""",
            [
                (2, 1, "同一文档片段二", "h2"),
                (3, 2, "同一文档片段三", "h3"),
                (4, 3, "同一文档片段四", "h4"),
            ],
        )
        conn.execute(
            "INSERT INTO contents (id, sha256, size) VALUES (2, 'b', 10)"
        )
        conn.execute(
            """INSERT INTO files
               (id, volume_uuid, inode, content_id, path, name, ext, size,
                state, mtime, detected_at)
               VALUES (2, 'vol', 2, 2, '/tmp/second.md', 'second.md', '.md',
                       10, 'active', 1, 1)"""
        )
        conn.execute(
            """INSERT INTO chunks
               (id, content_id, ordinal, text, text_hash, section_path)
               VALUES (5, 2, 0, '第二个文档', 'h5', '架构')"""
        )
        retrieval = _retrieval(1, 2, 3, 4, 5)

        selected = load_context_candidates(
            conn, retrieval, limit=4, max_per_content=3,
        )
    finally:
        conn.close()

    assert [candidate.chunk_id for candidate in selected] == [1, 2, 3, 5]
    assert retrieval.trace.stages[-1]["name"] == "diversify"
    assert retrieval.trace.stages[-1]["max_per_content"] == 3


def test_neighbor_expansion_preserves_ordinal_order():
    conn = _db()
    try:
        conn.execute("UPDATE chunks SET text = '前' WHERE id = 1")
        conn.executemany(
            """INSERT INTO chunks
               (id, content_id, ordinal, text, text_hash, section_path)
               VALUES (?, 1, ?, ?, ?, '架构')""",
            [(2, 1, "中", "h2"), (3, 2, "后", "h3")],
        )
        candidate = ContextCandidate(
            chunk_id=2, content_id=1, file_id=1,
            file_name="knowledge.md", file_path="/tmp/knowledge.md",
            page=None, section_path="架构", ordinal=1, text="中",
        )
        trace = RetrievalTrace(trace_id="trace", query_hash="hash")

        expanded = expand_neighbors(
            conn, [candidate], neighbor_span=1, trace=trace,
        )
    finally:
        conn.close()

    assert expanded[0].expanded_text == "前\n中\n后"
    assert candidate.expanded_text == ""
    assert trace.stages[-1]["name"] == "expand"


def test_assemble_context_is_stable_pass_through():
    candidate = ContextCandidate(
        chunk_id=1, content_id=1, file_id=1,
        file_name="knowledge.md", file_path="/tmp/knowledge.md",
        page=None, section_path="架构", ordinal=0, text="命中片",
        expanded_text="完整上下文",
    )
    trace = RetrievalTrace(trace_id="trace", query_hash="hash")

    spans = compress_evidence("完整上下文", [candidate], trace=trace)
    assembled = assemble_context(spans, trace=trace)

    assert assembled.spans
    assert assembled.spans[0].text == "命中片"
    assert trace.stages[-1]["name"] == "assemble"
