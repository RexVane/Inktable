"""级联的查询侧门控判据：词法置信度能不能分开「CE 该上」和「CE 会捣乱」。

真实库复测（`docs/RETRIEVAL-PERF.md` §5.5）里 CE 的收益与损害各自集中：

    修好  A09 16→4  P19 23→3  P20 11→7  A13/F20/F25 2→1  U02 4→3
    弄坏  X07 5→9   M08 1→3   U04 2→4   A06/F27/S20 1→2

被弄坏的看起来都是本地打分器**本来就排第 1** 的题（exact / metadata），
被修好的是本地打分器完全无能的改写类。若这两组在「候选池最强 IDF 加权词
覆盖」上能分开，那 CE 就该按查询门控 —— 高置信度直接走本地（省掉全部 CE
延迟），低置信度才交给 CE。P95 是分位数，只要多数查询不进 CE，它就会塌下来。

**这个脚本只量分布，不改行为。**若两组分不开，就不该加这个门控 —— 那说明
阈值只能靠刷指标选，53 题里 6 题的样本量撑不住。

只读。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["INKTABLE_RERANKER"] = "off"

from app.db.database import connect, init_db  # noqa: E402
from app.index.search import extract_query_terms  # noqa: E402
from app.retrieval.pipeline import run as run_retrieval  # noqa: E402
from app.retrieval.rerank import (  # noqa: E402
    CASCADE_LEX_FULL,
    CASCADE_MIN_HEAD,
    CASCADE_PAIRS,
    LocalStaticReranker,
    _load_inputs,
    _load_vectors,
    _term_idf,
)
from tests.eval_gold import DEFAULT_GOLD_PATH, load_gold  # noqa: E402
from tests.evalset import ALL_CASES  # noqa: E402


def rank_deltas(static_path: Path, cascade_path: Path) -> dict[str, tuple]:
    a = {c["qid"]: c for c in json.loads(
        static_path.read_text(encoding="utf-8"))["results"] if "rank" in c}
    b = {c["qid"]: c for c in json.loads(
        cascade_path.read_text(encoding="utf-8"))["results"] if "rank" in c}
    out = {}
    for qid in a:
        if qid in b:
            out[qid] = (a[qid]["rank"], b[qid]["rank"])
    return out


def verdict(ra, rb) -> str:
    if ra is None or rb is None or ra == rb:
        return "不变"
    return "CE 修好" if rb < ra else "CE 弄坏"


def lexical_confidence(conn, query: str, route_limit: int) -> tuple[float, int]:
    """复刻 CascadeReranker 里那一行 —— 候选头部的最强 IDF 加权词覆盖。

    头部按融合顺序截取（与实装一致），不按本地分：本地打分器在改写类问题上
    会把 gold 压下去，用它截断等于提前淘汰 CE 最该救的题。
    """
    retrieval = run_retrieval(
        conn, query, route_limit=route_limit, candidate_limit=route_limit)
    selected, _ = _load_inputs(conn, retrieval.candidates)
    if not selected:
        return 0.0, 0
    head_size = max(1, min(max(CASCADE_PAIRS, CASCADE_MIN_HEAD), len(selected)))
    head = selected[:head_size]
    terms = extract_query_terms(query)
    local = LocalStaticReranker(
        term_idf=_term_idf(conn, set(terms)),
        chunk_vectors=_load_vectors(conn, [i.chunk_id for i in head]),
    )
    haystacks = [f"{i.section_path}\n{i.text}".lower() for i in head]
    return max((local._weighted_coverage(terms, h) for h in haystacks),
               default=0.0), len(selected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route-limit", type=int, default=120)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD_PATH)
    parser.add_argument("--static", type=Path, required=True)
    parser.add_argument("--cascade", type=Path, required=True)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    gold = load_gold(args.gold)
    cases = [c for c in ALL_CASES
             if gold[c.qid].status in {"answerable", "metadata"}]
    deltas = rank_deltas(args.static, args.cascade)

    conn = connect()
    init_db(conn)
    print("%d 道可评估题 · route_limit=%d · CASCADE_LEX_FULL=%.2f\n"
          % (len(cases), args.route_limit, CASCADE_LEX_FULL))

    rows = []
    for case in cases:
        conf, n = lexical_confidence(conn, case.query, args.route_limit)
        ra, rb = deltas.get(case.qid, (None, None))
        rows.append({"qid": case.qid, "difficulty": case.difficulty,
                     "conf": conf, "static_rank": ra, "cascade_rank": rb,
                     "verdict": verdict(ra, rb), "candidates": n})

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["verdict"], []).append(r)
    for label in ("CE 弄坏", "CE 修好", "不变"):
        g = sorted(groups.get(label, []), key=lambda r: -r["conf"])
        if not g:
            continue
        confs = [r["conf"] for r in g]
        print("%s（%d 题）  置信度 min=%.3f  中位=%.3f  max=%.3f"
              % (label, len(g), min(confs),
                 sorted(confs)[len(confs) // 2], max(confs)))
        for r in g if label != "不变" else g[:0]:
            print("   %-5s %-11s conf=%.3f  rank %s → %s"
                  % (r["qid"], r["difficulty"], r["conf"],
                     r["static_rank"], r["cascade_rank"]))
        print()

    _separation(groups)
    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print("明细写入 %s" % args.json)
    conn.close()
    return 0


def _separation(groups: dict[str, list[dict]]) -> None:
    """能不能用一条阈值把两组分开。分不开就别加门控。"""
    hurt = [r["conf"] for r in groups.get("CE 弄坏", [])]
    good = [r["conf"] for r in groups.get("CE 修好", [])]
    if not hurt or not good:
        print("有一组是空的，无法判断分离度")
        return
    print("分离度：弄坏组最低 %.3f，修好组最高 %.3f" % (min(hurt), max(good)))
    if min(hurt) > max(good):
        print("→ **完全分开**。阈值取两者之间即可，不是刷出来的。")
        return
    overlap_hurt = sum(1 for c in hurt if c <= max(good))
    overlap_good = sum(1 for c in good if c >= min(hurt))
    print("→ **有重叠**：弄坏组里 %d/%d 落在修好组区间内，修好组里 %d/%d 落在"
          "弄坏组区间内。此时任何阈值都要牺牲一边，53 题里这点样本量"
          "不足以支撑选择 —— 不该加这个门控。"
          % (overlap_hurt, len(hurt), overlap_good, len(good)))


if __name__ == "__main__":
    raise SystemExit(main())
