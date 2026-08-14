"""信息层分类 —— PLAN §9 categories / A6。

**分类是文件信息的分类，不是目录分类**（v6 定稿）：
`categories` 是纯虚拟树，磁盘上永远不存在对应文件夹。
改分类 = 改一个字段，文件本身毫发无损 —— 所以不需要确认卡点（§2.3）。

规则引擎：扩展名 / 来源 / 文件名关键词 三条件 AND（NULL = 不限），
按 priority 升序，首条命中即停。规则来自两处：
  · 用户手建
  · 用户手动归类时的「回流学习」（§11.4）—— 归类一个文件后可一键
    生成"同来源同类型都这么归"的规则，并回溯应用到存量文件。
"""

from __future__ import annotations

import fnmatch
import sqlite3
import time

AUTO_EXT_ROOT = "按扩展名"
AUTO_EXT_CONFIDENCE = 0.66
AUTO_EXT_RULE_PRIORITY = 900


class CategoryError(RuntimeError):
    pass


# ---------------------------------------------------------------- 分类树

def create_category(conn: sqlite3.Connection, name: str, parent_id: int | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise CategoryError("分类名不能为空")
    if parent_id is not None:
        if conn.execute("SELECT 1 FROM categories WHERE id = ?", (parent_id,)).fetchone() is None:
            raise CategoryError("父分类不存在")
    dup = conn.execute(
        "SELECT 1 FROM categories WHERE name = ? AND parent_id IS ?",
        (name, parent_id),
    ).fetchone()
    if dup:
        raise CategoryError(f"同级已有「{name}」")
    cur = conn.execute(
        "INSERT INTO categories (name, parent_id) VALUES (?, ?)", (name, parent_id)
    )
    return cur.lastrowid


def rename_category(conn: sqlite3.Connection, cat_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise CategoryError("分类名不能为空")
    if conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, cat_id)).rowcount == 0:
        raise CategoryError("分类不存在")


def delete_category(conn: sqlite3.Connection, cat_id: int) -> None:
    """删除分类。非空（有文件或有子分类）时拒绝 —— 防误删（方案 §13）。"""
    n_files = conn.execute(
        "SELECT count(*) c FROM files WHERE category_id = ?", (cat_id,)
    ).fetchone()["c"]
    if n_files:
        raise CategoryError(f"分类下还有 {n_files} 个文件，先移走或改归类")
    n_children = conn.execute(
        "SELECT count(*) c FROM categories WHERE parent_id = ?", (cat_id,)
    ).fetchone()["c"]
    if n_children:
        raise CategoryError("分类下还有子分类")
    conn.execute("DELETE FROM rules WHERE category_id = ?", (cat_id,))
    if conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,)).rowcount == 0:
        raise CategoryError("分类不存在")


def category_tree(conn: sqlite3.Connection) -> list[dict]:
    """带文件数的树（扁平列表 + depth，前端好渲染）。

    计数只含可见文件：停用来源的文件不计入（与 /stats、/files 同口径）。
    """
    rows = conn.execute(
        """SELECT c.id, c.parent_id, c.name, c.sort_order,
                  (SELECT count(*) FROM files f
                   LEFT JOIN sources s ON s.id = f.source_id
                   WHERE f.category_id = c.id AND f.state != 'missing'
                     AND (f.source_id IS NULL OR s.enabled = 1)) AS file_count
           FROM categories c ORDER BY c.sort_order, c.name"""
    ).fetchall()
    by_parent: dict[int | None, list] = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)

    out: list[dict] = []

    def walk(parent: int | None, depth: int):
        for r in by_parent.get(parent, []):
            out.append({"id": r["id"], "parent_id": r["parent_id"], "name": r["name"],
                        "depth": depth, "file_count": r["file_count"]})
            walk(r["id"], depth + 1)

    walk(None, 0)
    # 子树文件数汇总（父分类显示"自己 + 全部后代"）
    total: dict[int, int] = {e["id"]: e["file_count"] for e in out}
    for e in reversed(out):
        if e["parent_id"] is not None:
            total[e["parent_id"]] = total.get(e["parent_id"], 0) + total[e["id"]]
    for e in out:
        e["total_count"] = total[e["id"]]
    return out


