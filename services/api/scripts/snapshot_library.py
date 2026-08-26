"""把线上库做成一致快照，供剪枝实验用。

用 SQLite backup API 而不是文件拷贝：应用正在运行，WAL 里有未合并的事务，
直接 copy 出来的 .db 可能缺最近的写入或落在事务中间。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time

DEFAULT_SRC = os.path.join(os.path.expanduser("~"), "Library",
                           "Application Support", "Inktable", "library.db")


def snapshot(src_path: str, dst_path: str) -> None:
    t0 = time.time()
    uri = "file:" + src_path.replace("\\", "/") + "?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=30)
    if os.path.exists(dst_path):
        os.remove(dst_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA quick_check").fetchone()[0]
        chunks = dst.execute("SELECT count(*) FROM chunks").fetchone()[0]
        files = dst.execute("SELECT count(*) FROM files").fetchone()[0]
    finally:
        dst.close()
        src.close()
    print("quick_check=%s  chunks=%d  files=%d" % (check, chunks, files))
    print("%.0f MB in %.0fs -> %s"
          % (os.path.getsize(dst_path) / 1e6, time.time() - t0, dst_path))
    if check != "ok":
        raise SystemExit("快照未通过 quick_check，不要在它上面做实验")


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 2 else DEFAULT_SRC
    snapshot(src, sys.argv[-1])
