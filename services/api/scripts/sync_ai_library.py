"""Build or refresh the derived Ordo AI Library layer.

Usage from ``services/api``::

    uv run python scripts/sync_ai_library.py

The script never moves, copies, renames or deletes user files.  It only creates
or refreshes derived ``library_*`` rows inside the selected Ordo database.
"""

from __future__ import annotations

import json

from app.db.database import connect, init_db
from app.library.core import sync_library_items


def main() -> None:
    conn = connect()
    try:
        init_db(conn)
        conn.execute("BEGIN")
        try:
            result = sync_library_items(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        total = conn.execute("SELECT COUNT(*) FROM library_items").fetchone()[0]
        payload = {**result, "total": int(total)}
        print(json.dumps(payload, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
