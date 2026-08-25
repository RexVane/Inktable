"""核实真资料目录里有没有被这次排除**连带**弄成 ignored 的文件。

候选列表里 WPSDrive 显示 753 个，排除后只数到 129 个可见。差额必须解释清楚：
若是本来就 missing（WPS 云盘占位文件、未下载到本地）那与排除无关；若是
state 被改成了 ignored，那就是排除范围写错了，必须回滚。
"""

from __future__ import annotations

import collections
import io
import os
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

db = os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                  "Inktable", "library.db")
conn = sqlite3.connect("file:{}?mode=ro".format(db.replace("\\", "/")), uri=True)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT path, state, is_dataless FROM files").fetchall()


def norm(p: str) -> str:
    return p.casefold().replace("/", "\\")


TARGETS = [
    ("WPSDrive", "wpsdrive"),
    ("Documents", "users\\guica\\documents"),
    ("WeChat", "wechat profiles"),
    ("QQ", "qq profiles"),
    ("OneDrive", "onedrive"),
]

bad = 0
for label, marker in TARGETS:
    sub = [r for r in rows if marker in norm(r["path"])]
    states = collections.Counter(r["state"] for r in sub)
    dataless = sum(1 for r in sub if r["is_dataless"])
    print("%-10s 总 %5d  %s  is_dataless=%d"
          % (label, len(sub), dict(states), dataless))
    # 这些目录都不在 B:\devcache 下，任何 ignored 都说明排除范围过宽
    if states.get("ignored"):
        bad += states["ignored"]
        print("   !! 有 %d 个被置为 ignored，排除范围可能过宽：" % states["ignored"])
        for r in [x for x in sub if x["state"] == "ignored"][:5]:
            print("      ", r["path"][:96])

print()
print("真资料目录里被连带排除的文件数: %d（应为 0）" % bad)
conn.close()
raise SystemExit(1 if bad else 0)
