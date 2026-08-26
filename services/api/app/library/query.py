"""Side-effect-free read model for the Inktable AI Library."""

from __future__ import annotations

import sqlite3

from app.db.visibility import visible_content_exists, visible_files_condition
from app.library.core import get_library_item


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
    other_visible = visible_content_exists("other.content_id", "vf", "vs")
    related = [
        dict(row)
        for row in conn.execute(
            f"""SELECT other.id, other.content_id, other.title, other.item_type,
                       other.summary, other.enrichment_status,
                       rel.score, rel.source, rel.relation_type
                FROM library_relations rel
                JOIN library_items other
                  ON other.id = CASE
                       WHEN rel.source_item_id = ? THEN rel.target_item_id
                       ELSE rel.source_item_id
                     END
                WHERE (rel.source_item_id = ? OR rel.target_item_id = ?)
                  AND {other_visible}
                ORDER BY COALESCE(rel.score, 0) DESC, other.id
                LIMIT 24""",
            (item_id, item_id, item_id),
        ).fetchall()
    ]

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
    related = int(
        conn.execute(
            "SELECT COUNT(*) FROM library_relations WHERE relation_type = 'related_to'"
        ).fetchone()[0]
    )
    return {
        "total": total,
        "by_status": by_status,
        "tagged": tagged,
        "relations": related,
    }
