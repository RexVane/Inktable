"""向量索引 —— sqlite-vec（PLAN §12.2 ④、§4.3）。

向量与 chunks、FTS5 在**同一个 SQLite 文件、同一个事务**里。
这是选 sqlite-vec 而非 LanceDB 的核心理由：跨库方案要自己实现两阶段提交，
而这里三表同步天然原子 —— 索引不会出现"搜得到但打不开"的悬挂状态。

检索策略随库规模切换（个人电脑场景，实测推算）：

    ≤ 10 万分片   全量载入内存做矩阵乘  ← 98 MB，最快
    > 10 万分片   走 sqlite-vec 的 KNN  ← 按需读磁盘，不全量载入

暴力矩阵乘在小库上比 sqlite-vec 的 KNN 更快（无 SQL 开销），
但内存随库线性增长，所以设了切换阈值。
"""

from __future__ import annotations

import logging
import sqlite3
import struct

import numpy as np

from app.index.embedding import DIM

log = logging.getLogger("inktable.vector")

# 超过这个分片数就不再全量载入内存
INMEM_LIMIT = 100_000


def ensure_schema(conn: sqlite3.Connection, dim: int = DIM) -> bool:
    """建向量表。sqlite-vec 不可用时返回 False，调用方降级为纯 FTS5。"""
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as e:
        log.warning("sqlite-vec 不可用，语义检索关闭：%s", e)
        return False

    conn.execute(
        f"""CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
                embedding float[{dim}]
            )"""
    )
    return True


def load_extension(conn: sqlite3.Connection) -> bool:
    """给已有连接加载扩展。每个新连接都要调用一次 —— 扩展不随库持久化。"""
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def _serialize(v: np.ndarray) -> bytes:
    """float32 向量 → sqlite-vec 的二进制格式。"""
    return struct.pack(f"{len(v)}f", *v.astype(np.float32))


def upsert(conn: sqlite3.Connection, rows: list[tuple[int, np.ndarray]]) -> int:
    """写入向量。rowid 对齐 chunks.id。

    先删后插 —— vec0 表不支持 UPSERT，重复 rowid 会报约束冲突。
    """
    if not rows:
        return 0
    ids = [cid for cid, _ in rows]
    marks = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({marks})", ids)
    conn.executemany(
        "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
        [(cid, _serialize(v)) for cid, v in rows],
    )
    return len(rows)


def delete(conn: sqlite3.Connection, chunk_ids: list[int]) -> None:
    if not chunk_ids:
        return
    marks = ",".join("?" * len(chunk_ids))
    conn.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({marks})", chunk_ids)


def count(conn: sqlite3.Connection) -> int:
    try:
        return conn.execute("SELECT count(*) c FROM chunks_vec").fetchone()["c"]
    except sqlite3.Error:
        return 0


def search(
    conn: sqlite3.Connection,
    query_vec: np.ndarray,
    limit: int = 50,
    candidate_ids: list[int] | None = None,
) -> list[tuple[int, float]]:
    """向量检索，返回 [(chunk_id, 余弦相似度)]，分数越大越相关。

    candidate_ids 用于「先过滤后检索」（§12.3b ③）：
    元数据过滤后只在子集里搜，避免召回被无关文件占满。

    小库走内存矩阵乘，大库走 sqlite-vec KNN —— 见模块 docstring。
    """
    q = np.asarray(query_vec, dtype=np.float32)
    q = q / max(float(np.linalg.norm(q)), 1e-12)

    total = count(conn)
    if total == 0:
        return []

    if total <= INMEM_LIMIT:
        return _search_inmem(conn, q, limit, candidate_ids)
    return _search_knn(conn, q, limit, candidate_ids)


def _search_inmem(conn, q: np.ndarray, limit: int, candidate_ids) -> list[tuple[int, float]]:
    """全量载入做矩阵乘。10 万分片约 98 MB，个人电脑可承受。"""
    sql = "SELECT rowid, embedding FROM chunks_vec"
    params: list = []
    if candidate_ids:
        marks = ",".join("?" * len(candidate_ids))
        sql += f" WHERE rowid IN ({marks})"
        params = list(candidate_ids)

    ids: list[int] = []
    vecs: list[np.ndarray] = []
    for row in conn.execute(sql, params):
        ids.append(row[0])
        vecs.append(np.frombuffer(row[1], dtype=np.float32))
    if not ids:
        return []

    M = np.vstack(vecs)
    # 库里存的已是归一化向量，点积即余弦
    scores = M @ q
    k = min(limit, len(ids))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(ids[i], float(scores[i])) for i in top]


def _search_knn(conn, q: np.ndarray, limit: int, candidate_ids) -> list[tuple[int, float]]:
    """走 sqlite-vec 的 KNN，不全量载入内存。"""
    sql = (
        "SELECT rowid, distance FROM chunks_vec "
        "WHERE embedding MATCH ? AND k = ?"
    )
    params: list = [_serialize(q), limit]
    if candidate_ids:
        marks = ",".join("?" * len(candidate_ids))
        sql += f" AND rowid IN ({marks})"
        params += list(candidate_ids)

    try:
        rows = conn.execute(sql + " ORDER BY distance", params).fetchall()
    except sqlite3.Error as e:
        log.warning("向量 KNN 失败：%s", e)
        return []
    # sqlite-vec 返回 L2 距离；归一化向量下 cos = 1 - d²/2
    return [(r[0], 1.0 - (r[1] ** 2) / 2.0) for r in rows]
