"""Core data model for the Ordo AI Library.

This module intentionally contains no LLM calls. It turns the existing
filesystem/content index into a rebuildable knowledge layer:

    files -> contents -> library_items

``files`` and ``contents`` remain the source of truth. Library metadata is
purely derived and can be dropped/rebuilt without moving, renaming, copying or
mutating user files.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterable, Sequence

from app.db.visibility import visible_content_exists, visible_files_condition


# CREATE-only by design. Read APIs and domain writes must never backfill schema
# as a side effect; normal application startup owns schema initialization.
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

-- A user-triggered enrichment pass is a durable, bounded unit of work.  The
-- item ledger prevents a failed document from being selected over and over in
-- one click-loop, while the cancellation flag lets the UI stop before the next
-- batch without pretending an in-flight model request was interrupted.
CREATE TABLE IF NOT EXISTS library_enrichment_runs (
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'running'
                         CHECK (status IN ('running', 'cancelled', 'completed')),
    include_failed   INTEGER NOT NULL DEFAULT 0 CHECK (include_failed IN (0, 1)),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    claimed_count    INTEGER NOT NULL DEFAULT 0,
    ready_count      INTEGER NOT NULL DEFAULT 0,
    failed_count     INTEGER NOT NULL DEFAULT 0,
    stale_count      INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS library_enrichment_run_items (
    run_id     TEXT NOT NULL
                   REFERENCES library_enrichment_runs(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL
                   REFERENCES library_items(id) ON DELETE CASCADE,
    outcome    TEXT NOT NULL DEFAULT 'running'
                   CHECK (outcome IN ('running', 'ready', 'failed', 'stale')),
    error      TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_library_enrichment_run_items_outcome
    ON library_enrichment_run_items(run_id, outcome);

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

-- v0.4 starts with a deliberately small relation ontology. ``related_to`` is
-- symmetric, therefore each pair is stored once in canonical id order.
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

-- Derived relation edges are only trustworthy for the exact Library input
-- versions they were computed from. Keep the version snapshot in a companion
-- table so old relations can be hidden immediately after OCR/parser/index
-- changes without changing the compact edge table or the future manual-edge
-- model. Rows cascade with their parent relation.
CREATE TABLE IF NOT EXISTS library_relation_versions (
    source_item_id    INTEGER NOT NULL,
    target_item_id    INTEGER NOT NULL,
    relation_type     TEXT NOT NULL DEFAULT 'related_to',
    source_input_hash TEXT NOT NULL,
    target_input_hash TEXT NOT NULL,
    created_at        REAL NOT NULL,
    PRIMARY KEY (source_item_id, target_item_id, relation_type),
    FOREIGN KEY (source_item_id, target_item_id, relation_type)
        REFERENCES library_relations(source_item_id, target_item_id, relation_type)
        ON DELETE CASCADE
);
"""


# Executed only by the normal database initialization path. It gives existing
# v2 libraries stable AI-Library identities immediately after upgrading to v3.
# ``input_hash`` is temporarily seeded from the content sha; the first normal
# sync upgrades it to the active-document-aware hash below.
LIBRARY_BOOTSTRAP_SQL = """
INSERT OR IGNORE INTO library_items
    (content_id, title, input_hash, created_at, updated_at)
SELECT
    c.id,
    COALESCE((
        SELECT f2.name
        FROM files f2
        LEFT JOIN sources s2 ON s2.id = f2.source_id
        WHERE f2.content_id = c.id
          AND (f2.source_id IS NULL OR s2.enabled = 1)
          AND f2.state != 'ignored'
          AND (f2.state != 'missing' OR COALESCE(f2.preserved_path, '') != '')
        ORDER BY CASE WHEN f2.state = 'registered' THEN 0 ELSE 1 END,
                 COALESCE(f2.mtime, 0) DESC, f2.id
        LIMIT 1
    ), ''),
    c.sha256,
    CAST(strftime('%s','now') AS REAL),
    CAST(strftime('%s','now') AS REAL)
FROM contents c
WHERE EXISTS (
    SELECT 1
    FROM files vf
    LEFT JOIN sources vs ON vs.id = vf.source_id
    WHERE vf.content_id = c.id
      AND (vf.source_id IS NULL OR vs.enabled = 1)
      AND vf.state != 'ignored'
      AND (vf.state != 'missing' OR COALESCE(vf.preserved_path, '') != '')
);
"""


