"""跑检索评测 —— PLAN §18.2。

用法：
    uv run python tests/run_eval.py            # 跑全部
    uv run python tests/run_eval.py --verbose  # 逐题细节

指标（方案定的验收线）：
    Recall@5      ≥ 0.80   有依据题的答案是否出现在前 5 个文件里
    正确拒答率     ≥ 0.80   无依据题是否被判为"无依据"
    误拒率        ≤ 0.05   有依据题被误判为无依据的比例

**这个脚本的基线值必须在向量检索实现之前记录下来** —— 否则无法判断
向量检索到底带来了增益还是只是增加了复杂度（方案 §12.9 的前置条件）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import connect, init_db  # noqa: E402
from app.retrieval.pipeline import run as run_retrieval  # noqa: E402
from tests.evalset import ALL_CASES, ANSWERABLE, UNANSWERABLE, summary  # noqa: E402

TOP_K = 5
DEEP_K = 50
RERANK_K = 20

# 拒答门限（§12.3c）。
#
# **纯词法阶段无法标定这个值** —— 实测有依据组与无依据组的词法信号
# 大量重叠：词覆盖率在有依据组最低 29%、无依据组最高 100%，BM25 亦然。
# 典型反例：「杭州地铁到浙江音乐学院怎么换乘」，"杭州""浙江音乐学院"
# 都在参赛手册里，但手册根本没讲地铁 —— 词法只能判断"词出现了没有"，
# 判断不了"这些词是否在回答这个问题"。
#
# 这正是向量检索的必要性所在：语义相似度能区分"提到了"和"回答了"。
# 所以基线阶段设为 0（永不拒答，宁可多召回也不误拒），
# 待向量路上线后按 §12.3c 用余弦绝对分标定。
ABSTAIN_THRESHOLD = 0.0


def retrieve(conn, query: str, limit: int = 40) -> tuple[list[dict], float]:
    """检索并按文件聚合，返回 (文件列表, 最高融合分)。"""
    retrieval = run_retrieval(
        conn, query, route_limit=limit, candidate_limit=limit,
    )
    fused = {candidate.chunk_id: candidate.final_score
             for candidate in retrieval.candidates}
    if not fused:
        return [], 0.0

    top = sorted(fused.items(), key=lambda kv: -kv[1])[:limit]
    ids = [cid for cid, _ in top]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT ch.id, ch.text, ch.section_path, ch.page, f.name
            FROM chunks ch JOIN files f ON f.content_id = ch.content_id
            WHERE ch.id IN ({marks})""",
        ids,
    ).fetchall()

    by_file: dict[str, dict] = {}
    for r in rows:
        sc = fused.get(r["id"], 0.0)
        e = by_file.setdefault(
            r["name"], {"name": r["name"], "score": 0.0, "ranked_texts": []},
        )
        e["score"] = max(e["score"], sc)
        e["ranked_texts"].append((sc, r["text"]))

    files = sorted(by_file.values(), key=lambda e: -e["score"])
    for entry in files:
        entry["texts"] = [
            text for _score, text in sorted(
                entry.pop("ranked_texts"), key=lambda item: item[0], reverse=True,
            )[:3]
        ]
    return files, max(fused.values())


