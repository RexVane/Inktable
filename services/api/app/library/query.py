"""Side-effect-free read model for the Ordo AI Library."""

from __future__ import annotations

import sqlite3

from app.db.visibility import visible_content_exists, visible_files_condition
from app.library.core import (
    compute_input_hash,
    get_library_item,
    list_library_items,
)
from app.library.relations import RELATION_SOURCE, relation_status


_RELATION_VERSION_COLUMNS = (
    "source_stored_hash",
    "target_stored_hash",
    "source_content_sha",
    "source_active_index_version",
    "source_document_text_hash",
    "target_content_sha",
    "target_active_index_version",
    "target_document_text_hash",
    "source_input_hash",
    "target_input_hash",
)


def _derived_relation_is_fresh(row: sqlite3.Row) -> bool:
    """Check an edge against the active document, not only Library metadata.

    A parser/OCR worker can activate a new document generation before the next
    Library sync runs. Read paths must hide the old derived edge during that
    window as well; otherwise a stale relation briefly becomes user-visible.
    """
    source_current = compute_input_hash(
        str(row["source_content_sha"]),
        row["source_active_index_version"],
        row["source_document_text_hash"],
    )
    target_current = compute_input_hash(
        str(row["target_content_sha"]),
        row["target_active_index_version"],
        row["target_document_text_hash"],
    )
    return (
        row["source_input_hash"] is not None
        and row["target_input_hash"] is not None
        and str(row["source_input_hash"]) == source_current
        and str(row["target_input_hash"]) == target_current
        and str(row["source_stored_hash"]) == source_current
        and str(row["target_stored_hash"]) == target_current
    )


