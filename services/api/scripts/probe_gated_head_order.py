"""门控进 CE 的那批题，若按**向量路**次序截头部，gold 能浅多少。

§5.6 的墙是：进 CE 的 4 题里最深 gold 在第 25 位（P19），所以 K=26 不能降，
Rerank P95 就下不来。但头部现在按**融合**次序截 —— 而门控挑出来的正是词法
无能的改写类题，它们的 gold 本来就该由向量路找到。若按向量路次序截，同一个
gold 可能浅得多，K 就能跟着降。

这是个测量，不是改动。分三种次序报 gold 位置：融合序（现状）、向量路序、
两者取较浅。只读。
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

os.environ["ORDO_RERANKER"] = "off"

from app.db.database import connect, init_db  # noqa: E402
from app.retrieval.pipeline import run as run_retrieval  # noqa: E402
from app.retrieval.rerank import _load_inputs  # noqa: E402
from tests.eval_gold import DEFAULT_GOLD_PATH, load_gold  # noqa: E402
from tests.evalset import ALL_CASES  # noqa: E402


def gold_content_ids(conn, case) -> set[int]:
    hashes = sorted({sha for g in case.content_groups for sha in g.content_sha256})
    if not hashes:
        return set()
    marks = ",".join("?" * len(hashes))
    return {r["id"] for r in conn.execute(
        f"SELECT id FROM contents WHERE sha256 IN ({marks})", hashes)}


def first_pos(items, wanted_ids, owner) -> int | None:
    for i, chunk_id in enumerate(items, start=1):
        if owner.get(chunk_id) in wanted_ids:
            return i
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route-limit", type=int, default=120)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD_PATH)
    parser.add_argument("--gate-json", type=Path, required=True,
                        help="probe_lexical_gate.py 的输出，用来标出进 CE 的题")
    parser.add_argument("--gate", type=float, default=0.45)
    args = parser.parse_args()

    gold = load_gold(args.gold)
    conf = {r["qid"]: r["conf"] for r in json.loads(
        args.gate_json.read_text(encoding="utf-8"))}
    cases = [c for c in ALL_CASES
             if gold[c.qid].status in {"answerable", "metadata"}]

    conn = connect()
    init_db(conn)
    print("门控阈值 %.2f · route_limit=%d\n" % (args.gate, args.route_limit))
    print("%-5s %-6s %-7s %-7s %-7s %s" % (
        "qid", "conf", "融合序", "向量序", "取较浅", "进CE"))

    gated_fusion, gated_best = [], []
    for case in cases:
        wanted = gold_content_ids(conn, gold[case.qid])
        if not wanted:
            continue
        retrieval = run_retrieval(
            conn, case.query,
            route_limit=args.route_limit, candidate_limit=args.route_limit)
        selected, _ = _load_inputs(conn, retrieval.candidates)
        if not selected:
            continue
        ids = [i.chunk_id for i in selected]
        owner = {i.chunk_id: i.content_id for i in selected}

        fusion_pos = first_pos(ids, wanted, owner)
        vec = [cid for cid, _score in retrieval.routes.get("vector", [])]
        keep = set(ids)
        vec_order = [c for c in vec if c in keep]
        vec_order += [c for c in ids if c not in set(vec_order)]
        vec_pos = first_pos(vec_order, wanted, owner)
        best = min([p for p in (fusion_pos, vec_pos) if p], default=None)

        c = conf.get(case.qid, 1.0)
        in_ce = c < args.gate
        print("%-5s %-6.3f %-7s %-7s %-7s %s" % (
            case.qid, c, fusion_pos, vec_pos, best, "是" if in_ce else ""))
        if in_ce:
            if fusion_pos:
                gated_fusion.append(fusion_pos)
            if best:
                gated_best.append(best)

    print()
    print("进 CE 的题：融合序最深 %s → 取较浅后最深 %s"
          % (max(gated_fusion, default='—'), max(gated_best, default='—')))
    if gated_best and gated_fusion and max(gated_best) < max(gated_fusion):
        print("→ K 可以从 %d 降到 %d；按每对约 105ms 估，Rerank 可省约 %dms"
              % (max(gated_fusion) + 1, max(gated_best) + 1,
                 105 * (max(gated_fusion) - max(gated_best))))
    else:
        print("→ 换次序没让 gold 变浅，这条路不通。")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
