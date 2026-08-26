"""只读核实：剪枝后的真实库仍能检索到真资料。

剪的是索引行，动的是真实用户库。断言「可见分片一个没少」是结构性的；
这里再走一遍真正的多路检索，确认拿回来的是真文档而不是空壳。
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.index.search import search  # noqa: E402

DB = os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                  "Inktable", "library.db")

QUERIES = ["银行家算法", "检索延迟", "文件身份 inode", "微信"]

conn = sqlite3.connect("file:{}?mode=ro".format(DB.replace("\\", "/")), uri=True)
conn.row_factory = sqlite3.Row
try:
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
except Exception as e:  # noqa: BLE001
    print("sqlite-vec 未加载（向量路会缺席）：%s" % e)

bad = 0
for q in QUERIES:
    routes = search(conn, q, limit=20)
    hits = {cid for route in routes.values() for cid, _ in route}
    if not hits:
        print("%-14s 0 命中  <<< 检索路全空" % q)
        bad += 1
        continue
    ids = sorted(hits)[:3]
    marks = ",".join("?" * len(ids))
    names = [r["name"] for r in conn.execute(
        f"""SELECT DISTINCT f.name FROM chunks ch
            JOIN files f ON f.content_id = ch.content_id
            WHERE ch.id IN ({marks}) AND f.state != 'ignored' LIMIT 3""", ids)]
    per_route = {k: len(v) for k, v in routes.items() if v}
    print("%-14s %3d 命中  %s  例: %s" % (q, len(hits), per_route, names))
    if not names:
        print("   !! 命中的分片没有可见文件 —— 剪枝把可见性搞坏了")
        bad += 1

print("\n检索异常项: %d（应为 0）" % bad)
conn.close()
raise SystemExit(1 if bad else 0)