def ensure_library_schema(conn: sqlite3.Connection) -> None:
    """Explicitly create AI Library tables for isolated tests/tools.

    ``sqlite3.executescript`` may commit an open transaction, so production
    business functions deliberately never call this helper. ``init_db`` owns
    schema creation before request handling begins.
    """
    conn.executescript(LIBRARY_SCHEMA)


def compute_input_hash(
    content_sha: str,
    active_index_version: int | None,
    document_text_hash: str | None,
) -> str:
    """Version enrichment against what the model actually reads.

    File bytes alone are insufficient: OCR/parser upgrades can rebuild the
    active document representation without changing the source file SHA. The
    active index generation and its document text hash therefore participate in
    the identity of model input.
    """
    payload = (
        f"{content_sha}\0{int(active_index_version or 0)}\0"
        f"{document_text_hash or ''}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    """Return one deterministic user-facing representation per visible content.

    Visibility is exactly the same contract used by search/QA: disabled
    sources and ignored files are hidden; missing files remain visible only
    when a preservation copy exists. The title prefers the active document
    representation, then the newest visible file name.

    Retrieval-only document abstracts are intentionally *not* returned here.
    They are index hints, not user-facing Library summaries. Paths are also not
    copied into ``library_items``; mutable physical metadata is resolved through
    ``files`` when the UI needs it.
    """
    preferred_cond = visible_files_condition("f2", "s2")
    exists_cond = visible_files_condition("vf", "vs")
    return conn.execute(
        f"""
        SELECT
            c.id AS content_id,
            c.sha256 AS content_sha,
            c.active_index_version,
            COALESCE(NULLIF(dr.title, ''), f.name, '') AS title,
            COALESCE(f.ext, '') AS ext,
            dr.text_hash AS document_text_hash
        FROM contents c
        LEFT JOIN document_representations dr
          ON dr.content_id = c.id
         AND dr.index_version = c.active_index_version
        LEFT JOIN files f
          ON f.id = (
              SELECT f2.id
              FROM files f2
              LEFT JOIN sources s2 ON s2.id = f2.source_id
              WHERE f2.content_id = c.id
                AND {preferred_cond}
              ORDER BY
                  CASE WHEN f2.state = 'registered' THEN 0 ELSE 1 END,
                  COALESCE(f2.mtime, 0) DESC,
                  f2.id
              LIMIT 1
          )
        WHERE EXISTS (
            SELECT 1
            FROM files vf
            LEFT JOIN sources vs ON vs.id = vf.source_id
            WHERE vf.content_id = c.id
              AND {exists_cond}
        )
        ORDER BY c.id
        """
    ).fetchall()


def sync_library_items(conn: sqlite3.Connection, *, now: float | None = None) -> dict[str, int]:
    """Synchronize one item per currently visible content.

    ``init_db`` must already have created the Library schema. This operation is
    idempotent and transaction-neutral: it never commits or rolls back on the
    caller's behalf.

    ``document_representations.abstract`` is deliberately excluded. That field
    is retrieval-only and may contain keyword-dense text unsuitable for display.
    A Library item becomes ``ready`` only after explicit Library enrichment.
    """
    ts = time.time() if now is None else now
    created = 0
    refreshed = 0
    stale = 0

    for row in _candidate_rows(conn):
        current_hash = compute_input_hash(
            row["content_sha"],
            row["active_index_version"],
            row["document_text_hash"],
        )
        existing = conn.execute(
            "SELECT * FROM library_items WHERE content_id = ?",
            (row["content_id"],),
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO library_items (
                    content_id, title, item_type, input_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["content_id"],
                    row["title"],
                    _item_type(row["ext"]),
                    current_hash,
                    ts,
                    ts,
                ),
            )
            created += 1
            continue

        hash_changed = existing["input_hash"] != current_hash
        status = existing["enrichment_status"]

        # Compatibility cleanup for the brief v3 development window where the
        # retrieval-only document abstract was copied into Library summary and
        # incorrectly marked ready. Genuine Library enrichment always carries a
        # prompt_version, so this cleanup is narrow and deterministic. Do not
        # infer a user-facing summary from any document representation here.
        legacy_abstract_bootstrap = (
            status == "ready"
            and not existing["prompt_version"]
            and bool((existing["summary"] or "").strip())
        )
        if legacy_abstract_bootstrap:
            conn.execute(
                """
                UPDATE library_items
                SET summary = '', language = '', category_id = NULL,
                    enrichment_status = 'pending', enrichment_model = NULL,
                    enrichment_error = NULL, enriched_at = NULL
                WHERE id = ?
                """,
                (existing["id"],),
            )
            status = "pending"

        if hash_changed and status in {"ready", "running", "failed"}:
            status = "stale"
            stale += 1

        # ``updated_at`` doubles as the enrichment lease timestamp. A routine
        # sync must not keep a running worker alive indefinitely; only a real
        # identity/status/metadata transition should move it forward while an
        # item is running.
        lease_is_active = status == "running" and not hash_changed
        next_updated_at = (
            existing["updated_at"]
            if lease_is_active and not legacy_abstract_bootstrap
            else ts
        )

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
                current_hash,
                status,
                next_updated_at,
                existing["id"],
            ),
        )
        refreshed += 1

    return {"created": created, "refreshed": refreshed, "stale": stale}


def get_library_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    """Read one visible item. Database initialization guarantees the schema."""
    visible = visible_content_exists("li.content_id", "vf", "vs")
    return conn.execute(
        f"""
        SELECT li.*,
               c.sha256,
               c.chunk_count,
               cat.name AS category_name
        FROM library_items li
        JOIN contents c ON c.id = li.content_id
        LEFT JOIN categories cat ON cat.id = li.category_id
        WHERE li.id = ? AND {visible}
        """,
        (item_id,),
    ).fetchone()


def list_library_items(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    category_id: int | None = None,
    tag_id: int | None = None,
    uncategorized: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """List visible items without mutating schema or metadata."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    visible = visible_content_exists("li.content_id", "vf", "vs")
    clauses: list[str] = [visible]
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
    where = " WHERE " + " AND ".join(clauses)
    source_cond = visible_files_condition("sf", "ss")
    params.extend([limit, offset])
    return conn.execute(
        f"""
        SELECT li.*, cat.name AS category_name,
               (SELECT COUNT(*) FROM files sf
                LEFT JOIN sources ss ON ss.id = sf.source_id
                WHERE sf.content_id = li.content_id AND {source_cond})
                   AS source_file_count
        FROM library_items li
        LEFT JOIN categories cat ON cat.id = li.category_id
        {where}
        ORDER BY li.updated_at DESC, li.id DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()


def current_input_hash(conn: sqlite3.Connection, item_id: int) -> str | None:
    """Compute the hash for the document representation active *right now*.

    Read models and asynchronous writers use this same primitive so a caller
    cannot accidentally treat the stored Library hash as authoritative after
    an active OCR/parser generation changed but before the next Library sync.
    """
    row = conn.execute(
        """SELECT c.sha256 AS content_sha, c.active_index_version,
                  dr.text_hash AS document_text_hash
           FROM library_items li
           JOIN contents c ON c.id = li.content_id
           LEFT JOIN document_representations dr
             ON dr.content_id = c.id
            AND dr.index_version = c.active_index_version
           WHERE li.id = ?""",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    return compute_input_hash(
        row["content_sha"], row["active_index_version"], row["document_text_hash"]
    )


# Kept as a private compatibility alias for older internal callers.
_current_input_hash = current_input_hash


def update_enrichment(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    summary: str,
    category_id: int | None,
    language: str = '',
    model: str,
    prompt_version: str,
    input_hash: str,
    now: float | None = None,
) -> bool:
    """Persist validated model output only if it matches current model input.

    The check is independent of a prior ``sync_library_items`` call: it derives
    the active document hash directly from ``contents`` + the active document
    representation before writing. Caller serializes writes with Ordo's
    single-writer lock.
    """
    if current_input_hash(conn, item_id) != input_hash:
        return False

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
    """Replace an item's controlled-vocabulary tags in the caller transaction.

    ``tags`` contains ``(tag_id, source, confidence)`` tuples. Tag creation and
    alias resolution intentionally live outside this primitive so the model
    cannot create uncontrolled vocabulary through this function.
    """
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
    source: str = 'embedding',
    now: float | None = None,
) -> None:
    """Replace ``related_to`` edges produced by one relation source."""
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
