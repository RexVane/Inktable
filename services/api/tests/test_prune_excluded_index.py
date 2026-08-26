"""剪掉「已排除内容」索引行这条破坏性路径的契约测试。

这条路径删的是索引，不是文件，也不是记录。它必须同时满足两件互相拉扯的事：

- 把残留的 chunks/FTS/向量行删干净（否则 BM25 的分母与向量路的 KNN 池
  仍然是噪声，排除只挡住了展示，没挡住排序）；
- 一个**可见**分片都不能少（少了就等于把正确答案从库里拿掉，评测会因此
  「变好」而失去意义）。

以及一件容易被忽略的事：剪过之后取消排除必须能恢复。剪掉索引行的内容若以
「已索引」身份复活，用户会看到「我取消排除了但还是搜不到」。
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

from app.db.database import connect, init_db
from app.domain.exclusions import add_exclusion, remove_exclusion
from app.index.pipeline import count_readable_pending

SCRIPT = (Path(__file__).resolve().parents[1]
          / "scripts" / "prune_excluded_index.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("prune_excluded_index", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prune_mod = _load_script()


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


def _indexed(conn, source_id: int, path: Path, ordinal: int) -> dict:
    """造一份「已索引」的内容：contents + files + chunks + FTS + 文档层。"""
    content_id = conn.execute(
        "INSERT INTO contents (sha256, size, parse_state, chunk_count, "
        "active_index_version, indexed_at) VALUES (?, 1, 'indexed', 2, 1, ?)",
        (f"sha-{ordinal}", time.time()),
    ).lastrowid
    conn.execute(
        """INSERT INTO files
           (volume_uuid, inode, content_id, path, name, source_id, ext, size,
            state, mtime, detected_at)
           VALUES ('v', ?, ?, ?, ?, ?, ?, 1, 'registered', ?, ?)""",
        (ordinal, content_id, str(path), path.name, source_id,
         path.suffix.lower(), time.time(), time.time()),
    )
    chunk_ids = []
    for i in range(2):
        text = f"{path.stem} 正文 {i}"
        cid = conn.execute(
            """INSERT INTO chunks
               (content_id, index_version, ordinal, text, section_path,
                start_offset, end_offset, token_count, text_hash)
               VALUES (?, 1, ?, ?, '', 0, 1, 3, ?)""",
            (content_id, i, text, f"ch-{ordinal}-{i}"),
        ).lastrowid
        conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
                     (cid, text))
        conn.execute("INSERT INTO chunks_fts_tri(rowid, text) VALUES (?, ?)",
                     (cid, text))
        chunk_ids.append(cid)
    rep_id = conn.execute(
        """INSERT INTO document_representations
           (content_id, index_version, title, summary_text, full_text, text_hash)
           VALUES (?, 1, ?, '摘要', '正文', ?)""",
        (content_id, path.stem, f"h-{ordinal}"),
    ).lastrowid
    conn.execute("INSERT INTO documents_fts(rowid, text) VALUES (?, ?)",
                 (rep_id, path.stem))
    return {"content_id": content_id, "chunk_ids": chunk_ids, "rep_id": rep_id}


@pytest.fixture
def library(db, tmp_path):
    """一个被排除的目录 + 一个真资料目录，各一份已索引内容。"""
    root = tmp_path / "drive"
    source_id = _source(db, root)
    noisy = _indexed(db, source_id, root / "devcache" / "noise.md", 1)
    keep = _indexed(db, source_id, root / "资料" / "笔记.md", 2)
    db.commit()
    with db:
        add_exclusion(db, root / "devcache")
    return {"root": root, "noisy": noisy, "keep": keep}


def test_survey_counts_only_excluded_content_as_prunable(db, library):
    info = prune_mod.survey(db)
    assert info["ids"] == [library["noisy"]["content_id"]]
    assert info["stats"]["chunks_total"] == 4
    assert info["stats"]["chunks_visible"] == 2
    assert info["stats"]["chunks_prunable"] == 2


def test_missing_replica_protects_content_from_pruning(db, library, tmp_path):
    """不可见 ≠ 可剪。`missing` 是过渡态，剪掉它没有唤醒路径。

    外置盘拔下来、云盘文件没下载都会让文件变 `missing`。若按「没有可见副本」
    剪索引，插回硬盘之后内容仍是 `excluded` 状态、零分片，而只有「取消排除」
    才会 requeue —— 用户永远等不到它回来，而且没有任何报错。
    """
    root = library["root"]
    source_id = db.execute("SELECT id FROM sources").fetchone()["id"]
    gone = _indexed(db, source_id, root / "外置盘" / "论文.pdf", 3)
    db.execute("UPDATE files SET state = 'missing', preserved_path = '' "
               "WHERE content_id = ?", (gone["content_id"],))
    db.commit()

    info = prune_mod.survey(db)
    assert gone["content_id"] not in info["ids"]
    # 它确实不可见，但刻意留着 —— 这个差额要能被看见
    assert info["stats"]["chunks_invisible_kept"] == 2

    prune_mod.prune(db, info["ids"], batch_size=10)
    assert db.execute("SELECT count(*) c FROM chunks WHERE content_id = ?",
                      (gone["content_id"],)).fetchone()["c"] == 2


def test_prune_removes_residue_index_rows_across_all_tables(db, library):
    info = prune_mod.survey(db)
    result = prune_mod.prune(db, info["ids"], batch_size=10)
    assert result["pruned_contents"] == 1

    gone = library["noisy"]["chunk_ids"]
    marks = ",".join("?" * len(gone))
    for table in ("chunks", "chunks_fts", "chunks_fts_tri"):
        left = db.execute(
            f"SELECT count(*) c FROM {table} WHERE rowid IN ({marks})", gone,
        ).fetchone()["c"]
        assert left == 0, f"{table} 还留着被排除内容的行"
    assert db.execute(
        "SELECT count(*) c FROM document_representations WHERE content_id = ?",
        (library["noisy"]["content_id"],),
    ).fetchone()["c"] == 0
    assert db.execute(
        "SELECT count(*) c FROM documents_fts WHERE rowid = ?",
        (library["noisy"]["rep_id"],),
    ).fetchone()["c"] == 0


def test_prune_never_touches_a_visible_chunk(db, library):
    """最重要的一条：可见分片集合必须逐 id 不变。

    prune() 自己就断言这一点；这里额外从外部核对，避免那条断言被误改成
    「数量相同」—— 数量相同而 id 不同同样是灾难。
    """
    kept = {r["id"] for r in db.execute(prune_mod.VISIBLE_CHUNKS)}
    assert kept == set(library["keep"]["chunk_ids"])
    prune_mod.prune(db, prune_mod.survey(db)["ids"], batch_size=10)
    assert {r["id"] for r in db.execute(prune_mod.VISIBLE_CHUNKS)} == kept
    for cid in library["keep"]["chunk_ids"]:
        assert db.execute("SELECT count(*) c FROM chunks_fts WHERE rowid = ?",
                          (cid,)).fetchone()["c"] == 1


def test_prune_rejects_a_widened_residue_predicate(db, library):
    """判据写宽时必须失败退出，而不是安静地删掉真资料。"""
    all_contents = [r["id"] for r in db.execute("SELECT id FROM contents")]
    with pytest.raises(SystemExit) as err:
        prune_mod.prune(db, all_contents, batch_size=10)
    assert "可见分片" in str(err.value)


def test_pruned_content_is_not_queued_for_reindexing(db, library):
    """剪过的内容不能进解析队列。

    `index_pending()` 的队列查询不看 `state='ignored'`，所以剪完后若把
    parse_state 写成 pending，10 万个被排除的文件会立刻回到队列 ——
    排除的收益当场清零。
    """
    prune_mod.prune(db, prune_mod.survey(db)["ids"], batch_size=10)
    state = db.execute("SELECT parse_state FROM contents WHERE id = ?",
                       (library["noisy"]["content_id"],)).fetchone()[0]
    assert state == "excluded"
    assert count_readable_pending(db) == 0


def test_second_run_finds_nothing_left_to_prune(db, library):
    """幂等：重跑不能再把已经空掉的内容当成待剪。

    少了这条，日常维护里每次 `--apply` 都会对 9 万个 content 空转一遍删除
    批次，而输出看起来一切正常。
    """
    prune_mod.prune(db, prune_mod.survey(db)["ids"], batch_size=10)
    again = prune_mod.survey(db)
    assert again["ids"] == []
    assert again["stats"]["chunks_prunable"] == 0


def test_cancelling_exclusion_requeues_pruned_content(db, library):
    """取消排除后必须重新排队，否则文件以「已索引、零分片」的身份复活。"""
    prune_mod.prune(db, prune_mod.survey(db)["ids"], batch_size=10)
    with db:
        out = remove_exclusion(db, library["root"] / "devcache")
    assert out["files_restored"] == 1
    assert out["contents_requeued"] == 1
    assert db.execute("SELECT parse_state FROM contents WHERE id = ?",
                      (library["noisy"]["content_id"],)).fetchone()[0] == "pending"
    # 没被剪过的内容不该被牵连着重新解析一遍
    assert db.execute("SELECT parse_state FROM contents WHERE id = ?",
                      (library["keep"]["content_id"],)).fetchone()[0] == "indexed"


def test_rebuild_keeps_backfilled_abstract_in_document_index():
    """清理路径重建 documents_fts 时不能把已回填的 abstract 抹掉。

    抹掉的话，「跑一次清理」等于「撤销一次回填」，而两者在日志里毫无关联。
    """
    import inspect

    from app.maintenance import ingestion_cleanup

    src = inspect.getsource(ingestion_cleanup._populate_empty_virtual_indexes)
    assert "document_index_text" in src
    assert "abstract" in src
