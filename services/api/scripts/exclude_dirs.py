"""按目录排除噪声（列出候选 / 排除 / 取消排除）。

入库白名单按**扩展名**过滤，而 `.md` 同时是用户笔记和代码仓库样板的格式，
实测占可见文件 41%。自动识别试过两条路都不成立：整盘剪代码项目会误删写在
带代码标记目录里的面试笔记；按 `docs/` 目录名排除会误删用户自己项目的设计
文档（`InkHole/docs` 下的「墨洞项目计划」正是 gold 评测 X07 依赖的资料）。
第三方克隆项目与自己的项目在路径上无法区分 —— 只有你知道。

所以这里只统计、不判断，由你挑。**不动磁盘上的任何文件**：排除只把索引记录
置为 ignored，取消排除后重新扫描即可恢复。

    # 看哪些目录最占地方
    .venv/Scripts/python.exe scripts/exclude_dirs.py --list

    # 排除（可多个）
    .venv/Scripts/python.exe scripts/exclude_dirs.py --add "B:/openclaw" --add "B:/TradingAgent"

    # 取消
    .venv/Scripts/python.exe scripts/exclude_dirs.py --remove "B:/openclaw"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import (  # noqa: E402
    AlreadyRunning,
    acquire_single_instance_lock,
    connect,
    init_db,
    release_single_instance_lock,
)
from app.domain.exclusions import (  # noqa: E402
    add_exclusion,
    list_exclusions,
    noisy_directory_candidates,
    remove_exclusion,
)


def _show(conn, *, depth: int, limit: int) -> None:
    excluded = list_exclusions(conn)
    print(f"已排除的目录（{len(excluded)}）：")
    for path in excluded:
        print(f"   {path}")
    if not excluded:
        print("   （无）")

    print(f"\n可见文件最多的目录（前 {limit}，深度 {depth}）：")
    print(f"   {'文件数':>7}  {'状态':<8} 目录 / 主要格式")
    for bucket in noisy_directory_candidates(conn, depth=depth, limit=limit):
        top = sorted(bucket["exts"].items(), key=lambda kv: -kv[1])[:3]
        exts = "  ".join(f"{ext or '(无)'}×{count}" for ext, count in top)
        state = "已排除" if bucket["already_excluded"] else ""
        print(f"   {bucket['files']:>7}  {state:<8} {bucket['path']}")
        print(f"   {'':>7}  {'':<8}   {exts}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="列出已排除与候选目录")
    parser.add_argument("--add", action="append", default=[], help="排除一个目录（可重复）")
    parser.add_argument("--remove", action="append", default=[], help="取消排除（可重复）")
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    if not (args.list or args.add or args.remove):
        parser.error("至少给一个动作：--list / --add / --remove")

    try:
        acquire_single_instance_lock()
    except AlreadyRunning:
        print("Inktable 正在运行 —— 请先退出桌面端，避免两个进程同时写库（H18）。")
        return 1

    try:
        conn = connect()
        init_db(conn)
        try:
            for path in args.add:
                result = add_exclusion(conn, path)
                conn.commit()
                print(f"已排除 {result['path']}：隐藏 {result['files_hidden']} 个文件记录")
            for path in args.remove:
                result = remove_exclusion(conn, path)
                conn.commit()
                print(f"已取消排除 {result['path']}："
                      f"恢复 {result['files_restored']} 个文件记录")
            if args.list or args.add or args.remove:
                print()
                _show(conn, depth=args.depth, limit=args.limit)
        finally:
            conn.close()
    finally:
        release_single_instance_lock()

    if args.add or args.remove:
        print("\n提示：排除立即生效于浏览/检索/问答；下次扫描不再收录被排除的目录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
