"""只读盘点真实库里的噪声构成，为过滤规则提供依据而不是猜测。

用户报的现象是「快 10 万个文件」「检索效果和知识库很差」。界面第一屏几乎全是
哈希命名的 .html —— 那是浏览器缓存 / 打包产物，不是资料。本脚本量清楚：
到底哪些形态占了多少，好让过滤规则针对真实分布而不是印象。

只读打开，不改任何东西。
"""

from __future__ import annotations

import collections
import io
import os
import re
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

path = os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                    "Inktable", "library.db")
conn = sqlite3.connect("file:{}?mode=ro".format(path.replace("\\", "/")), uri=True)
conn.row_factory = sqlite3.Row

# 纯十六进制主名（缓存/内容寻址产物的典型形态）。长度 >= 16 才算：
# 短的十六进制名可能是真文件（如 "2024.md"、"a1.txt"）。
HEX_NAME = re.compile(r"^[0-9a-f]{16,}$")
# UUID 形态
UUID_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def stem(name: str) -> str:
    base = name.casefold()
    return base.rpartition(".")[0] or base


print("=== 总量 ===")
total = conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
visible = conn.execute(
    "SELECT COUNT(*) n FROM files WHERE state = 'registered'").fetchone()["n"]
print("files 总数 %d，已登记 %d" % (total, visible))

print("\n=== 扩展名分布（已登记，前 12）===")
rows = conn.execute(
    """SELECT LOWER(ext) e, COUNT(*) n FROM files
        WHERE state = 'registered'
        GROUP BY LOWER(ext) ORDER BY n DESC LIMIT 12""").fetchall()
for r in rows:
    print("  %-8s %7d" % (r["e"] or "(空)", r["n"]))

print("\n=== 哈希/UUID 命名的文件（已登记）===")
names = conn.execute(
    """SELECT name, path FROM files
        WHERE state = 'registered'""").fetchall()
hexish = [r for r in names if HEX_NAME.match(stem(r["name"]))]
uuidish = [r for r in names if UUID_NAME.match(stem(r["name"]))]
print("  纯十六进制主名 >=16 位：%d" % len(hexish))
print("  UUID 主名：%d" % len(uuidish))
by_ext = collections.Counter(
    (r["name"].rpartition(".")[2] or "").casefold() for r in hexish)
print("  其扩展名分布：", dict(by_ext.most_common(6)))

print("\n=== 这些文件集中在哪些目录（前 10）===")
dirs = collections.Counter(
    os.path.dirname(r["path"]).replace("/", "\\") for r in hexish)
for d, n in dirs.most_common(10):
    print("  %6d  %s" % (n, d[:100]))

print("\n=== 已登记文件的目录热点（前 12，看缓存目录是否在列）===")
alldirs = collections.Counter(
    os.path.dirname(r["path"]).replace("/", "\\") for r in names)
for d, n in alldirs.most_common(12):
    print("  %6d  %s" % (n, d[:100]))

print("\n=== 路径里含常见缓存/依赖目录名的已登记文件 ===")
MARKERS = ["\\cache\\", "\\Cache\\", "\\node_modules\\", "\\.git\\",
           "\\site-packages\\", "\\dist\\", "\\build\\", "\\.venv\\",
           "\\AppData\\Local\\Temp\\", "\\__pycache__\\", "\\.next\\",
           "\\vendor\\", "\\Code Cache\\", "\\GPUCache\\"]
for m in MARKERS:
    n = sum(1 for r in names if m.casefold() in r["path"].casefold())
    if n:
        print("  %-30s %7d" % (m.strip("\\"), n))

print("\n=== 重复内容（同一 content 被多个 file 引用）===")
dup = conn.execute(
    """SELECT COUNT(*) n FROM (
           SELECT content_id FROM files
            WHERE content_id IS NOT NULL AND state = 'registered'
            GROUP BY content_id HAVING COUNT(*) > 1)""").fetchone()["n"]
print("  有多副本的 content 数：%d" % dup)

conn.close()
