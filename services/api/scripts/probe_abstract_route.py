"""A/B 探针：Document 层加上 LLM 主题摘要，文档路的 gold 命中会不会变好？

**这是一个假设检验，不是优化。** 假设是：`documents_fts` 现在索引
`title + full_text[:1000]`，那是**截断**；主题类查询（「哪份资料讲了 X」）
只在关键词恰好落在正文开头时才命中。若换成带主题词汇的摘要，文档路的
gold 命中名次应当变好。

**方法上必须守住的两条**

1. 两臂语料完全相同。只给 gold 目标文档生成摘要、让干扰文档保持截断，
   等于给正确答案单独加буст —— 那测的是「我偏袒了谁」，不是假设。所以
   抽样里的每一篇（含干扰文档）都生成摘要。
2. 分词、FTS 配置、查询构造逐字复用生产代码（`segment_for_index` /
   `build_fts_query` / `unicode61`）。自己写一份「差不多的」对比，测出来的
   是那份仿制品的性质。

**不碰真实库**：从隔离评测库只读取样，在独立的探针库里建两张 FTS 表。

用法：
    ORDO_OLLAMA_URL=http://127.0.0.1:18434 \\
      .venv/Scripts/python.exe scripts/probe_abstract_route.py --sample 400
"""

from __future__ import annotations

import argparse
import io
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# line_buffering：这是个跑几十分钟的任务，进度必须能实时看到。默认的块缓冲
# 会让重定向到文件的输出一直是空的，看起来像卡死。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

from app.index.abstract import AbstractUnavailable, generate, model_available  # noqa: E402
from app.index.hierarchy import document_index_text  # noqa: E402
from app.index.search import build_fts_query  # noqa: E402
from tests.evalset import ALL_CASES  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
EVAL_DB = REPO / "output" / "release-gate-20260817" / "library-working.db"
GOLD = REPO / "docs" / "eval" / "gold-evidence-spans.json"


