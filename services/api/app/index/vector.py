"""向量索引 —— sqlite-vec（PLAN §12.2 ④、§4.3）。

向量与 chunks、FTS5 在**同一个 SQLite 文件、同一个事务**里。
这是选 sqlite-vec 而非 LanceDB 的核心理由：跨库方案要自己实现两阶段提交，
而这里三表同步天然原子 —— 索引不会出现"搜得到但打不开"的悬挂状态。

主路径走进程内矩阵乘。逐行从 vec0 取向量确实慢得无法用于查询路径
（71378 条实测 369.7 秒，约 5ms/行，换任何 SELECT 写法都一样），但向量
并不是逐行存的：sqlite-vec 把每 1024 条打包成一个 blob 放在影子表
`<表名>_vector_chunks00` 里。整块读 70 个 blob 重建同一个矩阵只要 1.1 秒，
之后每次查询 13ms，而 vec0 KNN 每次 780-980ms。

影子表是 sqlite-vec 的内部实现，所以这条路全程校验（表存在、blob 尺寸能
整除维度、validity 位图长度相符、重建条数与 vec0 count 一致），任一步不符
就退回 KNN。查询路径永远不会触发逐行慢导出。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import struct
import sys
import threading
import ctypes

import numpy as np

from app.index.embedding import DIM

log = logging.getLogger("ordo.vector")

# 旧版本用固定的 100k 行数阈值决定是否建矩阵。这个阈值在不同机器上
# 没有可比性：102,965 条在本机仍然只占约 402 MiB，而 148k 条在低内存
# 机器上可能触发分页。现在按可用内存和显式预算决定；保留这个名字只是
# 为了兼容外部诊断脚本，查询路径不再把它当硬上限。
INMEM_LIMIT = 100_000
_CACHE_DEFAULT_CAP_BYTES = 1024 * 1024 * 1024
# The full 148k-row matrix is about 580 MiB resident and needs roughly 760 MiB
# during construction. Keep a modest headroom so a 16 GiB desktop can warm the
# cache while Ollama is resident; callers can raise this with the environment
# variable on memory-constrained machines.
_CACHE_DEFAULT_RESERVE_BYTES = 256 * 1024 * 1024
_CACHE_BUILD_OVERHEAD = 1.25
_CACHE_BUILD_FIXED_BYTES = 32 * 1024 * 1024


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


# ---- 内存矩阵缓存 ----
# 30k 分片实测：每次查询重新反序列化全部向量要 ~240ms，矩阵乘本身 <5ms。
# 单实例锁（H18）保证本进程是唯一写者，所以写路径主动失效即可保证一致；
# 键上再叠加 (库路径, 行数, 最大 rowid)，防守同进程内多个测试库的串扰。
_matrix_cache: dict = {"key": None, "ids": None, "matrix": None}
_write_generation = 0
_matrix_build_lock = threading.Lock()


def _invalidate_cache() -> None:
    global _write_generation
    _write_generation += 1
    _matrix_cache["key"] = None


def invalidate_cache() -> None:
    """Invalidate the process cache after a direct chunks_vec rebuild/copy."""
    _invalidate_cache()


def _main_db_path(conn: sqlite3.Connection) -> str:
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main":
                return row[2] or ":memory:"
    except sqlite3.Error:
        pass
    return "?"


def _available_memory_bytes() -> int | None:
    """Return an approximate amount of currently available physical memory.

    This deliberately has no third-party dependency: the API is also bundled
    into the frozen sidecar.  A missing/failed probe means "unknown", in which
    case the explicit cache cap remains the safety guard.
    """
    if sys.platform == "win32":
        class _Status(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_uint32),
                ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        status = _Status()
        status.dwLength = ctypes.sizeof(_Status)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys)
        except (AttributeError, OSError):
            pass
        return None

    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return int(pages * page_size)
    except (AttributeError, OSError, ValueError):
        pass
    return None


def _env_bytes(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(float(raw) * 1024 * 1024))
    except ValueError:
        return default


def _matrix_budget_allows(rows: int, dim: int = DIM) -> bool:
    """Decide whether a full matrix is safe to build on this machine."""
    if rows <= 0 or _matrix_disabled():
        return False
    cap = _env_bytes("ORDO_VECTOR_CACHE_MB", _CACHE_DEFAULT_CAP_BYTES)
    if cap == 0:
        return False
    steady = rows * (dim * 4 + 8)  # float32 matrix + int64 row ids
    required = int(steady * _CACHE_BUILD_OVERHEAD + _CACHE_BUILD_FIXED_BYTES)
    if required > cap:
        return False
    available = _available_memory_bytes()
    reserve = _env_bytes(
        "ORDO_VECTOR_CACHE_RESERVE_MB",
        _CACHE_DEFAULT_RESERVE_BYTES,
    )
    if available is not None and required > max(0, available - reserve):
        return False
    return True


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
    _invalidate_cache()
    return len(rows)


def delete(conn: sqlite3.Connection, chunk_ids: list[int]) -> None:
    if not chunk_ids:
        return
    marks = ",".join("?" * len(chunk_ids))
    conn.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({marks})", chunk_ids)
    _invalidate_cache()


def count(conn: sqlite3.Connection) -> int:
    try:
        return conn.execute("SELECT count(*) c FROM chunks_vec").fetchone()["c"]
    except sqlite3.Error:
        return 0


# sqlite-vec vec0 的影子表布局（v0.1.x）：
#   <t>_chunks(chunk_id, size, validity BLOB, rowids BLOB)
#       size     该块的槽位数（末块可能不满）
#       validity 每槽 1 bit 的有效位图，小端
#       rowids   每槽 1 个 int64 的 rowid 数组
#   <t>_vector_chunks00(rowid=chunk_id, vectors BLOB)
#       该块全部槽位的向量按行紧密排列的 float32
_VEC_TABLE = "chunks_vec"


def _matrix_disabled() -> bool:
    """`ORDO_VECTOR_NO_MATRIX=1` 关掉矩阵路，强制走 vec0 KNN。

    用于两件事：验证两条路给出的检索结果一致（把它设上跑一遍评测，指标
    应当逐位相同），以及影子表布局出问题时的现场排查开关。
    """
    return os.environ.get("ORDO_VECTOR_NO_MATRIX", "").strip() in {"1", "true", "yes"}


def _shadow_bulk_vectors(conn: sqlite3.Connection, dim: int = DIM):
    """整块读影子表重建 (ids, matrix)。布局不符合预期时返回 None。

    只读且全程校验 —— 这里踩的是 sqlite-vec 的内部实现，升级扩展版本后
    布局若变化必须安全退化，而不是给出错位的向量。
    """
    try:
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
                (f"{_VEC_TABLE}_chunks", f"{_VEC_TABLE}_vector_chunks00"),
            )
        }
        if len(names) != 2:
            return None

        # Keep the count and both shadow tables in one snapshot. Iterate the
        # cursor directly so raw vector blobs are released block by block;
        # fetchall() would temporarily retain almost a second full matrix.
        own_snapshot = not conn.in_transaction
        if own_snapshot:
            conn.execute("BEGIN")
        try:
            expected = count(conn)
            if expected <= 0:
                return None
            stride = dim * 4  # float32
            ids = np.empty(expected, dtype=np.int64)
            matrix = np.empty((expected, dim), dtype=np.float32)
            filled = 0
            rows = conn.execute(
                f"""SELECT c.chunk_id, c.size, c.validity, c.rowids, v.vectors
                    FROM {_VEC_TABLE}_chunks c
                    JOIN {_VEC_TABLE}_vector_chunks00 v ON v.rowid = c.chunk_id
                    ORDER BY c.chunk_id"""
            )
            for chunk_id, size, validity, rowids, raw in rows:
                if raw is None or not isinstance(raw, (bytes, bytearray)):
                    return None
                size = int(size or 0)
                if size <= 0:
                    continue
                # Validate all three shadow payloads before copying a block.
                if len(raw) % stride or len(raw) // stride < size:
                    return None
                if not isinstance(rowids, (bytes, bytearray)) or len(rowids) < size * 8:
                    return None
                if not isinstance(validity, (bytes, bytearray)) or len(validity) * 8 < size:
                    return None
                vectors = np.frombuffer(raw, dtype=np.float32).reshape(-1, dim)[:size]
                row_ids = np.frombuffer(rowids, dtype=np.int64)[:size]
                valid = np.unpackbits(
                    np.frombuffer(validity, dtype=np.uint8), bitorder="little",
                )[:size].astype(bool)
                take = int(valid.sum())
                if take:
                    end = filled + take
                    if end > expected:
                        return None
                    ids[filled:end] = row_ids[valid]
                    matrix[filled:end] = vectors[valid]
                    filled = end

            # The reconstructed row count must agree with vec0's own count.
            if filled != expected:
                log.warning(
                    "影子表重建 %d 条与 chunks_vec 的 %d 条不一致，退回 KNN",
                    filled, expected,
                )
                return None
            return ids, matrix
        finally:
            if own_snapshot:
                conn.rollback()
    except sqlite3.Error as e:
        log.debug("影子表读取失败，退回逐行/KNN：%s", e)
        return None


def search(
    conn: sqlite3.Connection,
    query_vec: np.ndarray,
    limit: int = 50,
    candidate_ids: list[int] | None = None,
) -> list[tuple[int, float]]:
    """向量检索，返回 [(chunk_id, 余弦相似度)]，分数越大越相关。

    candidate_ids 用于「先过滤后检索」（§12.3b ③）：
    元数据过滤后只在子集里搜，避免召回被无关文件占满。

    主路径优先走内存矩阵；预算不足或影子表布局不兼容时退回 KNN。
    """
    q = np.asarray(query_vec, dtype=np.float32)
    q = q / max(float(np.linalg.norm(q)), 1e-12)

    total = count(conn)
    if total == 0:
        return []

    # Always ask the cache first. _cached_matrix() applies the memory budget
    # only when it must build; an already-resident matrix remains cheap to use
    # even though allocating it naturally reduced the reported free memory.
    hits = _search_inmem(conn, q, limit, candidate_ids)
    if hits is not None:
        return hits
    return _search_knn(conn, q, limit, candidate_ids)


def _load_inmem_matrix(conn, candidate_ids=None, *, allow_slow: bool = False):
    """Load vectors once so several query variants share the same matrix."""
    if candidate_ids:
        # 小候选集优先从整库缓存里切片 —— 逐行取 vec0 约 5ms/行，
        # 80 个候选就是 400ms，比整块重建全库还慢。Honor the same
        # INMEM_LIMIT as search/warmup: the old candidate path accidentally
        # rebuilt a 400+ MiB full matrix even when the explicit guard said not
        # to load one for libraries above 100k vectors.
        cached = _cached_matrix(conn)
        if cached is not None:
            ids_all, matrix_all = cached
            wanted = np.asarray(
                sorted({int(i) for i in candidate_ids}), dtype=np.int64,
            )
            positions = np.clip(
                np.searchsorted(ids_all, wanted), 0, len(ids_all) - 1,
            )
            hit = positions[ids_all[positions] == wanted]
            if len(hit) == 0:
                return None, None
            return ids_all[hit], matrix_all[hit]
        marks = ",".join("?" * len(candidate_ids))
        ids: list[int] = []
        vecs: list[np.ndarray] = []
        for row in conn.execute(
            f"SELECT rowid, embedding FROM chunks_vec WHERE rowid IN ({marks})",
            list(candidate_ids),
        ):
            ids.append(row[0])
            vecs.append(np.frombuffer(row[1], dtype=np.float32))
        if not ids:
            return None, None
        return np.asarray(ids), np.vstack(vecs)

    cached = _cached_matrix(conn, allow_slow=allow_slow)
    if cached is None:
        return None, None
    return cached


def _cached_matrix(conn, *, allow_slow: bool = False):
    """整库矩阵缓存。取不到且不允许慢路径时返回 None，由调用方退回 KNN。

    键必须每次现查 `count(*)` 与 `max(rowid)`，不能只依赖写代数：
    事务回滚会让实际行数变化而**不经过** upsert/delete，写代数不会递增；
    只用写代数做键会在回滚后交出一份与库不符的矩阵（index atomicity
    与 orphan cleanup 的用例正是守这一点）。`max(rowid)` 同时挡住同进程内
    多个 `:memory:` 测试库共用一个键的串扰。
    """
    stats = conn.execute(
        "SELECT count(*) AS c, coalesce(max(rowid), 0) AS m FROM chunks_vec"
    ).fetchone()
    data_version = conn.execute("PRAGMA data_version").fetchone()[0]
    key = (_main_db_path(conn), stats["c"], stats["m"], data_version,
           _write_generation)
    if _matrix_cache["key"] == key:
        return _matrix_cache["ids"], _matrix_cache["matrix"]

    if not _matrix_budget_allows(int(stats["c"])):
        return None
    # Single-flight: warmup and the first request must not build two 400–600MB
    # matrices concurrently. Double-check the key after waiting for the lock.
    with _matrix_build_lock:
        stats = conn.execute(
            "SELECT count(*) AS c, coalesce(max(rowid), 0) AS m FROM chunks_vec"
        ).fetchone()
        data_version = conn.execute("PRAGMA data_version").fetchone()[0]
        key = (_main_db_path(conn), stats["c"], stats["m"], data_version,
               _write_generation)
        if _matrix_cache["key"] == key:
            return _matrix_cache["ids"], _matrix_cache["matrix"]
        built = None if _matrix_disabled() else _shadow_bulk_vectors(conn)
        if built is None and allow_slow:
            ids_list: list[int] = []
            vecs: list[np.ndarray] = []
            for row in conn.execute("SELECT rowid, embedding FROM chunks_vec"):
                ids_list.append(row[0])
                vecs.append(np.frombuffer(row[1], dtype=np.float32))
            if ids_list:
                built = (np.asarray(ids_list), np.vstack(vecs))
        if built is None:
            # 查询路径绝不在这里做逐行导出：71378 条实测 369.7 秒。
            return None

        ids, matrix = built
        # sqlite-vec rowids normally arrive sorted by chunk block. Sort only
        # when the extension gives us a non-monotonic layout.
        if len(ids) > 1 and np.any(ids[1:] < ids[:-1]):
            order = np.argsort(ids, kind="stable")
            ids = ids[order]
            matrix = matrix[order]
        # Normalize in place to avoid retaining a second full-size matrix.
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix /= np.maximum(norms, 1e-12)
        _matrix_cache["ids"] = ids
        _matrix_cache["matrix"] = matrix
        _matrix_cache["key"] = key
        return ids, matrix


def warmup(conn: sqlite3.Connection) -> bool:
    """预热整库矩阵。首次约 1.1 秒（71k 条），放后台线程避免撞到首个查询。"""
    total = count(conn)
    if total == 0 or not _matrix_budget_allows(total):
        return False
    return _cached_matrix(conn) is not None


def vectors_for(conn: sqlite3.Connection, chunk_ids) -> dict[int, np.ndarray]:
    """按 chunk_id 取向量，优先走整库缓存切片（重排复用）。"""
    ids = [int(i) for i in chunk_ids]
    if not ids:
        return {}
    got_ids, matrix = _load_inmem_matrix(conn, ids)
    if got_ids is None:
        return {}
    return {int(got_ids[i]): matrix[i] for i in range(len(got_ids))}



def _top_rows(scores, id_array, limit):
    k = min(limit, len(id_array))
    if k <= 0:
        return []
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(int(id_array[i]), float(scores[i])) for i in top]


def _search_inmem(
    conn, q: np.ndarray, limit: int, candidate_ids,
) -> list[tuple[int, float]] | None:
    """矩阵乘检索。返回 None 表示矩阵不可用，调用方应退回 KNN。

    区分 None 与 [] 是这条路径的关键契约：把"矩阵取不到"误当成"没有结果"
    会让语义检索静默消失，而不是慢一点。
    """
    id_array, matrix = _load_inmem_matrix(conn, candidate_ids)
    if id_array is None:
        # 指定了候选集却一条向量都没取到，是真空结果；否则是缓存缺失
        return [] if candidate_ids else None
    if len(id_array) == 0:
        return []
    return _top_rows(matrix @ q, id_array, limit)


def search_many(
    conn: sqlite3.Connection,
    query_vecs: list[np.ndarray],
    limit: int = 50,
    candidate_ids: list[int] | None = None,
) -> list[list[tuple[int, float]]]:
    """Search several normalized query vectors in one matrix multiplication."""
    if not query_vecs:
        return []
    total = count(conn)
    if total == 0 or candidate_ids:
        return [search(conn, query, limit, candidate_ids) for query in query_vecs]
    id_array, matrix = _load_inmem_matrix(conn)
    if id_array is None or len(id_array) == 0:
        # 矩阵拿不到时逐条走 search()，由它退回 KNN —— 不能当作无结果
        return [search(conn, query, limit, candidate_ids) for query in query_vecs]
    q = np.asarray(query_vecs, dtype=np.float32)
    norms = np.linalg.norm(q, axis=1, keepdims=True)
    q = q / np.maximum(norms, 1e-12)
    scores = matrix @ q.T
    return [_top_rows(scores[:, index], id_array, limit)
            for index in range(len(query_vecs))]


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
