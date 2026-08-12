"""M4 压缩评测 —— 镜像 /ask 的真实上下文链路（PLAN §10.3）。

对 60 道有依据题走 检索 → 多样性 → 邻居扩展 → 压缩 → 装配，统计：

  · 关键词证据保留率：ContextPack 内是否存在含答案关键词的原文区间。
    这是以标注关键词为锚的代理指标，不冒充精确 gold span 的
    Evidence Recall（PLAN M4 的说明沿用此口径）。
  · 字符压缩率中位数：1 - packed_chars / source_chars（token 的代理）。
  · offset 往返：每个区间的文本必须与 chunks 原文切片逐字节一致，
    错一个即 M4 失败（K4：证据必须可回溯）。
  · 压缩阶段 P95 延迟。

用法：
    uv run python tests/run_compress_eval.py [--json 输出路径]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import connect, init_db  # noqa: E402
from app.qa.answer import MAX_PER_CONTENT, NEIGHBOR_SPAN, TOP_CONTEXT  # noqa: E402
from app.retrieval.pipeline import (  # noqa: E402
    assemble_context,
    compress_evidence,
    expand_neighbors,
    load_context_candidates,
    run as run_retrieval,
)
from tests.evalset import ANSWERABLE  # noqa: E402

RECALL_GATE = 0.95
COMPRESSION_GATE = 0.35


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=str, help="结果写入 JSON 文件")
    args = ap.parse_args()

    conn = connect()
    init_db(conn)

    results = []
    roundtrip_errors = 0
    compress_ms: list[float] = []

    for case in ANSWERABLE:
        retrieval = run_retrieval(
            conn, case.query, route_limit=60, candidate_limit=60,
        )
        candidates = load_context_candidates(
            conn, retrieval, limit=TOP_CONTEXT, max_per_content=MAX_PER_CONTENT,
        )
        candidates = expand_neighbors(
            conn, candidates, neighbor_span=NEIGHBOR_SPAN, trace=retrieval.trace,
        )
        source_chars = sum(len(source.text) for source in {
            (source.file_id, source.chunk_id): source
            for candidate in candidates
            for source in candidate.expanded_sources
        }.values())
        spans = compress_evidence(case.query, candidates, trace=retrieval.trace)
        pack = assemble_context(
            spans, trace=retrieval.trace, source_chars=source_chars,
        )

        stage_ms = {
            stage["name"]: stage["duration_ms"]
            for stage in retrieval.trace.stages
        }
        compress_ms.append(stage_ms.get("compress", 0.0))

        # offset 往返：区间文本必须与 chunk 原文逐字节一致
        case_roundtrip_ok = True
        for span in pack.spans:
            row = conn.execute(
                "SELECT text FROM chunks WHERE id = ?", (span.chunk_id,),
            ).fetchone()
            if row is None or row["text"][span.start_offset:span.end_offset] != span.text:
                case_roundtrip_ok = False
                roundtrip_errors += 1

        keywords = case.answer_keywords or []
        blob = " ".join(span.text for span in pack.spans)
        kw_hit = (not keywords) or any(kw in blob for kw in keywords)
        gold_doc_present = (not case.doc_hints) or any(
            any(hint in span.file_name for hint in case.doc_hints)
            for span in pack.spans
        )
        compression = 1 - (pack.packed_chars / pack.source_chars) if pack.source_chars else 0.0

        results.append({
            "qid": case.qid, "query": case.query,
            "kw_hit": kw_hit, "gold_doc_in_pack": gold_doc_present,
            "spans": len(pack.spans), "packed_chars": pack.packed_chars,
            "source_chars": pack.source_chars,
            "compression": round(compression, 4),
            "roundtrip_ok": case_roundtrip_ok,
        })

    recall = sum(r["kw_hit"] for r in results) / len(results)
    median_compression = statistics.median(r["compression"] for r in results)
    p95 = sorted(compress_ms)[int(len(compress_ms) * 0.95) - 1]
    misses = [r["qid"] for r in results if not r["kw_hit"]]
    upstream = [r["qid"] for r in results if not r["kw_hit"] and not r["gold_doc_in_pack"]]

    print(f"关键词证据保留率     {recall:>7.1%}   （门槛 {RECALL_GATE:.0%}）"
          f"{'  ✓' if recall >= RECALL_GATE else '  ✗'}")
    print(f"字符压缩率中位数     {median_compression:>7.1%}   （门槛 ≥{COMPRESSION_GATE:.0%}）"
          f"{'  ✓' if median_compression >= COMPRESSION_GATE else '  ✗'}")
    print(f"offset 往返错误      {roundtrip_errors:>5} 个   （门槛 0）"
          f"{'  ✓' if roundtrip_errors == 0 else '  ✗'}")
    print(f"压缩阶段 P95         {p95:>6.1f}ms  （预算 500ms）"
          f"{'  ✓' if p95 <= 500 else '  ✗'}")
    if misses:
        print(f"\n漏例：{misses}")
        print(f"  其中金文档整体未进上下文（上游召回问题）：{upstream}")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "summary": {
                "keyword_evidence_recall": recall,
                "median_compression": median_compression,
                "roundtrip_errors": roundtrip_errors,
                "compress_p95_ms": p95,
                "misses": misses,
                "upstream_misses": upstream,
            },
            "results": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.json}")

    ok = (recall >= RECALL_GATE and median_compression >= COMPRESSION_GATE
          and roundtrip_errors == 0 and p95 <= 500)
    print("✓ 达标" if ok else "✗ 未达标")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