def gold_content_shas() -> dict[str, set[str]]:
    """qid → 该题 gold 证据所在的 content_sha256 集合。"""
    data = json.loads(GOLD.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for case in data["cases"]:
        shas: set[str] = set()
        for group in case.get("content_groups") or []:
            for sha in group.get("content_sha256") or []:
                shas.add(sha)
        if shas:
            out[case["qid"]] = shas
    return out


def load_docs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT d.id, d.content_id, d.title, d.summary_text,
                  substr(d.full_text, 1, 8000) AS body, c.sha256
           FROM document_representations d
           JOIN contents c ON c.id = d.content_id
           WHERE d.index_version = c.active_index_version"""
    ).fetchall()


def build_fts(db: sqlite3.Connection, table: str) -> None:
    db.execute(
        f"CREATE VIRTUAL TABLE {table} USING fts5("
        "text, content='', contentless_delete=1, tokenize='unicode61')"
    )


def rank_of(db: sqlite3.Connection, table: str, query: str,
            targets: set[int]) -> int | None:
    """gold 文档在该 FTS 表里的名次（1 起）。未命中返回 None。"""
    fts_query = build_fts_query(query, segment=True)
    if not fts_query:
        return None
    rows = db.execute(
        f"SELECT rowid FROM {table} WHERE {table} MATCH ? "
        f"ORDER BY bm25({table}) LIMIT 50", (fts_query,)).fetchall()
    for i, row in enumerate(rows, start=1):
        if row[0] in targets:
            return i
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=400,
                        help="抽样文档数（含全部 gold 目标）")
    parser.add_argument("--seed", type=int, default=20260826)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    if not EVAL_DB.exists():
        print("找不到隔离评测库：", EVAL_DB)
        return 2
    if not model_available():
        print("摘要模型不可用（检查 ORDO_OLLAMA_URL 与已拉取模型）")
        return 2

    src = sqlite3.connect(f"file:{EVAL_DB.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    docs = load_docs(src)
    src.close()
    print("评测库文档表示：%d 篇" % len(docs))

    by_sha: dict[str, list[sqlite3.Row]] = {}
    for doc in docs:
        by_sha.setdefault(doc["sha256"], []).append(doc)

    gold = gold_content_shas()
    queries = {case.qid: case.query for case in ALL_CASES if case.answerable}

    # gold 目标文档必须全部入样，否则题目根本没有正确答案可命中
    must: list[sqlite3.Row] = []
    seen: set[int] = set()
    covered: dict[str, set[int]] = {}
    for qid, shas in gold.items():
        if qid not in queries:
            continue
        ids: set[int] = set()
        for sha in shas:
            for doc in by_sha.get(sha, []):
                ids.add(doc["id"])
                if doc["id"] not in seen:
                    seen.add(doc["id"])
                    must.append(doc)
        if ids:
            covered[qid] = ids
    print("可评估题数：%d（gold 目标文档 %d 篇）" % (len(covered), len(must)))

    rng = random.Random(args.seed)
    pool = [doc for doc in docs if doc["id"] not in seen]
    rng.shuffle(pool)
    extra = pool[:max(0, args.sample - len(must))]
    sample = must + extra
    print("抽样合计：%d 篇（gold %d + 干扰 %d）"
          % (len(sample), len(must), len(extra)))

    db = sqlite3.connect(":memory:")
    build_fts(db, "base_fts")
    build_fts(db, "abs_fts")

    ok = 0
    failed = 0
    started = time.time()
    for i, doc in enumerate(sample, start=1):
        try:
            abstract = generate(doc["title"] or "", doc["body"] or "")
            ok += 1
        except AbstractUnavailable as exc:
            abstract = None
            failed += 1
            if failed <= 3:
                print("  摘要失败（%s…）：%s" % ((doc["title"] or "")[:24], exc))
        db.execute("INSERT INTO base_fts(rowid, text) VALUES (?, ?)",
                   (doc["id"], document_index_text(
                       doc["title"] or "", None, doc["summary_text"] or "")))
        db.execute("INSERT INTO abs_fts(rowid, text) VALUES (?, ?)",
                   (doc["id"], document_index_text(
                       doc["title"] or "", abstract, doc["summary_text"] or "")))
        if i % 25 == 0 or i == len(sample):
            rate = (time.time() - started) / i
            print("  %d/%d 篇，%.1fs/篇，预计剩余 %.0f 分钟"
                  % (i, len(sample), rate, rate * (len(sample) - i) / 60))
    db.commit()
    print("摘要生成：成功 %d，失败 %d，用时 %.1f 分钟"
          % (ok, failed, (time.time() - started) / 60))

    better = worse = same = 0
    base_miss = abs_miss = 0
    rows_out = []
    for qid in sorted(covered):
        targets = covered[qid]
        rb = rank_of(db, "base_fts", queries[qid], targets)
        ra = rank_of(db, "abs_fts", queries[qid], targets)
        rows_out.append({"qid": qid, "base": rb, "abstract": ra})
        if rb is None:
            base_miss += 1
        if ra is None:
            abs_miss += 1
        kb = rb if rb is not None else 999
        ka = ra if ra is not None else 999
        if ka < kb:
            better += 1
        elif ka > kb:
            worse += 1
        else:
            same += 1
        flag = "+" if ka < kb else ("-" if ka > kb else " ")
        print("  %s %-4s 截断=%-4s 摘要=%-4s"
              % (flag, qid, rb if rb else "miss", ra if ra else "miss"))

    def mrr(key: str) -> float:
        vals = [r[key] for r in rows_out]
        return sum(1.0 / v for v in vals if v) / max(1, len(vals))

    print()
    print("题数 %d：变好 %d / 变差 %d / 不变 %d" % (len(rows_out), better, worse, same))
    print("未命中：截断 %d 题，摘要 %d 题" % (base_miss, abs_miss))
    print("文档路 MRR：截断 %.4f → 摘要 %.4f" % (mrr("base"), mrr("abstract")))

    if args.json:
        args.json.write_text(json.dumps({
            "sample": len(sample), "gold_docs": len(must),
            "abstract_ok": ok, "abstract_failed": failed,
            "better": better, "worse": worse, "same": same,
            "base_miss": base_miss, "abstract_miss": abs_miss,
            "mrr_base": mrr("base"), "mrr_abstract": mrr("abstract"),
            "model": os.environ.get("ORDO_ABSTRACT_MODEL", "qwen3:8b"),
            "cases": rows_out,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("产物：", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
