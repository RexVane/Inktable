"""Core data model for the Inktable AI Library.

This module intentionally contains no LLM calls.  It turns the existing
filesystem/content index into a rebuildable knowledge layer:

    files -> contents -> library_items

``files`` and ``contents`` remain the source of truth.  Library metadata is
purely derived and can be dropped/rebuilt without moving, renaming, copying or
mutating user files.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Sequence


LIBRARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_items (
    id                 INTEGER PRIMARY KEY,
    content_id         INTEGER NOT NULL UNIQUE
                           REFERENCES contents(id) ON DELETE CASCADE,
    title              TEXT NOT NULL DEFAULT '',
    item_type          TEXT NOT NULL DEFAULT 'document',
    summary            TEXT NOT NULL DEFAULT '',
    language           TEXT NOT NULL DEFAULT '',
    category_id        INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    enrichment_status  TEXT NOT NULL DEFAULT 'pending'
                           CHECK (enrichment_status IN
                                  ('pending', 'running', 'ready', 'failed', 'stale')),
    enrichment_model   TEXT,
    prompt_version     TEXT,
    input_hash         TEXT NOT NULL,
    enrichment_error   TEXT,
    enriched_at        REAL,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_library_items_status
    ON library_items(enrichment_status);
CREATE INDEX IF NOT EXISTS idx_library_items_category
    ON library_items(category_id);
CREATE INDEX IF NOT EXISTS idx_library_items_updated
    ON library_items(updated_at DESC);

CREATE TABLE IF NOT EXISTS library_item_tags (
    library_item_id INTEGER NOT NULL
        REFERENCES library_items(id) ON DELETE CASCADE,
    tag_id          INTEGER NOT NULL
        REFERENCES tags(id) ON DELETE CASCADE,
    source          TEXT NOT NULL DEFAULT 'ai',
    confidence      REAL,
    PRIMARY KEY (library_item_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_library_item_tags_tag
    ON library_item_tags(tag_id, library_item_id);

-- v0.4 starts with a deliberately small relation ontology.  ``related_to``
-- is symmetric, therefore each pair is stored once in canonical id order.
CREATE TABLE IF NOT EXISTS library_relations (
    source_item_id INTEGER NOT NULL
        REFERENCES library_items(id) ON DELETE CASCADE,
    target_item_id INTEGER NOT NULL
        REFERENCES library_items(id) ON DELETE CASCADE,
    relation_type  TEXT NOT NULL DEFAULT 'related_to',
    score          REAL,
    source         TEXT NOT NULL DEFAULT 'embedding',
    created_at     REAL NOT NULL,
    CHECK (source_item_id < target_item_id),
    PRIMARY KEY (source_item_id, target_item_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_library_relations_target
    ON library_relations(target_item_id, relation_type);
"""


def ensure_library_schema(conn: sqlite3.Connection) -> None:
    """Create the rebuildable AI Library tables if they do not exist."""
    conn.executescript(LIBRARY_SCHEMA)


def _item_type(ext: str | None) -> str:
    ext = (ext or "").lower().lstrip(".")
    return {
        "pdf": "pdf",
        "docx": "document",
        "md": "note",
        "txt": "note",
        "csv": "table",
        "html": "webpage",
        "htm": "webpage",
    }.get(ext, "document")