def library_page(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    category_id: int | None = None,
    tag_id: int | None = None,
    uncategorized: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Return a stable paged list plus a matching visible total."""
    visible = visible_content_exists("li.content_id", "vf", "vs")
    clauses = [visible]
    params: list[object] = []
    if status:
        clauses.append("li.enrichment_status = ?")
        params.append(status)
    if category_id is not None:
        clauses.append("li.category_id = ?")
        params.append(category_id)
    if tag_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM library_item_tags lit "
            "WHERE lit.library_item_id = li.id AND lit.tag_id = ?)")
        params.append(tag_id)
    if uncategorized:
        clauses.append("li.category_id IS NULL")
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM library_items li WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()[0]
    )
    items = [
        dict(row)
        for row in list_library_items(
            conn,
            status=status,
            category_id=category_id,
            tag_id=tag_id,
            uncategorized=uncategorized,
            limit=limit,
            offset=offset,
        )
    ]
    return {
        "total": total,
        "items": items,
        "offset": max(0, int(offset)),
        "has_more": max(0, int(offset)) + len(items) < total,
    }


def library_item_detail(conn: sqlite3.Connection, item_id: int) -> dict | None:
    """Return a bounded, user-facing knowledge-card view for one visible item."""
    item = get_library_item(conn, item_id)
    if item is None:
        return None

    tags = [
        dict(row)
        for row in conn.execute(
            """SELECT t.id, t.name, t.color, lit.source, lit.confidence
               FROM library_item_tags lit
               JOIN tags t ON t.id = lit.tag_id
               WHERE lit.library_item_id = ?
               ORDER BY COALESCE(lit.confidence, 0) DESC, t.name, t.id""",
            (item_id,),
        ).fetchall()
    ]

    source_cond = visible_files_condition("f", "s")
    source_files = [
        dict(row)
        for row in conn.execute(
            f"""SELECT f.id, f.name, f.path, f.ext, f.state, f.mtime,
                       f.preserved_path, s.name AS source_name
                FROM files f
                LEFT JOIN sources s ON s.id = f.source_id
                WHERE f.content_id = ? AND {source_cond}
                ORDER BY CASE WHEN f.state = 'registered' THEN 0 ELSE 1 END,
                         COALESCE(f.mtime, 0) DESC, f.id
                LIMIT 50""",
            (item["content_id"],),
        ).fetchall()
    ]

    # Relations are useful only if the other endpoint is currently visible.
    # Automatically derived edges are also versioned: an OCR/parser/index
    # change invalidates the old edge until the relation builder runs again.
    # Manual/future non-derived relations are not subject to this AI lifecycle.
    other_visible = visible_content_exists("other.content_id", "vf", "vs")
    relation_rows = conn.execute(
        f"""SELECT other.id, other.content_id, other.title, other.item_type,
                   other.summary, other.enrichment_status,
                   rel.score, rel.source, rel.relation_type,
                   src.input_hash AS source_stored_hash,
                   dst.input_hash AS target_stored_hash,
                   src_c.sha256 AS source_content_sha,
                   src_c.active_index_version AS source_active_index_version,
                   src_dr.text_hash AS source_document_text_hash,
                   dst_c.sha256 AS target_content_sha,
                   dst_c.active_index_version AS target_active_index_version,
                   dst_dr.text_hash AS target_document_text_hash,
                   rv.source_input_hash, rv.target_input_hash
            FROM library_relations rel
            JOIN library_items src ON src.id = rel.source_item_id
            JOIN library_items dst ON dst.id = rel.target_item_id
            JOIN contents src_c ON src_c.id = src.content_id
            JOIN contents dst_c ON dst_c.id = dst.content_id
            LEFT JOIN document_representations src_dr
              ON src_dr.content_id = src_c.id
             AND src_dr.index_version = src_c.active_index_version
            LEFT JOIN document_representations dst_dr
              ON dst_dr.content_id = dst_c.id
             AND dst_dr.index_version = dst_c.active_index_version
            JOIN library_items other
              ON other.id = CASE
                   WHEN rel.source_item_id = ? THEN rel.target_item_id
                   ELSE rel.source_item_id
                 END
            LEFT JOIN library_relation_versions rv
              ON rv.source_item_id = rel.source_item_id
             AND rv.target_item_id = rel.target_item_id
             AND rv.relation_type = rel.relation_type
            WHERE (rel.source_item_id = ? OR rel.target_item_id = ?)
              AND {other_visible}
            ORDER BY COALESCE(rel.score, 0) DESC, other.id""",
        (item_id, item_id, item_id),
    ).fetchall()
    related = []
    for row in relation_rows:
        if row["source"] == RELATION_SOURCE and not _derived_relation_is_fresh(row):
            continue
        payload = dict(row)
        for column in _RELATION_VERSION_COLUMNS:
            payload.pop(column, None)
        related.append(payload)
        if len(related) >= 24:
            break

    payload = dict(item)
    payload["tags"] = tags
    payload["source_files"] = source_files
    payload["related"] = related
    return payload


def library_stats(conn: sqlite3.Connection) -> dict:
    """Return knowledge-layer counts using the same visibility contract as QA."""
    visible = visible_content_exists("li.content_id", "vf", "vs")
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM library_items li WHERE {visible}"
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"""SELECT li.enrichment_status, COUNT(*) AS n
            FROM library_items li
            WHERE {visible}
            GROUP BY li.enrichment_status"""
    ).fetchall()
    by_status = {row["enrichment_status"]: int(row["n"]) for row in rows}
    tagged = int(
        conn.execute(
            f"""SELECT COUNT(DISTINCT li.id)
                FROM library_items li
                JOIN library_item_tags lit ON lit.library_item_id = li.id
                WHERE {visible}"""
        ).fetchone()[0]
    )

    source_visible = visible_content_exists("src.content_id", "svf", "svs")
    target_visible = visible_content_exists("dst.content_id", "tvf", "tvs")
    derived_related = int(relation_status(conn)["relations"])
    manual_related = int(
        conn.execute(
            f"""SELECT COUNT(*)
                FROM library_relations rel
                JOIN library_items src ON src.id = rel.source_item_id
                JOIN library_items dst ON dst.id = rel.target_item_id
                WHERE rel.relation_type = 'related_to'
                  AND rel.source != ?
                  AND {source_visible}
                  AND {target_visible}""",
            (RELATION_SOURCE,),
        ).fetchone()[0]
    )
    related = derived_related + manual_related
    return {
        "total": total,
        "by_status": by_status,
        "tagged": tagged,
        "relations": related,
    }


def library_taxonomy(conn: sqlite3.Connection) -> dict:
    """分类与标签及各自的可见条目数 —— 左栏知识馆树的数据源。

    分类含空分类（新建分类也要看得见）；标签只列有条目挂靠的。
    未分类单独给出计数，让"还没归类的"有一个可点击的入口。
    """
    visible = visible_content_exists("li.content_id", "vf", "vs")
    categories = [
        dict(row)
        for row in conn.execute(
            f"""SELECT c.id, c.name, c.parent_id, COUNT(li.id) AS count
                FROM categories c
                LEFT JOIN library_items li
                  ON li.category_id = c.id AND {visible}
                GROUP BY c.id
                ORDER BY c.sort_order, c.name"""
        )
    ]
    tags = [
        dict(row)
        for row in conn.execute(
            f"""SELECT t.id, t.name, COUNT(lit.library_item_id) AS count
                FROM tags t
                JOIN library_item_tags lit ON lit.tag_id = t.id
                JOIN library_items li ON li.id = lit.library_item_id AND {visible}
                GROUP BY t.id
                ORDER BY COUNT(lit.library_item_id) DESC, t.name"""
        )
    ]
    uncategorized = int(conn.execute(
        f"SELECT COUNT(*) FROM library_items li "
        f"WHERE {visible} AND li.category_id IS NULL"
    ).fetchone()[0])
    return {"categories": categories, "tags": tags,
            "uncategorized": uncategorized}


def library_tree(conn: sqlite3.Connection) -> dict:
    """左栏知识馆树的全量叶子 —— 只取轻量列，不做分页。

    树需要"每个分类下有哪些条目"的完整视图，分页会让树残缺；
    只取 id/title/category_id/状态，2000 条封顶（可见条目超过时截断并标记）。
    """
    visible = visible_content_exists("li.content_id", "vf", "vs")
    items = [
        dict(row)
        for row in conn.execute(
            f"""SELECT li.id, li.title, li.category_id, li.enrichment_status
                FROM library_items li
                WHERE {visible}
                ORDER BY (li.category_id IS NULL), li.category_id, li.title
                LIMIT 2000"""
        )
    ]
    return {"items": items, "truncated": len(items) >= 2000}
