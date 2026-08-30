"""排除后的核对：可见文件降到多少、真资料是否都还在、gold 目标是否存活。

排除只作用于索引层（state='ignored'），磁盘文件一个都没动。这个脚本用只读
连接核对结果，并特别检查两件容易出错的事：

1. gold 评测依赖的资料有没有被连带排掉 —— 若排掉了，评测会凭空变好（题目
   的正确答案不在库里了，检索"没找到"反而不算错），指标就失去意义。
2. 用户自己写的内容（Documents / WPSDrive / 微信收到的文件）是否完整保留。
"""

from __future__ import annotations

import collections
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

REPO = Path(__file__).resolve().parents[3]
db = os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                  "Ordo", "library.db")
conn = sqlite3.connect("file:{}?mode=ro".format(db.replace("\\", "/")), uri=True)
conn.row_factory = sqlite3.Row

DEVCACHE = "b:\\devcache"


def norm(p: str) -> str:
    return p.casefold().replace("/", "\\")


print("排除项:", [r[0] for r in conn.execute("SELECT path FROM excluded_paths")])
print("state 分布:", dict(collections.Counter(
    r[0] for r in conn.execute("SELECT state FROM files"))))

rows = conn.execute(
    "SELECT path, ext, content_id FROM files WHERE state = 'registered'").fetchall()
print("\n可见文件: %d" % len(rows))
print("扩展名:", dict(collections.Counter(
    (r["ext"] or "").casefold() for r in rows).most_common(8)))
leftover = [r for r in rows if DEVCACHE in norm(r["path"])]
print("其中仍在 devcache 下: %d（应为 0）" % len(leftover))

print("\n可见文件的目录热点:")
for d, n in collections.Counter(
        os.path.dirname(r["path"]) for r in rows).most_common(10):
    print("   %5d  %s" % (n, d[:86]))

# 真资料存活核对
print("\n=== 真资料是否完整保留 ===")
for label, marker in [("Documents", "\\users\\guica\\documents\\"),
                      ("WPSDrive", "\\wpsdrive\\"),
                      ("微信收到", "\\wechat profiles\\"),
                      ("QQ 收到", "\\qq profiles")]:
    n = sum(1 for r in rows if marker in norm(r["path"]))
    print("  %-10s %5d 个可见" % (label, n))

# gold 目标存活核对 —— 这条最关键
gold_path = REPO / "docs" / "eval" / "gold-evidence-spans.json"
if gold_path.exists():
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    shas: set[str] = set()
    for case in gold["cases"]:
        for group in case.get("content_groups") or []:
            for sha in group.get("content_sha256") or []:
                shas.add(sha)
    visible_contents = {r["content_id"] for r in rows if r["content_id"]}
    alive = 0
    for sha in shas:
        row = conn.execute("SELECT id FROM contents WHERE sha256 = ?", (sha,)).fetchone()
        if row and row["id"] in visible_contents:
            alive += 1
    print("\n=== gold 证据 content 存活 ===")
    print("  gold 引用 %d 份 content，其中 %d 份仍可见" % (len(shas), alive))
    if alive < len(shas):
        print("  !! 有 gold 资料被排掉了 —— 评测会凭空变好，必须先修排除范围")

conn.close()