def _candidate_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return one deterministic user-facing representation per content.

    The title prefers the active document representation, then the newest live
    file name.  We intentionally do not copy paths into ``library_items``:
    paths are mutable physical metadata and are always resolved through
    ``files`` when the UI needs them.
    """
    return conn.execute(
        """
        SELECT
            c.id AS content_id,
            c.sha256 AS input_hash,
            COALESCE(NULLIF(dr.title, ''), f.name, '') AS title,
            COALESCE(f.ext, '') AS ext,
            dr.abstract AS abstract,
            dr.abstract_model AS abstract_model
        FROM contents c
        LEFT JOIN document_representations dr
          ON dr.content_id = c.id
         AND dr.index_version = c.active_index_version
        LEFT JOIN files f
          ON f.id = (
              SELECT f2.id
              FROM files f2
              WHERE f2.content_id = c.id
                AND f2.state NOT IN ('ignored', 'excluded')
              ORDER BY
                  CASE WHEN f2.state = 'registered' THEN 0 ELSE 1 END,
                  COALESCE(f2.mtime, 0) DESC,
                  f2.id
              LIMIT 1
          )
        WHERE EXISTS (
            SELECT 1 FROM files visible
            WHERE visible.content_id = c.id
              AND visible.state NOT IN ('ignored', 'excluded')
        )
        ORDER BY c.id
        """
    ).fetchall()


def sync_library_items(conn: sqlite3.Connection, *, now: float | None = None) -> dict[str, int]:
    """Synchronize one ``library_item`` for every currently represented content.

    This operation is idempotent and deliberately cheap enough to run after a
    scan/index batch.  Content hash changes mark prior enrichment stale instead
    of silently treating model output as current.
    """
    ensure_library_schema(conn)
    ts = time.time() if now is None else now
    created = 0
    refreshed = 0
    stale = 0

    for row in _candidate_rows(conn):
        existing = conn.execute(
            "SELECT * FROM library_items WHERE content_id = ?",
            (row["content_id"],),
        ).fetchone()
        summary = (row["abstract"] or "").strip()
        abstract_model = row["abstract_model"]
        inferred_status = "ready" if summary else "pending"

        if existing is None:
            conn.execute(
                """
                INSERT INTO library_items (
                    content_id, title, item_type, summary, enrichment_status,
                    enrichment_model, input_hash, enriched_at, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["content_id"],
                    row["title"],
                    _item_type(row["ext"]),
                    summary,
                    inferred_status,
                    abstract_model,
                    row["input_hash"],
                    ts if summary else None,
                    ts,
                    ts,
                ),
            )
            created += 1
            continue

        hash_changed = existing["input_hash"] != row["input_hash"]
        status = existing["enrichment_status"]
        if hash_changed and status == "ready":
            status = "stale"
            stale += 1

        # Existing explicit enrichment wins over the older document abstract.
        # The abstract is only used to bootstrap a new item.
        conn.execute(
            """
            UPDATE library_items
            SET title = ?, item_type = ?, input_hash = ?,
                enrichment_status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                row["title"],
                _item_type(row["ext"]),
                row["input_hash"],
                status,
                ts,
                existing["id"],
            ),
        )
        refreshed += 1

    return {"created": created, "refreshed": refreshed, "stale": stale}


def get_library_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    ensure_library_schema(conn)
    return conn.execute(
        """
        SELECT li.*,
               c.sha256,
               c.chunk_count,
               cat.name AS category_name
        FROM library_items li
        JOIN contents c ON c.id = li.content_id
        LEFT JOIN categories cat ON cat.id = li.category_id
        WHERE li.id = ?
        """,
        (item_id,),
    ).fetchone()


def list_library_items(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    category_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    ensure_library_schema(conn)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("li.enrichment_status = ?")
        params.append(status)
    if category_id is not None:
        clauses.append("li.category_id = ?")
        params.append(category_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.extend([limit, offset])
    return conn.execute(
        f"""
        SELECT li.*, cat.name AS category_name,
               (SELECT COUNT(*) FROM files f
                WHERE f.content_id = li.content_id) AS source_file_count
        FROM library_items li
        LEFT JOIN categories cat ON cat.id = li.category_id
        {where}
        ORDER BY li.updated_at DESC, li.id DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()


def update_enrichment(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    summary: str,
    category_id: int | None,
    language: str = "",
    model: str,
    prompt_version: str,
    input_hash: str,
    now: float | None = None,
) -> bool:
    """Persist validated model output only if it matches the current content.

    A worker may finish after a file changed.  ``input_hash`` makes that race
    explicit: stale model output is rejected instead of overwriting the newer
    library item.
    """
    ensure_library_schema(conn)
    ts = time.time() if now is None else now
    cursor = conn.execute(
        """
        UPDATE library_items
        SET summary = ?, category_id = ?, language = ?,
            enrichment_status = 'ready', enrichment_model = ?,
            prompt_version = ?, enrichment_error = NULL,
            enriched_at = ?, updated_at = ?
        WHERE id = ? AND input_hash = ?
        """,
        (
            summary.strip(),
            category_id,
            language.strip(),
            model,
            prompt_version,
            ts,
            ts,
            item_id,
            input_hash,
        ),
    )
    return cursor.rowcount == 1


def mark_library_item_stale(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    error: str | None = None,
    now: float | None = None,
) -> None:
    ensure_library_schema(conn)
    ts = time.time() if now is None else now
    conn.execute(
        """
        UPDATE library_items
        SET enrichment_status = 'stale', enrichment_error = ?, updated_at = ?
        WHERE id = ?
        """,
        (error, ts, item_id),
    )


def replace_library_item_tags(
    conn: sqlite3.Connection,
    item_id: int,
    tags: Sequence[tuple[int, str, float | None]],
) -> None:
    """Replace an item's controlled-vocabulary tags atomically.

    ``tags`` contains ``(tag_id, source, confidence)`` tuples.  Tag creation and
    alias resolution intentionally live outside this primitive so the model
    cannot create uncontrolled vocabulary through this function.
    """
    ensure_library_schema(conn)
    conn.execute("DELETE FROM library_item_tags WHERE library_item_id = ?", (item_id,))
    conn.executemany(
        """
        INSERT INTO library_item_tags
            (library_item_id, tag_id, source, confidence)
        VALUES (?, ?, ?, ?)
        """,
        ((item_id, tag_id, source, confidence) for tag_id, source, confidence in tags),
    )


def set_related_items(
    conn: sqlite3.Connection,
    item_id: int,
    related: Iterable[tuple[int, float | None]],
    *,
    source: str = "embedding",
    now: float | None = None,
) -> None:
    """Replace ``related_to`` edges produced by one relation source."""
    ensure_library_schema(conn)
    ts = time.time() if now is None else now
    conn.execute(
        """
        DELETE FROM library_relations
        WHERE relation_type = 'related_to' AND source = ?
          AND (source_item_id = ? OR target_item_id = ?)
        """,
        (source, item_id, item_id),
    )
    rows: list[tuple[int, int, float | None, str, float]] = []
    seen: set[int] = set()
    for other_id, score in related:
        other_id = int(other_id)
        if other_id == item_id or other_id in seen:
            continue
        seen.add(other_id)
        left, right = sorted((item_id, other_id))
        rows.append((left, right, score, source, ts))
    conn.executemany(
        """
        INSERT INTO library_relations
            (source_item_id, target_item_id, relation_type, score, source, created_at)
        VALUES (?, ?, 'related_to', ?, ?, ?)
        ON CONFLICT(source_item_id, target_item_id, relation_type)
        DO UPDATE SET score = excluded.score,
                      source = excluded.source,
                      created_at = excluded.created_at
        """,
        rows,
    )
