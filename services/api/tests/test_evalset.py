"""v7 evaluation set structure and annotation guardrails."""

from __future__ import annotations

from collections import Counter

from tests.evalset import ALL_CASES, ANSWERABLE, UNANSWERABLE, summary


def test_v7_evalset_has_required_size_and_unique_ids():
    assert len(ALL_CASES) >= 60
    assert len({case.qid for case in ALL_CASES}) == len(ALL_CASES)
    assert len(UNANSWERABLE) >= 10
    assert all(case.query.strip() for case in ALL_CASES)


def test_v7_evalset_covers_release_categories():
    info = summary()
    by_category = Counter(
        case.category or (
            "single_fact" if case.difficulty == "exact" else
            "paraphrase" if case.difficulty == "paraphrase" else
            "cross_chunk"
        )
        for case in ANSWERABLE
    )

    assert info["total"] == len(ALL_CASES)
    assert by_category["single_fact"] >= 20
    assert by_category["paraphrase"] >= 10
    assert by_category["cross_chunk"] >= 10
    assert by_category["cross_document"] >= 10
    assert by_category["metadata"] >= 10


def test_answerable_cases_have_gold_document_hints():
    assert all(case.doc_hints for case in ANSWERABLE)
    assert all(case.doc_hint is None for case in UNANSWERABLE)

