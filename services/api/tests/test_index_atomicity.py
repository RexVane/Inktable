"""单个 content 的 chunks / FTS / vector 必须原子替换。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.db.database import connect, init_db
from app.index import embedding as emb
from app.index import pipeline
from app.index import vector as vec
from app.index.embedding import DIM
from app.index.search import index_chunk


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


def _seed_content(db, *, state: str = "pending") -> None:
    db.execute(
        "INSERT INTO contents (id, sha256, size, parse_state) VALUES (1, 'sha', 1, ?)",
        (state,),
    )
    db.commit()


def _two_chunk_doc(path) -> None:
    path.write_text("甲" * 600 + "\n\n" + "乙" * 600, encoding="utf-8")


def _counts(db) -> tuple[int, int, int, int]:
    return (
        db.execute("SELECT count(*) FROM chunks").fetchone()[0],
        db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0],
        db.execute("SELECT count(*) FROM chunks_fts_tri").fetchone()[0],
        vec.count(db),
    )


def _insert_file(db, *, file_id: int, content_id: int, path, ext: str,
                 state: str = "registered") -> None:
    now = time.time()
    db.execute(
        "INSERT INTO files(id, volume_uuid, inode, content_id, path, name, ext, "
        "size, state, mtime, detected_at) VALUES (?, 'v', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            file_id, file_id, content_id, str(path), path.name, ext,
            path.stat().st_size if path.exists() else 0, state, now, now,
        ),
    )


def test_fts_failure_rolls_back_only_current_content(db, tmp_path, monkeypatch):
    """中途 FTS 失败不能留下半份索引，也不能回滚调用方已有事务。"""
    _seed_content(db)
    path = tmp_path / "doc.txt"
    _two_chunk_doc(path)

    # 调用方在进入 index_content 前已有未提交写入；content savepoint 不能动它。
    db.execute("INSERT INTO settings(key, value) VALUES ('outer-write', 'kept')")
    original = pipeline.index_chunk
    calls = 0

    def fail_on_second(conn, chunk_id, text, section_path=""):
        nonlocal calls
        calls += 1
        original(conn, chunk_id, text, section_path)
        if calls == 2:
            raise RuntimeError("injected FTS failure")

    monkeypatch.setattr(pipeline, "index_chunk", fail_on_second)

    with pytest.raises(RuntimeError, match="injected FTS failure"):
        pipeline.index_content(db, 1, path)

    assert _counts(db) == (0, 0, 0, 0)
    assert db.execute(
        "SELECT value FROM settings WHERE key = 'outer-write'"
    ).fetchone()[0] == "kept"
    assert db.in_transaction

    db.rollback()
    assert db.execute(
        "SELECT 1 FROM settings WHERE key = 'outer-write'"
    ).fetchone() is None


def test_vector_failure_rolls_back_chunks_fts_and_partial_vector(
    db, tmp_path, monkeypatch
):
    """向量写到一半再失败时，四张索引表全部恢复为空且异常必须冒泡。"""
    if not pipeline._vector_table_exists(db):
        pytest.skip("sqlite-vec 不可用")

    _seed_content(db)
    path = tmp_path / "doc.txt"
    _two_chunk_doc(path)

    class FakeEmbedder:
        model_id = "fake-d256"

        def encode(self, texts):
            return np.ones((len(texts), DIM), dtype=np.float32)

    monkeypatch.setattr(emb, "is_available", lambda: True)
    monkeypatch.setattr(emb, "get_embedder", lambda: FakeEmbedder())

    original_upsert = vec.upsert

    def fail_after_first(conn, rows):
        original_upsert(conn, rows[:1])
        raise RuntimeError("injected vector failure")

    monkeypatch.setattr(vec, "upsert", fail_after_first)

    with pytest.raises(RuntimeError, match="injected vector failure"):
        pipeline.index_content(db, 1, path)

    assert _counts(db) == (0, 0, 0, 0)
    row = db.execute(
        "SELECT parse_state, chunk_count FROM contents WHERE id = 1"
    ).fetchone()
    assert (row["parse_state"], row["chunk_count"]) == ("pending", 0)


def test_parse_failure_preserves_previous_complete_index(db, tmp_path, monkeypatch):
    """重建时解析器崩溃，旧 chunks/FTS/vector 必须原样保留。"""
    if not pipeline._vector_table_exists(db):
        pytest.skip("sqlite-vec 不可用")

    _seed_content(db, state="indexed")
    cur = db.execute(
        "INSERT INTO chunks(content_id, ordinal, text, text_hash) "
        "VALUES (1, 0, '旧正文', 'old-hash')"
    )
    index_chunk(db, cur.lastrowid, "旧正文")
    vector = np.ones(DIM, dtype=np.float32)
    vector /= np.linalg.norm(vector)
    vec.upsert(db, [(cur.lastrowid, vector)])
    db.execute("UPDATE contents SET chunk_count = 1 WHERE id = 1")
    db.commit()
    before = _counts(db)

    def fail_parse(_path):
        raise RuntimeError("injected parse failure")

    monkeypatch.setattr(pipeline, "parse", fail_parse)

    with pytest.raises(RuntimeError, match="injected parse failure"):
        pipeline.index_content(db, 1, tmp_path / "doc.txt")

    assert _counts(db) == before == (1, 1, 1, 1)
    row = db.execute(
        "SELECT parse_state, chunk_count FROM contents WHERE id = 1"
    ).fetchone()
    assert (row["parse_state"], row["chunk_count"]) == ("indexed", 1)


def test_success_does_not_commit_callers_transaction(db, tmp_path, monkeypatch):
    """无外层事务时由函数开启 BEGIN，但成功后仍交给调用方 commit/rollback。"""
    _seed_content(db)
    path = tmp_path / "doc.txt"
    _two_chunk_doc(path)
    monkeypatch.setattr(emb, "is_available", lambda: False)

    result = pipeline.index_content(db, 1, path)

    assert result["state"] == "indexed"
    assert db.in_transaction
    assert _counts(db)[:3] == (2, 2, 2)

    db.rollback()
    assert _counts(db) == (0, 0, 0, 0)
    assert db.execute(
        "SELECT parse_state FROM contents WHERE id = 1"
    ).fetchone()[0] == "pending"


def test_index_pending_continues_after_one_document_rolls_back(
    db, tmp_path, monkeypatch
):
    """批处理捕获已完成回滚的文档异常，后续文档仍正常建立完整索引。"""
    bad = tmp_path / "bad.txt"
    good = tmp_path / "good.txt"
    bad.write_text("坏文档" * 200, encoding="utf-8")
    good.write_text("好文档" * 200, encoding="utf-8")
    now = time.time()

    for cid, path in ((1, bad), (2, good)):
        db.execute(
            "INSERT INTO contents(id, sha256, size, parse_state) "
            "VALUES (?, ?, ?, 'pending')",
            (cid, f"sha-{cid}", path.stat().st_size),
        )
        db.execute(
            "INSERT INTO files(volume_uuid, inode, content_id, path, name, ext, "
            "size, state, mtime, detected_at) VALUES ('v', ?, ?, ?, ?, '.txt', "
            "?, 'registered', ?, ?)",
            (cid, cid, str(path), path.name, path.stat().st_size, now, now),
        )
    db.commit()

    original = pipeline.index_chunk

    def fail_bad_document(conn, chunk_id, text, section_path=""):
        original(conn, chunk_id, text, section_path)
        if "坏文档" in text:
            raise RuntimeError("injected one-document failure")

    monkeypatch.setattr(pipeline, "index_chunk", fail_bad_document)
    monkeypatch.setattr(emb, "is_available", lambda: False)

    result = pipeline.index_pending(db, limit=10)

    assert result["total"] == 2
    assert result["indexed"] == 1
    assert result["failed"] == 1
    states = {
        r["id"]: r["parse_state"]
        for r in db.execute("SELECT id, parse_state FROM contents ORDER BY id")
    }
    assert states == {1: "index_failed", 2: "indexed"}
    assert db.execute(
        "SELECT count(*) FROM chunks WHERE content_id = 1"
    ).fetchone()[0] == 0
    good_chunks = db.execute(
        "SELECT count(*) FROM chunks WHERE content_id = 2"
    ).fetchone()[0]
    assert good_chunks > 0
    assert db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == good_chunks
    assert db.execute("SELECT count(*) FROM chunks_fts_tri").fetchone()[0] == good_chunks


@pytest.mark.parametrize("initial_state", ["pending", "unsupported"])
def test_shared_content_uses_supported_replica_instead_of_min_ext(
    db, tmp_path, monkeypatch, initial_state
):
    """共享 content 只要有一个非 missing 的可解析副本，就不能标 unsupported。"""
    _seed_content(db, state=initial_state)
    opaque = tmp_path / "a.bin"
    readable = tmp_path / "z.txt"
    shared = "共享内容可以通过 TXT 副本解析。" * 30
    opaque.write_text(shared, encoding="utf-8")
    readable.write_text(shared, encoding="utf-8")
    _insert_file(db, file_id=1, content_id=1, path=opaque, ext=".bin")
    _insert_file(db, file_id=2, content_id=1, path=readable, ext=".txt")
    db.commit()
    monkeypatch.setattr(emb, "is_available", lambda: False)

    result = pipeline.index_pending(db, limit=10)

    assert result["indexed"] == 1
    assert db.execute(
        "SELECT parse_state FROM contents WHERE id = 1"
    ).fetchone()[0] == "indexed"
    assert db.execute(
        "SELECT count(*) FROM chunks WHERE content_id = 1"
    ).fetchone()[0] > 0


def test_shared_content_skips_stale_path_and_uses_readable_replica(
    db, tmp_path, monkeypatch
):
    """排序靠前但已消失的副本，不能遮蔽同 content 的健康副本。"""
    _seed_content(db)
    stale = tmp_path / "a.txt"
    healthy = tmp_path / "z.txt"
    healthy.write_text("健康副本仍然可以完成索引。" * 30, encoding="utf-8")
    _insert_file(db, file_id=1, content_id=1, path=stale, ext=".txt")
    _insert_file(db, file_id=2, content_id=1, path=healthy, ext=".txt")
    db.commit()
    monkeypatch.setattr(emb, "is_available", lambda: False)

    result = pipeline.index_pending(db, limit=10)

    assert result["indexed"] == 1
    assert db.execute(
        "SELECT parse_state FROM contents WHERE id = 1"
    ).fetchone()[0] == "indexed"


def test_missing_original_can_index_from_preserved_copy(db, tmp_path, monkeypatch):
    """原件消失后，待索引内容可从保全副本恢复解析。"""
    _seed_content(db)
    preserved = tmp_path / "preserved.txt"
    preserved.write_text("保全副本仍然包含可检索正文。" * 30, encoding="utf-8")
    dead = tmp_path / "微信缓存" / "dead.txt"
    _insert_file(db, file_id=1, content_id=1, path=dead, ext=".txt", state="missing")
    db.execute(
        "UPDATE files SET preserved_path = ? WHERE id = 1", (str(preserved),)
    )
    db.commit()
    monkeypatch.setattr(emb, "is_available", lambda: False)

    result = pipeline.index_pending(db, limit=10)

    assert result["indexed"] == 1
    assert db.execute(
        "SELECT parse_state FROM contents WHERE id = 1"
    ).fetchone()[0] == "indexed"
    assert db.execute("SELECT count(*) FROM chunks WHERE content_id = 1").fetchone()[0] > 0


@pytest.mark.parametrize("donor_model", [None, "old-model"])
def test_vector_reuse_rejects_null_or_old_model_and_records_current_model(
    db, monkeypatch, donor_model
):
    """text_hash 相同也只能复用当前模型的向量；新 chunk 必须记录模型 id。"""
    if not pipeline._vector_table_exists(db):
        pytest.skip("sqlite-vec 不可用")

    db.execute(
        "INSERT INTO contents(id, sha256, size, parse_state) "
        "VALUES (1, 'old', 1, 'indexed'), (2, 'new', 1, 'pending')"
    )
    donor = db.execute(
        "INSERT INTO chunks(content_id, ordinal, text, text_hash, embedding_model_id) "
        "VALUES (1, 0, '相同正文', 'same-hash', ?)",
        (donor_model,),
    ).lastrowid
    target = db.execute(
        "INSERT INTO chunks(content_id, ordinal, text, text_hash) "
        "VALUES (2, 0, '相同正文', 'same-hash')"
    ).lastrowid
    vec.upsert(db, [(donor, np.ones(DIM, dtype=np.float32))])

    encoded: list[list[str]] = []

    class CurrentEmbedder:
        model_id = "current-model"

        def encode(self, texts):
            encoded.append(list(texts))
            return np.ones((len(texts), DIM), dtype=np.float32)

    monkeypatch.setattr(emb, "is_available", lambda: True)
    monkeypatch.setattr(emb, "get_embedder", lambda: CurrentEmbedder())

    result = pipeline._embed_chunks(
        db,
        [(target, SimpleNamespace(
            text="相同正文", text_hash="same-hash", section_path="",
        ))],
    )

    assert result == "current-model"
    assert len(encoded) == 1
    assert db.execute(
        "SELECT embedding_model_id FROM chunks WHERE id = ?", (target,)
    ).fetchone()[0] == "current-model"
    assert vec.count(db) == 2


def test_vector_reuse_accepts_current_model_and_records_it(db, monkeypatch):
    """当前模型的 donor 应零编码复用，并把模型 id 写入新 chunk。"""
    if not pipeline._vector_table_exists(db):
        pytest.skip("sqlite-vec 不可用")

    db.execute(
        "INSERT INTO contents(id, sha256, size, parse_state) "
        "VALUES (1, 'old', 1, 'indexed'), (2, 'new', 1, 'pending')"
    )
    donor = db.execute(
        "INSERT INTO chunks(content_id, ordinal, text, text_hash, embedding_model_id) "
        "VALUES (1, 0, '相同正文', 'same-hash', 'current-model')"
    ).lastrowid
    target = db.execute(
        "INSERT INTO chunks(content_id, ordinal, text, text_hash) "
        "VALUES (2, 0, '相同正文', 'same-hash')"
    ).lastrowid
    vec.upsert(db, [(donor, np.ones(DIM, dtype=np.float32))])

    class CurrentEmbedder:
        model_id = "current-model"

        def encode(self, texts):
            raise AssertionError("当前模型的向量不应重新编码")

    monkeypatch.setattr(emb, "is_available", lambda: True)
    monkeypatch.setattr(emb, "get_embedder", lambda: CurrentEmbedder())

    result = pipeline._embed_chunks(
        db,
        [(target, SimpleNamespace(
            text="相同正文", text_hash="same-hash", section_path="",
        ))],
    )

    assert result == "current-model"
    assert db.execute(
        "SELECT embedding_model_id FROM chunks WHERE id = ?", (target,)
    ).fetchone()[0] == "current-model"
    assert vec.count(db) == 2


def test_embed_backfill_covers_legacy_chunks(db, tmp_path, monkeypatch):
    """模型晚于内容入库：backfill 分批补齐存量向量，幂等、可中断。

    这正是真实库的形态 —— 大量文档在纯 FTS 降级期索引完成，
    嵌入模型后来才就位，没有补课路径这些分片就是语义检索盲区。
    """
    if not pipeline._vector_table_exists(db):
        pytest.skip("sqlite-vec 不可用")

    _seed_content(db)
    path = tmp_path / "doc.txt"
    _two_chunk_doc(path)

    # 第一阶段：模型不可用，纯 FTS 索引 → 分片没有向量
    monkeypatch.setattr(emb, "is_available", lambda: False)
    pipeline.index_content(db, 1, path)
    db.commit()
    assert vec.count(db) == 0
    assert db.execute(
        "SELECT count(*) FROM chunks WHERE embedding_model_id IS NULL"
    ).fetchone()[0] == 2

    # 第二阶段：模型可用了，分批补课
    class FakeEmbedder:
        model_id = "fake-d256"

        def encode(self, texts):
            return np.ones((len(texts), DIM), dtype=np.float32)

    monkeypatch.setattr(emb, "is_available", lambda: True)
    monkeypatch.setattr(emb, "get_embedder", lambda: FakeEmbedder())

    first = pipeline.embed_backfill(db, limit=1)
    assert first["embedded"] == 1
    assert first["remaining"] == 1
    assert first["model"] == "fake-d256"

    second = pipeline.embed_backfill(db, limit=10)
    assert second["embedded"] == 1
    assert second["remaining"] == 0
    db.commit()

    assert vec.count(db) == 2
    assert db.execute(
        "SELECT count(*) FROM chunks WHERE embedding_model_id IS NULL"
    ).fetchone()[0] == 0

    # 幂等：补完再跑一遍什么都不动
    third = pipeline.embed_backfill(db)
    assert third["embedded"] == 0
    assert third["remaining"] == 0
