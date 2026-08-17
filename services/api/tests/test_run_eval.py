from __future__ import annotations

from types import SimpleNamespace

from tests.eval_gold import ContentGroup, EvidenceRequirement, GoldCase, GoldSpan
from tests.run_eval import judge


def test_gold_evidence_recall_at_50_uses_deep_chunk_pool():
    sha = "a" * 64
    span = GoldSpan(
        content_sha256=sha,
        chunk_text_hash="b" * 40,
        quote="gold",
        chunk_start_offset=0,
        chunk_end_offset=4,
        document_start_offset=10,
        document_end_offset=14,
    )
    gold = GoldCase(
        qid="T01",
        status="answerable",
        reason="",
        content_groups=(ContentGroup("source", "Source", frozenset({sha})),),
        evidence_requirements=(EvidenceRequirement("evidence", "source", (span,)),),
    )
    top_document = {
        "sha256": sha,
        "names": ["source.md"],
        "chunks": [{
            "text_hash": "c" * 40,
            "text_length": 20,
            "document_start_offset": 100,
            "document_end_offset": 120,
        }],
    }
    deep_evidence = [{
        "sha256": sha,
        "chunks": [{
            "text_hash": span.chunk_text_hash,
            "text_length": 4,
            "document_start_offset": 10,
            "document_end_offset": 14,
        }],
    }]

    result = judge(
        SimpleNamespace(qid="T01", difficulty="exact"),
        gold,
        [top_document],
        deep_evidence,
        1.0,
    )

    assert result["evidence_recall"] == 0.0
    assert result["evidence_recall_50"] == 1.0
    assert result["evidence_hits_50"] == 1
