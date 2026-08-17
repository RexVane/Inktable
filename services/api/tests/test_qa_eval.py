"""Privacy-safe real QA evaluation accounting."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_qa_eval import (
    _claim_evidence,
    _is_provider_failure,
    _judge_claim_support,
    _parse_json_object,
    _passes_gates,
    _statement_citation_coverage,
    _summarize,
    choose_provider,
)


def test_claim_extraction_binds_only_declared_citation_tags():
    claims = _claim_evidence(
        "第一项事实有来源说明 [C1]。第二项事实没有引用。",
        [{"tag": "C1", "snippet": "第一项事实的原始证据"}],
    )

    assert len(claims) == 2
    assert claims[0]["evidence"] == ["第一项事实的原始证据"]
    assert claims[1]["evidence"] == []


def test_claim_extraction_binds_citation_after_sentence_punctuation():
    answer = "第一项事实有直接依据。 [C1]\n第二项事实也有直接依据。[C2]"
    citations = [
        {"tag": "C1", "snippet": "第一项事实有直接依据"},
        {"tag": "C2", "snippet": "第二项事实也有直接依据"},
    ]

    claims = _claim_evidence(answer, citations)

    assert [claim["evidence"] for claim in claims] == [
        ["第一项事实有直接依据"], ["第二项事实也有直接依据"],
    ]
    assert _statement_citation_coverage(answer) == (2, 2)


def test_short_numeric_claim_is_counted_without_stripping_its_value():
    answer = "32 字节 [C1]。"
    claims = _claim_evidence(answer, [{
        "tag": "C1",
        "snippet": "FAT12 的目录项长度为 32 字节。",
    }])

    assert len(claims) == 1
    assert claims[0]["claim"].startswith("32 字节")
    assert _statement_citation_coverage(answer) == (1, 1)


def test_metadata_claim_evidence_includes_cited_file_name():
    claims = _claim_evidence(
        "记录平台网站思路的是《平台网站思路.docx》 [C1]。",
        [{
            "tag": "C1",
            "file_name": "平台网站思路.docx",
            "snippet": "网站整体视觉采用克制的配色。",
        }],
    )

    assert claims[0]["evidence"] == [
        "文件名：平台网站思路.docx\n网站整体视觉采用克制的配色。",
    ]


def test_judge_json_parser_accepts_fenced_json_only():
    assert _parse_json_object('```json\n{"judgments":[true,false]}\n```') == {
        "judgments": [True, False],
    }


def test_citation_judge_retries_only_transient_provider_failures(monkeypatch):
    from app.qa import llm

    calls = 0

    def transient_then_success(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise llm.LLMHTTPError(429)
        return '{"judgments":[true]}'

    monkeypatch.setattr(llm, "chat", transient_then_success)
    supported, total, error = _judge_claim_support(
        [{"claim": "有直接证据的事实", "evidence": ["原始证据"]}],
        retries=1,
    )

    assert (supported, total, error) == (1, 1, "")
    assert calls == 2


def test_qa_summary_keeps_semantic_support_and_gold_recall_separate():
    results = [
        {
            "qid": "A01", "kind": "answerable", "answer_success": True,
            "supported_claims": 19, "claims": 20,
            "gold_support_hit": 2, "gold_support_total": 3,
            "exact_citations": 4, "citation_count": 4,
            "cited_statements": 20, "statements": 20,
        },
        {
            "qid": "U01", "kind": "unanswerable", "correct_refusal": True,
        },
    ]

    summary = _summarize(results, {"A01", "U01"})

    assert summary["complete"] is True
    assert summary["citation_support_rate"] == 0.95
    assert summary["gold_evidence_citation_recall"] == 2 / 3
    assert summary["correct_refusal_rate"] == 1.0


def test_qa_gates_skip_metrics_without_an_applicable_subset():
    base = {
        "complete": True,
        "citation_support_rate": 0.0,
        "correct_refusal_rate": 1.0,
        "exact_citation_rate": 0.0,
    }
    assert _passes_gates(
        base, has_evaluable=False, has_unanswerable=True,
    )

    answerable_only = {
        **base,
        "citation_support_rate": 1.0,
        "correct_refusal_rate": 0.0,
        "exact_citation_rate": 1.0,
    }
    assert _passes_gates(
        answerable_only, has_evaluable=True, has_unanswerable=False,
    )


def test_strict_qa_gates_require_answer_success_and_clean_execution():
    summary = {
        "complete": True,
        "answer_success_rate": 0.95,
        "citation_support_rate": 0.95,
        "correct_refusal_rate": 0.85,
        "exact_citation_rate": 1.0,
        "provider_failures": 0,
        "degraded_cases": 0,
    }
    assert _passes_gates(summary, has_evaluable=True, has_unanswerable=True)
    assert not _passes_gates(
        {**summary, "answer_success_rate": 0.94},
        has_evaluable=True, has_unanswerable=True,
    )
    assert not _passes_gates(
        {**summary, "provider_failures": 1},
        has_evaluable=True, has_unanswerable=True,
    )
    assert not _passes_gates(
        {**summary, "degraded_cases": 1},
        has_evaluable=True, has_unanswerable=True,
    )


def test_choose_provider_can_select_a_named_non_current_provider():
    providers = [
        {
            "name": "Current",
            "model": "model-a",
            "api_key": "key-a",
            "is_current": True,
            "openai_native": True,
        },
        {
            "name": "Kocode",
            "model": "model-b",
            "api_key": "key-b",
            "is_current": False,
            "openai_native": True,
        },
    ]

    assert choose_provider(providers)["name"] == "Current"
    assert choose_provider(providers, "kocode")["name"] == "Kocode"
    assert choose_provider(providers, "missing") is None


def test_provider_failure_detection_is_limited_to_transport_failures():
    assert _is_provider_failure({
        "status": "fallback",
        "validation": {
            "error": "无法连接模型服务",
            "error_type": "LLMConnectionError",
            "error_code": "unreachable",
        },
    })
    assert _is_provider_failure({
        "validation": {"error": "模型服务返回 429"},
    })
    assert _is_provider_failure({
        "status": "fallback",
        "validation": {"error": "无法连接模型服务"},
    })
    assert _is_provider_failure({
        "citation_judge_error": "LLMHTTPError",
    })
    assert not _is_provider_failure({
        "status": "fallback",
        "validation": {"unsupported_claims": 1},
        "citation_judge_error": "no_substantive_claim",
    })