# ---------------------------------------------------------------- 归类

def assign_category(
    conn: sqlite3.Connection,
    file_ids: list[int],
    category_id: int | None,
    by: str = "user",
) -> int:
    """批量归类（category_id=None 表示移出分类）。写 file_history 可追溯。"""
    if category_id is not None:
        if conn.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone() is None:
            raise CategoryError("分类不存在")
    if not file_ids:
        return 0
    batch = f"batch-{int(time.time() * 1000)}"
    now = time.time()
    n = 0
    for fid in file_ids:
        old = conn.execute(
            "SELECT category_id FROM files WHERE id = ?", (fid,)
        ).fetchone()
        if old is None:
            continue
        conn.execute(
            "UPDATE files SET category_id = ?, confirmed_by_user = ? WHERE id = ?",
            (category_id, 1 if by == "user" else 0, fid),
        )
        conn.execute(
            "INSERT INTO file_history (file_id, field, old, new, by, batch_id, at) "
            "VALUES (?,?,?,?,?,?,?)",
            (fid, "category_id", str(old["category_id"]), str(category_id), by, batch, now),
        )
        n += 1
    return n


# ---------------------------------------------------------------- 规则引擎

def apply_rules(conn: sqlite3.Connection, name: str, ext: str,
                source_id: int | None) -> tuple[int, float] | None:
    """按 priority 顺序找第一条命中的规则。返回 (category_id, confidence)。"""
    rules = conn.execute(
        "SELECT id, priority, match_ext, match_source_id, match_name_pattern, "
        "category_id, confidence FROM rules ORDER BY priority, id"
    ).fetchall()
    ext = (ext or "").lower()
    for r in rules:
        if r["match_ext"] and r["match_ext"].lower() != ext:
            continue
        if r["match_source_id"] is not None and r["match_source_id"] != source_id:
            continue
        if r["match_name_pattern"] and not fnmatch.fnmatch(name, r["match_name_pattern"]):
            continue
        return r["category_id"], float(r["confidence"] or 0.8)
    return None


def classify_new_file(conn: sqlite3.Connection, file_id: int) -> bool:
    """给新登记的文件跑规则。**只碰未归类且用户没确认过的** ——
    规则永远不覆盖用户的手动决定（§11.4）。"""
    row = conn.execute(
        "SELECT name, ext, source_id, category_id, confirmed_by_user "
        "FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    if row is None or row["category_id"] is not None or row["confirmed_by_user"]:
        return False
    hit = apply_rules(conn, row["name"], row["ext"], row["source_id"])
    if hit is None:
        return False
    cat_id, conf = hit
    conn.execute(
        "UPDATE files SET category_id = ?, confidence = ? WHERE id = ?",
        (cat_id, conf, file_id),
    )
    return True


def create_rule(
    conn: sqlite3.Connection,
    category_id: int,
    match_ext: str | None = None,
    match_source_id: int | None = None,
    match_name_pattern: str | None = None,
    learned_from: int | None = None,
) -> int:
    if not any([match_ext, match_source_id, match_name_pattern]):
        raise CategoryError("规则至少要有一个匹配条件")
    if conn.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone() is None:
        raise CategoryError("分类不存在")
    cur = conn.execute(
        "INSERT INTO rules (priority, match_ext, match_source_id, match_name_pattern, "
        "category_id, confidence, learned_from_file_id) VALUES (100,?,?,?,?,0.8,?)",
        (match_ext, match_source_id, match_name_pattern, category_id, learned_from),
    )
    return cur.lastrowid


def backfill_rule(conn: sqlite3.Connection, rule_id: int) -> int:
    """把一条规则回溯应用到存量文件（§11.4）。

    只动「未归类且用户没确认过」的 —— 索引模式下这只是批量 UPDATE，
    代价接近零；这正是 v3 砍掉"移动归档"换来的红利。
    """
    r = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if r is None:
        raise CategoryError("规则不存在")
    rows = conn.execute(
        "SELECT id, name, ext, source_id FROM files "
        "WHERE category_id IS NULL AND confirmed_by_user = 0"
    ).fetchall()
    ext = (r["match_ext"] or "").lower()
    hit_ids = []
    for f in rows:
        if ext and (f["ext"] or "").lower() != ext:
            continue
        if r["match_source_id"] is not None and f["source_id"] != r["match_source_id"]:
            continue
        if r["match_name_pattern"] and not fnmatch.fnmatch(f["name"], r["match_name_pattern"]):
            continue
        hit_ids.append(f["id"])
    if hit_ids:
        marks = ",".join("?" * len(hit_ids))
        conn.execute(
            f"UPDATE files SET category_id = ?, confidence = ? WHERE id IN ({marks})",
            [r["category_id"], float(r["confidence"] or 0.8), *hit_ids],
        )
    return len(hit_ids)


def _normalize_ext(ext: str | None) -> str:
    value = (ext or "").strip().lower()
    if value and not value.startswith("."):
        value = "." + value
    return value


def _get_or_create_category(
    conn: sqlite3.Connection, name: str, parent_id: int | None
) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT id FROM categories WHERE name = ? AND parent_id IS ?",
        (name, parent_id),
    ).fetchone()
    if row is not None:
        return int(row["id"]), False
    return create_category(conn, name, parent_id), True


