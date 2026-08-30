"""Run the real-model QA grounding and refusal evaluation over HTTP.

Credentials are selected from cc-switch and sent only to the in-memory sidecar
configuration endpoint. The persisted artifact contains no key, answer text,
source snippet, or file path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import connect
from app.integrations.ccswitch import read_providers
from app.qa.answer import (
    claim_statements,
    is_substantive_statement,
    plain_claim_text,
)
from tests.eval_gold import DEFAULT_GOLD_PATH, load_gold, packed_span_matches
from tests.evalset import ALL_CASES

from runtime_acceptance import Sidecar, choose_provider, emit


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "docs" / "eval" / "v8-qa-real.json"
ANSWER_SUCCESS_GATE = 0.95
CITATION_SUPPORT_GATE = 0.95
CORRECT_REFUSAL_GATE = 0.85
_CITE = re.compile(r"\[C(\d{1,3})\]")
_PROVIDER_ERROR_TYPES = {
    "LLMConnectionError", "LLMHTTPError", "TimeoutError",
}


def _safe_timings(payload: dict) -> list[dict]:
    allowed = {
        "name", "duration_ms", "candidate_count", "reranked_count",
        "model_id", "degraded", "route_limit", "subquery_count",
    }
    return [
        {key: value for key, value in stage.items() if key in allowed}
        for stage in payload.get("timings", [])
        if isinstance(stage, dict)
    ]


def _is_provider_failure(result: dict) -> bool:
    validation = result.get("validation")
    if not isinstance(validation, dict):
        validation = {}
    error_values = (
        validation.get("error_type"),
        validation.get("error_code"),
        validation.get("error"),
        validation.get("support_verifier_error"),
        result.get("citation_judge_error"),
    )
    for value in error_values:
        text = str(value or "")
        if value in _PROVIDER_ERROR_TYPES:
            return True
        if any(marker in text for marker in (
            "模型服务返回 401", "模型服务返回 403", "模型服务返回 429",
            "auth_failed", "rate_limited",
        )):
            return True
    # Older artifacts predate structured error metadata. A fallback carrying
    # validation.error can only come from the model call in answer.py, so it is
    # safe to retry without treating ordinary grounding fallbacks as failures.
    if result.get("status") == "fallback" and validation.get("error"):
        return True
    return False


def _statement_citation_coverage(answer: str) -> tuple[int, int]:
    supported = 0
    total = 0
    for statement in claim_statements(answer):
        if not is_substantive_statement(statement):
            continue
        total += 1
        supported += int(bool(_CITE.search(statement)))
    return supported, total


def _claim_evidence(answer: str, citations: list[dict]) -> list[dict]:
    by_tag = {
        str(citation.get("tag") or ""): "\n".join(filter(None, (
            f"文件名：{citation.get('file_name')}" if citation.get("file_name") else "",
            str(citation.get("snippet") or ""),
        )))
        for citation in citations
    }
    claims = []
    for statement in claim_statements(answer):
        if not is_substantive_statement(statement):
            continue
        plain = plain_claim_text(statement)
        tags = [f"C{item.group(1)}" for item in _CITE.finditer(statement)]
        evidence = [by_tag[tag] for tag in tags if by_tag.get(tag)]
        claims.append({"claim": plain, "evidence": evidence})
    return claims


def _parse_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("citation judge did not return an object")
    return value


def _judge_claim_support(
    claims: list[dict], *, retries: int = 0, retry_delay: float = 0.0,
) -> tuple[int, int, str]:
    """Use the configured real model as a strict entailment judge.

    Only counts are returned and persisted; claims and source snippets remain in
    memory. Uncited statements are deterministically unsupported and are not
    sent to the judge.
    """
    from app.qa import llm

    if not claims:
        return 0, 1, "no_substantive_claim"
    cited = [item for item in claims if item["evidence"]]
    uncited = len(claims) - len(cited)
    if not cited:
        return 0, len(claims), "all_claims_uncited"
    items = [
        {
            "id": index,
            "claim": item["claim"],
            "evidence": item["evidence"],
        }
        for index, item in enumerate(cited, start=1)
    ]
    prompt = (
        "逐项判断 claim 是否被对应 evidence 完整支持。只有证据直接蕴含全部"
        "事实、数字和限定条件时才为 true；部分支持、推断、常识补充或证据矛盾"
        "均为 false。只输出严格 JSON：{\"judgments\":[true,false,...]}，顺序和"
        "输入一致，不要解释。\n\n" + json.dumps(items, ensure_ascii=False)
    )
    for attempt in range(retries + 1):
        try:
            raw = llm.chat(
                [
                    {"role": "system", "content": "你是严格的引用蕴含评测器。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=2000,
                timeout=180,
            )
            payload = _parse_json_object(raw)
            judgments = payload.get("judgments")
            if (
                not isinstance(judgments, list)
                or len(judgments) != len(cited)
                or any(not isinstance(value, bool) for value in judgments)
            ):
                raise ValueError("citation judge returned invalid judgments")
            return sum(judgments), len(claims), "" if not uncited else "has_uncited_claims"
        except Exception as exc:
            retryable = isinstance(
                exc, (llm.LLMConnectionError, llm.LLMHTTPError, TimeoutError),
            )
            if retryable and attempt < retries:
                if retry_delay:
                    time.sleep(retry_delay)
                continue
            return 0, len(claims), type(exc).__name__


def _load_citation(conn, citation: dict) -> dict | None:
    chunk_id = citation.get("chunk_id")
    if not isinstance(chunk_id, int):
        return None
    row = conn.execute(
        """SELECT ch.text, ch.text_hash, ch.start_offset AS chunk_document_start,
                  ch.end_offset AS chunk_document_end, c.sha256
           FROM chunks ch JOIN contents c ON c.id = ch.content_id
           WHERE ch.id = ? AND ch.index_version = c.active_index_version""",
        (chunk_id,),
    ).fetchone()
    if row is None:
        return None
    start = citation.get("start_offset")
    end = citation.get("end_offset")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    exact = 0 <= start < end <= len(row["text"])
    exact = exact and citation.get("snippet") == row["text"][start:end]
    exact = exact and citation.get("span_id") == f"ch{chunk_id}:{start}-{end}"
    expected_document_start = (
        row["chunk_document_start"] + start
        if row["chunk_document_start"] is not None else None
    )
    expected_document_end = (
        row["chunk_document_start"] + end
        if row["chunk_document_start"] is not None else None
    )
    exact = exact and citation.get("document_start_offset") == expected_document_start
    exact = exact and citation.get("document_end_offset") == expected_document_end
    return {
        "tag": str(citation.get("tag") or ""),
        "content_sha256": row["sha256"],
        "chunk_text_hash": row["text_hash"],
        "chunk_start_offset": start,
        "chunk_end_offset": end,
        "document_start_offset": expected_document_start,
        "document_end_offset": expected_document_end,
        "exact": bool(exact),
    }


def _support_units(annotation, citations: list[dict]) -> tuple[int, int]:
    exact = [citation for citation in citations if citation["exact"]]
    if annotation.status == "metadata":
        hits = sum(
            any(citation["content_sha256"] in group.content_sha256 for citation in exact)
            for group in annotation.content_groups
        )
        return hits, len(annotation.content_groups)

    hits = 0
    for requirement in annotation.evidence_requirements:
        matched = any(
            any(
                packed_span_matches(
                    alternative,
                    content_sha256=citation["content_sha256"],
                    chunk_text_hash=citation["chunk_text_hash"],
                    chunk_start_offset=citation["chunk_start_offset"],
                    chunk_end_offset=citation["chunk_end_offset"],
                    document_start_offset=citation["document_start_offset"],
                    document_end_offset=citation["document_end_offset"],
                )
                for alternative in requirement.alternatives
            )
            for citation in exact
        )
        hits += int(matched)
    return hits, len(annotation.evidence_requirements)


def _summarize(results: list[dict], expected_qids: set[str]) -> dict:
    evaluable = [result for result in results if result["kind"] != "unanswerable"]
    unanswerable = [result for result in results if result["kind"] == "unanswerable"]
    claim_hit = sum(result.get("supported_claims", 0) for result in evaluable)
    claim_total = sum(result.get("claims", 0) for result in evaluable)
    gold_hit = sum(result.get("gold_support_hit", 0) for result in evaluable)
    gold_total = sum(result.get("gold_support_total", 0) for result in evaluable)
    exact_citations = sum(result.get("exact_citations", 0) for result in evaluable)
    citations = sum(result.get("citation_count", 0) for result in evaluable)
    statement_hit = sum(result.get("cited_statements", 0) for result in evaluable)
    statements = sum(result.get("statements", 0) for result in evaluable)
    refused = sum(result.get("correct_refusal", False) for result in unanswerable)
    answered = sum(result.get("answer_success", False) for result in evaluable)
    fallback_count = sum(result.get("status") == "fallback" for result in evaluable)
    provider_failures = sum(_is_provider_failure(result) for result in results)
    degraded_cases = sum(bool(result.get("degraded")) for result in results)
    completed_qids = {result["qid"] for result in results}
    return {
        "complete": completed_qids == expected_qids,
        "completed": len(completed_qids),
        "expected": len(expected_qids),
        "answer_success_rate": answered / len(evaluable) if evaluable else 0.0,
        "answer_successes": answered,
        "evaluable": len(evaluable),
        "fallback_count": fallback_count,
        "provider_failures": provider_failures,
        "degraded_cases": degraded_cases,
        "citation_support_rate": claim_hit / claim_total if claim_total else 0.0,
        "supported_claims": claim_hit,
        "claims": claim_total,
        "gold_evidence_citation_recall": gold_hit / gold_total if gold_total else 0.0,
        "gold_evidence_citation_hit": gold_hit,
        "gold_evidence_citation_total": gold_total,
        "exact_citation_rate": exact_citations / citations if citations else 0.0,
        "statement_citation_coverage": statement_hit / statements if statements else 0.0,
        "correct_refusal_rate": refused / len(unanswerable) if unanswerable else 0.0,
        "correct_refusals": refused,
        "unanswerable": len(unanswerable),
    }


def _passes_gates(
    summary: dict, *, has_evaluable: bool, has_unanswerable: bool,
) -> bool:
    return bool(
        summary["complete"]
        and summary.get("provider_failures", 0) == 0
        and summary.get("degraded_cases", 0) == 0
        and (
            not has_evaluable
            or summary.get("answer_success_rate", 1.0) >= ANSWER_SUCCESS_GATE
        )
        and (
            not has_evaluable
            or summary["citation_support_rate"] >= CITATION_SUPPORT_GATE
        )
        and (
            not has_unanswerable
            or summary["correct_refusal_rate"] >= CORRECT_REFUSAL_GATE
        )
        and (not has_evaluable or summary["exact_citation_rate"] == 1.0)
    )


def _write_output(
    path: Path, *, provider_name: str, model: str, gold_path: Path,
    results: list[dict], expected_qids: set[str], probe: dict,
    fingerprint_path: Path | None = None,
) -> dict:
    summary = _summarize(results, expected_qids)
    try:
        gold_reference = gold_path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        gold_reference = gold_path.as_posix()
    fingerprint = None
    if fingerprint_path:
        raw = fingerprint_path.read_bytes()
        fingerprint = {
            "path": fingerprint_path.as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "manifest": json.loads(raw),
        }
    payload = {
        "schema_version": 3,
        "provider": provider_name,
        "fingerprint": fingerprint,
        "model": model,
        "gold": gold_reference,
        "probe": {
            "available": bool(probe.get("available")),
            "code": probe.get("code"),
            "latency_ms": probe.get("latency_ms"),
        },
        "summary": summary,
        "results": results,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD_PATH)
    parser.add_argument(
        "--fingerprint", type=Path,
        help="release fingerprint JSON bound to the QA artifact",
    )
    parser.add_argument("--ollama-url", default="http://127.0.0.1:18434")
    parser.add_argument("--reranker", default="auto")
    parser.add_argument(
        "--provider",
        help="cc-switch provider name to use instead of the current provider",
    )
    parser.add_argument(
        "--model",
        help="override the selected provider model without changing cc-switch",
    )
    parser.add_argument(
        "--ccswitch-db", type=Path,
        help="read providers from a cc-switch database or backup without modifying it",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--retry-provider-failures", action="store_true",
        help="with --resume, rerun cases that failed because of provider errors",
    )
    parser.add_argument(
        "--case-delay", type=float, default=0.0,
        help="seconds to wait between QA cases to respect provider rate limits",
    )
    parser.add_argument(
        "--judge-retries", type=int, default=0,
        help="retry transient citation-judge provider failures within a case",
    )
    parser.add_argument(
        "--judge-retry-delay", type=float, default=0.0,
        help="seconds to wait before retrying a transient citation-judge failure",
    )
    parser.add_argument(
        "--max-consecutive-provider-failures", type=int, default=3,
        help="stop the batch after this many consecutive provider failures",
    )
    parser.add_argument("--qids", help="comma-separated diagnostic subset")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.retry_provider_failures and not args.resume:
        parser.error("--retry-provider-failures requires --resume")
    if args.case_delay < 0:
        parser.error("--case-delay must be non-negative")
    if args.judge_retries < 0:
        parser.error("--judge-retries must be non-negative")
    if args.judge_retry_delay < 0:
        parser.error("--judge-retry-delay must be non-negative")
    if args.max_consecutive_provider_failures < 1:
        parser.error("--max-consecutive-provider-failures must be at least 1")
    if args.ccswitch_db and not args.ccswitch_db.is_file():
        parser.error(f"cc-switch database does not exist: {args.ccswitch_db}")

    gold = load_gold(args.gold)
    selected = [
        case for case in ALL_CASES
        if gold[case.qid].status in {"answerable", "metadata", "unanswerable"}
    ]
    if args.qids:
        requested = {item.strip() for item in args.qids.split(",") if item.strip()}
        selected = [case for case in selected if case.qid in requested]
        missing = requested - {case.qid for case in selected}
        if missing:
            parser.error(f"unknown or non-runnable qids: {sorted(missing)}")
    if args.limit > 0:
        selected = selected[:args.limit]
    expected_qids = {case.qid for case in selected}

    results: list[dict] = []
    previous: dict = {}
    if args.resume and args.json.is_file():
        previous = json.loads(args.json.read_text(encoding="utf-8"))
        results = [
            result for result in previous.get("results", [])
            if result.get("qid") in expected_qids
        ]
        if args.retry_provider_failures:
            results = [
                result for result in results
                if not _is_provider_failure(result)
            ]
    completed = {result["qid"] for result in results}

    previous_reranker = os.environ.get("ORDO_RERANKER")
    os.environ["ORDO_RERANKER"] = args.reranker
    sidecar = Sidecar(ollama_url=args.ollama_url)
    conn = connect()
    try:
        integration = (
            read_providers(args.ccswitch_db)
            if args.ccswitch_db else
            sidecar.request("GET", "/integrations/ccswitch")
        )
        provider = choose_provider(
            integration.get("providers", []), args.provider,
        )
        if provider is None:
            detail = f" named {args.provider!r}" if args.provider else ""
            raise RuntimeError(
                f"no usable cc-switch provider{detail} with model and API key"
            )
        provider = dict(provider)
        if args.model:
            provider["model"] = args.model
        if previous and (
            previous.get("provider") != provider.get("name")
            or previous.get("model") != provider.get("model")
        ):
            raise RuntimeError(
                "resume artifact uses a different provider/model; choose a new output path"
            )
        sidecar.request("POST", "/settings/llm", {
            "endpoint": provider["endpoint"],
            "api_key": provider["api_key"],
            "model": provider["model"],
        })
        from app.qa import llm as judge_llm
        judge_llm.configure(
            provider["endpoint"], provider["api_key"], provider["model"],
        )
        probe = sidecar.request("POST", "/settings/llm/test", {}, timeout=240)
        emit("qa_eval_probe", {
            "provider": provider.get("name"),
            "model": provider.get("model"),
            "available": probe.get("available"),
            "code": probe.get("code"),
            "latency_ms": probe.get("latency_ms"),
        })
        if not probe.get("available"):
            raise RuntimeError(f"LLM probe failed: {probe.get('code')}")

        consecutive_provider_failures = 0
        for index, case in enumerate(selected, start=1):
            if case.qid in completed:
                continue
            annotation = gold[case.qid]
            question = (
                f"请仅根据我的文件库回答：{case.query}"
                if annotation.status == "unanswerable" else case.query
            )
            started = time.perf_counter()
            try:
                answer = sidecar.request(
                    "POST", "/ask",
                    {"question": question, "history": []},
                    timeout=420,
                )
                latency_ms = (time.perf_counter() - started) * 1000
                answer_text = str(answer.get("answer") or "")
                raw_citations = [
                    item for item in answer.get("citations", []) if isinstance(item, dict)
                ]
                citations = [
                    loaded for item in raw_citations
                    if (loaded := _load_citation(conn, item)) is not None
                ]
                tags_in_answer = {f"C{match.group(1)}" for match in _CITE.finditer(answer_text)}
                citation_tags = {citation["tag"] for citation in citations}
                gold_support_hit, gold_support_total = (
                    _support_units(annotation, citations)
                    if annotation.status in {"answerable", "metadata"} else (0, 0)
                )
                cited_statements, statements = _statement_citation_coverage(answer_text)
                if annotation.status in {"answerable", "metadata"}:
                    supported_claims, claims, judge_error = _judge_claim_support(
                        _claim_evidence(answer_text, raw_citations),
                        retries=args.judge_retries,
                        retry_delay=args.judge_retry_delay,
                    )
                else:
                    supported_claims, claims, judge_error = 0, 0, ""
                exact_citations = sum(citation["exact"] for citation in citations)
                answer_success = (
                    annotation.status in {"answerable", "metadata"}
                    and answer.get("status") == "answered"
                    and answer.get("mode") == "knowledge"
                    and bool(citations)
                    and tags_in_answer <= citation_tags
                )
                correct_refusal = (
                    annotation.status == "unanswerable"
                    and answer.get("status") == "refused"
                    and answer.get("mode") == "knowledge"
                )
                result = {
                    "qid": case.qid,
                    "kind": annotation.status,
                    "knowledge_scope_prompted": annotation.status == "unanswerable",
                    "status": answer.get("status"),
                    "mode": answer.get("mode"),
                    "answer_sha256": (
                        hashlib.sha256(answer_text.encode("utf-8")).hexdigest()
                        if answer_text else None
                    ),
                    "answer_chars": len(answer_text),
                    "answer_success": answer_success,
                    "correct_refusal": correct_refusal,
                    "citation_count": len(citations),
                    "exact_citations": exact_citations,
                    "citation_tags_resolved": tags_in_answer <= citation_tags,
                    "supported_claims": supported_claims,
                    "claims": claims,
                    "citation_judge_error": judge_error,
                    "gold_support_hit": gold_support_hit,
                    "gold_support_total": gold_support_total,
                    "cited_statements": cited_statements,
                    "statements": statements,
                    "validation": answer.get("validation", {}),
                    "degraded": answer.get("degraded", []),
                    "latency_ms": round(latency_ms, 1),
                    "timings": _safe_timings(answer),
                }
            except Exception as exc:  # keep the batch resumable after one bad response
                result = {
                    "qid": case.qid,
                    "kind": annotation.status,
                    "knowledge_scope_prompted": annotation.status == "unanswerable",
                    "status": "error",
                    "mode": "knowledge",
                    "answer_success": False,
                    "correct_refusal": False,
                    "citation_count": 0,
                    "exact_citations": 0,
                    "supported_claims": 0,
                    "claims": 1 if annotation.status in {"answerable", "metadata"} else 0,
                    "citation_judge_error": type(exc).__name__,
                    "gold_support_hit": 0,
                    "gold_support_total": (
                        len(annotation.evidence_requirements)
                        if annotation.status == "answerable" else
                        len(annotation.content_groups)
                        if annotation.status == "metadata" else 0
                    ),
                    "cited_statements": 0,
                    "statements": 0,
                    "validation": {"error": type(exc).__name__},
                    "degraded": [],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "timings": [],
                }
            results.append(result)
            results.sort(key=lambda item: next(
                i for i, candidate in enumerate(selected) if candidate.qid == item["qid"]
            ))
            summary = _write_output(
                args.json,
                provider_name=str(provider.get("name") or ""),
                model=str(provider.get("model") or ""),
                gold_path=args.gold,
                results=results,
                expected_qids=expected_qids,
                probe=probe,
                fingerprint_path=args.fingerprint,
            )
            emit("qa_eval_case", {
                "index": index,
                "total": len(selected),
                "qid": case.qid,
                "status": result["status"],
                "citation_support": [
                    result.get("supported_claims", 0), result.get("claims", 0),
                ],
                "gold_support": [
                    result.get("gold_support_hit", 0),
                    result.get("gold_support_total", 0),
                ],
                "correct_refusal": result["correct_refusal"],
                "latency_ms": result["latency_ms"],
                "running_citation_support": summary["citation_support_rate"],
                "running_refusal": summary["correct_refusal_rate"],
            })
            if _is_provider_failure(result):
                consecutive_provider_failures += 1
            else:
                consecutive_provider_failures = 0
            if (
                consecutive_provider_failures
                >= args.max_consecutive_provider_failures
            ):
                emit("qa_eval_aborted", {
                    "reason": "consecutive_provider_failures",
                    "count": consecutive_provider_failures,
                    "completed": summary["completed"],
                    "expected": summary["expected"],
                    "output": str(args.json),
                })
                return 2
            if args.case_delay:
                time.sleep(args.case_delay)

        summary = _write_output(
            args.json,
            provider_name=str(provider.get("name") or ""),
            model=str(provider.get("model") or ""),
            gold_path=args.gold,
            results=results,
            expected_qids=expected_qids,
            probe=probe,
        )
        has_evaluable = any(
            gold[case.qid].status in {"answerable", "metadata"}
            for case in selected
        )
        has_unanswerable = any(
            gold[case.qid].status == "unanswerable" for case in selected
        )
        passed = _passes_gates(
            summary,
            has_evaluable=has_evaluable,
            has_unanswerable=has_unanswerable,
        )
        emit("qa_eval_complete", {**summary, "passed": passed, "output": str(args.json)})
        return 0 if passed else 1
    finally:
        try:
            from app.qa import llm as judge_llm
            judge_llm.configure("", "", "")
        except Exception:
            pass
        conn.close()
        sidecar.close()
        if previous_reranker is None:
            os.environ.pop("ORDO_RERANKER", None)
        else:
            os.environ["ORDO_RERANKER"] = previous_reranker


if __name__ == "__main__":
    raise SystemExit(main())
