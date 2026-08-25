from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from app.db.database import backup_is_restorable, connect, init_db
from app.discovery.sources import Source
from app.index import vector
from app.index.embedding import DIM
from app.index.pipeline import cleanup_orphan_contents, count_orphan_contents
from app.maintenance.ingestion_cleanup import (
    apply_cleanup_plan,
    build_cleanup_plan,
    build_compacted_database,
    enable_planned_system_sources,
    install_compacted_database,
    rebuild_virtual_indexes_after_orphan_cleanup,
)


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


def _add_orphan_content(conn, ordinal: int) -> tuple[int, int]:
    content_id = conn.execute(
        "INSERT INTO contents (sha256, size, parse_state) VALUES (?, 1, 'indexed')",
        (f"{ordinal:064x}",),
    ).lastrowid
    chunk_id = conn.execute(
        """INSERT INTO chunks
           (content_id, ordinal, text, text_hash, embedding_model_id, index_version)
           VALUES (?, 0, ?, ?, 'test-model', 1)""",
        (content_id, f"orphan-{ordinal}", f"hash-{ordinal}"),
    ).lastrowid
    conn.execute(
        "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
        (chunk_id, f"orphan-{ordinal}"),
    )
    conn.execute(
        "INSERT INTO chunks_fts_tri(rowid, text) VALUES (?, ?)",
        (chunk_id, f"orphan-{ordinal}"),
    )
    vector.upsert(conn, [(chunk_id, np.zeros(DIM, dtype=np.float32))])
    return content_id, chunk_id


def test_orphan_cleanup_is_bounded_and_keeps_indexes_in_sync(db):
    for ordinal in range(3):
        _add_orphan_content(db, ordinal)
    db.commit()

    assert cleanup_orphan_contents(db, limit=2) == 2
    db.commit()
    assert count_orphan_contents(db) == 1
    assert db.execute("SELECT count(*) FROM contents").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM chunks_fts_tri").fetchone()[0] == 1
    assert vector.count(db) == 1

    assert cleanup_orphan_contents(db, limit=2) == 1
    db.commit()
    assert count_orphan_contents(db) == 0
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    assert vector.count(db) == 0


def test_large_cleanup_rebuilds_virtual_indexes_from_live_rows(db, tmp_path):
    kept_content, kept_chunk = _add_orphan_content(db, 100)
    for ordinal in range(101, 106):
        _add_orphan_content(db, ordinal)
    source_id = _insert_source(db, "S", tmp_path)
    path = tmp_path / "kept.txt"
    path.write_text("kept", encoding="utf-8")
    file_id = _insert_historical_file(db, source_id, path, 100)
    db.execute("UPDATE files SET content_id = ? WHERE id = ?", (kept_content, file_id))
    db.commit()

    result = rebuild_virtual_indexes_after_orphan_cleanup(db)
    assert result == {"contents_removed": 5, "chunks": 1, "vectors": 1}
    assert count_orphan_contents(db) == 0
    assert db.execute("SELECT id FROM chunks").fetchone()[0] == kept_chunk
    assert db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM chunks_fts_tri").fetchone()[0] == 1
    assert vector.count(db) == 1


def _insert_source(conn, name: str, path: Path) -> int:
    return conn.execute(
        """INSERT INTO sources
           (name, path, kind, discovered_by, enabled, created_at)
           VALUES (?, ?, 'manual', 'manual', 1, ?)""",
        (name, str(path), time.time()),
    ).lastrowid


def _insert_historical_file(
    conn,
    source_id: int,
    path: Path,
    ordinal: int,
    *,
    confirmed: bool = False,
) -> int:
    return conn.execute(
        """INSERT INTO files
           (volume_uuid, inode, path, name, origin_path, source_id, ext, size,
            state, confirmed_by_user, mtime, detected_at)
           VALUES ('test', ?, ?, ?, ?, ?, ?, ?, 'registered', ?, ?, ?)""",
        (
            ordinal,
            str(path),
            path.name,
            str(path),
            source_id,
            path.suffix.lower(),
            path.stat().st_size,
            int(confirmed),
            path.stat().st_mtime,
            time.time(),
        ),
    ).lastrowid