def _ensure_global_ext_rule(
    conn: sqlite3.Connection, ext: str, category_id: int
) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT id, category_id FROM rules "
        "WHERE match_ext = ? AND match_source_id IS NULL AND match_name_pattern IS NULL "
        "ORDER BY priority, id LIMIT 1",
        (ext,),
    ).fetchone()
    if row is not None:
        return int(row["category_id"]), False
    rid = conn.execute(
        "INSERT INTO rules (priority, match_ext, match_source_id, match_name_pattern, "
        "category_id, confidence, learned_from_file_id) VALUES (?, ?, NULL, NULL, ?, ?, NULL)",
        (AUTO_EXT_RULE_PRIORITY, ext, category_id, AUTO_EXT_CONFIDENCE),
    ).lastrowid
    return category_id, bool(rid)


def auto_classify_by_ext(conn: sqlite3.Connection) -> dict:
    """默认自动分类：把未分类文件按扩展名归入分类，不覆盖用户手工结果。"""
    rows = conn.execute(
        "SELECT id, ext FROM files "
        "WHERE state != 'missing' AND category_id IS NULL AND confirmed_by_user = 0"
    ).fetchall()
    if not rows:
        return {"classified": 0, "groups": 0, "categories_created": 0, "rules_created": 0}

    grouped: dict[str, list[int]] = {}
    for row in rows:
        key = _normalize_ext(row["ext"])
        grouped.setdefault(key, []).append(int(row["id"]))

    root_id = None
    categories_created = 0
    rules_created = 0
    classified = 0

    for ext, file_ids in grouped.items():
        target_category_id: int
        if ext:
            existing = conn.execute(
                "SELECT category_id FROM rules "
                "WHERE match_ext = ? AND match_source_id IS NULL AND match_name_pattern IS NULL "
                "ORDER BY priority, id LIMIT 1",
                (ext,),
            ).fetchone()
            if existing is not None:
                target_category_id = int(existing["category_id"])
            else:
                if root_id is None:
                    root_id, created = _get_or_create_category(conn, AUTO_EXT_ROOT, None)
                    categories_created += int(created)
                target_category_id, created = _get_or_create_category(conn, ext, root_id)
                categories_created += int(created)
                _, rule_created = _ensure_global_ext_rule(conn, ext, target_category_id)
                rules_created += int(rule_created)
        else:
            if root_id is None:
                root_id, created = _get_or_create_category(conn, AUTO_EXT_ROOT, None)
                categories_created += int(created)
            target_category_id, created = _get_or_create_category(conn, "无扩展名", root_id)
            categories_created += int(created)

        marks = ",".join("?" * len(file_ids))
        conn.execute(
            f"UPDATE files SET category_id = ?, confidence = ? "
            f"WHERE id IN ({marks}) AND category_id IS NULL AND confirmed_by_user = 0",
            [target_category_id, AUTO_EXT_CONFIDENCE, *file_ids],
        )
        classified += len(file_ids)

    return {
        "classified": classified,
        "groups": len(grouped),
        "categories_created": categories_created,
        "rules_created": rules_created,
    }
