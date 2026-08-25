"""Deterministic SQL answers for *unambiguous* file-metadata questions.

H11 says pure metadata questions bypass RAG. The inverse is just as important:
when classification is uncertain, PLAN §5.2 requires the content-retrieval path.
This module therefore uses a conservative allow-list rather than treating any
question containing "最近" or "多少" as a file-count request.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.db.visibility import VISIBLE_FILES_COND

_EXT_WORDS = {
    "txt": ".txt", "docx": ".docx", "pdf": ".pdf", "md": ".md",
    "csv": ".csv", "html": ".html", "htm": ".htm",
}

# Content-question signals win over metadata words. Examples that must go to
# RAG: “这份 PDF 里讲了多少种情况”, “本周的会议纪要说了什么”.
_CONTENT_SIGNALS = re.compile(
    r"(?:里|中|内)(?:有|提到|包含|写|说|讲)|"
    r"提到|讲了|写了|说了|内容|正文|为什么|如何|怎么|"
    r"多少(?:种|条|项|页|章|步|方法|步骤|情况|原因)"
)
_FILE_NOUN = re.compile(r"文件|文档|资料|清单")


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


def _calendar_week_bounds(now_ts: float) -> tuple[float, float, float]:
    """Return this-Monday, next-Monday and previous-Monday local timestamps."""
    now = datetime.fromtimestamp(now_ts)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    next_monday = monday + timedelta(days=7)
    prev_monday = monday - timedelta(days=7)
    return monday.timestamp(), next_monday.timestamp(), prev_monday.timestamp()


def _is_unambiguous_metadata_question(
    q: str,
    *,
    ext: str | None,
    count_intent: bool,
    recent_intent: bool,
    source_intent: bool,
    list_intent: bool,
) -> bool:
    if _CONTENT_SIGNALS.search(q):
        return False
    # A file noun is mandatory unless the explicit list/source phrase already
    # contains one. An extension alone plus “多少” is insufficient: “PDF 里有
    # 多少种情况” is a content question, not a library count.
    has_file_noun = bool(_FILE_NOUN.search(q))
    if not has_file_noun and not list_intent:
        return False
    if source_intent or list_intent:
        return True
    if count_intent:
        return bool(ext or recent_intent or has_file_noun)
    # “最近/最新的文件” asks for a list even without “列出”.
    return bool(recent_intent and has_file_noun)


def answer_metadata(
    conn,
    question: str,
    *,
    now: float | None = None,
    recent_days: int = 30,
) -> MetadataAnswer | None:
    q = re.sub(r"\s+", "", str(question or "").lower())
    if not q:
        return None
    ext = _extension(question)
    count_intent = any(token in q for token in ("多少", "几个", "数量", "总数"))
    recent_intent = any(token in q for token in ("最近", "最新", "上周", "本周", "今天"))
    source_intent = any(token in q for token in ("来源", "来自哪个盘", "来自哪", "哪个盘"))
    list_intent = any(token in q for token in ("有哪些文件", "列出文件", "文件清单"))
    if not _is_unambiguous_metadata_question(
        q,
        ext=ext,
        count_intent=count_intent,
        recent_intent=recent_intent,
        source_intent=source_intent,
        list_intent=list_intent,
    ):
        return None

    params: list[object] = []
    cond = VISIBLE_FILES_COND
    if ext:
        cond += " AND lower(f.ext) = ?"
        params.append(ext)

    now_ts = time.time() if now is None else now
    if recent_intent:
        this_monday, next_monday, prev_monday = _calendar_week_bounds(now_ts)
        if "上周" in q:
            cond += " AND f.mtime >= ? AND f.mtime < ?"
            params.extend([prev_monday, this_monday])
        elif "本周" in q:
            cond += " AND f.mtime >= ? AND f.mtime < ?"
            params.extend([this_monday, next_monday])
        elif "今天" in q:
            today = datetime.fromtimestamp(now_ts).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            cond += " AND f.mtime >= ? AND f.mtime < ?"
            params.extend([today.timestamp(), (today + timedelta(days=1)).timestamp()])
        else:
            # “最近/最新” previously triggered this branch but applied no
            # predicate, returning the all-time count. Define “最近” as a
            # documented rolling 30-day window; callers may override in tests.
            cond += " AND f.mtime >= ? AND f.mtime <= ?"
            params.extend([now_ts - max(1, recent_days) * 86400, now_ts])

    from_sql = "FROM files f LEFT JOIN sources s ON s.id=f.source_id WHERE " + cond
    if source_intent:
        rows = conn.execute(
            f"SELECT COALESCE(s.name, '未归属') AS name, count(*) AS n {from_sql} "
            "GROUP BY s.id, s.name ORDER BY n DESC",
            params,
        ).fetchall()
        if not rows:
            return MetadataAnswer("当前没有可见文件。", "metadata_source")
        return MetadataAnswer(
            "来源文件数量：" + "；".join(f"{r['name']} {r['n']} 个" for r in rows),
            "metadata_source",
        )
    if list_intent or (recent_intent and not count_intent):
        rows = conn.execute(
            f"SELECT f.name {from_sql} ORDER BY f.mtime DESC LIMIT 20", params,
        ).fetchall()
        return MetadataAnswer(
            "文件：" + "、".join(r["name"] for r in rows)
            if rows else "当前没有匹配文件。",
            "metadata_list",
        )
    n = conn.execute(f"SELECT count(*) AS n {from_sql}", params).fetchone()["n"]
    label = ext or "可见文件"
    return MetadataAnswer(f"{label} 文件数量：{n} 个。", "metadata_count")
