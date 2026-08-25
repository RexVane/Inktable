"""只读盘点真实库的 Document 层规模，用于规划摘要回填的量级。

只读打开（mode=ro）：不设 journal_mode、不写 WAL，不会与运行中的实例竞争。
"""
import os
import sqlite3

path = os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                    "Inktable", "library.db")
uri = "file:{}?mode=ro".format(path.replace("\\", "/"))
conn = sqlite3.connect(uri, uri=True)
conn.row_factory = sqlite3.Row

counts = [
    ("contents", "SELECT COUNT(*) n FROM contents"),
    ("document_representations", "SELECT COUNT(*) n FROM document_representations"),
    ("chunks", "SELECT COUNT(*) n FROM chunks"),
    ("files 可见", "SELECT COUNT(*) n FROM files WHERE status != 'ignored'"),
]
for label, q in counts:
    try:
        print("%-26s %s" % (label, conn.execute(q).fetchone()["n"]))
    except Exception as exc:  # 表可能不存在
        print("%-26s ERR %s" % (label, exc))

print()
row = conn.execute(
    "SELECT AVG(LENGTH(full_text)) a, MAX(LENGTH(full_text)) m,"
    "       AVG(LENGTH(summary_text)) s"
    "  FROM document_representations").fetchone()
print("full_text     平均 %.0f 字，最长 %s 字" % (row["a"] or 0, row["m"]))
print("summary_text  平均 %.0f 字（= full_text[:1000] 的截断）" % (row["s"] or 0))

# 摘要要花多少钱：按当前 Document 表示数估算
n = conn.execute("SELECT COUNT(*) n FROM document_representations").fetchone()["n"]
print()
print("若每篇一次 qwen3:8b 调用（本地，实测短输出约 1-3 秒）：")
print("  %d 篇 → 串行约 %.1f~%.1f 小时" % (n, n * 1.5 / 3600, n * 4.0 / 3600))
conn.close()
