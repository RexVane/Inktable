"""评估「整盘扫描也剪枝代码项目」这一策略翻转的影响（只读）。

现状：`policy.resolve_source_policy` 对固定盘根返回 `prune_projects=False`，
于是整盘收录只按扩展名白名单过滤，代码仓库的 docs/ 会整批进索引
（实测可见 .md 占全库 41%，大头全是仓库文档与 agent 的 skill.md）。

翻转后：整盘扫描会跳过含 .git / package.json / go.mod 等标记的项目树；
用户若确实要某个项目的文档，把该目录单独加成来源即可（来源根豁免标记检查）。

本工具在不改任何东西的前提下，算出翻转会让多少**当前可见**的文件不再入库，
并按扩展名和目录列出来，避免误杀真实资料。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import DB_PATH  # noqa: E402
from app.db.visibility import VISIBLE_FILES_COND  # noqa: E402
from app.watcher.scanner import should_skip_path  # noqa: E402

WHITELIST = {".txt", ".docx", ".pdf", ".md", ".csv", ".html", ".htm"}


def drive_root(path: str) -> str:
    return str(Path(path).anchor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path(DB_PATH))
    parser.add_argument("--show-dirs", type=int, default=18)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""SELECT f.path, lower(coalesce(f.ext, '')) AS ext
            FROM files f LEFT JOIN sources s ON s.id = f.source_id
            WHERE {VISIBLE_FILES_COND}"""
    ).fetchall()
    conn.close()

    dropped: list[sqlite3.Row] = []
    kept: list[sqlite3.Row] = []
    for row in rows:
        root = drive_root(row["path"])
        if not root:
            kept.append(row)
            continue
        # 翻转后整盘扫描等价于 check_markers=True
        if should_skip_path(row["path"], root, check_markers=True):
            dropped.append(row)
        else:
            kept.append(row)

    print(f"当前可见文件：{len(rows)}")
    print(f"翻转后不再入库：{len(dropped)}   仍保留：{len(kept)}\n")

    print("不再入库的按扩展名：")
    for ext, count in Counter(row["ext"] for row in dropped).most_common():
        total = sum(1 for row in rows if row["ext"] == ext)
        print(f"   {ext or '(无)':8s} {count:5d} / {total:5d}  "
              f"({count / max(total, 1) * 100:5.1f}%)")

    print("\n仍保留的按扩展名：")
    for ext, count in Counter(row["ext"] for row in kept).most_common():
        print(f"   {ext or '(无)':8s} {count:5d}")

    separator = "\\"
    print(f"\n不再入库的按前 3 级目录（前 {args.show_dirs}）：")
    buckets = Counter(
        separator.join(row["path"].split(separator)[:4]) for row in dropped
    )
    for directory, count in buckets.most_common(args.show_dirs):
        print(f"   {count:5d}  {directory}")

    # 真实资料最可能受影响的格式，单独列出来人工过目
    risky = [row for row in dropped if row["ext"] in {".pdf", ".docx", ".csv"}]
    print(f"\n其中 PDF / DOCX / CSV 共 {len(risky)} 个（这些最可能是真实资料，"
          f"请过目）：")
    for row in risky[:40]:
        print("     ", row["path"])
    if len(risky) > 40:
        print(f"      ... 还有 {len(risky) - 40} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
