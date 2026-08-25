"""把 Document 层的主题摘要补齐（可中断续跑）。

摘要不在入库路径上生成：一次 LLM 调用 5-13 秒，挂在入库上会破坏「投放文件
→ 约 3.5 秒可搜」这条实测指标。所以由本脚本离线补，补上之前文档路的行为
与引入该列之前一致。

**可中断续跑**：只处理 `abstract IS NULL` 的行，每篇一提交。20,882 篇串行
约 8.7-23 小时，中途 Ctrl+C 或断电后重跑会从断点继续。

**默认只跑活跃版本**：`d.index_version = c.active_index_version`。历史版本的
表示不参与检索，给它们生成摘要纯属浪费。

用法：
    INKTABLE_OLLAMA_URL=http://127.0.0.1:18434 \\
      .venv/Scripts/python.exe scripts/backfill_abstracts.py --limit 100
    # 全量（很慢，建议放后台）：省掉 --limit

先读 docs/RETRIEVAL-PERF.md 的摘要探针一节：400 篇抽样上文档路 MRR
0.5761 → 0.5931，只有 4 题变好、1 题变差、18 题两臂都不命中。收益真实
但很小，全量回填之前请确认这笔时间值得花。
"""

from __future__ import annotations

import argparse
import io
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

from app.db.database import connect, init_db  # noqa: E402
from app.index.abstract import (  # noqa: E402
    AbstractUnavailable,
    generate,
    model_available,
)
from app.index.hierarchy import document_index_text  # noqa: E402

MODEL_TAG = os.environ.get("INKTABLE_ABSTRACT_MODEL", "qwen3:8b")


def pending(conn: sqlite3.Connection, limit: int | None) -> list[sqlite3.Row]:
    sql = """SELECT d.id, d.title, d.summary_text,
                    substr(d.full_text, 1, 8000) AS body
               FROM document_representations d
               JOIN contents c ON c.id = d.content_id
              WHERE d.abstract IS NULL
                AND d.index_version = c.active_index_version
              ORDER BY d.id"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="本次最多处理多少篇（省略=全部）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not model_available():
        print("摘要模型不可用：检查 INKTABLE_OLLAMA_URL 与是否已拉取",
              MODEL_TAG)
        return 2

    conn = connect()
    init_db(conn)
    todo = pending(conn, args.limit)
    total_null = conn.execute(
        """SELECT COUNT(*) FROM document_representations d
             JOIN contents c ON c.id = d.content_id
            WHERE d.abstract IS NULL
              AND d.index_version = c.active_index_version""").fetchone()[0]
    print("待补摘要 %d 篇（本次处理 %d 篇）" % (total_null, len(todo)))
    if args.dry_run or not todo:
        return 0

    ok = failed = 0
    started = time.time()
    for i, row in enumerate(todo, start=1):
        try:
            abstract = generate(row["title"] or "", row["body"] or "")
        except AbstractUnavailable as exc:
            failed += 1
            print("  失败 id=%s：%s" % (row["id"], exc))
            # 连不上通常不是单篇问题；连续失败就停，避免空转几小时
            if failed >= 5 and ok == 0:
                print("连续失败，提前停止。修好模型服务后重跑即可续。")
                break
            continue
        with conn:
            conn.execute(
                "UPDATE document_representations SET abstract = ?, "
                "abstract_model = ? WHERE id = ?",
                (abstract, MODEL_TAG, row["id"]))
            # documents_fts 是 contentless：必须先删再插，UPDATE 不会同步。
            conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (row["id"],))
            conn.execute(
                "INSERT INTO documents_fts(rowid, text) VALUES (?, ?)",
                (row["id"], document_index_text(
                    row["title"] or "", abstract, row["summary_text"] or "")))
        ok += 1
        if i % 20 == 0 or i == len(todo):
            rate = (time.time() - started) / i
            print("  %d/%d，%.1fs/篇，剩余约 %.0f 分钟"
                  % (i, len(todo), rate, rate * (len(todo) - i) / 60))
    print("完成：成功 %d，失败 %d，用时 %.1f 分钟"
          % (ok, failed, (time.time() - started) / 60))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
