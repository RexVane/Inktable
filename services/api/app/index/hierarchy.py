"""Persistent Document / Section / Child hierarchy and soft routing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.db.visibility import visible_content_exists
from app.index.search import build_fts_query, segment_for_index


@dataclass(frozen=True)
class SectionDraft:
    key: tuple[str, ...]
    parent_key: tuple[str, ...] | None
    ordinal: int
    title: str
    heading_path: str
    summary_text: str
    start_chunk_ordinal: int
    end_chunk_ordinal: int
    start_offset: int
    end_offset: int
    text_hash: str
    token_count: int
    structure_confidence: float


@dataclass(frozen=True)
class HierarchyDraft:
    title: str
    full_text: str
    summary_text: str
    text_hash: str
    structure_confidence: float
    sections: list[SectionDraft]
    chunk_sections: dict[int, tuple[str, ...]]
    chunk_offsets: dict[int, tuple[int, int]]


def build_hierarchy(doc, chunks, fallback_title: str) -> HierarchyDraft:
    full_text = doc.text
    offsets: dict[int, tuple[int, int]] = {}
    cursor = 0
    for chunk in chunks:
        start = full_text.find(chunk.text, cursor)
        if start < 0:
            start = full_text.find(chunk.text)
        if start < 0:
            start = cursor
        end = start + len(chunk.text)
        offsets[chunk.ordinal] = (start, end)
        cursor = max(cursor, end)

    paths: dict[int, tuple[str, ...]] = {}
    has_heading = any(chunk.section_path for chunk in chunks)
    for chunk in chunks:
        if chunk.section_path:
            paths[chunk.ordinal] = tuple(
                part.strip() for part in chunk.section_path.split(" › ") if part.strip()
            )
        elif chunk.locator.page is not None:
            first = ((chunk.locator.page - 1) // 5) * 5 + 1
            paths[chunk.ordinal] = (f"第 {first}-{first + 4} 页",)
        else:
            paths[chunk.ordinal] = (f"正文 {chunk.ordinal // 6 + 1}",)

    all_keys: list[tuple[str, ...]] = []
    for path in paths.values():
        for size in range(1, len(path) + 1):
            key = path[:size]
            if key not in all_keys:
                all_keys.append(key)

    sections: list[SectionDraft] = []
    for ordinal, key in enumerate(all_keys):
        members = [
            chunk for chunk in chunks
            if paths[chunk.ordinal][:len(key)] == key
        ]
        start_ordinal = min(chunk.ordinal for chunk in members)
        end_ordinal = max(chunk.ordinal for chunk in members)
        start_offset = min(offsets[chunk.ordinal][0] for chunk in members)
        end_offset = max(offsets[chunk.ordinal][1] for chunk in members)
        section_text = "\n\n".join(chunk.text for chunk in members)
        sections.append(SectionDraft(
            key=key,
            parent_key=key[:-1] or None,
            ordinal=ordinal,
            title=key[-1],
            heading_path=" › ".join(key),
            summary_text=section_text[:1000],
            start_chunk_ordinal=start_ordinal,
            end_chunk_ordinal=end_ordinal,
            start_offset=start_offset,
            end_offset=end_offset,
            text_hash=hashlib.sha256(section_text.encode("utf-8")).hexdigest(),
            token_count=len(section_text),
            structure_confidence=1.0 if has_heading else 0.35,
        ))

    title = (doc.title or fallback_title or "未命名文档").strip()
    return HierarchyDraft(
        title=title,
        full_text=full_text,
        summary_text=full_text[:1000],
        text_hash=hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
        structure_confidence=1.0 if has_heading else 0.35,
        sections=sections,
        chunk_sections=paths,
        chunk_offsets=offsets,
    )


def document_index_text(title: str, abstract: str | None, summary_text: str) -> str:
    """documents_fts 的索引文本 —— 摘要在前，截断在后。

    单独抽成函数，因为**索引管线与摘要回填脚本必须逐字用同一份拼法**：
    回填时若拼法与首次入库不同，同一篇文档在两条路径下会得到不同的索引
    文本，检索结果就随「这篇是新入库的还是回填过的」而变。

    abstract 缺失（未装模型 / 生成失败）时结果与未引入该列时逐字一致，
    所以这一列是纯增量，不需要功能开关。
    """
    parts = [title]
    if abstract:
        parts.append(abstract)
    parts.append(summary_text)
    return segment_for_index("\n".join(parts))


def insert_hierarchy(conn, content_id: int, version: int, hierarchy: HierarchyDraft):
    rep_id = conn.execute(
        """INSERT INTO document_representations
           (content_id, index_version, title, summary_text, full_text, text_hash,
            token_count, structure_confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (content_id, version, hierarchy.title, hierarchy.summary_text,
         hierarchy.full_text, hierarchy.text_hash, len(hierarchy.full_text),
         hierarchy.structure_confidence),
    ).lastrowid
    # 入库路径不同步生成摘要：一次 LLM 调用要 5-13 秒，挂在入库上会把
    # 「投放文件 → 约 3.5 秒可搜」这条实测指标破坏掉。摘要由回填任务补，
    # 补上之前文档路的行为与改动前一致。
    conn.execute(
        "INSERT INTO documents_fts(rowid, text) VALUES (?, ?)",
        (rep_id, document_index_text(hierarchy.title, None, hierarchy.summary_text)),
    )

    ids: dict[tuple[str, ...], int] = {}
    for section in hierarchy.sections:
        section_id = conn.execute(
            """INSERT INTO sections
               (content_id, parent_id, index_version, ordinal, heading_path,
                title, summary_text, start_chunk_ordinal, end_chunk_ordinal,
                start_offset, end_offset, text_hash, token_count,
                structure_confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (content_id, ids.get(section.parent_key), version, section.ordinal,
             section.heading_path, section.title, section.summary_text,
             section.start_chunk_ordinal, section.end_chunk_ordinal,
             section.start_offset, section.end_offset, section.text_hash,
             section.token_count, section.structure_confidence),
        ).lastrowid
        ids[section.key] = section_id
        conn.execute(
            "INSERT INTO sections_fts(rowid, text) VALUES (?, ?)",
            (section_id, segment_for_index(
                f"{section.heading_path}\n{section.summary_text}"
            )),
        )
    return rep_id, ids


def hierarchy_routes(conn, query: str, limit: int = 100):
    """Return low-weight Document and Section candidate routes."""
    fts_query = build_fts_query(query, segment=True)
    out = {"document": [], "section": []}
    if not fts_query:
        return out

    try:
        docs = conn.execute(
            f"""SELECT d.content_id, -bm25(documents_fts) AS score
               FROM documents_fts
               JOIN document_representations d ON d.id = documents_fts.rowid
               JOIN contents c ON c.id = d.content_id
               WHERE documents_fts MATCH ?
                 AND d.index_version = c.active_index_version
                 AND {visible_content_exists('d.content_id')}
               ORDER BY bm25(documents_fts) LIMIT 12""",
            (fts_query,),
        ).fetchall()
        for doc in docs:
            rows = conn.execute(
                f"""SELECT ch.id FROM chunks ch JOIN contents c ON c.id = ch.content_id
                   WHERE ch.content_id = ?
                     AND ch.index_version = c.active_index_version
                     AND {visible_content_exists('ch.content_id')}
                   ORDER BY ch.ordinal LIMIT 5""",
                (doc["content_id"],),
            ).fetchall()
            out["document"].extend((row["id"], float(doc["score"])) for row in rows)
            if len(out["document"]) >= limit:
                break

        sections = conn.execute(
            f"""SELECT s.content_id, s.index_version, s.start_chunk_ordinal,
                      s.end_chunk_ordinal, -bm25(sections_fts) AS score
               FROM sections_fts
               JOIN sections s ON s.id = sections_fts.rowid
               JOIN contents c ON c.id = s.content_id
               WHERE sections_fts MATCH ?
                 AND s.index_version = c.active_index_version
                 AND {visible_content_exists('s.content_id')}
               ORDER BY bm25(sections_fts) LIMIT 20""",
            (fts_query,),
        ).fetchall()
        for section in sections:
            rows = conn.execute(
                f"""SELECT id FROM chunks
                   WHERE content_id = ? AND index_version = ?
                     AND ordinal BETWEEN ? AND ?
                     AND {visible_content_exists('content_id')}
                   ORDER BY ordinal LIMIT 5""",
                (section["content_id"], section["index_version"],
                 section["start_chunk_ordinal"],
                 section["end_chunk_ordinal"]),
            ).fetchall()
            out["section"].extend(
                (row["id"], float(section["score"])) for row in rows
            )
            if len(out["section"]) >= limit:
                break
    except Exception:
        return {"document": [], "section": []}

    for name in out:
        seen = set()
        out[name] = [
            item for item in out[name]
            if not (item[0] in seen or seen.add(item[0]))
        ][:limit]
    return out
