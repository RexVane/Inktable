"""Run the frozen, content-addressed retrieval evaluation.

Document metrics use content SHA-256 groups rather than filenames. Exact
evidence metrics use the same reviewed source spans as compression evaluation.
Cases whose required source is absent from the current corpus are reported but
excluded from ranking gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import connect, init_db
from app.retrieval.pipeline import run as run_retrieval
from tests.eval_gold import (
    DEFAULT_GOLD_PATH,
    GoldCase,
    load_gold,
    matching_group,
    packed_span_matches,
)
from tests.evalset import ALL_CASES, summary


TOP_K = 5
DEEP_K = 50
RERANK_K = 20
ABSTAIN_THRESHOLD = 0.0
REPO_ROOT = Path(__file__).resolve().parents[3]


def _portable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def retrieve(
    conn, query: str, limit: int = 200,
) -> tuple[list[dict], list[dict], float, dict]:
    """Retrieve ranked documents plus the exact top-50 Child evidence pool."""
    retrieval = run_retrieval(
        conn, query, route_limit=limit, candidate_limit=limit,
    )
    fused = {
        candidate.chunk_id: candidate.final_score
        for candidate in retrieval.candidates
    }
    trace = retrieval.trace.to_dict()
    if not fused:
        return [], [], 0.0, trace

    ranked_ids = [
        chunk_id for chunk_id, _score in
        sorted(fused.items(), key=lambda item: item[1], reverse=True)[:limit]
    ]
    marks = ",".join("?" * len(ranked_ids))
    rows = conn.execute(
        f"""SELECT ch.id, ch.content_id, ch.text, ch.text_hash,
                   ch.start_offset, ch.end_offset, c.sha256
            FROM chunks ch JOIN contents c ON c.id = ch.content_id
            WHERE ch.id IN ({marks})
              AND ch.index_version = c.active_index_version""",
        ranked_ids,
    ).fetchall()
    row_by_id = {row["id"]: row for row in rows}
    deep_evidence = []
    for chunk_id in trace.get("deep_candidates", [])[:DEEP_K]:
        row = row_by_id.get(chunk_id)
        if row is None:
            continue
        deep_evidence.append({
            "sha256": row["sha256"],
            "chunks": [{
                "chunk_id": row["id"],
                "text_hash": row["text_hash"],
                "text_length": len(row["text"]),
                "document_start_offset": row["start_offset"],
                "document_end_offset": row["end_offset"],
            }],
        })

    content_ids = sorted({row["content_id"] for row in rows})
    name_map: dict[int, list[str]] = {content_id: [] for content_id in content_ids}
    if content_ids:
        content_marks = ",".join("?" * len(content_ids))
        for row in conn.execute(
            f"SELECT content_id, name FROM files WHERE content_id IN ({content_marks})",
            content_ids,
        ):
            name_map[row["content_id"]].append(row["name"])

    by_content: dict[str, dict] = {}
    for row in rows:
        score = fused.get(row["id"], 0.0)
        entry = by_content.setdefault(row["sha256"], {
            "content_id": row["content_id"],
            "sha256": row["sha256"],
            "names": sorted(set(name_map.get(row["content_id"], []))),
            "score": 0.0,
            "ranked_chunks": [],
        })
        entry["score"] = max(entry["score"], score)
        entry["ranked_chunks"].append((score, {
            "chunk_id": row["id"],
            "text_hash": row["text_hash"],
            "text_length": len(row["text"]),
            "document_start_offset": row["start_offset"],
            "document_end_offset": row["end_offset"],
        }))

    documents = sorted(by_content.values(), key=lambda item: item["score"], reverse=True)
    for document in documents:
        document["chunks"] = [
            chunk for _score, chunk in sorted(
                document.pop("ranked_chunks"),
                key=lambda item: item[0],
                reverse=True,
            )[:3]
        ]
    return documents, deep_evidence, max(fused.values()), trace


def _groups_hit(documents: list[dict], gold: GoldCase) -> bool:
    return all(
        any(document["sha256"] in group.content_sha256 for document in documents)
        for group in gold.content_groups
    )


def _evidence_requirement_hit(requirement, documents: list[dict]) -> bool:
    for document in documents:
        for chunk in document["chunks"]:
            if any(
                packed_span_matches(
                    alternative,
                    content_sha256=document["sha256"],
                    chunk_text_hash=chunk["text_hash"],
                    chunk_start_offset=0,
                    chunk_end_offset=chunk["text_length"],
                    document_start_offset=chunk["document_start_offset"],
                    document_end_offset=chunk["document_end_offset"],
                )
                for alternative in requirement.alternatives
            ):
                return True
    return False


def judge(
    case, gold: GoldCase, documents: list[dict],
    deep_evidence: list[dict], top_score: float,
) -> dict:
    abstained = top_score < ABSTAIN_THRESHOLD or not documents
    if gold.status == "corpus_missing":
        return {
            "qid": case.qid,
            "kind": "corpus_missing",
            "excluded": True,
            "reason": gold.reason,
            "pass": None,
        }
    if gold.status == "unanswerable":
        return {
            "qid": case.qid,
            "kind": "unanswerable",
            "excluded": ABSTAIN_THRESHOLD == 0.0,
            "pass": abstained if ABSTAIN_THRESHOLD > 0.0 else None,
            "abstained": abstained,
            "top_score": top_score,
            "top": documents[0]["names"][0] if documents and documents[0]["names"] else None,
        }

    topk = documents[:TOP_K]
    deep = documents[:DEEP_K]
    rerank_top = documents[:RERANK_K]
    doc_hit = _groups_hit(topk, gold)
    doc_hit_20 = _groups_hit(rerank_top, gold)
    doc_hit_50 = _groups_hit(deep, gold)

    group_ranks = [
        next(
            (
                index for index, document in enumerate(documents, start=1)
                if document["sha256"] in group.content_sha256
            ),
            None,
        )
        for group in gold.content_groups
    ]
    rank = max(group_ranks) if all(value is not None for value in group_ranks) else None
    rank_50 = rank if rank is not None and rank <= DEEP_K else None

    used_groups: set[str] = set()
    gains: list[int] = []
    reciprocal_rank = 0.0
    for index, document in enumerate(documents[:10], start=1):
        group = matching_group(document["sha256"], gold.content_groups, used_groups)
        gain = int(group is not None)
        gains.append(gain)
        if group is not None:
            used_groups.add(group.group_id)
            if reciprocal_rank == 0.0:
                reciprocal_rank = 1.0 / index
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal = min(len(gold.content_groups), 10)
    idcg = sum(1.0 / math.log2(index + 2) for index in range(ideal))
    ndcg_10 = dcg / idcg if idcg else 0.0

    requirement_hits = [
        _evidence_requirement_hit(requirement, topk)
        for requirement in gold.evidence_requirements
    ]
    deep_requirement_hits = [
        _evidence_requirement_hit(requirement, deep_evidence)
        for requirement in gold.evidence_requirements
    ]
    evidence_hit = all(requirement_hits) if requirement_hits else True
    evidence_recall = (
        sum(requirement_hits) / len(requirement_hits) if requirement_hits else 1.0
    )
    deep_evidence_recall = (
        sum(deep_requirement_hits) / len(deep_requirement_hits)
        if deep_requirement_hits else 1.0
    )
    top_name = None
    if topk and topk[0]["names"]:
        top_name = topk[0]["names"][0]
    return {
        "qid": case.qid,
        "kind": gold.status,
        "difficulty": case.difficulty,
        "pass": doc_hit and evidence_hit and not abstained,
        "doc_hit": doc_hit,
        "doc_hit_20": doc_hit_20,
        "doc_hit_50": doc_hit_50,
        "evidence_hit": evidence_hit,
        "evidence_recall": evidence_recall,
        "evidence_hits_50": sum(deep_requirement_hits),
        "evidence_recall_50": deep_evidence_recall,
        "evidence_requirements": len(requirement_hits),
        "rank": rank,
        "rank_50": rank_50,
        "mrr_10": reciprocal_rank,
        "ndcg_10": ndcg_10,
        "abstained": abstained,
        "top_score": top_score,
        "top": top_name,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--json", type=Path, help="write result JSON")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD_PATH)
    parser.add_argument("--label", default="content-sha-baseline")
    parser.add_argument(
        "--route-limit", type=int, default=200,
        help="retrieval depth; production search/QA uses 120",
    )
    parser.add_argument(
        "--enforce-latency", action="store_true",
        help="fail when search P95 >2.5s or rerank P95 >1.5s",
    )
    parser.add_argument(
        "--fingerprint", type=Path,
        help="release fingerprint JSON bound to this evaluation artifact",
    )
    parser.add_argument("--qids", help="comma-separated diagnostic subset")
    args = parser.parse_args()

    gold = load_gold(args.gold)
    expected_ids = {case.qid for case in ALL_CASES}
    if set(gold) != expected_ids:
        missing = sorted(expected_ids - set(gold))
        extra = sorted(set(gold) - expected_ids)
        raise RuntimeError(f"gold/evalset qid mismatch: missing={missing}, extra={extra}")

    selected_cases = list(ALL_CASES)
    if args.qids:
        requested = {item.strip() for item in args.qids.split(",") if item.strip()}
        selected_cases = [case for case in ALL_CASES if case.qid in requested]
        missing = requested - {case.qid for case in selected_cases}
        if missing:
            parser.error(f"unknown qids: {sorted(missing)}")
        if not selected_cases:
            parser.error("--qids selected no cases")

    conn = connect()
    init_db(conn)
    n_chunks = conn.execute("SELECT count(*) c FROM chunks").fetchone()["c"]
    if n_chunks == 0:
        print("库里没有分片，先启用来源并跑 /index/run")
        return 1

    info = summary()
    statuses: dict[str, int] = {}
    for case in selected_cases:
        item = gold[case.qid]
        statuses[item.status] = statuses.get(item.status, 0) + 1
    print(
        f"评测集 {len(selected_cases)}/{info['total']} 题 · 状态 {statuses} · "
        f"库中 {n_chunks} 个分片"
    )
    print(f"gold={args.gold}\n")

    results = []
    total_started = time.perf_counter()
    for case in selected_cases:
        annotation = gold[case.qid]
        if annotation.status == "corpus_missing":
            result = judge(case, annotation, [], [], 0.0)
            result["latency_ms"] = 0.0
            result["query"] = case.query
            results.append(result)
            if args.verbose:
                print(f"  - [{case.qid}] corpus_missing: {annotation.reason}")
            continue

        started = time.perf_counter()
        documents, deep_evidence, top_score, trace = retrieve(
            conn, case.query, limit=max(50, args.route_limit),
        )
        latency = (time.perf_counter() - started) * 1000
        result = judge(case, annotation, documents, deep_evidence, top_score)
        result["latency_ms"] = latency
        result["query"] = case.query
        rerank_stage = next(
            (stage for stage in trace.get("stages", []) if stage.get("name") == "rerank"),
            {},
        )
        result["rerank_ms"] = float(rerank_stage.get("duration_ms") or 0.0)
        result["rerank_model"] = rerank_stage.get("model_id")
        result["degraded"] = list(trace.get("degraded", []))
        results.append(result)

        if args.verbose or result.get("pass") is False:
            if result["kind"] in {"answerable", "metadata"}:
                print(
                    f"  {'OK' if result['pass'] else 'MISS':<4} [{result['qid']}] "
                    f"rank={result['rank']} doc={result['doc_hit']} "
                    f"evidence={result['evidence_recall']:.0%} "
                    f"latency={latency:.0f}ms"
                )
            elif args.verbose:
                print(
                    f"  - [{result['qid']}] unanswerable "
                    f"score={top_score:.4f} latency={latency:.0f}ms"
                )

    total_ms = (time.perf_counter() - total_started) * 1000
    conn.close()

    evaluable = [
        result for result in results
        if result["kind"] in {"answerable", "metadata"}
    ]
    evidence_cases = [result for result in results if result["kind"] == "answerable"]
    unanswerable = [result for result in results if result["kind"] == "unanswerable"]
    corpus_missing = [result for result in results if result["kind"] == "corpus_missing"]
    runtime_results = [result for result in results if result["kind"] != "corpus_missing"]

    recall = statistics.fmean(result["doc_hit"] for result in evaluable)
    recall_20 = statistics.fmean(result["doc_hit_20"] for result in evaluable)
    recall_50 = statistics.fmean(result["doc_hit_50"] for result in evaluable)
    mrr_10 = statistics.fmean(result["mrr_10"] for result in evaluable)
    ndcg_10 = statistics.fmean(result["ndcg_10"] for result in evaluable)
    strict = statistics.fmean(result["pass"] for result in evaluable)
    evidence_recall = (
        statistics.fmean(result["evidence_recall"] for result in evidence_cases)
        if evidence_cases else 1.0
    )
    evidence_requirements = sum(
        result["evidence_requirements"] for result in evidence_cases
    )
    evidence_hits_50 = sum(result["evidence_hits_50"] for result in evidence_cases)
    gold_evidence_recall_50 = (
        evidence_hits_50 / evidence_requirements if evidence_requirements else 1.0
    )
    latency_values = [result["latency_ms"] for result in runtime_results]
    rerank_values = [result["rerank_ms"] for result in runtime_results]
    p50 = _percentile(latency_values, 0.50)
    p95 = _percentile(latency_values, 0.95)
    rerank_p50 = _percentile(rerank_values, 0.50)
    rerank_p95 = _percentile(rerank_values, 0.95)
    degraded_count = sum(bool(result.get("degraded")) for result in runtime_results)
    rerank_models = sorted({
        result["rerank_model"] for result in runtime_results
        if result.get("rerank_model")
    })

    rows = [
        ("Content Recall@5", recall, 0.80),
        ("Content Recall@50", recall_50, None),
        ("Gold Evidence Recall@50", gold_evidence_recall_50, 0.90),
        ("Content Recall@20", recall_20, None),
        ("MRR@10", mrr_10, None),
        ("nDCG@10", ndcg_10, None),
        ("严格通过率", strict, None),
        ("Top-5 evidence requirement recall", evidence_recall, None),
    ]
    print(f"\n{'指标':<38}{'实测':>9}  {'验收线':>8}  结果")
    print("-" * 70)
    all_pass = True
    for name, value, gate in rows:
        if gate is None:
            print(f"{name:<38}{value:>8.1%}  {'-':>8}")
            continue
        passed = value >= gate
        all_pass &= passed
        print(
            f"{name:<38}{value:>8.1%}  {gate:>7.0%}   "
            f"{'PASS' if passed else 'FAIL'}"
        )
    latency_pass = p95 <= 2500.0 and rerank_p95 <= 1500.0
    if args.enforce_latency:
        all_pass &= latency_pass
    print(
        f"\n查询延迟 p50={p50:.1f}ms p95={p95:.1f}ms · "
        f"rerank p50={rerank_p50:.1f}ms p95={rerank_p95:.1f}ms · "
        f"{'PASS' if latency_pass else 'FAIL'}"
    )
    print(
        f"模型={rerank_models} · degraded={degraded_count}/{len(runtime_results)} · "
        f"全量={total_ms:.0f}ms"
    )
    print(
        f"语料缺失排除={len(corpus_missing)} · 无依据题={len(unanswerable)} "
        f"（拒答由真实 QA 评测，不由融合分伪判）"
    )

    by_difficulty: dict[str, list[bool]] = {}
    for result in evaluable:
        by_difficulty.setdefault(result["difficulty"], []).append(result["doc_hit"])
    print("\n按难度分解 Content Recall@5:")
    for difficulty, values in sorted(by_difficulty.items()):
        print(
            f"  {difficulty:<12} {sum(values)}/{len(values)} = "
            f"{sum(values) / len(values):.0%}"
        )

    fingerprint = None
    if args.fingerprint:
        raw_fingerprint = args.fingerprint.read_bytes()
        fingerprint = {
            "path": _portable_path(args.fingerprint),
            "sha256": hashlib.sha256(raw_fingerprint).hexdigest(),
            "manifest": json.loads(raw_fingerprint),
        }

    output = {
        "schema_version": 3,
        "label": args.label,
        "gold": _portable_path(args.gold),
        "fingerprint": fingerprint,
        "summary": {
            "evaluated": len(evaluable),
            "corpus_missing": len(corpus_missing),
            "unanswerable": len(unanswerable),
            "recall_at_5": recall,
            "recall_at_20": recall_20,
            "recall_at_50": recall_50,
            "mrr_at_10": mrr_10,
            "ndcg_at_10": ndcg_10,
            "strict": strict,
            "top5_evidence_requirement_recall": evidence_recall,
            "gold_evidence_hits_at_50": evidence_hits_50,
            "gold_evidence_requirements": evidence_requirements,
            "gold_evidence_recall_at_50": gold_evidence_recall_50,
            "p50_ms": p50,
            "p95_ms": p95,
            "rerank_p50_ms": rerank_p50,
            "rerank_p95_ms": rerank_p95,
            "rerank_models": rerank_models,
            "degraded_queries": degraded_count,
            "route_limit": args.route_limit,
            "latency_enforced": args.enforce_latency,
            "latency_pass": latency_pass,
            "chunks": n_chunks,
            "by_difficulty": {
                difficulty: sum(values) / len(values)
                for difficulty, values in by_difficulty.items()
            },
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

    print(f"\n{'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
