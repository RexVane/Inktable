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
from pathlib import Path

from app.index.search import index_chunk
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


def index_content(conn: sqlite3.Connection, content_id: int, path: Path) -> dict:
    """解析并索引单个 content。返回结果摘要。"""
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
        doc = parse(path)
    except UnsupportedFormat:
        conn.execute(
            "UPDATE contents SET parse_state = 'unsupported' WHERE id = ?", (content_id,)
        )
        return {"state": "unsupported", "chunks": 0}
    except (ParseError, OSError) as e:
        conn.execute(
            "UPDATE contents SET parse_state = 'parse_failed' WHERE id = ?", (content_id,)
        )
        return {"state": "parse_failed", "chunks": 0, "error": str(e)}

    chunks = chunk_document(doc)
    if not chunks:
        # 扫描件等无文本内容的文档：不是失败，但也没东西可索引
        conn.execute(
            "UPDATE contents SET parse_state = 'no_text', chunk_count = 0 WHERE id = ?",
            (content_id,),
        )
        return {"state": "no_text", "chunks": 0, "warnings": doc.warnings}

    # 重新索引前先清掉旧片（幂等，支持增量重建）。
    # FTS5 两索引与向量表都不随主表删除，漏一个就留悬挂条目。
    old = conn.execute(
        "SELECT id FROM chunks WHERE content_id = ?", (content_id,)
    ).fetchall()
    if old:
        ids = [r["id"] for r in old]
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", ids)
        conn.execute(f"DELETE FROM chunks_fts_tri WHERE rowid IN ({marks})", ids)
        try:
            conn.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({marks})", ids)
        except sqlite3.Error:
            pass  # 向量表不存在（扩展未加载）
        conn.execute("DELETE FROM chunks WHERE content_id = ?", (content_id,))

    inserted: list[tuple[int, object]] = []  # (chunk_id, Chunk) 供向量编码
    for c in chunks:
        cur = conn.execute(
            "INSERT INTO chunks (content_id, layer, page, section_path, ordinal, "
            "text, text_hash, token_count, bbox) VALUES (?,?,?,?,?,?,?,?,?)",
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
            ),
        )
        index_chunk(conn, cur.lastrowid, c.text, c.section_path)
        inserted.append((cur.lastrowid, c))

    # 向量与 chunks/FTS5 同事务写入（§12.2 ④ 三表原子）。
    # 模型不可用时静默跳过 —— 语义检索是增强而非依赖（§16.1a 降级链）。
    embedded = _embed_chunks(conn, inserted)

    conn.execute(
        "UPDATE contents SET parse_state = 'indexed', chunk_count = ?, "
        "embedding_model_id = ?, indexed_at = ? WHERE id = ?",
        (len(chunks), embedded, time.time(), content_id),
    )
    return {"state": "indexed", "chunks": len(chunks), "warnings": doc.warnings}


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
    try:
        from app.index import embedding as emb

        if not emb.is_available():
            return None
        m = emb.get_embedder()

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
                "ORDER BY id DESC LIMIT 1",
                (c.text_hash, min_new),
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
            from app.index import vector as vec

            vec.upsert(conn, [(cid, v) for (cid, _), v in zip(need, vectors)])

        for cid, first_id in pending_copy:
            conn.execute(
                "INSERT INTO chunks_vec(rowid, embedding) "
                "SELECT ?, embedding FROM chunks_vec WHERE rowid = ?",
                (cid, first_id),
            )

        if reused:
            logging.getLogger("inktable.pipeline").info(
                "向量复用 %d 片，仅编码 %d 片", reused, len(need))
        return m.model_id
    except Exception:  # noqa: BLE001 - 向量失败绝不拖垮索引主流程
        logging.getLogger("inktable.pipeline").exception("向量编码失败，已跳过")
        return None


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
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM chunks WHERE content_id = ?", (o["id"],)
        )]
        if ids:
            marks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", ids)
            conn.execute(f"DELETE FROM chunks_fts_tri WHERE rowid IN ({marks})", ids)
            try:
                conn.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({marks})", ids)
            except sqlite3.Error:
                pass
            conn.execute("DELETE FROM chunks WHERE content_id = ?", (o["id"],))
        conn.execute("DELETE FROM contents WHERE id = ?", (o["id"],))
    return len(orphans)


def index_pending(conn: sqlite3.Connection, limit: int = 500, progress=None) -> dict:
    """索引所有待处理的 content。

    只处理有对应文件、且扩展名可解析的 —— 源码类已在扫描阶段降级为
    仅元数据（§M2 决策），不会进到这里。
    """
    exts = indexable_exts()
    marks = ",".join("?" * len(exts))

    # 先把不可解析的一次性出队（源码、媒体、压缩包等）。
    # 否则它们永远停在 pending，让"待索引 N 个"这个数字永远降不下去，
    # 而调用方会据此反复重试 —— 队列不收敛。
    conn.execute(
        f"""UPDATE contents SET parse_state = 'unsupported'
            WHERE parse_state = 'pending' AND id IN (
                SELECT c.id FROM contents c JOIN files f ON f.content_id = c.id
                GROUP BY c.id
                HAVING lower(MIN(f.ext)) NOT IN ({marks})
            )""",
        list(exts),
    )
    conn.commit()

    rows = conn.execute(
        f"""SELECT c.id, MIN(f.path) AS path
            FROM contents c
            JOIN files f ON f.content_id = c.id
            WHERE c.parse_state = 'pending'
              AND f.state != 'missing'
              AND lower(f.ext) IN ({marks})
            GROUP BY c.id
            LIMIT ?""",
        [*exts, limit],
    ).fetchall()

    summary = {"indexed": 0, "chunks": 0, "no_text": 0, "failed": 0,
               "unsupported": 0, "too_large": 0}

    for i, row in enumerate(rows):
        p = Path(row["path"])
        if not p.exists():
            conn.execute(
                "UPDATE contents SET parse_state = 'missing_file' WHERE id = ?",
                (row["id"],),
            )
            continue

        result = index_content(conn, row["id"], p)
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