def judge(case, files: list[dict], top_score: float) -> dict:
    """判定单题。

    有依据题：期望文档出现在 top-K，且命中的文本包含答案关键词。
    无依据题：期望最高分低于拒答门限。
    """
    abstained = top_score < ABSTAIN_THRESHOLD or not files

    if not case.answerable:
        return {"qid": case.qid, "kind": "unanswerable",
                "pass": abstained, "abstained": abstained,
                "top_score": top_score,
                "top": files[0]["name"] if files else None}

    topk = files[:TOP_K]
    deep = files[:DEEP_K]
    rerank_top = files[:RERANK_K]
    hints = case.doc_hints
    doc_hit = all(any(hint in f["name"] for f in topk) for hint in hints)
    doc_hit_50 = all(any(hint in f["name"] for f in deep) for hint in hints)
    doc_hit_20 = all(any(hint in f["name"] for f in rerank_top) for hint in hints)
    ranks = [
        next((i + 1 for i, f in enumerate(topk) if hint in f["name"]), None)
        for hint in hints
    ]
    rank = max((r for r in ranks if r is not None), default=None)
    deep_ranks = [
        next((i + 1 for i, f in enumerate(deep) if hint in f["name"]), None)
        for hint in hints
    ]
    rank_50 = max((r for r in deep_ranks if r is not None), default=None)

    matched_hints: set[str] = set()
    gains: list[int] = []
    reciprocal_rank = 0.0
    for index, file in enumerate(files[:10], start=1):
        match = next(
            (hint for hint in hints
             if hint not in matched_hints and hint in file["name"]),
            None,
        )
        gains.append(1 if match else 0)
        if match:
            matched_hints.add(match)
            if reciprocal_rank == 0.0:
                reciprocal_rank = 1.0 / index
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal = min(len(hints), 10)
    idcg = sum(1.0 / math.log2(index + 2) for index in range(ideal))
    ndcg_10 = dcg / idcg if idcg else 0.0

    # 关键词检查：答案 chunk 是否真的含有答案（防止"文档对了但片段不对"）
    kw_hit = True
    if case.answer_keywords and doc_hit:
        matched = [
            f for f in topk if any(hint in f["name"] for hint in hints)
        ]
        blob = " ".join(text for f in matched for text in f["texts"])
        kw_hit = any(kw in blob for kw in case.answer_keywords)

    return {"qid": case.qid, "kind": "answerable", "difficulty": case.difficulty,
            "pass": doc_hit and kw_hit and not abstained,
            "doc_hit": doc_hit, "doc_hit_20": doc_hit_20,
            "doc_hit_50": doc_hit_50,
            "kw_hit": kw_hit, "rank": rank, "rank_50": rank_50,
            "mrr_10": reciprocal_rank, "ndcg_10": ndcg_10,
            "abstained": abstained, "top_score": top_score,
            "top": topk[0]["name"] if topk else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--json", type=str, help="结果写入 JSON 文件")
    ap.add_argument("--label", default="baseline-fts5",
                    help="本次评测的标签，写入结果便于跨版本对比")
    args = ap.parse_args()

    conn = connect()
    init_db(conn)
    n_chunks = conn.execute("SELECT count(*) c FROM chunks").fetchone()["c"]
    if n_chunks == 0:
        print("库里没有分片，先启用来源并跑 /index/run")
        return 1

    info = summary()
    print(f"评测集 {info['total']} 题（有依据 {info['answerable']} / 无依据 "
          f"{info['unanswerable']}）· 库中 {n_chunks} 个分片")
    print(f"拒答门限 {ABSTAIN_THRESHOLD}\n")

    results = []
    t0 = time.perf_counter()
    for case in ALL_CASES:
        t = time.perf_counter()
        files, top_score = retrieve(conn, case.query, limit=200)
        latency = (time.perf_counter() - t) * 1000
        r = judge(case, files, top_score)
        r["latency_ms"] = latency
        r["query"] = case.query
        results.append(r)

        if args.verbose or not r["pass"]:
            mark = "✓" if r["pass"] else "✗"
            extra = ""
            if r["kind"] == "answerable":
                extra = f"rank={r['rank']} doc={r['doc_hit']} kw={r['kw_hit']}"
            print(f"  {mark} [{r['qid']}] {case.query[:30]:<32} "
                  f"score={top_score:.4f} {extra}")
            if not r["pass"] and r["top"]:
                print(f"      实际首位: {r['top'][:52]}")

    total_ms = (time.perf_counter() - t0) * 1000

    ans = [r for r in results if r["kind"] == "answerable"]
    una = [r for r in results if r["kind"] == "unanswerable"]

    recall = sum(r["doc_hit"] for r in ans) / len(ans)
    recall_50 = sum(r["doc_hit_50"] for r in ans) / len(ans)
    recall_20 = sum(r["doc_hit_20"] for r in ans) / len(ans)
    mrr_10 = sum(r["mrr_10"] for r in ans) / len(ans)
    ndcg_10 = sum(r["ndcg_10"] for r in ans) / len(ans)
    strict = sum(r["pass"] for r in ans) / len(ans)
    abstain_ok = sum(r["pass"] for r in una) / len(una)
    false_abstain = sum(r["abstained"] for r in ans) / len(ans)
    p50 = sorted(r["latency_ms"] for r in results)[len(results) // 2]

    print(f"\n{'指标':<26}{'实测':>9}  {'验收线':>8}  结果")
    print("-" * 58)
    rows = [
        ("Recall@5（文档命中）", recall, 0.80, True),
        ("Recall@50（文档深召回）", recall_50, 0.90, True),
        ("Recall@20（精排保真）", recall_20, None, None),
        ("MRR@10", mrr_10, None, None),
        ("nDCG@10", ndcg_10, None, None),
        ("严格通过率（含关键词）", strict, None, None),
        ("正确拒答率", abstain_ok, 0.80, True),
        ("误拒率（越低越好）", false_abstain, 0.05, False),
    ]
    all_pass = True
    for name, val, line, higher in rows:
        if line is None:
            print(f"{name:<26}{val:>8.1%}  {'—':>8}")
            continue
        # 门限为 0 意味着拒答功能未启用，报告为"待实现"而不是失败 ——
        # 把"还没做"和"做了但不达标"混为一谈会掩盖真实进度
        if name == "正确拒答率" and ABSTAIN_THRESHOLD == 0.0:
            print(f"{name:<26}{'未启用':>8}  {line:>7.0%}   ⊘ 待向量路标定")
            continue
        ok = val >= line if higher else val <= line
        all_pass &= ok
        print(f"{name:<26}{val:>8.1%}  {line:>7.0%}   {'✓' if ok else '✗'}")

    print(f"\n延迟 p50 {p50:.1f}ms · 全量 {total_ms:.0f}ms")

    by_diff: dict[str, list] = {}
    for r in ans:
        by_diff.setdefault(r["difficulty"], []).append(r["doc_hit"])
    print("\n按难度分解 Recall:")
    for d, v in sorted(by_diff.items()):
        print(f"  {d:<12} {sum(v)}/{len(v)} = {sum(v)/len(v):.0%}")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "label": args.label,
            "summary": {"recall_at_5": recall, "recall_at_20": recall_20,
                        "recall_at_50": recall_50, "mrr_at_10": mrr_10,
                        "ndcg_at_10": ndcg_10,
                        "strict": strict,
                        "abstain_ok": abstain_ok, "false_abstain": false_abstain,
                        "p50_ms": p50, "chunks": n_chunks,
                        "by_difficulty": {d: sum(v) / len(v) for d, v in by_diff.items()}},
            "results": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.json}")

    print(f"\n{'✓ 达标' if all_pass else '✗ 未达标'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
