"""把隔离验收库迁回真实数据库（带备份与验证）。

发布验收全程在隔离库 `output/release-gate-<date>/library-working.db` 上完成：
它经过入库白名单清理、工具链目录清理和向量回填，向量覆盖率 100%。真实库
仍是清理前的状态（索引里混着 Go 模块缓存、Python 文档等工具链噪声，向量
覆盖率不到一半）。本脚本把验收过的状态搬到真实库。

**不动磁盘上的任何原始文件** —— 迁移只发生在索引层。被移除的只是索引记录，
且迁移前请先跑 `scripts/audit_migration_loss.py` 确认这些记录全是工具链噪声。

两个必须处理的细节：

1. **旧 WAL 是陷阱**。真实库带着上百 MB 的 `-wal`。若只覆盖 `.db` 而留下
   旧的 `-wal` / `-shm`，SQLite 下次打开会把旧 WAL 回放到新库上，直接损坏。
   所以必须连 `-wal` / `-shm` 一起清掉。
2. **用 SQLite 的 backup API，不用文件复制**。backup() 会穿过 WAL 读出
   一致快照并写成单文件，源库即使有未合并的 WAL 也安全；直接 copy 文件则
   可能拿到撕裂状态。

用法：
    .venv/Scripts/python.exe scripts/migrate_working_to_real.py --dry-run
    .venv/Scripts/python.exe scripts/migrate_working_to_real.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import APP_DIR, DB_PATH  # noqa: E402

DEFAULT_WORKING = (
    Path(__file__).resolve().parents[3]
    / "output" / "release-gate-20260817" / "library-working.db"
)
SIDECARS = ("-wal", "-shm")


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_vec(conn: sqlite3.Connection) -> None:
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as exc:  # noqa: BLE001
        print(f"  警告：sqlite-vec 未加载，向量统计不可用（{exc}）")


def describe(label: str, path: Path) -> dict:
    conn = open_ro(path)
    load_vec(conn)
    try:
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        stats = {
            "files": one("SELECT count(*) FROM files"),
            "contents": one("SELECT count(*) FROM contents"),
            "chunks": one(
                "SELECT count(*) FROM chunks ch JOIN contents ct"
                " ON ct.id = ch.content_id AND ch.index_version = ct.active_index_version"
            ),
            "quick_check": one("PRAGMA quick_check"),
        }
        try:
            stats["vectors"] = one("SELECT count(*) FROM chunks_vec")
        except sqlite3.Error:
            stats["vectors"] = None
    finally:
        conn.close()
    coverage = (
        f"{stats['vectors'] / stats['chunks'] * 100:.1f}%"
        if stats["vectors"] is not None and stats["chunks"] else "—"
    )
    print(f"  {label}: files={stats['files']} contents={stats['contents']} "
          f"活跃chunks={stats['chunks']} 向量={stats['vectors']} 覆盖={coverage} "
          f"quick_check={stats['quick_check']}")
    return stats


def snapshot(source: Path, target: Path) -> None:
    """用 backup API 导出一致的单文件快照（穿过 WAL，源库可只读）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = open_ro(source)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--working", type=Path, default=DEFAULT_WORKING)
    parser.add_argument("--real", type=Path, default=Path(DB_PATH))
    parser.add_argument("--apply", action="store_true", help="真正执行迁移")
    parser.add_argument("--dry-run", action="store_true", help="只体检不落地（默认）")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply 与 --dry-run 互斥")

    real, working = args.real, args.working
    if not working.is_file():
        print(f"隔离库不存在：{working}")
        return 1
    if not real.is_file():
        print(f"真实库不存在：{real}")
        return 1

    lock = APP_DIR / "inktable.lock"
    print(f"真实库 : {real}")
    print(f"隔离库 : {working}")
    print(f"单实例锁存在：{lock.is_file()}（迁移前请确认桌面端已退出）\n")

    print("迁移前：")
    before_real = describe("真实库", real)
    before_working = describe("隔离库", working)

    if not args.apply:
        print("\n[dry-run] 未做任何改动。确认无误后加 --apply。")
        print("          务必先跑 scripts/audit_migration_loss.py 核对被移除的记录。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = APP_DIR / "backups" / f"library-pre-migration-{stamp}.db"
    print(f"\n1/4 备份真实库 → {backup}")
    snapshot(real, backup)
    print(f"    备份 {backup.stat().st_size / 1e6:.0f}MB")
    describe("备份校验", backup)

    staged = real.with_name(f"library-migrating-{stamp}.db")
    print(f"\n2/4 从隔离库导出一致快照 → {staged.name}")
    snapshot(working, staged)
    describe("待上线快照", staged)

    print("\n3/4 移除旧库与旧 WAL/SHM，换上新快照")
    for suffix in SIDECARS:
        stale = Path(str(real) + suffix)
        if stale.exists():
            stale.unlink()
            print(f"    删除陈旧 {stale.name}（留着会被回放到新库上）")
    real.unlink()
    shutil.move(str(staged), str(real))
    print(f"    已就位：{real.name}")

    print("\n4/4 验证")
    after = describe("迁移后真实库", real)
    conn = open_ro(real)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    print(f"  integrity_check={integrity}  外键错误={len(fk)}")

    ok = (
        after["files"] == before_working["files"]
        and after["contents"] == before_working["contents"]
        and after["chunks"] == before_working["chunks"]
        and after["vectors"] == before_working["vectors"]
        and after["quick_check"] == "ok"
        and integrity == "ok"
        and not fk
    )
    print(f"\n结果：{'成功' if ok else '不一致，请用备份回滚'}")
    print(f"  files {before_real['files']} → {after['files']}")
    print(f"  向量  {before_real['vectors']} → {after['vectors']}")
    print(f"  回滚：把 {backup} 复制回 {real}（并删掉同名 -wal/-shm）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
