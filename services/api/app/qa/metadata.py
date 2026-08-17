"""Deterministic SQL answers for pure file-metadata questions."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from app.db.visibility import VISIBLE_FILES_COND

_EXT_WORDS = {
    "txt": ".txt", "docx": ".docx", "pdf": ".pdf", "md": ".md",
    "csv": ".csv", "html": ".html", "htm": ".htm",
}


@dataclass(frozen=True)
class MetadataAnswer:
    answer: str
    query_kind: str


def _visible_from() -> str:
    return f"FROM files f LEFT JOIN sources s ON s.id=f.source_id WHERE {VISIBLE_FILES_COND}"


def _extension(question: str) -> str | None:
    lower = question.lower()
    for word, ext in _EXT_WORDS.items():
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lower):
            return ext
    return None


def answer_metadata(conn, question: str) -> MetadataAnswer | None:
    q = re.sub(r"\s+", "", str(question or "").lower())
    if not q:
        return None
    ext = _extension(question)
    count_intent = any(token in q for token in ("多少", "几个", "数量", "总数"))
    recent_intent = any(token in q for token in ("最近", "最新", "上周", "本周", "今天"))
    source_intent = any(token in q for token in ("来源", "来自哪个盘", "来自哪", "哪个盘"))
    list_intent = any(token in q for token in ("有哪些文件", "列出文件", "文件清单"))
    metadata_marker = bool(ext or recent_intent or source_intent or list_intent)
    if not metadata_marker or not (count_intent or recent_intent or source_intent or list_intent):
        return None
    params: list[object] = []
    cond = VISIBLE_FILES_COND
    if ext:
        cond += " AND lower(f.ext) = ?"
        params.append(ext)
    if recent_intent:
        if "上周" in q:
            cond += " AND f.mtime >= ? AND f.mtime < ?"
            now = time.time(); params.extend([now - 14 * 86400, now - 7 * 86400])
        elif "本周" in q:
            cond += " AND f.mtime >= ?"; params.append(time.time() - 7 * 86400)
        elif "今天" in q:
            cond += " AND f.mtime >= ?"; params.append(time.time() - 86400)
    from_sql = "FROM files f LEFT JOIN sources s ON s.id=f.source_id WHERE " + cond
    if source_intent:
        rows = conn.execute(
            f"SELECT COALESCE(s.name, '未归属') AS name, count(*) AS n {from_sql} GROUP BY s.id, s.name ORDER BY n DESC",
            params,
        ).fetchall()
        if not rows: return MetadataAnswer("当前没有可见文件。", "metadata_source")
        return MetadataAnswer("来源文件数量：" + "；".join(f"{r['name']} {r['n']} 个" for r in rows), "metadata_source")
    if list_intent:
        rows = conn.execute(f"SELECT f.name {from_sql} ORDER BY f.mtime DESC LIMIT 20", params).fetchall()
        return MetadataAnswer("文件：" + "、".join(r["name"] for r in rows) if rows else "当前没有匹配文件。", "metadata_list")
    n = conn.execute(f"SELECT count(*) AS n {from_sql}", params).fetchone()["n"]
    label = ext or "可见文件"
    return MetadataAnswer(f"{label} 文件数量：{n} 个。", "metadata_count")
