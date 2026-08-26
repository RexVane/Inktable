from __future__ import annotations

import numpy as np

from app.db import database
from app.library.core import sync_library_items
from app.library.query import library_item_detail, library_stats
import app.library.relations as relations
from app.library.relations import apply_relation_plan, build_relation_plan, relation_status


def _seed():
    conn = database.connect(":memory:")
    database.init_db(conn)
    conn.execute(
        "INSERT INTO sources(id,name,path,kind,discovered_by,enabled,created_at) "
        "VALUES (1,'资料','/docs','manual','manual',1,1)"
    )
    for cid, name in enumerate(("os-a.md", "os-b.md", "rag-a.md", "rag-b.md"), start=1):
        conn.execute(
            "INSERT INTO contents(id,sha256,size,parse_state,active_index_version) "
            "VALUES (?,?,10,'indexed',1)",
            (cid, f"sha-{cid}"),
        )
        conn.execute(
            """INSERT INTO files
               (id,volume_uuid,inode,content_id,path,name,source_id,ext,size,state,
                detected_at,mtime)
               VALUES (?, 'vol', ?, ?, ?, ?, 1, 'md', 10, 'registered', 1, ?)""",
            (cid, cid, cid, f"/docs/{name}", name, cid),
        )
        conn.execute(
            """INSERT INTO chunks
               (id,content_id,layer,section_path,ordinal,text,text_hash,
                embedding_model_id,index_version)
               VALUES (?,?,'child','',0,?,?, 'ollama:bge-m3',1)""",
            (cid * 10, cid, f"document {cid}", f"chunk-{cid}"),
        )
    sync_library_items(conn, now=10)
    conn.commit()
    return conn


def _fake_vectors():
    # 1/2 form one semantic cluster; 3/4 form another.
    raw = {
        10: np.array([1.0, 0.0], dtype=np.float32),
        20: np.array([0.98, 0.20], dtype=np.float32),
        30: np.array([0.0, 1.0], dtype=np.float32),
        40: np.array([0.12, 0.99], dtype=np.float32),
    }
    return {key: value / np.linalg.norm(value) for key, value in raw.items()}


def _plan(conn, monkeypatch):
    vectors = _fake_vectors()
    monkeypatch.setattr(
        relations.vector,
        "vectors_for",
        lambda _conn, ids: {cid: vectors[cid] for cid in ids if cid in vectors},
    )
    return build_relation_plan(
        conn,
        limit=10,
        top_k=1,
        min_score=0.60,
        chunks_per_item=1,
    )


def test_relation_plan_uses_mutual_top_k_and_existing_vectors(monkeypatch) -> None:
    conn = _seed()
    plan = _plan(conn, monkeypatch)

    item_by_content = {
        int(row["content_id"]): int(row["id"])
        for row in conn.execute("SELECT id,content_id FROM library_items")
    }
    expected_pairs = {
        tuple(sorted((item_by_content[1], item_by_content[2]))),
        tuple(sorted((item_by_content[3], item_by_content[4]))),
    }
    assert plan.vectorized == 4
    assert {(left, right) for left, right, _score in plan.edges} == expected_pairs
    assert all(score >= 0.60 for _left, _right, score in plan.edges)

    result = apply_relation_plan(conn, plan, now=20)
    conn.commit()
    assert result["relations"] == 2
    stored = {
        (int(row["source_item_id"]), int(row["target_item_id"]))
        for row in conn.execute(
            "SELECT source_item_id,target_item_id FROM library_relations"
        )
    }
    assert stored == expected_pairs
    versions = conn.execute(
        """SELECT source_item_id,target_item_id,source_input_hash,target_input_hash
           FROM library_relation_versions ORDER BY source_item_id,target_item_id"""
    ).fetchall()
    assert len(versions) == 2
    for row in versions:
        source_hash = conn.execute(
            "SELECT input_hash FROM library_items WHERE id=?",
            (row["source_item_id"],),
        ).fetchone()[0]
        target_hash = conn.execute(
            "SELECT input_hash FROM library_items WHERE id=?",
            (row["target_item_id"],),
        ).fetchone()[0]
        assert row["source_input_hash"] == source_hash
        assert row["target_input_hash"] == target_hash

    status = relation_status(conn)
    assert status["relations"] == 2
    assert status["stale_relations"] == 0
    assert status["covered_items"] == 4
    assert status["coverage"] == 1.0
    assert status["needs_rebuild"] is False
    conn.close()


