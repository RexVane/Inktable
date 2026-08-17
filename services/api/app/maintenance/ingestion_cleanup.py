"""Plan and apply database-only cleanup for ingestion noise.

The cleanup never renames, moves, modifies, or removes source files. It only
repairs source ownership and removes database rows that the current scanner
policy would not admit.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

from app.discovery.sources import Source, discover_system
from app.index.embedding import DIM
from app.index.pipeline import cleanup_orphan_contents, count_orphan_contents
from app.index.search import segment_for_index
from app.watcher.policy import resolve_source_policy
from app.watcher.scanner import (
    classify_ext,
    path_is_within,
    should_skip_file_name,
    should_skip_path,
)


SYSTEM_SOURCE_NAMES = {"下载", "图片", "音乐", "视频"}


@dataclass(frozen=True)
class FileRemoval:
    file_id: int
    reason: str


@dataclass(frozen=True)
class CleanupPlan:
    inspected: int
    protected: int
    reassignments: tuple[tuple[int, int], ...]
    removals: tuple[FileRemoval, ...]

    @property
    def reason_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.reason for item in self.removals).items()))

    def summary(self) -> dict[str, object]:
        return {
            "inspected": self.inspected,
            "protected": self.protected,
            "reassignments": len(self.reassignments),
            "removals": len(self.removals),
            "reasons": self.reason_counts,
        }


def _normal_path(value: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(value))))


def _best_source(path: str, sources: list[sqlite3.Row]) -> sqlite3.Row | None:
    owners = [row for row in sources if path_is_within(path, row["path"])]
    if not owners:
        return None
    return max(owners, key=lambda row: len(_normal_path(row["path"])))


@lru_cache(maxsize=None)
def _directory_is_skipped(
    directory: str, root: str, prune_projects: bool,
) -> bool:
    probe = Path(directory) / "__inktable_policy_probe__.txt"
    return should_skip_path(probe, root, check_markers=prune_projects)


def build_cleanup_plan(conn: sqlite3.Connection) -> CleanupPlan:
    """Build a deterministic cleanup plan without changing the database."""
    sources = conn.execute(
        "SELECT id, path FROM sources WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    rows = conn.execute(
        """SELECT f.id, f.path, f.name, f.ext, f.source_id,
                  f.confirmed_by_user, f.preserved_path,
                  EXISTS(SELECT 1 FROM book_members bm WHERE bm.file_id = f.id) AS in_book,
                  EXISTS(SELECT 1 FROM file_tags ft WHERE ft.file_id = f.id) AS tagged
           FROM files f ORDER BY f.id"""
    ).fetchall()

    protected = 0
    reassignments: list[tuple[int, int]] = []
    removals: list[FileRemoval] = []
    _directory_is_skipped.cache_clear()

    for row in rows:
        owner = _best_source(row["path"], sources)
        effective_source_id = owner["id"] if owner else row["source_id"]
        if owner and effective_source_id != row["source_id"]:
            reassignments.append((row["id"], effective_source_id))

        is_protected = bool(
            row["confirmed_by_user"]
            or row["preserved_path"]
            or row["in_book"]
            or row["tagged"]
        )
        if is_protected:
            protected += 1
            continue

        if classify_ext(row["ext"] or "") == "ignore":
            removals.append(FileRemoval(row["id"], "extension"))
            continue
        if should_skip_file_name(row["name"] or Path(row["path"]).name):
            removals.append(FileRemoval(row["id"], "filename"))
            continue
        if owner:
            policy = resolve_source_policy(owner["path"])
            if _directory_is_skipped(
                str(Path(row["path"]).parent),
                str(policy.root),
                policy.prune_projects,
            ):
                removals.append(FileRemoval(row["id"], "directory"))

    return CleanupPlan(
        inspected=len(rows),
        protected=protected,
        reassignments=tuple(reassignments),
        removals=tuple(removals),
    )


def _batches(values: tuple, size: int) -> Iterable[tuple]:
    if size < 1:
        raise ValueError("batch size must be positive")
    for start in range(0, len(values), size):
        yield values[start:start + size]


def apply_cleanup_plan(
    conn: sqlite3.Connection,
    plan: CleanupPlan,
    *,
    file_batch_size: int = 500,
    orphan_batch_size: int = 25,
    rebuild_threshold: int = 1000,
    cleanup_orphans: bool = True,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, int]:
    """Apply a previously reviewed plan with a commit after every batch."""
    reassigned = 0
    for batch in _batches(plan.reassignments, file_batch_size):
        conn.executemany(
            "UPDATE files SET source_id = ? WHERE id = ?",
            [(source_id, file_id) for file_id, source_id in batch],
        )
        conn.commit()
        reassigned += len(batch)
        if progress:
            progress("reassign", reassigned, len(plan.reassignments))

    removal_ids = tuple(item.file_id for item in plan.removals)
    removed = 0
    for batch in _batches(removal_ids, file_batch_size):
        conn.executemany("DELETE FROM files WHERE id = ?", [(file_id,) for file_id in batch])
        conn.commit()
        removed += len(batch)
        if progress:
            progress("files", removed, len(removal_ids))

    orphan_total = count_orphan_contents(conn)
    rebuilt = cleanup_orphans and orphan_total >= rebuild_threshold
    if not cleanup_orphans:
        orphans_cleaned = 0
    elif rebuilt:
        rebuild_result = rebuild_virtual_indexes_after_orphan_cleanup(conn, progress=progress)
        orphans_cleaned = rebuild_result["contents_removed"]
    else:
        orphans_cleaned = 0
        while True:
            cleaned = cleanup_orphan_contents(conn, limit=orphan_batch_size)
            conn.commit()
            orphans_cleaned += cleaned
            if progress:
                progress("contents", orphans_cleaned, orphan_total)
            if cleaned < orphan_batch_size:
                break

    return {
        "reassigned": reassigned,
        "files_removed": removed,
        "contents_removed": orphans_cleaned,
        "contents_remaining": count_orphan_contents(conn),
        "index_rebuilt": int(rebuilt),
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _populate_empty_virtual_indexes(
    conn: sqlite3.Connection,
    vector_rows: list[sqlite3.Row | tuple],
    *,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, int]:
    chunks = conn.execute(
        "SELECT id, text, section_path FROM chunks ORDER BY id"
    ).fetchall()
    conn.executemany(
        "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
        [
            (
                row["id"],
                segment_for_index(
                    f"{row['section_path']}\n{row['text']}"
                    if row["section_path"] else row["text"]
                ),
            )
            for row in chunks
        ],
    )
    conn.executemany(
        "INSERT INTO chunks_fts_tri(rowid, text) VALUES (?, ?)",
        [
            (
                row["id"],
                f"{row['section_path']}\n{row['text']}"
                if row["section_path"] else row["text"],
            )
            for row in chunks
        ],
    )
    if progress:
        progress("chunks_fts", len(chunks), len(chunks))

    documents = conn.execute(
        "SELECT id, title, summary_text FROM document_representations ORDER BY id"
    ).fetchall()
    conn.executemany(
        "INSERT INTO documents_fts(rowid, text) VALUES (?, ?)",
        [
            (row["id"], segment_for_index(f"{row['title']}\n{row['summary_text']}"))
            for row in documents
        ],
    )
    sections = conn.execute(
        "SELECT id, heading_path, summary_text FROM sections ORDER BY id"
    ).fetchall()
    conn.executemany(
        "INSERT INTO sections_fts(rowid, text) VALUES (?, ?)",
        [
            (
                row["id"],
                segment_for_index(f"{row['heading_path']}\n{row['summary_text']}"),
            )
            for row in sections
        ],
    )
    conn.executemany(
        "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
        [(row[0], row[1]) for row in vector_rows],
    )
    if progress:
        hierarchy_total = len(documents) + len(sections)
        progress("hierarchy_fts", hierarchy_total, hierarchy_total)
        progress("restore_vectors", len(vector_rows), len(vector_rows))

    checks = {
        "chunks_fts": (len(chunks), conn.execute(
            "SELECT count(*) FROM chunks_fts").fetchone()[0]),
        "chunks_fts_tri": (len(chunks), conn.execute(
            "SELECT count(*) FROM chunks_fts_tri").fetchone()[0]),
        "documents_fts": (len(documents), conn.execute(
            "SELECT count(*) FROM documents_fts").fetchone()[0]),
        "sections_fts": (len(sections), conn.execute(
            "SELECT count(*) FROM sections_fts").fetchone()[0]),
        "chunks_vec": (len(vector_rows), conn.execute(
            "SELECT count(*) FROM chunks_vec").fetchone()[0]),
    }
    mismatches = {name: values for name, values in checks.items() if values[0] != values[1]}
    if mismatches:
        raise RuntimeError(f"rebuilt index count mismatch: {mismatches}")
    return {
        "chunks": len(chunks),
        "documents": len(documents),
        "sections": len(sections),
        "vectors": len(vector_rows),
    }


def rebuild_virtual_indexes_after_orphan_cleanup(
    conn: sqlite3.Connection,
    *,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, int]:
    """Rebuild virtual indexes when most indexed content became orphaned.

    Deleting hundreds of thousands of FTS5/vec0 rows is pathologically slow.
    Keeping the much smaller live set and rebuilding is both faster and easier
    to verify. The destructive portion is one SQLite transaction, so a crash
    restores every original virtual table.
    """
    if conn.in_transaction:
        conn.commit()
    orphan_count = count_orphan_contents(conn)
    if orphan_count == 0:
        return {
            "contents_removed": 0,
            "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
            "vectors": 0,
        }

    conn.execute("DROP TABLE IF EXISTS temp.inktable_kept_vectors")
    conn.execute(
        "CREATE TEMP TABLE inktable_kept_vectors "
        "(rowid INTEGER PRIMARY KEY, embedding BLOB NOT NULL)"
    )
    if _table_exists(conn, "chunks_vec"):
        conn.execute(
            """INSERT INTO temp.inktable_kept_vectors(rowid, embedding)
               SELECT v.rowid, v.embedding
               FROM chunks_vec v JOIN chunks ch ON ch.id = v.rowid
               WHERE EXISTS (
                   SELECT 1 FROM files f WHERE f.content_id = ch.content_id
               )"""
        )
    kept_vectors = conn.execute(
        "SELECT count(*) FROM temp.inktable_kept_vectors"
    ).fetchone()[0]
    conn.commit()
    if progress:
        progress("preserve_vectors", kept_vectors, kept_vectors)

    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in (
            "chunks_fts", "chunks_fts_tri", "documents_fts", "sections_fts", "chunks_vec",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        if progress:
            progress("drop_virtual_indexes", 1, 1)

        before_contents = conn.execute("SELECT count(*) FROM contents").fetchone()[0]
        conn.execute(
            """DELETE FROM contents
               WHERE NOT EXISTS (
                   SELECT 1 FROM files f WHERE f.content_id = contents.id
               )"""
        )
        after_contents = conn.execute("SELECT count(*) FROM contents").fetchone()[0]
        removed = before_contents - after_contents
        if progress:
            progress("contents", removed, orphan_count)

        conn.execute(
            """CREATE VIRTUAL TABLE chunks_fts USING fts5(
                   text, content='', contentless_delete=1, tokenize='unicode61'
               )"""
        )
        conn.execute(
            """CREATE VIRTUAL TABLE chunks_fts_tri USING fts5(
                   text, content='', contentless_delete=1, tokenize='trigram'
               )"""
        )
        conn.execute(
            """CREATE VIRTUAL TABLE documents_fts USING fts5(
                   text, content='', contentless_delete=1, tokenize='unicode61'
               )"""
        )
        conn.execute(
            """CREATE VIRTUAL TABLE sections_fts USING fts5(
                   text, content='', contentless_delete=1, tokenize='unicode61'
               )"""
        )
        conn.execute(
            f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{DIM}])"
        )

        vector_rows = conn.execute(
            "SELECT rowid, embedding FROM temp.inktable_kept_vectors ORDER BY rowid"
        ).fetchall()
        index_counts = _populate_empty_virtual_indexes(
            conn, vector_rows, progress=progress,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.inktable_kept_vectors")
        conn.commit()

    return {
        "contents_removed": removed,
        "chunks": index_counts["chunks"],
        "vectors": index_counts["vectors"],
    }


_COPY_TABLES = (
    "sources",
    "contents",
    "categories",
    "tags",
    "books",
    "files",
    "rules",
    "file_tags",
    "chunks",
    "document_representations",
    "sections",
    "index_versions",
    "tasks",
    "operations",
    "file_history",
    "book_members",
    "settings",
)

_COPY_FILTERS = {
    "contents": "EXISTS (SELECT 1 FROM source_db.files f WHERE f.content_id = src.id)",
    "chunks": "EXISTS (SELECT 1 FROM source_db.files f WHERE f.content_id = src.content_id)",
    "document_representations": (
        "EXISTS (SELECT 1 FROM source_db.files f WHERE f.content_id = src.content_id)"
    ),
    "sections": "EXISTS (SELECT 1 FROM source_db.files f WHERE f.content_id = src.content_id)",
    "index_versions": (
        "EXISTS (SELECT 1 FROM source_db.files f WHERE f.content_id = src.content_id)"
    ),
    "file_tags": "EXISTS (SELECT 1 FROM source_db.files f WHERE f.id = src.file_id)",
    "book_members": "EXISTS (SELECT 1 FROM source_db.files f WHERE f.id = src.file_id)",
    "operations": (
        "src.file_id IS NULL OR EXISTS "
        "(SELECT 1 FROM source_db.files f WHERE f.id = src.file_id)"
    ),
    "file_history": "EXISTS (SELECT 1 FROM source_db.files f WHERE f.id = src.file_id)",
    "tasks": (
        "src.file_id IS NULL OR EXISTS "
        "(SELECT 1 FROM source_db.files f WHERE f.id = src.file_id)"
    ),
}


def _table_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _copy_relational_table(
    conn: sqlite3.Connection,
    table: str,
) -> int:
    source_columns = set(_table_columns(conn, "source_db", table))
    columns = [
        name for name in _table_columns(conn, "main", table) if name in source_columns
    ]
    if not columns:
        raise RuntimeError(f"no compatible columns for table {table}")
    quoted = ", ".join(f'"{name}"' for name in columns)
    where = _COPY_FILTERS.get(table)
    where_sql = f" WHERE {where}" if where else ""
    mode = "INSERT OR REPLACE" if table == "settings" else "INSERT"
    conn.execute(
        f'{mode} INTO main."{table}" ({quoted}) '
        f'SELECT {quoted} FROM source_db."{table}" AS src{where_sql}'
    )
    return conn.execute(f'SELECT count(*) FROM main."{table}"').fetchone()[0]


def build_compacted_database(
    source_path: Path | str,
    target_path: Path | str,
    *,
    add_system_sources: bool = True,
    vacuum: bool = True,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, object]:
    """Build a clean database from the live relational subset of a noisy one."""
    from app.db.database import connect, init_db, integrity_check, quick_check

    source = Path(source_path).resolve()
    target = Path(target_path).resolve()
    if source == target:
        raise ValueError("source and target databases must differ")
    if target.exists() or target.with_name(target.name + "-wal").exists():
        raise FileExistsError(f"clean database target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    conn: sqlite3.Connection | None = None
    try:
        conn = connect(target)
        init_db(conn)
        conn.execute("ATTACH DATABASE ? AS source_db", (str(source),))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        copied: dict[str, int] = {}
        for position, table in enumerate(_COPY_TABLES, start=1):
            copied[table] = _copy_relational_table(conn, table)
            if progress:
                progress("copy_relational", position, len(_COPY_TABLES))
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

        foreign_key_errors = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        if foreign_key_errors:
            raise RuntimeError(f"foreign key violations: {foreign_key_errors[:5]}")

        vector_rows = conn.execute(
            """SELECT v.rowid, v.embedding
               FROM source_db.chunks_vec v JOIN main.chunks ch ON ch.id = v.rowid
               ORDER BY v.rowid"""
        ).fetchall()
        conn.execute("BEGIN")
        index_counts = _populate_empty_virtual_indexes(
            conn, vector_rows, progress=progress,
        )
        conn.commit()

        system_sources = {"added": [], "enabled": []}
        if add_system_sources:
            system_sources = enable_planned_system_sources(conn)

        expected = {
            "files": conn.execute("SELECT count(*) FROM source_db.files").fetchone()[0],
            "contents": conn.execute(
                """SELECT count(*) FROM source_db.contents c
                   WHERE EXISTS (
                       SELECT 1 FROM source_db.files f WHERE f.content_id = c.id
                   )"""
            ).fetchone()[0],
            "chunks": conn.execute(
                """SELECT count(*) FROM source_db.chunks ch
                   WHERE EXISTS (
                       SELECT 1 FROM source_db.files f WHERE f.content_id = ch.content_id
                   )"""
            ).fetchone()[0],
        }
        actual = {
            "files": conn.execute("SELECT count(*) FROM main.files").fetchone()[0],
            "contents": conn.execute("SELECT count(*) FROM main.contents").fetchone()[0],
            "chunks": conn.execute("SELECT count(*) FROM main.chunks").fetchone()[0],
        }
        if expected != actual:
            raise RuntimeError(f"compacted row count mismatch: expected={expected}, actual={actual}")

        conn.execute("ANALYZE")
        conn.commit()
        conn.execute("DETACH DATABASE source_db")
        if vacuum:
            if progress:
                progress("vacuum", 0, 1)
            conn.execute("VACUUM")
            if progress:
                progress("vacuum", 1, 1)
        checkpoint = list(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        if not quick_check(conn):
            raise RuntimeError("compacted database quick_check failed")
        diagnostics = integrity_check(conn)
        if diagnostics != ["ok"]:
            raise RuntimeError(f"compacted database integrity_check failed: {diagnostics[:5]}")
        metrics = cleanup_metrics(conn)
        conn.close()
        conn = None
        return {
            "copied": copied,
            "indexes": index_counts,
            "system_sources": system_sources,
            "checkpoint": checkpoint,
            "metrics": metrics,
            "integrity_check": diagnostics,
            "target": str(target),
        }
    except Exception:
        if conn is not None:
            conn.close()
        for path in (
            target,
            target.with_name(target.name + "-wal"),
            target.with_name(target.name + "-shm"),
        ):
            path.unlink(missing_ok=True)
        raise


def install_compacted_database(
    source_path: Path | str,
    replacement_path: Path | str,
) -> dict[str, object]:
    """Checkpoint the old database and atomically install a verified replacement."""
    from app.db.database import backup_is_restorable, connect, integrity_check, quick_check

    source = Path(source_path).resolve()
    replacement = Path(replacement_path).resolve()
    if source.parent != replacement.parent:
        raise ValueError("atomic replacement must be in the same directory")
    if not backup_is_restorable(replacement):
        raise RuntimeError("replacement database is not restorable")

    old = connect(source)
    checkpoint = list(old.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    old.close()
    if checkpoint != [0, 0, 0]:
        raise RuntimeError(f"old database checkpoint did not drain WAL: {checkpoint}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = source.with_name(f"{source.stem}.noisy-{stamp}{source.suffix}")
    if archive.exists():
        raise FileExistsError(f"archive already exists: {archive}")
    for suffix in ("-wal", "-shm"):
        companion = source.with_name(source.name + suffix)
        if companion.exists() and companion.stat().st_size:
            raise RuntimeError(f"non-empty database companion remains: {companion}")
        companion.unlink(missing_ok=True)

    os.replace(source, archive)
    try:
        os.replace(replacement, source)
        final = connect(source)
        try:
            if not quick_check(final):
                raise RuntimeError("installed database quick_check failed")
            diagnostics = integrity_check(final)
            if diagnostics != ["ok"]:
                raise RuntimeError(f"installed database integrity_check failed: {diagnostics[:5]}")
            metrics = cleanup_metrics(final)
            final_checkpoint = list(final.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        finally:
            final.close()
    except Exception:
        failed = source.with_name(f"{source.stem}.failed-{stamp}{source.suffix}")
        if source.exists():
            os.replace(source, failed)
        os.replace(archive, source)
        raise
    return {
        "archive": str(archive),
        "checkpoint": checkpoint,
        "final_checkpoint": final_checkpoint,
        "metrics": metrics,
        "integrity_check": diagnostics,
    }


def enable_planned_system_sources(
    conn: sqlite3.Connection,
    candidates: list[Source] | None = None,
) -> dict[str, list[str]]:
    """Enable the four system sources required by the Windows ingestion plan."""
    wanted = [
        source for source in (candidates if candidates is not None else discover_system())
        if source.name in SYSTEM_SOURCE_NAMES
    ]
    existing = conn.execute("SELECT id, path, enabled FROM sources").fetchall()
    by_path = {_normal_path(row["path"]): row for row in existing}
    added: list[str] = []
    enabled: list[str] = []

    for source in wanted:
        key = _normal_path(source.path)
        row = by_path.get(key)
        if row:
            if not row["enabled"]:
                conn.execute("UPDATE sources SET enabled = 1 WHERE id = ?", (row["id"],))
                enabled.append(source.name)
            continue
        conn.execute(
            """INSERT INTO sources
               (name, path, kind, discovered_by, volatile, enabled,
                permission_ok, permission_checked_at, created_at)
               VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)""",
            (
                source.name,
                source.path,
                source.kind,
                source.discovered_by,
                int(source.volatile),
                time.time(),
                time.time(),
            ),
        )
        added.append(source.name)
    conn.commit()
    return {"added": added, "enabled": enabled}


def cleanup_metrics(conn: sqlite3.Connection) -> dict[str, object]:
    """Return stable, non-content metrics used for post-cleanup acceptance."""
    embedded, missing = conn.execute(
        """SELECT
               sum(CASE WHEN ch.embedding_model_id IS NOT NULL THEN 1 ELSE 0 END),
               sum(CASE WHEN ch.embedding_model_id IS NULL THEN 1 ELSE 0 END)
           FROM chunks ch JOIN contents c ON c.id = ch.content_id
           WHERE ch.index_version = c.active_index_version"""
    ).fetchone()
    return {
        "sources": conn.execute("SELECT count(*) FROM sources").fetchone()[0],
        "files": conn.execute("SELECT count(*) FROM files").fetchone()[0],
        "visible_files": conn.execute(
            """SELECT count(*) FROM files f LEFT JOIN sources s ON s.id = f.source_id
               WHERE (f.source_id IS NULL OR s.enabled = 1)
                 AND f.state != 'ignored'
                 AND (f.state != 'missing' OR coalesce(f.preserved_path, '') != '')"""
        ).fetchone()[0],
        "contents": conn.execute("SELECT count(*) FROM contents").fetchone()[0],
        "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],
        "embedded_active_chunks": int(embedded or 0),
        "unembedded_active_chunks": int(missing or 0),
        "orphan_contents": count_orphan_contents(conn),
    }
