"""用户排除的目录子树。

入库白名单按**扩展名**过滤，而 `.md` 同时是用户笔记和每个代码仓库样板的
格式。实测真实库里 `.md` 占可见文件 41%，大头是克隆的第三方项目的 `docs/`。

试过用启发式自动识别，两条都不成立：

* 打开整盘的代码项目剪枝 —— 会把用户写在带代码标记目录里的面试笔记、
  实现路线图一起排除（6266 个可见文件里 5409 个消失）。
* 按目录名把 `docs/` 当项目文档 —— 会删掉用户**自己**项目的设计文档，
  `InkHole/docs` 下的「墨洞项目计划」正是 gold 评测 X07 题依赖的资料。

第三方克隆项目与用户自己的项目在路径上无法区分，只有用户知道。所以这里
提供的是机制而不是猜测：用户点掉哪个目录，哪个目录就不再进检索。

**只作用于索引层**：不移动、不改名、不删除磁盘上的任何文件（§1 约束 1），
文件记录也保留（置为 `ignored`），取消排除后重新扫描即可恢复。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.watcher.scanner import _normal_path, path_is_within


def list_exclusions(conn: sqlite3.Connection) -> list[str]:
    return [
        row["path"] for row in conn.execute(
            "SELECT path FROM excluded_paths ORDER BY path"
        )
    ]


def is_excluded(path: str | Path, exclusions: list[str] | None = None,
                conn: sqlite3.Connection | None = None) -> bool:
    """路径是否落在任一排除目录内（含目录本身）。

    调用方通常先取一次 exclusions 再批量判断，避免每个文件查一次库。
    """
    if exclusions is None:
        if conn is None:
            return False
        exclusions = list_exclusions(conn)
    if not exclusions:
        return False
    target = _normal_path(path)
    return any(
        target == _normal_path(root) or path_is_within(path, root)
        for root in exclusions
    )


def add_exclusion(conn: sqlite3.Connection, path: str | Path) -> dict:
    """排除一个目录，并把其下已入库的文件标记为 ignored。

    返回受影响的文件数。不动磁盘，也不删记录 —— 取消排除后可恢复。
    """
    canonical = str(Path(path))
    conn.execute(
        "INSERT OR IGNORE INTO excluded_paths(path, created_at) VALUES (?, ?)",
        (canonical, time.time()),
    )
    # 用 path_is_within 而不是 LIKE 前缀：LIKE 会把 `B:\foo2` 当成 `B:\foo`
    # 的子路径，Windows 上还要处理大小写与分隔符
    affected = [
        row["id"] for row in conn.execute(
            "SELECT id, path FROM files WHERE state != 'ignored'"
        )
        if path_is_within(row["path"], canonical)
    ]
    for batch in _batches(affected, 400):
        marks = ",".join("?" * len(batch))
        conn.execute(
            f"UPDATE files SET state = 'ignored', indexed_at = NULL "
            f"WHERE id IN ({marks})",
            batch,
        )
    return {"path": canonical, "files_hidden": len(affected)}


def remove_exclusion(conn: sqlite3.Connection, path: str | Path) -> dict:
    """取消排除。已被隐藏的记录恢复为 registered，等下次扫描重新索引。"""
    canonical = str(Path(path))
    conn.execute("DELETE FROM excluded_paths WHERE path = ?", (canonical,))
    remaining = list_exclusions(conn)
    restored = [
        row["id"] for row in conn.execute(
            "SELECT id, path FROM files WHERE state = 'ignored'"
        )
        if path_is_within(row["path"], canonical)
        and not is_excluded(row["path"], remaining)
    ]
    for batch in _batches(restored, 400):
        marks = ",".join("?" * len(batch))
        conn.execute(
            f"UPDATE files SET state = 'registered' WHERE id IN ({marks})", batch,
        )
    return {"path": canonical, "files_restored": len(restored)}


def _batches(values: list, size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def noisy_directory_candidates(
    conn: sqlite3.Connection, *, depth: int = 3, limit: int = 20,
    min_files: int = 20,
) -> list[dict]:
    """按可见文件数列出最"占地方"的目录，供用户挑选要排除哪些。

    只统计不判断 —— 哪个目录是噪声由用户决定。
    """
    from app.db.visibility import VISIBLE_FILES_COND

    rows = conn.execute(
        f"""SELECT f.path, lower(coalesce(f.ext, '')) AS ext
            FROM files f LEFT JOIN sources s ON s.id = f.source_id
            WHERE {VISIBLE_FILES_COND}"""
    ).fetchall()

    buckets: dict[str, dict] = {}
    for row in rows:
        parts = Path(row["path"]).parts
        if len(parts) <= depth:
            continue
        key = str(Path(*parts[:depth + 1]))
        bucket = buckets.setdefault(key, {"path": key, "files": 0, "exts": {}})
        bucket["files"] += 1
        bucket["exts"][row["ext"]] = bucket["exts"].get(row["ext"], 0) + 1

    ranked = sorted(
        (b for b in buckets.values() if b["files"] >= min_files),
        key=lambda b: -b["files"],
    )
    excluded = list_exclusions(conn)
    for bucket in ranked:
        bucket["already_excluded"] = is_excluded(bucket["path"], excluded)
    return ranked[:limit]
