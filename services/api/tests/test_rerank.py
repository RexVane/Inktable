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


def test_redundant_coverage_demoted_within_document():
    """同文档里重复覆盖同一批查询词的分片让位给带新覆盖的分片。"""
    inputs = [
        rerank.RerankInput(1, 1, "如何启动 python 服务器", "", 0.9),
        rerank.RerankInput(2, 1, "再讲一遍 python 启动方式", "", 0.8),
        rerank.RerankInput(3, 1, "使用 python 3.11 版本 开发", "", 0.7),
    ]
    ranked = [
        rerank.RerankOutput(1, 0.90),
        rerank.RerankOutput(2, 0.88),  # 无新增覆盖，应被降权
        rerank.RerankOutput(3, 0.86),  # 新增覆盖"版本/开发"，应升到第 2
    ]
    adjusted = rerank._demote_redundant_coverage(
        "python 启动 版本 开发", inputs, ranked,
    )
    assert [item.chunk_id for item in adjusted] == [1, 3, 2]
    # 惩罚是软的：被降权分片仍在结果里，未被淘汰（K3）
    assert len(adjusted) == 3


def test_redundancy_pass_keeps_degraded_inputs_untouched():
    """降级路径（空文本输入）不受去冗影响，保持 RRF 顺序。"""
    inputs = [
        rerank.RerankInput(1, 0, "", "", 0.9),
        rerank.RerankInput(2, 0, "", "", 0.8),
    ]
    ranked = [rerank.RerankOutput(1, 0.9), rerank.RerankOutput(2, 0.8)]
    adjusted = rerank._demote_redundant_coverage("问题 词", inputs, ranked)
    assert [item.chunk_id for item in adjusted] == [1, 2]
    assert [item.score for item in adjusted] == [0.9, 0.8]
