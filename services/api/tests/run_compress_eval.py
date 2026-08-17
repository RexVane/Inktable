"""Formal exact-span evidence-compression evaluation.

The harness mirrors the /ask context path and scores overlap with reviewed
Document-offset gold spans. Keyword presence is no longer used as a proxy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import connect, init_db
from app.qa.answer import (
    CONTEXT_CHAR_BUDGET,
    MAX_PER_CONTENT,
    NEIGHBOR_SPAN,
    RETRIEVAL_LIMIT,
    TOP_CONTEXT,
)
from app.retrieval.pipeline import (
    assemble_context,
    compress_evidence,
    expand_neighbors,
    load_context_candidates,
)
from app.retrieval.pipeline import run as run_retrieval
from tests.eval_gold import DEFAULT_GOLD_PATH, load_gold, packed_span_matches
from tests.evalset import ALL_CASES


RECALL_GATE = 0.95
COMPRESSION_GATE = 0.35
COMPRESS_P95_BUDGET_MS = 500.0
REPO_ROOT = Path(__file__).resolve().parents[3]


def _portable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, help="write result JSON")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD_PATH)
    parser.add_argument(
        "--fingerprint", type=Path,
        help="release fingerprint JSON bound to this compression artifact",
    )
    parser.add_argument("--qids", help="comma-separated diagnostic subset")
    args = parser.parse_args()

    gold = load_gold(args.gold)
    cases = [case for case in ALL_CASES if gold[case.qid].status == "answerable"]
    if args.qids:
        requested = {item.strip() for item in args.qids.split(",") if item.strip()}
        cases = [case for case in cases if case.qid in requested]
        missing = requested - {case.qid for case in cases}
        if missing:
            parser.error(f"unknown or non-evidence qids: {sorted(missing)}")
    if not cases:
        raise RuntimeError("gold contract has no evidence-bearing answerable cases")

    conn = connect()
    init_db(conn)
    content_hashes = {
        row["id"]: row["sha256"]
        for row in conn.execute("SELECT id, sha256 FROM contents")
    }
    chunk_hashes = {
        row["id"]: row["text_hash"]
        for row in conn.execute("SELECT id, text_hash FROM chunks")
    }

    results = []
    roundtrip_errors = 0
    compress_ms: list[float] = []
    total_requirements = 0
    hit_requirements = 0

    for case in cases:
        annotation = gold[case.qid]
        retrieval = run_retrieval(
            conn, case.query, route_limit=RETRIEVAL_LIMIT,
            candidate_limit=RETRIEVAL_LIMIT,
        )
        candidates = load_context_candidates(
            conn, retrieval, limit=TOP_CONTEXT, max_per_content=MAX_PER_CONTENT,
        )
        candidates = expand_neighbors(
            conn, candidates, neighbor_span=NEIGHBOR_SPAN, trace=retrieval.trace,
        )
        unique_sources = {
            (source.file_id, source.chunk_id): source
            for candidate in candidates
            for source in candidate.expanded_sources
        }
        source_chars = sum(len(source.text) for source in unique_sources.values())
        source_requirement_hits = []
        for requirement_index, requirement in enumerate(annotation.evidence_requirements):
            source_requirement_hits.append(any(
                any(
                    packed_span_matches(
                        alternative,
                        content_sha256=content_hashes.get(source.content_id, ""),
                        chunk_text_hash=chunk_hashes.get(source.chunk_id, ""),
                        chunk_start_offset=0,
                        chunk_end_offset=len(source.text),
                        document_start_offset=source.document_start_offset,
                        document_end_offset=source.document_end_offset,
                    )
                    for alternative in requirement.alternatives
                )
                for source in unique_sources.values()
            ))
        spans = compress_evidence(case.query, candidates, trace=retrieval.trace)
        pack = assemble_context(
            spans, trace=retrieval.trace, source_chars=source_chars,
            char_budget=CONTEXT_CHAR_BUDGET,
        )

        stage_ms = {
            stage["name"]: stage["duration_ms"]
            for stage in retrieval.trace.stages
        }
        compress_ms.append(float(stage_ms.get("compress", 0.0)))

        case_roundtrip_ok = True
        for span in pack.spans:
            row = conn.execute(
                "SELECT text FROM chunks WHERE id = ?", (span.chunk_id,),
            ).fetchone()
            if row is None or row["text"][span.start_offset:span.end_offset] != span.text:
                case_roundtrip_ok = False
                roundtrip_errors += 1

        requirement_results = []
        for requirement_index, requirement in enumerate(annotation.evidence_requirements):
            source_matches = []
            for source in unique_sources.values():
                if any(
                    packed_span_matches(
                        alternative,
                        content_sha256=content_hashes.get(source.content_id, ""),
                        chunk_text_hash=chunk_hashes.get(source.chunk_id, ""),
                        chunk_start_offset=0,
                        chunk_end_offset=len(source.text),
                        document_start_offset=source.document_start_offset,
                        document_end_offset=source.document_end_offset,
                    )
                    for alternative in requirement.alternatives
                ):
                    source_matches.append({
                        "chunk_id": source.chunk_id,
                        "ordinal_distance": source.ordinal_distance,
                        "candidate_score": round(source.candidate_score, 6),
                    })
            extracted_match_ranks = []
            for rank, extracted in enumerate(spans, start=1):
                if any(
                    packed_span_matches(
                        alternative,
                        content_sha256=content_hashes.get(extracted.content_id, ""),
                        chunk_text_hash=chunk_hashes.get(extracted.chunk_id, ""),
                        chunk_start_offset=extracted.start_offset,
                        chunk_end_offset=extracted.end_offset,
                        document_start_offset=extracted.document_start_offset,
                        document_end_offset=extracted.document_end_offset,
                    )
                    for alternative in requirement.alternatives
                ):
                    extracted_match_ranks.append(rank)
            matched = False
            for span in pack.spans:
                content_sha256 = content_hashes.get(span.content_id, "")
                chunk_text_hash = chunk_hashes.get(span.chunk_id, "")
                if any(
                    packed_span_matches(
                        alternative,
                        content_sha256=content_sha256,
                        chunk_text_hash=chunk_text_hash,
                        chunk_start_offset=span.start_offset,
                        chunk_end_offset=span.end_offset,
                        document_start_offset=span.document_start_offset,
                        document_end_offset=span.document_end_offset,
                    )
                    for alternative in requirement.alternatives
                ):
                    matched = True
                    break
            requirement_results.append({
                "requirement_id": requirement.requirement_id,
                "source_hit": source_requirement_hits[requirement_index],
                "source_matches": source_matches,
                "extracted_match_ranks": extracted_match_ranks[:10],
                "hit": matched,
            })

        total_requirements += len(requirement_results)
        case_hits = sum(item["hit"] for item in requirement_results)
        hit_requirements += case_hits
        present_hashes = {
            content_hashes.get(span.content_id, "") for span in pack.spans
        }
        gold_groups_present = all(
            bool(group.content_sha256 & present_hashes)
            for group in annotation.content_groups
        )
        compression = pack.compression_ratio
        results.append({
            "qid": case.qid,
            "query": case.query,
            "evidence_requirements_hit": case_hits,
            "evidence_requirements_total": len(requirement_results),
            "evidence_recall": case_hits / len(requirement_results),
            "all_evidence_retained": case_hits == len(requirement_results),
            "source_evidence_recall": (
                sum(source_requirement_hits) / len(source_requirement_hits)
            ),
            "all_evidence_retrieved": all(source_requirement_hits),
            "gold_sources_in_pack": gold_groups_present,
            "spans": len(pack.spans),
            "packed_chars": pack.packed_chars,
            "source_chars": pack.source_chars,
            "compression": round(compression, 4),
            "roundtrip_ok": case_roundtrip_ok,
            "requirements": requirement_results,
        })

    conn.close()
    recall = hit_requirements / total_requirements
    complete_case_rate = statistics.fmean(
        result["all_evidence_retained"] for result in results
    )
    median_compression = statistics.median(result["compression"] for result in results)
    p95 = _percentile(compress_ms, 0.95)
    misses = [
        result["qid"] for result in results if not result["all_evidence_retained"]
    ]
    upstream = [
        result["qid"] for result in results
        if not result["all_evidence_retained"] and not result["all_evidence_retrieved"]
    ]
    source_evidence_recall = statistics.fmean(
        result["source_evidence_recall"] for result in results
    )

    print(
        f"Gold Evidence Recall    {recall:>7.1%}   (gate {RECALL_GATE:.0%}) "
        f"{'PASS' if recall >= RECALL_GATE else 'FAIL'}"
    )
    print(f"Complete-case recall    {complete_case_rate:>7.1%}")
    print(f"Pre-compression evidence {source_evidence_recall:>6.1%}")
    print(
        f"Median compression      {median_compression:>7.1%}   "
        f"(gate >={COMPRESSION_GATE:.0%}) "
        f"{'PASS' if median_compression >= COMPRESSION_GATE else 'FAIL'}"
    )
    print(
        f"Offset roundtrip errors {roundtrip_errors:>7}   (gate 0) "
        f"{'PASS' if roundtrip_errors == 0 else 'FAIL'}"
    )
    print(
        f"Compression P95         {p95:>7.1f}ms (budget "
        f"{COMPRESS_P95_BUDGET_MS:.0f}ms) "
        f"{'PASS' if p95 <= COMPRESS_P95_BUDGET_MS else 'FAIL'}"
    )
    if misses:
        print(f"\nEvidence misses: {misses}")
        print(f"Upstream source misses: {upstream}")

    fingerprint = None
    if args.fingerprint:
        raw = args.fingerprint.read_bytes()
        fingerprint = {
            "path": _portable_path(args.fingerprint),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "manifest": json.loads(raw),
        }
    output = {
        "schema_version": 3,
        "gold": _portable_path(args.gold),
        "fingerprint": fingerprint,
        "summary": {
            "evaluated_cases": len(results),
            "evidence_requirements": total_requirements,
            "gold_evidence_recall": recall,
            "complete_case_recall": complete_case_rate,
            "pre_compression_evidence_recall": source_evidence_recall,
            "median_compression": median_compression,
            "roundtrip_errors": roundtrip_errors,
            "compress_p95_ms": p95,
            "misses": misses,
            "upstream_misses": upstream,
        },
        "results": results,
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\n结果已写入 {args.json}")

    passed = (
        recall >= RECALL_GATE
        and median_compression >= COMPRESSION_GATE
        and roundtrip_errors == 0
        and p95 <= COMPRESS_P95_BUDGET_MS
    )
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