def test_cleanup_repairs_nested_source_and_never_touches_original_files(db, tmp_path):
    root = tmp_path / "drive"
    nested = root / "explicit-project"
    noisy_project = root / "tool-source"
    nested.mkdir(parents=True)
    noisy_project.mkdir()
    (nested / "package.json").write_text("{}", encoding="utf-8")
    (nested / "design.md").write_text("user selected project notes", encoding="utf-8")
    (noisy_project / "package.json").write_text("{}", encoding="utf-8")
    (noisy_project / "README.md").write_text("bundled software manual", encoding="utf-8")
    (root / "notes.txt").write_text("useful notes", encoding="utf-8")
    (root / "tool.py").write_text("print('noise')", encoding="utf-8")
    (root / "LICENSE.txt").write_text("license noise", encoding="utf-8")
    (root / "keep.py").write_text("manually retained", encoding="utf-8")

    broad_id = _insert_source(db, "drive", root)
    nested_id = _insert_source(db, "explicit", nested)
    paths = [
        nested / "design.md",
        noisy_project / "package.json",
        noisy_project / "README.md",
        root / "notes.txt",
        root / "tool.py",
        root / "LICENSE.txt",
    ]
    for ordinal, path in enumerate(paths, start=1):
        _insert_historical_file(db, broad_id, path, ordinal)
    keep_id = _insert_historical_file(
        db, broad_id, root / "keep.py", len(paths) + 1, confirmed=True
    )
    db.commit()

    plan = build_cleanup_plan(db)
    assert plan.summary()["reassignments"] == 1
    assert plan.summary()["removals"] == 4
    assert plan.reason_counts == {"directory": 1, "extension": 2, "filename": 1}

    result = apply_cleanup_plan(db, plan, file_batch_size=2, orphan_batch_size=2)
    assert result == {
        "reassigned": 1,
        "files_removed": 4,
        "contents_removed": 0,
        "contents_remaining": 0,
        "index_rebuilt": 0,
    }
    remaining = db.execute(
        "SELECT id, name, source_id FROM files ORDER BY id"
    ).fetchall()
    assert {row["name"] for row in remaining} == {"design.md", "notes.txt", "keep.py"}
    design = next(row for row in remaining if row["name"] == "design.md")
    assert design["source_id"] == nested_id
    assert next(row for row in remaining if row["id"] == keep_id)
    assert all(path.exists() for path in paths)
    assert (root / "keep.py").exists()


