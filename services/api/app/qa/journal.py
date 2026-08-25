"""调查日志 —— 把成功问答的结论沉淀成可找回的记录。

**为什么是导航面而不是证据面**

marginalia 的 journal 会作为召回面参与检索，让 agent 复用上次的结论。
Inktable 不能照搬：H8 要求答案每一行都引用**库内原文**，而 journal 存的是
模型自己的输出。把它当证据喂回上下文，模型就可能引用「自己上次说的话」，
证据链断在那里，而四条后置校验看到的是一个格式合法的引用 —— 校验发现不了
这种断裂。

所以 journal 只回答两个问题：「我以前问过什么」「当时引了哪些文件」。它把
用户导航到原始资料，引用仍然指向真实 content。marginalia 那个「不重复劳动」
的主要价值拿到了，H8 没有让步。

**只记成功的回答**：拒答与降级没有结论可沉淀，记下来只会让下次搜索命中
一堆「查不到」。
"""

from __future__ import annotations

import json
import logging
import re
import time

from app.index.search import build_fts_query, segment_for_index

log = logging.getLogger("inktable.journal")

# 单条记录的正文上限。日志是索引对象，不是归档：过长的回答对「找回上次
# 查过什么」没有帮助，只会摊薄 BM25。
_MAX_ANSWER_CHARS = 4000
_MAX_QUESTION_CHARS = 500


def _index_text(question: str, answer: str) -> str:
    """索引文本 = 问题 + 回答。问题权重靠它自然更短来体现。"""
    return segment_for_index(f"{question}\n{answer}")


def record(conn, question: str, answer: str, *, content_ids=None,
           book_id: int | None = None, model: str = "") -> int | None:
    """沉淀一条调查记录。返回 journal.id；输入不合格时返回 None。

    调用方只在 status == 'answered' 时调用。这里再挡一次空值，避免
    上游改动后悄悄写进一堆空记录。
    """
    question = (question or "").strip()[:_MAX_QUESTION_CHARS]
    answer = (answer or "").strip()[:_MAX_ANSWER_CHARS]
    if not question or not answer:
        return None
    ids = sorted({int(i) for i in (content_ids or []) if i is not None})
    row_id = conn.execute(
        """INSERT INTO journal
           (question, answer, content_ids, book_id, model, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (question, answer, json.dumps(ids), book_id, model or "", time.time()),
    ).lastrowid
    conn.execute("INSERT INTO journal_fts(rowid, text) VALUES (?, ?)",
                 (row_id, _index_text(question, answer)))
    return row_id


def delete(conn, journal_id: int) -> bool:
    """删除一条记录。contentless FTS 必须显式删（见 schema 里的说明）。"""
    cur = conn.execute("DELETE FROM journal WHERE id = ?", (journal_id,))
    if not cur.rowcount:
        return False
    conn.execute("DELETE FROM journal_fts WHERE rowid = ?", (journal_id,))
    return True


def clear(conn) -> int:
    n = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    conn.execute("DELETE FROM journal")
    conn.execute("DELETE FROM journal_fts")
    return int(n)


def _row_to_dict(row) -> dict:
    try:
        ids = json.loads(row["content_ids"] or "[]")
    except (TypeError, ValueError):
        ids = []
    return {
        "id": row["id"],
        "question": row["question"],
        "answer": row["answer"],
        "content_ids": ids if isinstance(ids, list) else [],
        "book_id": row["book_id"],
        "model": row["model"],
        "created_at": row["created_at"],
    }


def recent(conn, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM journal ORDER BY created_at DESC LIMIT ?",
        (max(1, min(200, limit)),),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def search(conn, query: str, limit: int = 10) -> list[dict]:
    """按内容找回过往调查。空查询回最近几条 —— 面板要有东西可看。"""
    query = (query or "").strip()
    if not query:
        return recent(conn, limit)
    fts_query = build_fts_query(query, segment=True)
    if not fts_query:
        return []
    try:
        rows = conn.execute(
            """SELECT j.*, -bm25(journal_fts) AS score
               FROM journal_fts
               JOIN journal j ON j.id = journal_fts.rowid
               WHERE journal_fts MATCH ?
               ORDER BY bm25(journal_fts) LIMIT ?""",
            (fts_query, max(1, min(50, limit))),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - FTS 语法/索引问题不该让问答挂掉
        log.warning("journal 检索失败：%s", exc)
        return []
    out = []
    for row in rows:
        item = _row_to_dict(row)
        item["score"] = float(row["score"])
        out.append(item)
    return out


_WORD = re.compile(r"[A-Za-z0-9]+|[一-鿿]")


def related(conn, question: str, limit: int = 3) -> list[dict]:
    """新问题的「你之前问过」提示。

    比 search() 更保守：只有与历史问题**明显重叠**的才提示。宁可不提示，
    也不要在每次提问时都甩出三条不相关的旧记录 —— 那会训练用户忽略它。
    判据取查询词覆盖率，而不是 BM25 绝对分：分数的量纲随库大小漂移，
    覆盖率不会。
    """
    hits = search(conn, question, limit=max(limit * 3, 6))
    terms = {t for t in _WORD.findall(question or "") if t}
    if not terms:
        return []
    out = []
    for hit in hits:
        past = {t for t in _WORD.findall(hit["question"]) if t}
        if not past:
            continue
        overlap = len(terms & past) / len(terms)
        # 0.5：一半以上的查询词在历史问题里出现过才算「问过类似的」。
        # 实测阈值再低会把「什么是 X」和「什么是 Y」判成相关（共享
        # 「什么」「是」两个高频词）。
        if overlap >= 0.5:
            hit = dict(hit)
            hit["overlap"] = round(overlap, 3)
            out.append(hit)
        if len(out) >= limit:
            break
    return out
