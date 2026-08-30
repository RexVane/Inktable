"""量准 B:\\devcache 与 rustup 文档树在已登记文件里的占比。

盘点发现 .html 占已登记文件的 94%，且集中在 rustup 的 stable/nightly 两套
`share/doc/rust/html` 文档树 —— 两套内容相同，这也是 17,807 个 content 有
多副本的来源。排除前必须量准：排掉之后还剩什么，会不会连真资料一起排掉。

只读，不改任何东西。
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
                  "Ordo", "library.db")
conn = sqlite3.connect("file:{}?mode=ro".format(db.replace("\\", "/")), uri=True)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT path, ext FROM files WHERE state = 'registered'").fetchall()
total = len(rows)


def norm(p: str) -> str:
    return p.casefold().replace("/", "\\")


DEVCACHE = "b:\\devcache\\"
RUST_DOC = "share\\doc\\rust\\html"
SHARE_DOC = "share\\doc\\"
TARGET_BUILD = ("\\target\\release\\", "\\target\\debug\\")

probes = [
    ("B:\\devcache 下", lambda s: DEVCACHE in s),
    ("rustup toolchains 下", lambda s: "rustup" in s and "toolchains" in s),
    ("share\\doc\\rust\\html 下", lambda s: RUST_DOC in s),
    ("任意 share\\doc 下", lambda s: SHARE_DOC in s),
    ("target\\release|debug 下", lambda s: any(m in s for m in TARGET_BUILD)),
]

print("已登记总数: %d" % total)
for label, pred in probes:
    n = sum(1 for r in rows if pred(norm(r["path"])))
    print("  %-26s %7d  (%.1f%%)" % (label, n, 100.0 * n / max(1, total)))

rest = [r for r in rows if DEVCACHE not in norm(r["path"])]
print("\n=== 排除 B:\\devcache 后 ===")
print("剩余文件: %d" % len(rest))
print("扩展名:", dict(collections.Counter(
    (r["ext"] or "").casefold() for r in rest).most_common(8)))
print("剩余目录热点:")
for d, n in collections.Counter(
        os.path.dirname(r["path"]) for r in rest).most_common(8):
    print("   %5d  %s" % (n, d[:88]))

# 真资料抽样：确认 devcache 里没有夹带用户自己写的东西
print("\n=== devcache 里的非 .html 文件（若有真资料会在这里露出）===")
inside = [r for r in rows
          if DEVCACHE in norm(r["path"]) and (r["ext"] or "").casefold() != ".html"]
print("非 .html 数量: %d" % len(inside))
print("扩展名:", dict(collections.Counter(
    (r["ext"] or "").casefold() for r in inside).most_common(8)))
for r in inside[:8]:
    print("   ", r["path"][:110])

conn.close()
