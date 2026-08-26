from __future__ import annotations

import json
import threading

import pytest

from app.db import database
from app.library.core import sync_library_items
import app.library.enrichment as enrichment
from app.library.enrichment import PROMPT_VERSION, claim_items, run_enrichment_batch


def _seed():
    conn = database.connect(":memory:")
    database.init_db(conn)
    conn.execute(
        "INSERT INTO sources(id,name,path,kind,discovered_by,enabled,created_at) "
        "VALUES (1,'资料','/private-vault','manual','manual',1,1)"
    )
    conn.execute(
        "INSERT INTO contents(id,sha256,size,parse_state,active_index_version) "
        "VALUES (1,?,100,'indexed',1)",
        ("a" * 64,),
    )
    conn.execute(
        """INSERT INTO files
           (id,volume_uuid,inode,content_id,path,name,source_id,ext,size,state,
            detected_at,mtime)
           VALUES (1,'vol',1,1,'/private-vault/os-note.md','os-note.md',1,'md',
                   100,'registered',1,1)"""
    )
    conn.execute(
        """INSERT INTO document_representations
           (content_id,index_version,title,summary_text,abstract,abstract_model,
            full_text,text_hash,
            token_count,structure_confidence)
           VALUES (1,1,'操作系统笔记','','RAG_ONLY_MARKER 检索关键词同义词',
                   'retrieval-model',?, 'doc-v1',100,1)""",
        (
            "进程调度、虚拟内存、文件系统。\n"
            "死锁的四个必要条件包括互斥、请求保持、不可剥夺和循环等待。",
        ),
    )
    conn.execute(
        """INSERT INTO sections
           (content_id,parent_id,index_version,ordinal,heading_path,title,summary_text,
            start_chunk_ordinal,end_chunk_ordinal,text_hash,token_count,structure_confidence)
           VALUES (1,NULL,1,0,'操作系统 › 死锁','死锁','',0,0,'sec-v1',10,1)"""
    )
    conn.execute(
        "INSERT INTO categories(id,name,sort_order) VALUES (1,'计算机基础',0),(2,'生活',1)"
    )
    conn.execute(
        "INSERT INTO tags(id,name) VALUES (1,'操作系统'),(2,'死锁'),(3,'数据库')"
    )
    sync_library_items(conn, now=10)
    conn.commit()
    return conn


def _valid_result() -> str:
    return json.dumps(
        {
            "summary": "这是一份操作系统笔记，重点整理进程、内存、文件系统与死锁条件。",
            "language": "zh",
            "category_id": 1,
            "tag_ids": [2, 1],
        },
        ensure_ascii=False,
    )


