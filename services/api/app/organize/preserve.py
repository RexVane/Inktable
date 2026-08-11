"""保全副本 —— PLAN §2.5 / A7。

**微信、QQ 会自行清理接收目录**。索引模式下文件留在原地，一旦被清理，
索引指向死路径。保全 = 把易失来源的文件**复制**一份到应用自己的空间。

三条铁律（§1 不可协商约束的延伸）：

  1. **复制，绝不移动** —— 失败最坏结果是多一个待清理的临时文件，
     原文件永远完好。
  2. **先落临时名，校验哈希后才转正** —— 复制中断不会留下半个"副本"
     冒充完整文件。
  3. **每次写盘记 operations 日志** —— 可审计、可撤销。

副本放 `~/Library/Application Support/Inktable/preserved/<来源>/`。
不放用户资料目录：那里可能在 iCloud 同步范围内（§7.8），
且用户整理文件时不该看到一堆来历不明的副本。
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import sqlite3
import time
from pathlib import Path

from app.db.database import APP_DIR

log = logging.getLogger("inktable.preserve")

PRESERVE_ROOT = APP_DIR / "preserved"
HASH_BLOCK = 8 * 1024 * 1024

# 文件名净化：路径分隔符与控制字符不能进文件名
_UNSAFE = re.compile(r'[/\\:\x00-\x1f]')


class PreserveError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(HASH_BLOCK):
            h.update(block)
    return h.hexdigest()


def _safe_name(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip() or "unnamed"
    return cleaned[:200]  # 255 字节上限，中文 UTF-8 占 3 字节，留余量


def _unique_dest(dir_: Path, name: str) -> Path:
    """重名时生成 name (2).ext —— 永不覆盖已有副本。"""
    dest = dir_ / name
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(2, 1000):
        cand = dir_ / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
    raise PreserveError(f"同名副本过多：{name}")


def preserve_file(conn: sqlite3.Connection, file_id: int) -> dict:
    """保全单个文件。幂等：已有有效副本时直接返回。"""
    row = conn.execute(
        """SELECT f.id, f.path, f.name, f.preserved_path, f.state,
                  c.sha256 AS content_sha, s.name AS source_name
           FROM files f
           LEFT JOIN contents c ON c.id = f.content_id
           LEFT JOIN sources s ON s.id = f.source_id
           WHERE f.id = ?""",
        (file_id,),
    ).fetchone()
    if row is None:
        raise PreserveError("文件不存在")

    # 幂等：副本还在且哈希对得上就不重复复制
    if row["preserved_path"]:
        prev = Path(row["preserved_path"])
        if prev.is_file() and (not row["content_sha"] or _sha256(prev) == row["content_sha"]):
            return {"file_id": file_id, "preserved_path": str(prev), "already": True}

    src = Path(row["path"])
    if not src.is_file():
        raise PreserveError("原文件已不存在，无法保全（这正是要提早保全的原因）")

    dest_dir = PRESERVE_ROOT / _safe_name(row["source_name"] or "手动")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(dest_dir, _safe_name(row["name"]))

    # 铁律 2：临时名 → 校验 → 转正。中断只留 .partial，不留假副本。
    tmp = dest.with_suffix(dest.suffix + ".partial")
    try:
        shutil.copy2(src, tmp)
        copied_sha = _sha256(tmp)
        # 原文件此刻的哈希可能已与登记时不同（被改写过）——
        # 以**原文件当前内容**为准校验复制完整性，而不是库里的旧哈希
        src_sha = _sha256(src)
        if copied_sha != src_sha:
            raise PreserveError("复制校验失败（复制过程中原文件被修改？）")
        tmp.rename(dest)
    except Exception:
        tmp.unlink(missing_ok=True)   # 铁律 1：清掉残留，原文件从未被动过
        raise

    conn.execute(
        "UPDATE files SET preserved_path = ? WHERE id = ?", (str(dest), file_id)
    )
    conn.execute(
        "INSERT INTO operations (file_id, kind, src_path, dst_path, method, "
        "sha256_before, created_at) VALUES (?,?,?,?,?,?,?)",
        (file_id, "preserve", str(src), str(dest), "copy", copied_sha, time.time()),
    )
    log.info("已保全：%s → %s", row["name"], dest)
    return {"file_id": file_id, "preserved_path": str(dest), "already": False}


def preserve_source(conn: sqlite3.Connection, source_id: int, limit: int = 10000) -> dict:
    """保全一个来源下的全部文件。单个失败不中断其余。"""
    rows = conn.execute(
        """SELECT id FROM files
           WHERE source_id = ? AND state NOT IN ('missing', 'cloud_placeholder')
             AND (preserved_path IS NULL OR preserved_path = '')
           LIMIT ?""",
        (source_id, limit),
    ).fetchall()

    done = failed = 0
    errors: list[str] = []
    for r in rows:
        try:
            preserve_file(conn, r["id"])
            done += 1
        except PreserveError as e:
            failed += 1
            if len(errors) < 5:
                errors.append(str(e))
        if (done + failed) % 50 == 0:
            conn.commit()
    conn.commit()
    return {"preserved": done, "failed": failed, "errors": errors}


def effective_path(row) -> str | None:
    """打开/定位文件时的实际路径：原件在用原件，原件没了用副本。

    这是保全存在的意义 —— 微信清了缓存，用户仍能打开内容。
    """
    p = row["path"]
    if p and Path(p).is_file():
        return p
    pp = row["preserved_path"] if "preserved_path" in row.keys() else None
    if pp and Path(pp).is_file():
        return pp
    return None
