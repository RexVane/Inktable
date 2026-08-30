"""Run Ordo AI Library enrichment explicitly with the local Ollama model.

From ``services/api``::

    uv run python scripts/enrich_ai_library.py
    uv run python scripts/enrich_ai_library.py --limit 5 --all

The command acquires the same single-instance database lock as the desktop
sidecar. If Ordo is already running, it exits instead of becoming a second
writer. Document excerpts are sent only through the local-Ollama enrichment
worker; this command never falls through to the cloud QA provider.
"""

from __future__ import annotations

import argparse
import json
import threading

from app.db.database import (
    AlreadyRunning,
    acquire_single_instance_lock,
    connect,
    init_db,
    release_single_instance_lock,
)
from app.library.enrichment import run_enrichment_batch, status


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich Ordo AI Library locally")
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        choices=range(1, 11),
        metavar="1..10",
        help="items claimed per batch (default: 3)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="continue bounded batches until no useful progress remains",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=100,
        help="safety ceiling for --all (default: 100)",
    )
    return parser.parse_args()


def main() -> int:
    args = _args()
    if args.max_batches < 1:
        raise SystemExit("--max-batches 必须大于 0")

    try:
        acquire_single_instance_lock()
    except AlreadyRunning:
        print(json.dumps({
            "ok": False,
            "error": "Ordo 正在运行；请关闭桌面端后再使用独立 enrichment CLI",
        }, ensure_ascii=False))
        return 2

    conn = None
    try:
        conn = connect()
        init_db(conn)
        capability = status()
        print(json.dumps({"event": "status", **capability}, ensure_ascii=False))
        if not capability["available"]:
            return 3

        lock = threading.Lock()
        for batch_no in range(1, args.max_batches + 1):
            result = run_enrichment_batch(
                lambda: conn,
                lock,
                limit=args.limit,
            )
            print(json.dumps(
                {"event": "batch", "batch": batch_no, **result},
                ensure_ascii=False,
            ))

            if not args.all:
                break
            if not result.get("available", False) or result.get("claimed", 0) == 0:
                break
            # Avoid retry-spinning forever on one permanently invalid document.
            if result.get("ready", 0) == 0 and result.get("stale", 0) == 0:
                break
        return 0
    finally:
        if conn is not None:
            conn.close()
        release_single_instance_lock()


if __name__ == "__main__":
    raise SystemExit(main())
