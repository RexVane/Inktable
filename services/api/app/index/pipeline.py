"""索引流水线 —— 解析 → 分片 → 写 FTS5（PLAN §12.2）。

**索引挂在 content 上，不挂 file**（§9 contents 表）：
同一份内容被存了 5 处，只解析一次、只建一次索引。

失败处理原则：单个文档失败只标记该 content，不中断整批。
`parse_state` 记录结果，UI 据此显示"3 个文档解析失败"而不是静默丢弃。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from itertools import count
from pathlib import Path

from app.index.search import index_chunk
from app.index.hierarchy import build_hierarchy, insert_hierarchy
from app.parsing.base import ParseError, UnsupportedFormat
from app.parsing.chunker import chunk_document
from app.parsing.parsers import PARSERS, parse


def indexable_exts() -> set[str]:
    return set(PARSERS.keys())


# 单文档全文索引上限。超过的只登记元数据，不解析正文。
#
# 实测触发：~/Documents 下一个 339MB 的 all_files.txt（机器生成的文件清单）
# 独自产生 19847 个分片，占全库 54%。这类日志/清单/导出文件没人会用
# 自然语言去搜，却会淹没检索结果、拖慢索引、撑大数据库。
MAX_INDEX_BYTES = 10 * 1024 * 1024

_savepoint_ids = count()


class _HandledParseFailure(Exception):
    """只包装 parse() 本身的预期失败，避免误吞后续索引阶段异常。"""

    def __init__(self, state: str, error: Exception):
        super().__init__(str(error))
        self.state = state
        self.error = error


@contextmanager
def _content_savepoint(conn: sqlite3.Connection):
    """原子替换一个 content 的全部索引，同时保留调用方事务边界。

    SQLite 在事务外执行 SAVEPOINT，并 RELEASE 最外层 savepoint 时会直接提交。
    index_content() 的历史契约则是「只写入，commit 由批处理/请求层决定」。因此
    连接当前没有事务时先显式 BEGIN：成功后仍保持未提交；失败时仅回滚本函数
    创建的空外层事务。调用方已有事务时只 ROLLBACK TO 本 content 的 savepoint，
    调用方在进入本函数前的写入不会被回滚。
    """
    name = f"inktable_content_{next(_savepoint_ids)}"
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            conn.execute(f"RELEASE SAVEPOINT {name}")
        finally:
            if owns_transaction and conn.in_transaction:
                conn.rollback()
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {name}")


def index_content(conn: sqlite3.Connection, content_id: int, path: Path) -> dict:
    """解析并索引单个 content。返回结果摘要。"""
    version: int | None = None
    try:
        if path.stat().st_size > MAX_INDEX_BYTES:
            conn.execute(
                "UPDATE contents SET parse_state = 'too_large' WHERE id = ?",
                (content_id,),
            )
            return {"state": "too_large", "chunks": 0}
    except OSError:
        pass

    try:
        with _content_savepoint(conn):
            try:
                doc = parse(path)
            except UnsupportedFormat as e:
                raise _HandledParseFailure("unsupported", e) from e
            except (ParseError, OSError) as e:
                raise _HandledParseFailure("parse_failed", e) from e
            chunks = chunk_document(doc)
            version = _next_index_version(conn, content_id)

            if not chunks:
                # 扫描件等无文本内容的文档：不是失败，但也没东西可索引。
                # 旧索引同样在 savepoint 内清理，避免状态已是 no_text 但仍能
                # 搜到上一次解析留下的正文。
                conn.execute(
                    """INSERT INTO index_versions
                       (content_id, version, status, section_count, chunk_count,
                        created_at, activated_at)
                       VALUES (?, ?, 'active', 0, 0, ?, ?)""",
                    (content_id, version, time.time(), time.time()),
                )
                conn.execute(
                    "UPDATE index_versions SET status = 'superseded' "
                    "WHERE content_id = ? AND version <> ? AND status = 'active'",
                    (content_id, version),
                )
                conn.execute(
                    "UPDATE contents SET parse_state = 'no_text', chunk_count = 0, "
                    "embedding_model_id = NULL, active_index_version = ?, "
                    "indexed_at = ? WHERE id = ?",
                    (version, time.time(), content_id),
                )
                result = {
                    "state": "no_text", "chunks": 0, "warnings": doc.warnings,
                }
            else:
                hierarchy = build_hierarchy(doc, chunks, path.name)
                conn.execute(
                    """INSERT INTO index_versions
                       (content_id, version, status, document_hash,
                        section_count, chunk_count, created_at)
                       VALUES (?, ?, 'building', ?, ?, ?, ?)""",
                    (content_id, version, hierarchy.text_hash,
                     len(hierarchy.sections), len(chunks), time.time()),
                )
                _document_id, section_ids = insert_hierarchy(
                    conn, content_id, version, hierarchy,
                )

                inserted: list[tuple[int, object]] = []  # (chunk_id, Chunk) 供向量编码
                for c in chunks:
                    start_offset, end_offset = hierarchy.chunk_offsets[c.ordinal]
                    cur = conn.execute(
                        "INSERT INTO chunks (content_id, layer, page, section_path, ordinal, "
                        "text, text_hash, token_count, bbox, section_id, start_offset, "
                        "end_offset, index_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            content_id,
                            "child",
                            c.locator.page,
                            c.section_path,
                            c.ordinal,
                            c.text,
                            c.text_hash,
                            len(c.text),
                            json.dumps(c.locator.to_dict(), ensure_ascii=False),
                            section_ids[hierarchy.chunk_sections[c.ordinal]],
                            start_offset,
                            end_offset,
                            version,
                        ),
                    )
                    index_chunk(conn, cur.lastrowid, c.text, c.section_path)
                    inserted.append((cur.lastrowid, c))

                # 模型/向量表明确不可用时允许纯 FTS 降级；一旦开始向量写入，
                # 任何异常必须冒泡，由 savepoint 回滚 chunks/FTS/vector 三者。
                embedded = _embed_chunks(conn, inserted)

                activated_at = time.time()
                conn.execute(
                    "UPDATE index_versions SET status = 'superseded' "
                    "WHERE content_id = ? AND status = 'active'",
                    (content_id,),
                )
                conn.execute(
                    """UPDATE index_versions SET status = 'active', activated_at = ?
                       WHERE content_id = ? AND version = ?""",
                    (activated_at, content_id, version),
                )
                conn.execute(
                    "UPDATE contents SET parse_state = 'indexed', chunk_count = ?, "
                    "embedding_model_id = ?, active_index_version = ?, indexed_at = ? "
                    "WHERE id = ?",
                    (len(chunks), embedded, version, activated_at, content_id),
                )
                result = {
                    "state": "indexed", "chunks": len(chunks),
                    "warnings": doc.warnings,
                }
    except _HandledParseFailure as failure:
        conn.execute(
            "UPDATE contents SET parse_state = ? WHERE id = ?",
            (failure.state, content_id),
        )
        result = {"state": failure.state, "chunks": 0}
        if failure.state == "parse_failed":
            result["error"] = str(failure.error)
        return result
    except Exception as failure:
        if version is not None:
            conn.execute(
                """INSERT INTO index_versions
                   (content_id, version, status, error, created_at)
                   VALUES (?, ?, 'failed', ?, ?)
                   ON CONFLICT(content_id, version) DO UPDATE SET
                     status = 'failed', error = excluded.error""",
                (content_id, version, str(failure)[:1000], time.time()),
            )
        raise
    return result


def _next_index_version(conn: sqlite3.Connection, content_id: int) -> int:
    row = conn.execute(
        "SELECT max(version) AS version FROM index_versions WHERE content_id = ?",
        (content_id,),
    ).fetchone()
    return int(row["version"] or 0) + 1


def activate_index_version(
    conn: sqlite3.Connection, content_id: int, version: int,
) -> dict:
    """Atomically activate a complete generation, including rollback targets."""
    with _content_savepoint(conn):
        target = conn.execute(
            """SELECT status, document_hash, section_count, chunk_count
               FROM index_versions WHERE content_id = ? AND version = ?""",
            (content_id, version),
        ).fetchone()
        if target is None:
            raise ValueError("索引版本不存在")
        if target["status"] not in {"active", "superseded"}:
            raise ValueError("只能激活已完成的索引版本")

        actual_chunks = conn.execute(
            "SELECT count(*) FROM chunks WHERE content_id = ? AND index_version = ?",
            (content_id, version),
        ).fetchone()[0]
        actual_sections = conn.execute(
            "SELECT count(*) FROM sections WHERE content_id = ? AND index_version = ?",
            (content_id, version),
        ).fetchone()[0]
        if (actual_chunks != target["chunk_count"] or
                actual_sections != target["section_count"]):
            raise RuntimeError("索引版本不完整，拒绝激活")
        if actual_chunks and conn.execute(
            """SELECT 1 FROM document_representations
               WHERE content_id = ? AND index_version = ?""",
            (content_id, version),
        ).fetchone() is None:
            raise RuntimeError("索引版本缺少 Document 表示，拒绝激活")

        previous = conn.execute(
            "SELECT active_index_version FROM contents WHERE id = ?",
            (content_id,),
        ).fetchone()
        if previous is None:
            raise ValueError("内容不存在")
        activated_at = time.time()
        conn.execute(
            "UPDATE index_versions SET status = 'superseded' "
            "WHERE content_id = ? AND status = 'active'",
            (content_id,),
        )
        conn.execute(
            """UPDATE index_versions SET status = 'active', activated_at = ?
               WHERE content_id = ? AND version = ?""",
            (activated_at, content_id, version),
        )
        model = conn.execute(
            """SELECT max(embedding_model_id) FROM chunks
               WHERE content_id = ? AND index_version = ?""",
            (content_id, version),
        ).fetchone()[0]
        conn.execute(
            """UPDATE contents SET active_index_version = ?, chunk_count = ?,
               embedding_model_id = ?, parse_state = ?, indexed_at = ?
               WHERE id = ?""",
            (version, actual_chunks, model,
             "indexed" if actual_chunks else "no_text", activated_at, content_id),
        )
    return {
        "content_id": content_id,
        "previous_version": previous["active_index_version"],
        "active_version": version,
        "chunks": actual_chunks,
        "sections": actual_sections,
    }


def _delete_content_indexes(conn: sqlite3.Connection, content_id: int) -> None:
    """删除一个 content 的 chunks、FTS 与向量；调用方负责事务边界。"""
    old = conn.execute(
        "SELECT id FROM chunks WHERE content_id = ?", (content_id,)
    ).fetchall()
    if old:
        ids = [r["id"] for r in old]
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", ids)
        conn.execute(f"DELETE FROM chunks_fts_tri WHERE rowid IN ({marks})", ids)
        if _vector_table_exists(conn):
            conn.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({marks})", ids)
        conn.execute("DELETE FROM chunks WHERE content_id = ?", (content_id,))

    section_ids = [row["id"] for row in conn.execute(
        "SELECT id FROM sections WHERE content_id = ?", (content_id,),
    )]
    if section_ids:
        marks = ",".join("?" * len(section_ids))
        conn.execute(f"DELETE FROM sections_fts WHERE rowid IN ({marks})", section_ids)
    document_ids = [row["id"] for row in conn.execute(
        "SELECT id FROM document_representations WHERE content_id = ?", (content_id,),
    )]
    if document_ids:
        marks = ",".join("?" * len(document_ids))
        conn.execute(f"DELETE FROM documents_fts WHERE rowid IN ({marks})", document_ids)
    conn.execute("DELETE FROM sections WHERE content_id = ?", (content_id,))
    conn.execute(
        "DELETE FROM document_representations WHERE content_id = ?", (content_id,)
    )
    conn.execute("DELETE FROM index_versions WHERE content_id = ?", (content_id,))


def _vector_table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'chunks_vec'"
    ).fetchone() is not None


def _embed_chunks(conn, inserted: list[tuple[int, object]]) -> str | None:
    """给刚入库的分片编码向量。返回模型 id（未编码则 None）。

    **按 text_hash 内容寻址复用**（§12.5 的落地形态）：

    编辑文件会产生新 sha256 → 新 content → 全部分片都是"新"的。
    若无脑全量编码，改 200 页合同的一条条款就要重嵌 200 页 ——
    方案明确说这决定产品在"文件被反复编辑"场景下是否可用。

    这里不追踪"旧版本是谁"（内容寻址模型里没有版本链），而是反过来：
    每个新分片先查**全库**有没有同 text_hash 且已有向量的旧片，有就直接
    SQL 复制向量行，只有真正没见过的文字才进编码器。
    副作用是纯赚的：两个不同文件的相同段落也自动共享向量。

    注：向量编码时前置了 section_path（§12.2 ③），复用忽略前缀差异 ——
    这是方案 §12.5 的明确取舍："仅位置变的片只更新 section_path"，
    标题重编号带来的轻微语义漂移是刻意接受的，换来免重嵌。

    编码在这里内联做而不是丢队列：model2vec 约 9000 片/秒，
    单文档几十片的开销是毫秒级，不值得为它引入异步复杂度。
    """
    if not inserted:
        return None
    from app.index import embedding as emb

    # 模型文件或向量扩展明确不存在时是受支持的纯 FTS 模式；表存在但写入失败
    # 则属于索引失败，必须向上抛出，不能留下只有部分 chunk 有向量的状态。
    if not emb.is_available() or not _vector_table_exists(conn):
        return None
    m = emb.get_embedder()
    model_id = m.model_id

    min_new = min(cid for cid, _ in inserted)
    need: list[tuple[int, object]] = []
    batch_first: dict[str, int] = {}   # 批内去重：同文字只编一次
    pending_copy: list[tuple[int, int]] = []  # (新id, 供体id/批内首见id)
    reused = 0

    for cid, c in inserted:
        if c.text_hash in batch_first:
            pending_copy.append((cid, batch_first[c.text_hash]))
            continue
        donor = conn.execute(
            "SELECT id FROM chunks WHERE text_hash = ? AND id < ? "
            "AND embedding_model_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (c.text_hash, min_new, model_id),
        ).fetchone()
        copied = 0
        if donor:
            copied = conn.execute(
                "INSERT INTO chunks_vec(rowid, embedding) "
                "SELECT ?, embedding FROM chunks_vec WHERE rowid = ?",
                (cid, donor["id"]),
            ).rowcount
        if copied:
            reused += 1
            batch_first[c.text_hash] = cid
        else:
            need.append((cid, c))
            batch_first[c.text_hash] = cid

    if need:
        texts = [emb.embed_text_for(c.text, c.section_path) for _, c in need]
        vectors = m.encode(texts)
        if len(vectors) != len(need):
            raise RuntimeError(
                f"嵌入模型返回 {len(vectors)} 条向量，预期 {len(need)} 条"
            )
        from app.index import vector as vec

        vec.upsert(conn, [(cid, v) for (cid, _), v in zip(need, vectors)])

    for cid, first_id in pending_copy:
        copied = conn.execute(
            "INSERT INTO chunks_vec(rowid, embedding) "
            "SELECT ?, embedding FROM chunks_vec WHERE rowid = ?",
            (cid, first_id),
        ).rowcount
        if copied != 1:
            raise RuntimeError(f"复制分片 {first_id} 的向量到 {cid} 失败")

    ids = [cid for cid, _ in inserted]
    marks = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE chunks SET embedding_model_id = ? WHERE id IN ({marks})",
        [model_id, *ids],
    )

    if reused:
        logging.getLogger("inktable.pipeline").info(
            "向量复用 %d 片，仅编码 %d 片", reused, len(need))
    return model_id


def cleanup_orphan_contents(conn: sqlite3.Connection) -> int:
    """清理没有任何文件指向的 content 及其索引。

    编辑文件 → 新 sha256 → 新 content，旧 content 随即成为孤儿。
    不清会让 chunks/FTS/向量三处滞留旧版内容 —— 搜索命中已不存在的段落。

    **必须放在索引批之后**：新 content 的向量复用要从旧 content 抄，
    先清就抄不到了（清早了只损失复用、不损失正确性，但没必要）。
    """
    orphans = conn.execute(
        """SELECT id FROM contents
           WHERE id NOT IN (SELECT content_id FROM files
                            WHERE content_id IS NOT NULL)"""
    ).fetchall()
    for o in orphans:
        # 每个孤儿 content 也独立原子清理；向量删除失败时保留它的 chunks/FTS，
        # 不制造「主表已删、向量还在」的悬挂条目。
        with _content_savepoint(conn):
            _delete_content_indexes(conn, o["id"])
            conn.execute("DELETE FROM contents WHERE id = ?", (o["id"],))
    return len(orphans)


def _readable_index_paths(conn: sqlite3.Connection, content_id: int,
                          exts: set[str]) -> list[Path]:
    """返回一个 content 当前可用于解析的全部副本。

    content 是按哈希共享的，同一内容可能同时叫 ``a.bin``、``b.txt``，也可能
    有一条尚未被 watcher 标成 missing 的陈旧路径。不能让排序最靠前的坏副本
    遮蔽后面的健康副本，因此保留全部 parser 支持的候选并逐个验证可读性。
    """
    marks = ",".join("?" * len(exts))
    # 原件可能在易失来源中已被清理，但保全副本仍是同一份内容。先尝试
    # 原路径，原路径不可读时再尝试 preserved_path；副本也必须经过实际
    # open 检查，避免把一个陈旧数据库路径交给解析器。
    replica_rows = conn.execute(
        f"""SELECT path, preserved_path FROM files
            WHERE content_id = ?
              AND lower(ext) IN ({marks})
              AND (state != 'missing' OR COALESCE(preserved_path, '') != '')
            ORDER BY id""",
        [content_id, *exts],
    ).fetchall()

    readable: list[Path] = []
    seen: set[str] = set()
    for row in replica_rows:
        candidates = [row["path"], row["preserved_path"]]
        for raw in candidates:
            if not raw or raw in seen:
                continue
            seen.add(raw)
            path = Path(raw)
            try:
                if not path.is_file():
                    continue
                # exists()/is_file() 不能证明当前进程有读取权限；实际 open 一次，
                # 同时把 watcher 尚未收敛的权限变化挡在解析器外面。
                with path.open("rb"):
                    pass
            except OSError:
                continue
            readable.append(path)
    return readable


def count_readable_pending(conn: sqlite3.Connection) -> int:
    """统计当前确实有可解析副本的 pending 内容。

    ``contents.parse_state`` 只描述逻辑状态，不能单独说明索引队列是否
    真能推进：原文件可能已经消失，但 ``preserved_path`` 仍可用；也可能
    两条副本路径都已失效。UI 若只按状态计数，会把不可处理的项目显示成
    永远卡住，或反过来把可由保全副本恢复的项目隐藏掉。

    这里复用 ``_readable_index_paths`` 的实际 ``is_file + open`` 检查，和
    ``index_pending`` 使用完全相同的可读性判据。返回的是 content 数而非
    file 数，同一份去重内容的多个副本只占一个队列槽位。
    """
    exts = indexable_exts()
    marks = ",".join("?" * len(exts))
    rows = conn.execute(
        f"""SELECT c.id
            FROM contents c
            WHERE c.parse_state = 'pending'
              AND EXISTS (
                  SELECT 1 FROM files f
                  WHERE f.content_id = c.id
                    AND (f.state != 'missing' OR COALESCE(f.preserved_path, '') != '')
                    AND lower(f.ext) IN ({marks})
              )
            ORDER BY c.id""",
        list(exts),
    ).fetchall()
    return sum(1 for row in rows if _readable_index_paths(conn, row["id"], exts))


def index_pending(conn: sqlite3.Connection, limit: int = 500, progress=None) -> dict:
    """索引所有待处理的 content。

    只处理有对应文件、且扩展名可解析的 —— 源码类已在扫描阶段降级为
    仅元数据（§M2 决策），不会进到这里。
    """
    exts = indexable_exts()
    marks = ",".join("?" * len(exts))

    # 先把当前没有任何可解析副本的内容一次性出队（源码、媒体、压缩包等）。
    # missing 原件若有保全副本仍然可解析，不能被提前标成 unsupported。
    # 同一 content 可被不同扩展名的多个 file 共享，不能用 MIN(ext) 代表整组。
    conn.execute(
        f"""UPDATE contents AS c SET parse_state = 'unsupported'
            WHERE c.parse_state = 'pending'
              AND EXISTS (
                  SELECT 1 FROM files f
                  WHERE f.content_id = c.id
                    AND (f.state != 'missing' OR COALESCE(f.preserved_path, '') != '')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM files f
                  WHERE f.content_id = c.id
                    AND (f.state != 'missing' OR COALESCE(f.preserved_path, '') != '')
                    AND lower(f.ext) IN ({marks})
              )""",
        list(exts),
    )
    conn.commit()

    # LIMIT 仍按 content 计，而不是按副本计。已经标成 unsupported 的 content
    # 若后来出现了可解析副本，也应被重新拾起（例如先看到 .bin，后出现 .txt）。
    rows = conn.execute(
        f"""SELECT c.id FROM contents c
            WHERE c.parse_state IN ('pending', 'unsupported')
              AND EXISTS (
                  SELECT 1 FROM files f
                  WHERE f.content_id = c.id
                    AND (f.state != 'missing' OR COALESCE(f.preserved_path, '') != '')
                    AND lower(f.ext) IN ({marks})
              )
            ORDER BY c.id
            LIMIT ?""",
        [*exts, limit],
    ).fetchall()

    summary = {"indexed": 0, "chunks": 0, "no_text": 0, "failed": 0,
               "unsupported": 0, "too_large": 0}

    for i, row in enumerate(rows):
        paths = _readable_index_paths(conn, row["id"], exts)
        if not paths:
            conn.execute(
                "UPDATE contents SET parse_state = 'missing_file' WHERE id = ?",
                (row["id"],),
            )
            continue

        try:
            # 文件可能在可读性检查后消失，解析器也可能只对某个副本失败。
            # 预期解析失败时继续试同 content 的下一副本；FTS/向量等索引阶段
            # 异常仍立即冒泡到批处理边界，不能靠换路径掩盖真实写入故障。
            for p in paths:
                result = index_content(conn, row["id"], p)
                if result["state"] not in {"parse_failed", "unsupported"}:
                    break
        except Exception as e:  # 单文档已由 savepoint 回滚；批处理继续其余文档
            logging.getLogger("inktable.pipeline").exception(
                "索引 content_id=%s 失败，已回滚该文档：%s", row["id"], e
            )
            conn.execute(
                "UPDATE contents SET parse_state = 'index_failed' WHERE id = ?",
                (row["id"],),
            )
            summary["failed"] += 1
        else:
            state = result["state"]
            if state == "indexed":
                summary["indexed"] += 1
                summary["chunks"] += result["chunks"]
            elif state in summary:
                summary[state] += 1
            else:
                summary["failed"] += 1

        if (i + 1) % 20 == 0:
            conn.commit()
            if progress:
                progress(i + 1, len(rows))

    # 孤儿清扫必须在索引批之后（向量复用要从旧 content 抄，见函数注释）
    summary["orphans_cleaned"] = cleanup_orphan_contents(conn)
    conn.commit()
    summary["total"] = len(rows)
    return summary
