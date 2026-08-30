"""迁库前的一次性核对：被清理掉的白名单文件是否全是工具链噪声。

只读，不写任何库。用于在把隔离 working 库迁回真实库之前，确认这次覆盖
不会抹掉用户的真实文档 —— 只抹掉 H15 目录黑名单本该挡住的依赖/工具链噪声。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

REAL = Path.home() / "Library" / "Application Support" / "Ordo" / "library.db"
WORKING = Path(__file__).resolve().parents[3] / "output" / "release-gate-20260817" / "library-working.db"

WHITELIST = {".txt", ".docx", ".pdf", ".md", ".csv", ".html", ".htm"}

# 工具链 / 依赖 / 系统目录标记，统一用正斜杠比对
TOOLCHAIN_MARKS = (
    "/go/pkg/mod", "node_modules", "site-packages", "/python/doc/",
    "/.venv", "/venv/", "dist-info", "/.cargo", "/vendor/", "__pycache__",
    "/appdata/", "/program files", "/.cache", "conda", "/.rustup",
    "/lib/", "/.git/", "/target/", "/build/", "/.tox",
)


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def is_toolchain(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    return any(mark in lowered for mark in TOOLCHAIN_MARKS)


def main() -> int:
    if not REAL.is_file():
        print(f"真实库不存在：{REAL}")
        return 1
    real, working = open_ro(REAL), open_ro(WORKING)
    try:
        kept = {
            (row["volume_uuid"], row["inode"])
            for row in working.execute("SELECT volume_uuid, inode FROM files")
        }
        removed = [
            row for row in real.execute(
                "SELECT volume_uuid, inode, path, lower(coalesce(ext, '')) AS ext FROM files"
            )
            if (row["volume_uuid"], row["inode"]) not in kept
        ]
    finally:
        real.close()
        working.close()

    whitelisted = [row for row in removed if row["ext"] in WHITELIST]
    toolchain = [row for row in whitelisted if is_toolchain(row["path"])]
    other = [row for row in whitelisted if not is_toolchain(row["path"])]

    print(f"覆盖真实库会移除的索引记录：{len(removed)} 条")
    print(f"  其中白名单扩展名：{len(whitelisted)} 条")
    print(f"    落在工具链/依赖/系统目录：{len(toolchain)} "
          f"({len(toolchain) / max(len(whitelisted), 1) * 100:.1f}%)")
    print(f"    其余：{len(other)} 条")
    if other:
        print("\n  其余全部路径（需人工确认这些不是真实文档）：")
        for row in other[:60]:
            print("     ", row["path"])
        if len(other) > 60:
            print(f"      ... 还有 {len(other) - 60} 条")
    else:
        print("\n  没有落在工具链目录之外的白名单文件 —— 覆盖不会丢失真实文档。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
