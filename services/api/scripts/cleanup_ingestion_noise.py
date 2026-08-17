"""Safely clean ingestion noise from an Inktable database.

Dry-run is the default. Applying changes requires both ``--apply`` and a
verified ``--backup``. Source files are never modified.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.database import (  # noqa: E402
    DB_PATH,
    acquire_single_instance_lock,
    backup_is_restorable,
    connect,
    integrity_check,
    quick_check,
    release_single_instance_lock,
)
from app.maintenance.ingestion_cleanup import (  # noqa: E402
    apply_cleanup_plan,
    build_cleanup_plan,
    build_compacted_database,
    cleanup_metrics,
    enable_planned_system_sources,
    install_compacted_database,
)


def _readonly(path: Path) -> sqlite3.Connection:
    uri = "file:" + str(path.resolve()).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _progress(phase: str, done: int, total: int) -> None:
    _emit({"phase": phase, "done": done, "total": total})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--file-batch-size", type=int, default=500)
    parser.add_argument("--orphan-batch-size", type=int, default=25)
    parser.add_argument("--skip-system-sources", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    args = parser.parse_args()

    db_path = args.db.resolve()
    if not db_path.is_file():
        parser.error(f"database does not exist: {db_path}")
    if args.apply and not args.backup:
        parser.error("--apply requires --backup")
    if args.backup and not backup_is_restorable(args.backup):
        parser.error(f"backup is not restorable: {args.backup}")

    if db_path == DB_PATH.resolve():
        os.environ.pop("INKTABLE_DB", None)
    else:
        os.environ["INKTABLE_DB"] = str(db_path)
    acquire_single_instance_lock()
    conn: sqlite3.Connection | None = None
    try:
        if args.apply:
            conn = connect(db_path)
        else:
            conn = _readonly(db_path)
        if not quick_check(conn):
            raise RuntimeError("database quick_check failed")

        before = cleanup_metrics(conn)
        plan = build_cleanup_plan(conn)
        _emit({"mode": "apply" if args.apply else "dry-run", "before": before,
               "plan": plan.summary()})
        if not args.apply:
            return 0

        result = apply_cleanup_plan(
            conn,
            plan,
            file_batch_size=args.file_batch_size,
            orphan_batch_size=args.orphan_batch_size,
            cleanup_orphans=False,
            progress=_progress,
        )
        remaining_orphans = cleanup_metrics(conn)["orphan_contents"]
        system_sources = {"added": [], "enabled": []}
        install_result: dict[str, object] | None = None
        if remaining_orphans >= 1000:
            conn.close()
            conn = None
            target = db_path.with_name(
                f"{db_path.stem}.cleaned-{int(time.time())}{db_path.suffix}"
            )
            built = build_compacted_database(
                db_path,
                target,
                add_system_sources=not args.skip_system_sources,
                vacuum=args.vacuum,
                progress=_progress,
            )
            install_result = install_compacted_database(db_path, target)
            system_sources = built["system_sources"]
            result.update({
                "contents_removed": remaining_orphans,
                "contents_remaining": 0,
                "index_rebuilt": 1,
            })
            conn = connect(db_path)
        else:
            tail = apply_cleanup_plan(
                conn,
                build_cleanup_plan(conn),
                file_batch_size=args.file_batch_size,
                orphan_batch_size=args.orphan_batch_size,
                progress=_progress,
            )
            result.update({
                "contents_removed": tail["contents_removed"],
                "contents_remaining": tail["contents_remaining"],
                "index_rebuilt": tail["index_rebuilt"],
            })
            if not args.skip_system_sources:
                system_sources = enable_planned_system_sources(conn)
            if args.vacuum:
                _emit({"phase": "vacuum", "done": 0, "total": 1})
                conn.execute("VACUUM")
                _emit({"phase": "vacuum", "done": 1, "total": 1})

        checkpoint = list(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())

        diagnostics = integrity_check(conn)
        if diagnostics != ["ok"]:
            raise RuntimeError(f"integrity_check failed: {diagnostics[:5]}")
        _emit({
            "result": result,
            "system_sources": system_sources,
            "checkpoint": checkpoint,
            "install": install_result,
            "after": cleanup_metrics(conn),
            "integrity_check": diagnostics,
        })
        return 0
    finally:
        if conn is not None:
            conn.close()
        release_single_instance_lock()


if __name__ == "__main__":
    raise SystemExit(main())
