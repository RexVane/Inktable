"""逐阶段 / 逐路线检索延迟剖析。

发布门槛把「非生成搜索 P95 <= 2.5 秒」和「Rerank P95 <= 1.5 秒」定为阻塞项
（PLAN §10.4）。评测产物只记录总延迟与 rerank 耗时，中间那两秒落在哪一条
路线上无法从产物反推 —— 优化只能靠猜。

本工具把 RetrievalTrace 的 stage 明细和 child_search 内部四条路线的单独耗时
一起摊平输出，给出每个阶段的 P50/P95 与总占比，用来定位真正的瓶颈。

    INKTABLE_DB=output/release-gate-20260817/library-working.db \
        .venv/Scripts/python.exe scripts/profile_retrieval.py --route-limit 120

只读：不写库、不改索引。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import connect, init_db  # noqa: E402
from app.index import search as search_mod  # noqa: E402
from app.index.hierarchy import hierarchy_routes  # noqa: E402
from app.retrieval.pipeline import run as run_retrieval  # noqa: E402
from app.retrieval.query import decompose_comparative  # noqa: E402
from tests.eval_gold import DEFAULT_GOLD_PATH, load_gold  # noqa: E402
from tests.evalset import ALL_CASES  # noqa: E402

# 阶段顺序固定，便于人眼按管线顺序读表
STAGE_ORDER = (
    "hierarchy_routing", "lexical_retrieval", "embed_query", "deep_retrieval",
    "decompose", "scope", "rrf", "rerank",
)
ROUTE_ORDER = (
    "fts_jieba", "fts_trigram", "substr", "filename", "vector",
    "hierarchy", "embed_query",
)


def _percentile(values: list[float], fraction: float) -> float:
    """与 run_eval._percentile 同口径，保证两边数字可直接对比。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _timed(fn, *args, **kwargs):
    started = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 剖析工具不该因单条路线失败而中断
        return (time.perf_counter() - started) * 1000, exc
    return (time.perf_counter() - started) * 1000, result