def test_enrichment_writes_summary_controlled_category_and_tags_without_path_leak() -> None:
    conn = _seed()
    prompts: list[str] = []

    def fake_generate(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(
            {
                "summary": "这是一份操作系统笔记，重点整理进程、内存、文件系统与死锁条件。",
                "language": "zh",
                "category_id": 1,
                # 999 is deliberately outside the controlled vocabulary.
                "tag_ids": [2, 999, 1, 2],
            },
            ensure_ascii=False,
        )

    result = run_enrichment_batch(
        lambda: conn,
        threading.Lock(),
        limit=1,
        generate_fn=fake_generate,
        model="fake-local-model",
    )

    assert result["claimed"] == 1
    assert result["ready"] == 1
    assert result["failed"] == 0
    assert prompts
    assert "/private-vault/os-note.md" not in prompts[0]
    assert "---BEGIN UNTRUSTED DOCUMENT---" in prompts[0]
    assert "RAG_ONLY_MARKER" not in prompts[0]
    assert "检索摘要提示" not in prompts[0]

    item = conn.execute("SELECT * FROM library_items WHERE content_id=1").fetchone()
    assert item["enrichment_status"] == "ready"
    assert item["enrichment_model"] == "fake-local-model"
    assert item["prompt_version"] == PROMPT_VERSION
    assert item["language"] == "zh"
    assert item["category_id"] == 1
    assert "死锁条件" in item["summary"]

    tag_ids = [
        row[0]
        for row in conn.execute(
            "SELECT tag_id FROM library_item_tags WHERE library_item_id=? ORDER BY tag_id",
            (item["id"],),
        ).fetchall()
    ]
    assert tag_ids == [1, 2]
    conn.close()


def test_enrichment_rejects_model_result_if_active_index_changes_mid_generation() -> None:
    conn = _seed()

    def change_document_then_return(_prompt: str) -> str:
        # Simulate a better OCR/parser result becoming active while the model is
        # generating. Source file bytes stay identical.
        conn.execute(
            """INSERT INTO document_representations
               (content_id,index_version,title,summary_text,full_text,text_hash,
                token_count,structure_confidence)
               VALUES (1,2,'操作系统笔记','',?, 'doc-v2',100,1)""",
            ("第二版 OCR：加入了页面中此前漏掉的关键内容。",),
        )
        conn.execute("UPDATE contents SET active_index_version=2 WHERE id=1")
        conn.commit()
        return json.dumps(
            {
                "summary": "这是基于旧 OCR 的摘要，不应写回。",
                "language": "zh",
                "category_id": 1,
                "tag_ids": [1],
            },
            ensure_ascii=False,
        )

    result = run_enrichment_batch(
        lambda: conn,
        threading.Lock(),
        limit=1,
        generate_fn=change_document_then_return,
        model="fake-local-model",
    )

    assert result["claimed"] == 1
    assert result["stale"] == 1
    item = conn.execute("SELECT * FROM library_items WHERE content_id=1").fetchone()
    assert item["enrichment_status"] == "stale"
    assert item["summary"] == ""
    assert item["enrichment_error"] == "content_changed_during_enrichment"
    conn.close()


def test_running_claim_is_only_recovered_after_lease_expires() -> None:
    conn = _seed()
    item_id = conn.execute("SELECT id FROM library_items WHERE content_id=1").fetchone()[0]
    conn.execute(
        "UPDATE library_items SET enrichment_status='running', updated_at=100 WHERE id=?",
        (item_id,),
    )

    not_expired = claim_items(conn, limit=1, now=200, lease_seconds=150)
    assert not_expired == []

    expired = claim_items(conn, limit=1, now=300, lease_seconds=150)
    assert len(expired) == 1
    assert expired[0].item_id == item_id
    conn.close()


def test_previous_prompt_version_is_reclaimed_after_boundary_change() -> None:
    conn = _seed()
    item_id = conn.execute("SELECT id FROM library_items WHERE content_id=1").fetchone()[0]
    conn.execute(
        """UPDATE library_items
           SET summary='旧提示生成的摘要', enrichment_status='ready',
               enrichment_model='local-model', prompt_version='library-enrichment-v1',
               updated_at=100
           WHERE id=?""",
        (item_id,),
    )

    claims = claim_items(conn, limit=1, now=200)

    assert PROMPT_VERSION == "library-enrichment-v2"
    assert len(claims) == 1
    assert claims[0].item_id == item_id
    assert conn.execute(
        "SELECT enrichment_status FROM library_items WHERE id=?", (item_id,)
    ).fetchone()[0] == "running"
    conn.close()


def test_summary_and_tags_apply_in_one_transaction(monkeypatch) -> None:
    """A tag-write failure must not leave a ready summary without its tags."""
    conn = _seed()

    def explode_tags(*_args, **_kwargs):
        raise RuntimeError("tag write failed")

    monkeypatch.setattr(enrichment, "replace_library_item_tags", explode_tags)

    with pytest.raises(RuntimeError, match="tag write failed"):
        run_enrichment_batch(
            lambda: conn,
            threading.Lock(),
            limit=1,
            generate_fn=lambda _prompt: _valid_result(),
            model="fake-local-model",
        )

    # The claim itself was intentionally committed before the model call, so a
    # crashed apply remains leased as running. The failed apply transaction,
    # however, must be fully rolled back: no summary/category/model/tag half-state.
    item = conn.execute("SELECT * FROM library_items WHERE content_id=1").fetchone()
    assert item["enrichment_status"] == "running"
    assert item["summary"] == ""
    assert item["category_id"] is None
    assert item["enrichment_model"] is None
    assert item["prompt_version"] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM library_item_tags WHERE library_item_id=?",
        (item["id"],),
    ).fetchone()[0] == 0
    conn.close()
