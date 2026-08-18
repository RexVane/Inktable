"""按目录排除的契约测试。

排除是"用户决定"的机制，不是启发式 —— 自动识别试过两条路都会误删真资料
（详见 app/domain/exclusions.py 的模块说明）。这里守住三件事：
只影响索引层、边界不能用字符串前缀、取消排除可恢复。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.db.database import connect, init_db
from app.domain.exclusions import (
    add_exclusion,
    is_excluded,
    list_exclusions,
    noisy_directory_candidates,
    remove_exclusion,
)


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


def _source(conn, path: Path) -> int:
    return conn.execute(
        """INSERT INTO sources (name, path, kind, discovered_by, enabled, created_at)
           VALUES ('drive', ?, 'system', 'fixed_drive', 1, ?)""",
        (str(path), time.time()),
    ).lastrowid


def _file(conn, source_id: int, path: Path, ordinal: int) -> int:
    return conn.execute(
        """INSERT INTO files
           (volume_uuid, inode, path, name, source_id, ext, size, state,
            mtime, detected_at)
           VALUES ('v', ?, ?, ?, ?, ?, 1, 'registered', ?, ?)""",
        (ordinal, str(path), path.name, source_id, path.suffix.lower(),
         time.time(), time.time()),
    ).lastrowid


def test_exclusion_hides_subtree_without_deleting_records(db, tmp_path):
    root = tmp_path / "drive"
    noisy = root / "repo"
    keep = root / "资料"
    source_id = _source(db, root)
    hidden_id = _file(db, source_id, noisy / "docs" / "a.md", 1)
    kept_id = _file(db, source_id, keep / "笔记.md", 2)
    db.commit()

    result = add_exclusion(db, noisy)
    db.commit()

    assert result["files_hidden"] == 1
    states = dict(db.execute("SELECT id, state FROM files").fetchall())
    # 记录保留、只是不可见 —— 排除绝不删记录，更不动磁盘（§1 约束 1）
    assert states[hidden_id] == "ignored"
    assert states[kept_id] == "registered"
    assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 2


def test_unexclude_restores_previously_hidden_files(db, tmp_path):
    root = tmp_path / "drive"
    noisy = root / "repo"
    source_id = _source(db, root)
    _file(db, source_id, noisy / "a.md", 1)
    db.commit()

    add_exclusion(db, noisy)
    db.commit()
    result = remove_exclusion(db, noisy)
    db.commit()

    assert result["files_restored"] == 1
    assert list_exclusions(db) == []
    assert db.execute("SELECT state FROM files").fetchone()[0] == "registered"


def test_unexclude_keeps_files_still_covered_by_another_exclusion(db, tmp_path):
    """嵌套排除：取消外层后，仍被内层覆盖的文件不能被放出来。"""
    root = tmp_path / "drive"
    outer = root / "repo"
    inner = outer / "vendor"
    source_id = _source(db, root)
    inner_id = _file(db, source_id, inner / "a.md", 1)
    outer_id = _file(db, source_id, outer / "b.md", 2)
    db.commit()

    add_exclusion(db, outer)
    add_exclusion(db, inner)
    db.commit()
    remove_exclusion(db, outer)
    db.commit()

    states = dict(db.execute("SELECT id, state FROM files").fetchall())
    assert states[inner_id] == "ignored", "仍被 vendor 排除覆盖，不该恢复"
    assert states[outer_id] == "registered"


def test_sibling_with_shared_prefix_is_not_excluded(db, tmp_path):
    """边界必须按路径判定，不能用字符串前缀。

    `B:\\foo2` 不是 `B:\\foo` 的子目录，但 LIKE 'B:\\foo%' 会把它算进去。
    """
    root = tmp_path / "drive"
    target = root / "foo"
    sibling = root / "foo2"
    source_id = _source(db, root)
    target_id = _file(db, source_id, target / "a.md", 1)
    sibling_id = _file(db, source_id, sibling / "b.md", 2)
    db.commit()

    add_exclusion(db, target)
    db.commit()

    states = dict(db.execute("SELECT id, state FROM files").fetchall())
    assert states[target_id] == "ignored"
    assert states[sibling_id] == "registered", "同前缀的兄弟目录不该被排除"
    assert is_excluded(target / "a.md", [str(target)])
    assert not is_excluded(sibling / "b.md", [str(target)])


def test_candidates_rank_by_visible_files_and_flag_excluded(db, tmp_path):
    root = tmp_path / "drive"
    source_id = _source(db, root)
    big = root / "big" / "sub"
    small = root / "small" / "sub"
    for index in range(25):
        _file(db, source_id, big / f"{index}.md", index + 1)
    for index in range(5):
        _file(db, source_id, small / f"{index}.md", 100 + index)
    db.commit()

    ranked = noisy_directory_candidates(db, depth=len(root.parts), min_files=20)
    assert ranked, "应至少给出一个候选"
    assert ranked[0]["files"] == 25
    assert ranked[0]["already_excluded"] is False
    # 少于 min_files 的目录不进候选，避免把列表刷满
    assert all(bucket["files"] >= 20 for bucket in ranked)

    # 排除之后该目录的文件变为 ignored、不再可见，因此它从"可见文件最多"的
    # 候选列表里消失 —— 已排除的目录改由 list_exclusions 单独列出，
    # 两份列表合起来才是完整视图（CLI 与 /sources/exclusions 都同时给）。
    excluded_path = ranked[0]["path"]
    add_exclusion(db, excluded_path)
    db.commit()
    again = noisy_directory_candidates(db, depth=len(root.parts), min_files=1)
    assert excluded_path not in {bucket["path"] for bucket in again}
    assert excluded_path in list_exclusions(db)