def test_relation_versions_hide_stale_edge_but_keep_other_cluster(monkeypatch) -> None:
    conn = _seed()
    plan = _plan(conn, monkeypatch)
    apply_relation_plan(conn, plan, now=20)
    conn.commit()

    os_a = conn.execute(
        "SELECT id FROM library_items WHERE content_id=1"
    ).fetchone()[0]
    os_b = conn.execute(
        "SELECT id FROM library_items WHERE content_id=2"
    ).fetchone()[0]

    # Simulate the normal sync step after this Library item's active document
    # changed. The relation version snapshot still points to the previous hash.
    conn.execute(
        "UPDATE library_items SET input_hash='new-library-version' WHERE id=?",
        (os_a,),
    )
    conn.commit()

    status = relation_status(conn)
    assert status["relations"] == 1
    assert status["stale_relations"] == 1
    assert status["covered_items"] == 2
    assert status["coverage"] == 0.5
    assert status["needs_rebuild"] is True

    detail = library_item_detail(conn, os_b)
    assert detail is not None
    assert all(related["id"] != os_a for related in detail["related"])
    conn.close()


def test_relation_apply_rejects_active_document_change_without_library_sync(monkeypatch) -> None:
    conn = _seed()
    plan = _plan(conn, monkeypatch)

    changed_item = conn.execute(
        "SELECT id FROM library_items WHERE content_id=1"
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO document_representations
           (content_id,index_version,title,summary_text,full_text,text_hash,
            token_count,structure_confidence)
           VALUES (1,2,'OS A','','new parsed text','doc-v2',3,1)"""
    )
    conn.execute("UPDATE contents SET active_index_version=2 WHERE id=1")
    # Deliberately do NOT call sync_library_items: apply must inspect the active
    # representation directly rather than trust the stored Library hash.

    result = apply_relation_plan(conn, plan, now=30)
    conn.commit()
    assert result["stale_skipped"] == 1
    assert not conn.execute(
        """SELECT 1 FROM library_relations
           WHERE source_item_id=? OR target_item_id=?""",
        (changed_item, changed_item),
    ).fetchone()
    # The unrelated RAG cluster is still safe to apply and gets a version row.
    assert result["relations"] == 1
    assert conn.execute("SELECT COUNT(*) FROM library_relation_versions").fetchone()[0] == 1
    conn.close()


def test_relation_reads_hide_active_document_change_before_library_sync(monkeypatch) -> None:
    conn = _seed()
    plan = _plan(conn, monkeypatch)
    apply_relation_plan(conn, plan, now=20)
    conn.commit()

    changed_item = conn.execute(
        "SELECT id FROM library_items WHERE content_id=1"
    ).fetchone()[0]
    other_item = conn.execute(
        "SELECT id FROM library_items WHERE content_id=2"
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO document_representations
           (content_id,index_version,title,summary_text,full_text,text_hash,
            token_count,structure_confidence)
           VALUES (1,2,'OS A','','new parsed text','doc-v2',3,1)"""
    )
    conn.execute("UPDATE contents SET active_index_version=2 WHERE id=1")
    conn.commit()

    status = relation_status(conn)
    assert status["relations"] == 1
    assert status["stale_relations"] == 1
    assert status["needs_rebuild"] is True

    detail = library_item_detail(conn, other_item)
    assert detail is not None
    assert all(related["id"] != changed_item for related in detail["related"])
    assert library_stats(conn)["relations"] == 1
    conn.close()
