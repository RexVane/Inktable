"""删除**已排除内容**的索引行。破坏性路径，默认干跑。

为什么要有这条路径 —— 排除 `B:\\devcache` 只把 `files.state` 置为 `ignored`，
索引行原样留在库里。实测（`scripts/audit_index_residue.py`）：

    chunks / chunks_fts / chunks_fts_tri   残留 198,589 / 244,664 = 81.2%
    chunks_vec                            残留 135,914 / 181,989 = 74.7%

这不只是占 1.8GB 磁盘。检索代码有两处会因此继续受影响：

1. `bm25()` 的 IDF 与长度归一化用的是**整张 FTS 表**的统计量。可见性过滤写在
   WHERE 里，但分数是先算出来的 —— 81% 的噪声还在分母里。
2. 向量路是「先 KNN、后过滤」（`app/index/search.py::_vector_search`），
   超取上限只有 3 倍。残留占 74.7% 时 top-3k 里绝大多数会被过滤掉，
   向量路的**有效深度**远小于请求深度。而向量路是唯一能命中同义表述的一路。

`cleanup_ingestion_noise.py` 处理不了这批：它的判据是「content 没有任何 files
行」（孤儿），而被排除的文件 files 行仍在，因此干跑恒为 `removals=0`。

**保留什么**：`files` 与 `contents` 行都不删。排除在记录层仍然可逆 ——
取消排除后 `remove_exclusion()` 会把 `parse_state='excluded'` 的内容重新置为
`pending`，下次扫描重新解析与嵌入（代价是重算，不是丢失）。

**不能置为 pending 的原因**：`index_pending()` 的队列查询不看 `state='ignored'`，
一旦置为 pending，10.7 万个被排除的文件会立刻回到索引队列 —— 排除的收益当场
清零。所以新增 `excluded` 这个既不在队列、又能被取消排除唤醒的状态。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.database import (  # noqa: E402
    DB_PATH,
    AlreadyRunning,
    acquire_single_instance_lock,
    backup_is_restorable,
    connect,
    quick_check,
    release_single_instance_lock,
)
from app.db.visibility import visible_content_exists  # noqa: E402
from app.index.pipeline import (  # noqa: E402
    _content_savepoint,
    _delete_content_indexes_batch,
)

RESIDUE_CONTENTS = """
    SELECT c.id FROM contents c
    WHERE EXISTS (SELECT 1 FROM files f WHERE f.content_id = c.id)
      -- 判据是「**每一份**副本都被排除」，不是「没有可见副本」。两者差别很大：
      -- 不可见的原因还有 `state='missing'`（外置盘拔了、云盘文件没下载）和
      -- 源被禁用。那些是**过渡态**，插回硬盘或重新启用就该恢复；而剪索引没有
      -- 对应的唤醒路径（只有取消排除会 requeue），一旦剪掉就是静默永久失踪。
      -- 排除是用户的显式决定，missing 不是 —— 只对显式决定动手。
      AND NOT EXISTS (
          SELECT 1 FROM files f
          WHERE f.content_id = c.id AND f.state != 'ignored'
      )
      -- 幂等：剪过的内容状态已是 excluded，重跑时不再入选。少了这一条，
      -- `--apply` 第二次会对 9 万个已经空掉的 content 重复走一遍删除批次。
      AND c.parse_state != 'excluded'
    ORDER BY c.id
"""

VISIBLE_CHUNKS = f"""
    SELECT ch.id FROM chunks ch
    WHERE {visible_content_exists('ch.content_id')}