def profile_routes(conn, query: str, limit: int) -> dict[str, float]:
    """单独计时 child_search 内部各条路线。

    直接调内部函数而不是复用 search()：search() 一次性跑完所有路线，
    分不出各自耗时，而路线之间的开销差了两个数量级。
    """
    out: dict[str, float] = {}

    jieba_q = search_mod.build_fts_query(query, segment=True)
    raw_q = search_mod.build_fts_query(query, segment=False)

    def run_fts(table: str, q: str):
        if not q:
            return []
        return conn.execute(
            f"SELECT {table}.rowid, bm25({table}) FROM {table} "
            f"JOIN chunks ch ON ch.id = {table}.rowid "
            f"JOIN contents c ON c.id = ch.content_id "
            f"WHERE {table} MATCH ? "
            f"AND ch.index_version = c.active_index_version "
            f"AND {search_mod.visible_content_exists('ch.content_id')} "
            f"ORDER BY bm25({table}) LIMIT ?",
            (q, limit),
        ).fetchall()

    out["fts_jieba"], jieba_rows = _timed(run_fts, "chunks_fts", jieba_q)
    out["fts_trigram"], tri_rows = _timed(run_fts, "chunks_fts_tri", raw_q)

    # substr 只在前两路召回不足时触发；照 search() 的原判据复现
    found = 0
    for rows in (jieba_rows, tri_rows):
        if isinstance(rows, list):
            found += len({r[0] for r in rows})
    out["substr_fired"] = float(found < min(3, limit))
    if found < min(3, limit):
        out["substr"], _ = _timed(search_mod._substring_search, conn, query, limit)
    else:
        out["substr"] = 0.0

    out["filename"], _ = _timed(search_mod._filename_search, conn, query, limit)

    # 向量路拆成「查询嵌入」与「向量检索」两段：前者是 Ollama HTTP 往返，
    # 后者是 sqlite-vec 扫描，优化手段完全不同，混在一起看不出该动哪边。
    from app.index import embedding as emb
    from app.index import vector as vec

    if emb.is_available() and vec.count(conn) > 0:
        out["embed_query"], qv = _timed(emb.get_embedder().encode_one, query)
        if isinstance(qv, Exception):
            out["vector"] = 0.0
        else:
            out["vector"], _ = _timed(
                search_mod._vector_search, conn, query, limit, query_vector=qv,
            )
    else:
        out["embed_query"] = 0.0
        out["vector"] = 0.0

    out["hierarchy"], _ = _timed(hierarchy_routes, conn, query, limit)
    out["subquery_count"] = float(len(tuple(decompose_comparative(query))))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route-limit", type=int, default=120)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD_PATH)
    parser.add_argument("--json", type=Path, help="write per-query detail JSON")
    parser.add_argument("--qids", help="comma-separated subset")
    parser.add_argument(
        "--skip-routes", action="store_true",
        help="only profile pipeline stages (routes double the runtime)",
    )
    args = parser.parse_args()

    gold = load_gold(args.gold)
    cases = [
        case for case in ALL_CASES
        if gold[case.qid].status != "corpus_missing"
    ]
    if args.qids:
        wanted = {item.strip() for item in args.qids.split(",") if item.strip()}
        cases = [case for case in cases if case.qid in wanted]

    conn = connect()
    init_db(conn)
    chunks = conn.execute("SELECT count(*) c FROM chunks").fetchone()["c"]
    print(f"库中 {chunks} 个分片 · {len(cases)} 条查询 · route_limit={args.route_limit}\n")

    stage_ms: dict[str, list[float]] = {name: [] for name in STAGE_ORDER}
    route_ms: dict[str, list[float]] = {name: [] for name in ROUTE_ORDER}
    totals: list[float] = []
    details: list[dict] = []
    substr_fires = 0
    subquery_queries = 0

    for case in cases:
        started = time.perf_counter()
        retrieval = run_retrieval(
            conn, case.query,
            route_limit=args.route_limit, candidate_limit=args.route_limit,
        )
        total = (time.perf_counter() - started) * 1000
        totals.append(total)

        stages = {
            stage["name"]: float(stage.get("duration_ms") or 0.0)
            for stage in retrieval.trace.to_dict().get("stages", [])
        }
        for name in STAGE_ORDER:
            stage_ms[name].append(stages.get(name, 0.0))

        row = {"qid": case.qid, "total_ms": total, "stages": stages}

        if not args.skip_routes:
            routes = profile_routes(conn, case.query, args.route_limit)
            for name in ROUTE_ORDER:
                route_ms[name].append(routes.get(name, 0.0))
            substr_fires += int(routes.get("substr_fired", 0.0))
            if routes.get("subquery_count", 0.0) > 1:
                subquery_queries += 1
            row["routes"] = routes

        details.append(row)
        print(f"  [{case.qid}] {total:7.0f}ms  " + "  ".join(
            f"{name[:9]}={stages.get(name, 0.0):6.0f}" for name in STAGE_ORDER
        ))

    def table(title: str, data: dict[str, list[float]], order) -> None:
        print(f"\n{title}")
        print("  " + "stage".ljust(18) + "p50".rjust(9) + "p95".rjust(9)
              + "mean".rjust(9) + "max".rjust(9) + "  share(mean)")
        mean_total = statistics.fmean(totals) if totals else 1.0
        for name in order:
            values = [v for v in data.get(name, []) if v is not None]
            if not values or max(values) == 0.0:
                continue
            mean = statistics.fmean(values)
            print("  " + name.ljust(18)
                  + f"{_percentile(values, 0.5):9.0f}"
                  + f"{_percentile(values, 0.95):9.0f}"
                  + f"{mean:9.0f}"
                  + f"{max(values):9.0f}"
                  + f"   {mean / mean_total * 100:5.1f}%")

    print("\n" + "=" * 74)
    print(f"总延迟  p50={_percentile(totals, 0.5):.0f}ms  "
          f"p95={_percentile(totals, 0.95):.0f}ms  "
          f"mean={statistics.fmean(totals):.0f}ms  max={max(totals):.0f}ms")
    print(f"门槛    搜索 P95 <= 2500ms  →  "
          f"{'PASS' if _percentile(totals, 0.95) <= 2500 else 'FAIL'}")
    rerank_p95 = _percentile(stage_ms["rerank"], 0.95)
    print(f"        rerank P95 <= 1500ms  →  "
          f"{'PASS' if rerank_p95 <= 1500 else 'FAIL'} ({rerank_p95:.0f}ms)")
    table("管线阶段（RetrievalTrace）", stage_ms, STAGE_ORDER)
    if not args.skip_routes:
        table("召回路线（单独计时，含重复执行开销）", route_ms, ROUTE_ORDER)
        print(f"\n  substr 兜底触发 {substr_fires}/{len(cases)} 条查询")
        print(f"  比较类分解（>1 子查询）{subquery_queries}/{len(cases)} 条查询")

    conn.close()

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "route_limit": args.route_limit,
            "chunks": chunks,
            "totals": totals,
            "details": details,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