def test_cleanup_fixed_drive_policy_keeps_project_content_but_drops_boilerplate(
    db, tmp_path, monkeypatch,
):
    """固定盘策略下，代码项目里的**真实内容**保留，**仓库样板**清掉。

    这条用例原先断言项目里的 README.md 也要保留（"important user document"）。
    2026-08-18 按真实使用反馈反转了 README 这一半：真实库里 readme.md 有
    348 个副本、skill.md 有 265 个，可见文件里 .md 占 41%，用户报告
    "很多没用的 .md 文件在干扰"。仓库样板是给仓库读的说明，不是个人知识。

    反转的范围被刻意限窄，只动样板文件名：
      · 整盘扫描仍然不剪整棵项目树 —— 实测剪了会连 OneDrive / WPSDrive 里的
        个人简历、证件、升学材料一起排除，6266 个可见文件里 5409 个会消失。
      · 项目里非样板名的文档照常保留（下面 design-notes.md 这一条守住它）。
      · 用户若确实要某个项目的样板文档，把该目录单独加成来源即可 ——
        来源根本身豁免标记检查。
    """
    from app.maintenance import ingestion_cleanup
    from app.watcher.policy import ScanPolicy

    root = tmp_path / "drive"
    project = root / "project"
    project.mkdir(parents=True)
    (project / "package.json").write_text("{}", encoding="utf-8")
    readme = project / "README.md"
    readme.write_text("repo boilerplate", encoding="utf-8")
    notes = project / "design-notes.md"
    notes.write_text("用户自己写在项目里的设计笔记", encoding="utf-8")
    source_id = _insert_source(db, "drive", root)
    _insert_historical_file(db, source_id, project / "package.json", 1)
    readme_id = _insert_historical_file(db, source_id, readme, 2)
    notes_id = _insert_historical_file(db, source_id, notes, 3)
    db.commit()

    monkeypatch.setattr(
        ingestion_cleanup,
        "resolve_source_policy",
        lambda _path: ScanPolicy(root=root, prune_projects=False),
    )
    plan = build_cleanup_plan(db)

    removed = {item.file_id for item in plan.removals}
    assert 1 in removed, "package.json 不在入库白名单内，应清掉"
    assert readme_id in removed, "代码项目里的 README 是仓库样板，应清掉"
    assert notes_id not in removed, "项目里非样板名的文档必须保留"


def test_enable_planned_system_sources_is_idempotent(db, tmp_path):
    downloads = tmp_path / "Downloads"
    pictures = tmp_path / "Pictures"
    downloads.mkdir()
    pictures.mkdir()
    _insert_source(db, "old downloads", downloads)
    db.execute("UPDATE sources SET enabled = 0 WHERE path = ?", (str(downloads),))
    db.commit()
    candidates = [
        Source("下载", str(downloads), "system", "default"),
        Source("图片", str(pictures), "system", "default"),
        Source("桌面", str(tmp_path), "system", "default"),
    ]

    first = enable_planned_system_sources(db, candidates)
    second = enable_planned_system_sources(db, candidates)
    assert first == {
        "added": ["图片"], "enabled": [], "pending": ["下载", "图片"],
    }
    assert second == {
        "added": [], "enabled": [], "pending": ["下载", "图片"],
    }
    assert db.execute("SELECT count(*) FROM sources WHERE enabled = 1").fetchone()[0] == 0
    # H6: maintenance discovery cannot reverse an explicit disable or enable
    # a newly discovered directory without user consent.
    rows = db.execute("SELECT path, enabled FROM sources ORDER BY path").fetchall()
    assert all(row["enabled"] == 0 for row in rows)


def test_compacted_database_build_and_atomic_install(tmp_path):
    source_path = tmp_path / "library.db"
    target_path = tmp_path / "library.cleaned.db"
    source = connect(source_path)
    init_db(source)
    root = tmp_path / "documents"
    root.mkdir()
    source_id = _insert_source(source, "docs", root)
    kept_content, kept_chunk = _add_orphan_content(source, 200)
    _add_orphan_content(source, 201)
    path = root / "kept.txt"
    path.write_text("kept source file", encoding="utf-8")
    file_id = _insert_historical_file(source, source_id, path, 200)
    source.execute("UPDATE files SET content_id = ? WHERE id = ?", (kept_content, file_id))
    source.commit()
    source.close()

    built = build_compacted_database(
        source_path,
        target_path,
        add_system_sources=False,
        vacuum=False,
    )
    assert built["metrics"]["files"] == 1
    assert built["metrics"]["contents"] == 1
    assert built["metrics"]["chunks"] == 1
    assert built["metrics"]["orphan_contents"] == 0
    assert backup_is_restorable(target_path)

    installed = install_compacted_database(source_path, target_path)
    assert Path(installed["archive"]).exists()
    assert installed["metrics"]["files"] == 1
    final = connect(source_path)
    try:
        assert final.execute("SELECT id FROM chunks").fetchone()[0] == kept_chunk
        assert final.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 1
        assert vector.count(final) == 1
    finally:
        final.close()
