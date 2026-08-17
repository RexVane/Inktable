"""Measure scanner traversal cost per enabled source without writing the DB."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.database import DB_PATH  # noqa: E402
from app.watcher.scanner import preview_source  # noqa: E402


def worker(path: str, limit: int) -> int:
    started = time.monotonic()
    result = preview_source(path, limit=limit)
    print(json.dumps({
        "path": path,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        **result,
    }, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--limit", type=int, default=100_000)
    parser.add_argument("--worker")
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker, args.limit)

    db = args.db.resolve()
    uri = "file:" + str(db).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    rows = conn.execute(
        "SELECT id, name, path FROM sources WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    conn.close()
    for source_id, name, path in rows:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            path,
            "--limit",
            str(args.limit),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=args.timeout,
                check=True,
            )
            result = json.loads(completed.stdout.strip().splitlines()[-1])
            result.update({"source_id": source_id, "name": name, "timed_out": False})
        except subprocess.TimeoutExpired:
            result = {
                "source_id": source_id,
                "name": name,
                "path": path,
                "timed_out": True,
                "timeout_seconds": args.timeout,
            }
        print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
