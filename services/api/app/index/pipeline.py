"""索引流水线 —— 解析 → 分片 → 写 FTS5（PLAN §12.2）。

**索引挂在 content 上，不挂 file**（§9 contents 表）：
同一份内容被存了 5 处，只解析一次、只建一次索引。

失败处理原则：单个文档失败只标记该 content，不中断整批。
`parse_state` 记录结果，UI 据此显示"3 个文档解析失败"而不是静默丢弃。
"""

from __future__ import annotations

import json
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

    # 重新索引前先清掉旧片（幂等，支持增量重建）
    old = conn.execute(
        "SELECT id FROM chunks WHERE content_id = ?", (content_id,)
    ).fetchall()
    if old:
        ids = [r["id"] for r in old]
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", ids)
        conn.execute(f"DELETE FROM chunks_fts_tri WHERE rowid IN ({marks})", ids)
        conn.execute("DELETE FROM chunks WHERE content_id = ?", (content_id,))

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
        index_chunk(conn, cur.lastrowid, c.text)

    conn.execute(
        "UPDATE contents SET parse_state = 'indexed', chunk_count = ?, indexed_at = ? "
        "WHERE id = ?",
        (len(chunks), time.time(), content_id),
    )
    return {"state": "indexed", "chunks": len(chunks), "warnings": doc.warnings}


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

    conn.commit()
    summary["total"] = len(rows)
    return summary
