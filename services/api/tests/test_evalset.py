"""v7 evaluation set structure and annotation guardrails."""

from __future__ import annotations

from collections import Counter

from scripts.build_eval_gold import _iter_term_matches
from tests.eval_gold import load_gold
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


def test_content_addressed_gold_covers_every_case_and_separates_missing_corpus():
    gold = load_gold()

    assert set(gold) == {case.qid for case in ALL_CASES}
    assert Counter(case.status for case in gold.values()) == {
        "answerable": 46,
        "metadata": 7,
        "corpus_missing": 12,
        "unanswerable": 12,
    }
    assert all(
        case.content_groups and case.evidence_requirements
        for case in gold.values() if case.status == "answerable"
    )
    assert all(
        case.content_groups and not case.evidence_requirements
        for case in gold.values() if case.status == "metadata"
    )


def test_scene_planning_is_never_accepted_as_ordo_plan_gold():
    scene_planning_sha = (
        "c54a8cf6cf864ff5a60d4569e4e285f0fca88e40f3cbff1350b70b099af319e0"
    )
    gold = load_gold()

    selected = {
        digest
        for case in gold.values()
        for group in case.content_groups
        for digest in group.content_sha256
    }
    assert scene_planning_sha not in selected


def test_corpus_drift_cases_are_reclassified_without_losing_refusal_coverage():
    gold = load_gold()

    assert all(gold[qid].status == "answerable" for qid in {
        "U02", "U04", "U06", "U09", "U10",
    })
    assert all(gold[qid].status == "unanswerable" for qid in {
        "U13", "U14", "U15", "U16", "U17",
    })


def test_gold_term_matching_rejects_partial_numeric_tokens():
    text = "35 135 350 3.5 35.0 .35 35\n"

    assert [start for start, _ in _iter_term_matches(text, "35")] == [0, 24]
    assert list(_iter_term_matches("A35B", "35")) == [(1, 3)]
    assert list(_iter_term_matches("135", "non-numeric")) == []
