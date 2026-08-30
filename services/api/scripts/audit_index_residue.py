"""排除 B:\\devcache 之后，索引层里**残留**了多少属于已排除内容的行。

为什么这不只是「省磁盘」——我先前把它归成空间回收，归错了。检索代码里有两处
用到全库统计或先检索后过滤，残留行会继续影响排序质量：

1. **BM25 全局统计**：`bm25(chunks_fts)` 的 IDF 用的是整张 FTS 表的文档频率，
   长度归一化用的是全表平均长度。残留行还在表里，rust 文档里高频的词
   （trait/struct/example…）IDF 被压低，可见分片之间的相对排序随之改变。
   过滤发生在 WHERE 里，但**分数是先算出来的**。

2. **向量路是「先检索、后过滤」**（`app/index/vector.py` 的 KNN +
   `_filter_vector_hits`），超取上限只有 3 倍。若 chunks_vec 里残留占 97%，
   那么 top-3k 里绝大多数会在过滤后被丢掉，向量路实际返回深度远小于请求深度
   —— 而向量路是四路里唯一能命中同义表述的一路。

本脚本只读，不改任何东西。目的是把上面两条从「推测」变成「有分母的事实」。
影子表只读元数据（chunk_id/size/validity/rowids），不读向量，因此很快。
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db.visibility import visible_content_exists  # noqa: E402

DB = os.environ.get("ORDO_DB") or os.path.join(
    os.path.expanduser("~"), "Library", "Application Support",
    "Ordo", "library.db")


def pct(part: int, whole: int) -> str:
    return "—" if not whole else "%.1f%%" % (100.0 * part / whole)


def main() -> int:
    conn = sqlite3.connect(
        "file:{}?mode=ro".format(DB.replace("\\", "/")), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA temp_store=MEMORY")

    print("库: %s (%.0f MB)" % (DB, os.path.getsize(DB) / 1e6))

    states = conn.execute(
        "SELECT state, count(*) c FROM files GROUP BY state ORDER BY c DESC")
    print("files 状态:", {r["state"]: r["c"] for r in states})

    # 残留 = 分片所属 content 没有任何可见副本。判据直接复用检索用的同一个
    # 谓词，避免「审计口径」与「检索口径」漂移得出好看但无意义的数字。
    print("\n正在标记残留分片（复用检索的可见性谓词）…")
    conn.execute("CREATE TEMP TABLE residue (id INTEGER PRIMARY KEY)")
    conn.execute(
        f"""INSERT INTO residue (id)
            SELECT ch.id FROM chunks ch
            WHERE NOT ({visible_content_exists('ch.content_id')})""")
    total = conn.execute("SELECT count(*) c FROM chunks").fetchone()["c"]
    residue = conn.execute("SELECT count(*) c FROM residue").fetchone()["c"]
    print("chunks           总 %8d  残留 %8d  占 %s" % (total, residue,
                                                       pct(residue, total)))

    for table in ("chunks_fts", "chunks_fts_tri"):
        try:
            n = conn.execute(f"SELECT count(*) c FROM {table}").fetchone()["c"]
            hit = conn.execute(
                f"""SELECT count(*) c FROM {table} t
                    JOIN residue r ON r.id = t.rowid""").fetchone()["c"]
            print("%-16s 总 %8d  残留 %8d  占 %s"
                  % (table, n, hit, pct(hit, n)))
        except sqlite3.Error as e:
            print("%-16s 读取失败: %s" % (table, e))

    vec_total, vec_residue = _vec_shares(conn)
    if vec_total is None:
        print("chunks_vec       影子表不可读，跳过")
    else:
        print("chunks_vec       总 %8d  残留 %8d  占 %s"
              % (vec_total, vec_residue, pct(vec_residue, vec_total)))
        _explain_vector_starvation(vec_total, vec_residue)

    conn.close()
    return 0


def _explain_vector_starvation(total: int, residue: int) -> None:
    """把残留占比换算成向量路的**有效深度**。

    上界估计，不是实测：这里假设残留向量与查询的距离分布和真资料相同。
    真实情况大概率没这么糟 —— 中文提问与英文 rust 文档在 bge-m3 空间里本就
    较远，残留未必挤进 top-k。所以这个数字只用来判断「值不值得实测」，
    结论必须由剪枝副本上的 65 题评测给出。
    """
    if not total:
        return
    p = residue / total
    print("  残留占比 p=%.3f；按 3 倍超取上限，向量路有效深度 ≈ %.2f×limit"
          % (p, 3 * (1 - p)))
    if 3 * (1 - p) < 1:
        print("  → 上界估计下，向量路已无法填满请求深度（<1×limit）。"
              "这是「先检索后过滤」的结构性后果，不是参数没调好。")


def _vec_shares(conn) -> tuple[int | None, int]:
    """从 vec0 影子表统计 rowid 分布。不加载扩展、不读向量。"""
    try:
        rows = conn.execute(
            """SELECT chunk_id, size, validity, rowids
                 FROM chunks_vec_chunks ORDER BY chunk_id""").fetchall()
    except sqlite3.Error as e:
        print("  (chunks_vec_chunks: %s)" % e)
        return None, 0
    ids: list[np.ndarray] = []
    for r in rows:
        size = int(r["size"] or 0)
        rowids, validity = r["rowids"], r["validity"]
        if size <= 0 or not isinstance(rowids, (bytes, bytearray)):
            continue
        if len(rowids) < size * 8:
            continue
        arr = np.frombuffer(rowids, dtype=np.int64)[:size]
        if isinstance(validity, (bytes, bytearray)) and len(validity) * 8 >= size:
            valid = np.unpackbits(
                np.frombuffer(validity, dtype=np.uint8), bitorder="little",
            )[:size].astype(bool)
            arr = arr[valid]
        ids.append(arr)
    if not ids:
        return 0, 0
    all_ids = np.concatenate(ids)
    residue = {r[0] for r in conn.execute("SELECT id FROM residue")}
    hit = int(sum(1 for i in all_ids.tolist() if i in residue))
    return int(all_ids.size), hit


if __name__ == "__main__":
    raise SystemExit(main())