"""


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def survey(conn) -> dict:
    residue = [r["id"] for r in conn.execute(RESIDUE_CONTENTS)]
    stats = {
        "prunable_contents": len(residue),
        "chunks_total": conn.execute(
            "SELECT count(*) c FROM chunks").fetchone()["c"],
        "chunks_visible": conn.execute(
            f"SELECT count(*) c FROM ({VISIBLE_CHUNKS})").fetchone()["c"],
        "sections_total": conn.execute(
            "SELECT count(*) c FROM sections").fetchone()["c"],
        "documents_total": conn.execute(
            "SELECT count(*) c FROM document_representations").fetchone()["c"],
    }
    stats["chunks_prunable"] = conn.execute(
        f"""SELECT count(*) c FROM chunks
            WHERE content_id IN ({RESIDUE_CONTENTS})"""
    ).fetchone()["c"]
    # 「不可见」比「可剪」多：missing / 源被禁用的内容也不可见，但它们的索引
    # 行必须留着。这两个数字的差额就是刻意不动的那部分，值得打出来。
    stats["chunks_invisible"] = (
        stats["chunks_total"] - stats["chunks_visible"])
    stats["chunks_invisible_kept"] = (
        stats["chunks_invisible"] - stats["chunks_prunable"])
    return {"stats": stats, "ids": residue}


def prune(conn, ids: list[int], batch_size: int) -> dict:
    """按批删除索引行。每批一个 savepoint —— 中途失败不会留下半个索引。

    可见分片集合在删除前后必须**逐 id 相同**。这条不变式比「gold 还在」更强：
    只要少了一个可见分片，就说明残留判据写宽了，评测会因为正确答案不在库里
    而「变好」。
    """
    before = {r["id"] for r in conn.execute(VISIBLE_CHUNKS)}
    done = 0
    t0 = time.time()
    for start in range(0, len(ids), batch_size):
        batch = ids[start:start + batch_size]
        with _content_savepoint(conn):
            _delete_content_indexes_batch(conn, batch)
            marks = ",".join("?" * len(batch))
            conn.execute(
                f"""UPDATE contents SET parse_state = 'excluded', chunk_count = 0,
                       embedding_model_id = NULL, indexed_at = NULL
                    WHERE id IN ({marks})""",
                batch,
            )
        conn.commit()
        done += len(batch)
        _emit({"phase": "prune", "done": done, "total": len(ids),
               "elapsed_s": round(time.time() - t0, 1)})

    after = {r["id"] for r in conn.execute(VISIBLE_CHUNKS)}
    if after != before:
        raise SystemExit(
            "可见分片集合被改动了（-%d/+%d）—— 判据写错，立即用备份回滚"
            % (len(before - after), len(after - before)))
    return {"pruned_contents": done, "visible_chunks_intact": len(after)}


def optimize(conn) -> None:
    """合并 FTS 段。contentless 表的删除是墓碑式的，不 optimize 不会真正腾空。"""
    for table in ("chunks_fts", "chunks_fts_tri", "documents_fts", "sections_fts"):
        t0 = time.time()
        conn.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
        conn.commit()
        _emit({"phase": "optimize", "table": table,
               "elapsed_s": round(time.time() - t0, 1)})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--optimize", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    args = parser.parse_args()

    is_real = args.db.resolve() == Path(DB_PATH).resolve()
    # 只有动**真实用户库**才强制备份。对实验副本强制备份等于要求再复制 1.8GB，
    # 而副本本身就是从真库快照出来的，真库才是权威。
    if args.apply and is_real:
        if not args.backup:
            parser.error("动真实库必须 --backup")
        if not backup_is_restorable(args.backup):
            parser.error(f"备份不可恢复: {args.backup}")

    os.environ["INKTABLE_DB"] = str(args.db)
    locked = False
    if is_real:
        try:
            acquire_single_instance_lock()
        except AlreadyRunning:
            parser.error("应用正在运行，先退出 Inktable 再执行（避免并发写）")
        locked = True
    try:
        conn = connect()
        info = survey(conn)
        _emit({"phase": "survey", **info["stats"],
               "db_mb": round(os.path.getsize(args.db) / 1e6)})
        if not args.apply:
            _emit({"phase": "dry-run", "would_prune": len(info["ids"])})
            return 0

        result = prune(conn, info["ids"], args.batch_size)
        _emit({"phase": "pruned", **result})
        if args.optimize:
            optimize(conn)
        _emit({"phase": "quick_check", "result": quick_check(conn)})
        after = survey(conn)
        _emit({"phase": "after", **after["stats"],
               "db_mb": round(os.path.getsize(args.db) / 1e6)})
        if args.vacuum:
            t0 = time.time()
            conn.execute("VACUUM")
            _emit({"phase": "vacuum", "elapsed_s": round(time.time() - t0, 1),
                   "db_mb": round(os.path.getsize(args.db) / 1e6)})
        conn.close()
    finally:
        if locked:
            release_single_instance_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
