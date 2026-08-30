"""级联重排的候选深度判据。

Cross-Encoder 每对候选约 50-80ms，1.5 秒的 rerank 门槛只够打分 16-20 对，
所以它不可能扫全部 80 个候选 —— 必须由便宜的一级顺序先截断。截断多深才
不丢 gold，是这个设计唯一的未知量：截浅了 gold 进不了二级重排，截深了
超延迟门槛。

判据取「第一个属于 gold content 的候选，在 Cross-Encoder 实际会看到的
那个列表里排第几位」。该列表是 pipeline 的 `_load_inputs` 输出 —— 已经
过了每内容软上限与同文去重，位置与 RRF 名次并不相同，必须实测。

一级顺序用 `ORDO_RERANKER=off` 从真实管线里取，不在探针里复制融合逻辑。

    ORDO_DB=output/release-gate-20260817/library-working.db \
        .venv/Scripts/python.exe scripts/probe_cascade_depth.py --route-limit 120

只读。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 必须在导入 rerank 之前定住，模块级常量在导入时求值
os.environ["ORDO_RERANKER"] = "off"

from app.db.database import connect, init_db  # noqa: E402
from app.retrieval.pipeline import run as run_retrieval  # noqa: E402
from app.retrieval.rerank import RERANK_LIMIT, _load_inputs  # noqa: E402
from tests.eval_gold import DEFAULT_GOLD_PATH, load_gold  # noqa: E402
from tests.evalset import ALL_CASES  # noqa: E402


def gold_content_ids(conn, case) -> set[int]:
    """gold 按内容 SHA 标注（文件名不稳定），换算成本库的 content_id。"""
    hashes = sorted({
        sha for group in case.content_groups for sha in group.content_sha256
    })
    if not hashes:
        return set()
    marks = ",".join("?" * len(hashes))
    return {
        row["id"] for row in conn.execute(
            f"SELECT id FROM contents WHERE sha256 IN ({marks})", hashes
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route-limit", type=int, default=120)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD_PATH)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    gold = load_gold(args.gold)
    cases = [
        case for case in ALL_CASES
        if gold[case.qid].status in {"answerable", "metadata"}
    ]

    conn = connect()
    init_db(conn)
    print(f"{len(cases)} 道可评估题 · route_limit={args.route_limit} · "
          f"RERANK_LIMIT={RERANK_LIMIT}\n")

    rows: list[dict] = []
    for case in cases:
        wanted = gold_content_ids(conn, gold[case.qid])
        if not wanted:
            rows.append({"qid": case.qid, "note": "gold sha 在本库中不存在"})
            continue

        retrieval = run_retrieval(
            conn, case.query,
            route_limit=args.route_limit, candidate_limit=args.route_limit,
        )
        # Cross-Encoder 实际打分的就是这个 selected 列表
        selected, _remainder = _load_inputs(conn, retrieval.candidates)
        pos = next(
            (i for i, item in enumerate(selected, start=1)
             if item.content_id in wanted),
            None,
        )
        # 融合层名次（未过 _load_inputs 的软上限/去重）作为对照
        ids = [c.chunk_id for c in retrieval.candidates]
        owner = {}
        if ids:
            marks = ",".join("?" * len(ids))
            owner = {
                row["id"]: row["content_id"] for row in conn.execute(
                    f"SELECT id, content_id FROM chunks WHERE id IN ({marks})", ids
                )
            }
        fuse_pos = next(
            (i for i, cid in enumerate(ids, start=1) if owner.get(cid) in wanted),
            None,
        )
        rows.append({
            "qid": case.qid,
            "difficulty": case.difficulty,
            "selected_pos": pos,
            "fuse_pos": fuse_pos,
            "selected_len": len(selected),
            "query": case.query,
        })

    conn.close()

    print("qid   diff        CE输入位  融合名次  候选数  query")
    for row in rows:
        if "note" in row:
            print(f"{row['qid']:6s}{row['note']}")
            continue
        print(f"{row['qid']:6s}{row['difficulty']:12s}"
              f"{str(row['selected_pos']):>8s}{str(row['fuse_pos']):>10s}"
              f"{row['selected_len']:>8d}  {row['query'][:36]}")

    def coverage(field: str, title: str) -> None:
        usable = [row for row in rows if "note" not in row]
        positions = [row[field] for row in usable if row.get(field)]
        misses = [row["qid"] for row in usable if not row.get(field)]
        print(f"\n{title}：{len(positions)}/{len(usable)} 题命中"
              + (f" · 候选池内根本没有 gold: {misses}" if misses else ""))
        if not positions:
            return
        for k in (5, 8, 10, 12, 16, 20, 24, 30, 40, 50, 80):
            covered = sum(1 for p in positions if p <= k)
            flag = "   <= 全覆盖" if covered == len(positions) else ""
            print(f"    K={k:<3d} 覆盖 {covered}/{len(positions)} = "
                  f"{covered / len(positions) * 100:5.1f}%{flag}")
        print(f"    最深 gold 位置 = {max(positions)}")

    coverage("selected_pos", "Cross-Encoder 输入列表内的 gold 深度（决定 K）")
    coverage("fuse_pos", "融合层名次（对照）")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"\n明细写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
