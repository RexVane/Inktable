"""Document relationships derived from Ordo's existing chunk vectors.

No extra embedding request is required. Each visible Library item samples a
bounded set of active child chunks, averages their already-normalized bge-m3
vectors into a document centroid, then compares document centroids.

A relationship is intentionally conservative: two items must be *mutual*
Top-K neighbours and their cosine must clear ``min_score``. This avoids turning
an unrelated personal library into a visually busy but meaningless graph.

Planning is read/CPU-only and can be slow. Applying the resulting edges is a
short transaction under Ordo's single-writer lock.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

import numpy as np

from app.db.visibility import visible_content_exists
from app.index import vector
from app.library.core import compute_input_hash


DEFAULT_LIMIT = 1000
MAX_LIMIT = 5000
DEFAULT_TOP_K = 8
MAX_TOP_K = 24
DEFAULT_CHUNKS_PER_ITEM = 16
MAX_CHUNKS_PER_ITEM = 48
DEFAULT_MIN_SCORE = float(
    os.environ.get("ORDO_LIBRARY_RELATION_MIN_SCORE", "0.60")
)
RELATION_SOURCE = "embedding-centroid-v1"
_QUERY_BLOCK = 128
_SQL_BATCH = 300
_VECTOR_BATCH = 400


@dataclass(frozen=True)
class RelationItemVersion:
    item_id: int
    content_id: int
    input_hash: str


@dataclass(frozen=True)
class RelationPlan:
    items: tuple[RelationItemVersion, ...]
    edges: tuple[tuple[int, int, float], ...]
    total_visible: int
    vectorized: int
    top_k: int
    min_score: float
    chunks_per_item: int

    @property
    def truncated(self) -> bool:
        return len(self.items) < self.total_visible


def _clamp_options(
    *, limit: int, top_k: int, min_score: float, chunks_per_item: int,
) -> tuple[int, int, float, int]:
    limit = max(2, min(int(limit), MAX_LIMIT))
    top_k = max(1, min(int(top_k), MAX_TOP_K))
    chunks_per_item = max(1, min(int(chunks_per_item), MAX_CHUNKS_PER_ITEM))
    min_score = max(-1.0, min(float(min_score), 1.0))
    return limit, top_k, min_score, chunks_per_item


def _visible_items(conn: sqlite3.Connection, limit: int) -> tuple[int, list[RelationItemVersion]]:
    visible = visible_content_exists("li.content_id", "vf", "vs")
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM library_items li WHERE {visible}"
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"""SELECT li.id, li.content_id,
                      c.sha256 AS content_sha,
                      c.active_index_version,
                      dr.text_hash AS document_text_hash
            FROM library_items li
            JOIN contents c ON c.id = li.content_id
            LEFT JOIN document_representations dr
              ON dr.content_id = c.id
             AND dr.index_version = c.active_index_version
            WHERE {visible}
            ORDER BY li.id
            LIMIT ?""",
        (limit,),
    ).fetchall()
    return total, [
        RelationItemVersion(
            item_id=int(row["id"]),
            content_id=int(row["content_id"]),
            # Capture the version of the representation the read-only planner
            # actually sees. The apply phase still requires library_items to
            # have been synced to that same hash, so a stale plan cannot write.
            input_hash=compute_input_hash(
                str(row["content_sha"]),
                row["active_index_version"],
                row["document_text_hash"],
            ),
        )
        for row in rows
    ]


def _sample_evenly(ids: list[int], limit: int) -> list[int]:
    if len(ids) <= limit:
        return ids
    # linspace keeps beginning/end/structure-spanning evidence instead of only
    # taking the document head, which was already shown to be a biased proxy.
    positions = np.linspace(0, len(ids) - 1, num=limit, dtype=np.int64)
    return [ids[int(pos)] for pos in np.unique(positions)]


def _sample_chunk_ids(
    conn: sqlite3.Connection,
    items: list[RelationItemVersion],
    chunks_per_item: int,
) -> dict[int, list[int]]:
    by_content: dict[int, list[int]] = {item.content_id: [] for item in items}
    content_ids = list(by_content)
    for start in range(0, len(content_ids), _SQL_BATCH):
        batch = content_ids[start : start + _SQL_BATCH]
        marks = ",".join("?" * len(batch))
        rows = conn.execute(
            f"""SELECT ch.id, ch.content_id
                FROM chunks ch
                JOIN contents c ON c.id = ch.content_id
                WHERE ch.content_id IN ({marks})
                  AND ch.index_version = c.active_index_version
                  AND ch.layer = 'child'
                  AND ch.embedding_model_id IS NOT NULL
                ORDER BY ch.content_id, ch.ordinal, ch.id""",
            batch,
        )
        for row in rows:
            by_content[int(row["content_id"])].append(int(row["id"]))
    return {
        content_id: _sample_evenly(ids, chunks_per_item)
        for content_id, ids in by_content.items()
        if ids
    }


def _load_sample_vectors(
    conn: sqlite3.Connection,
    sampled: dict[int, list[int]],
) -> dict[int, np.ndarray]:
    all_ids = [chunk_id for ids in sampled.values() for chunk_id in ids]
    loaded: dict[int, np.ndarray] = {}
    for start in range(0, len(all_ids), _VECTOR_BATCH):
        loaded.update(vector.vectors_for(conn, all_ids[start : start + _VECTOR_BATCH]))
    return loaded


def _document_centroids(
    items: list[RelationItemVersion],
    sampled: dict[int, list[int]],
    vectors: dict[int, np.ndarray],
) -> tuple[list[RelationItemVersion], np.ndarray | None]:
    kept: list[RelationItemVersion] = []
    centroids: list[np.ndarray] = []
    for item in items:
        vecs = [vectors[cid] for cid in sampled.get(item.content_id, []) if cid in vectors]
        if not vecs:
            continue
        centroid = np.asarray(vecs, dtype=np.float32).mean(axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm <= 1e-12:
            continue
        kept.append(item)
        centroids.append(centroid / norm)
    if not centroids:
        return kept, None
    return kept, np.vstack(centroids).astype(np.float32, copy=False)


def _mutual_top_k_edges(
    items: list[RelationItemVersion],
    matrix: np.ndarray,
    *, top_k: int,
    min_score: float,
) -> tuple[tuple[int, int, float], ...]:
    n = len(items)
    if n < 2:
        return ()
    k = min(top_k, n - 1)
    neighbours: list[dict[int, float]] = [dict() for _ in range(n)]

    # Block the score matrix: a 5k-item library would otherwise allocate a
    # 100MB dense pairwise matrix just to keep results that are immediately
    # reduced to Top-K.
    for start in range(0, n, _QUERY_BLOCK):
        stop = min(start + _QUERY_BLOCK, n)
        scores = matrix[start:stop] @ matrix.T
        for local, index in enumerate(range(start, stop)):
            row = scores[local]
            row[index] = -np.inf
            top = np.argpartition(-row, k - 1)[:k]
            top = top[np.argsort(-row[top])]
            for other in top:
                score = float(row[int(other)])
                if score >= min_score:
                    neighbours[index][int(other)] = score

    edges: list[tuple[int, int, float]] = []
    for left_index, candidates in enumerate(neighbours):
        for right_index, score in candidates.items():
            if right_index <= left_index:
                continue
            if left_index not in neighbours[right_index]:
                continue
            left_id = items[left_index].item_id
            right_id = items[right_index].item_id
            edges.append((min(left_id, right_id), max(left_id, right_id), score))
    edges.sort(key=lambda edge: (edge[0], edge[1]))
    return tuple(edges)


def build_relation_plan(
    conn: sqlite3.Connection,
    *,
    limit: int = DEFAULT_LIMIT,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    chunks_per_item: int = DEFAULT_CHUNKS_PER_ITEM,
) -> RelationPlan:
    """Read existing vectors and compute a versioned relation plan."""
    limit, top_k, min_score, chunks_per_item = _clamp_options(
        limit=limit,
        top_k=top_k,
        min_score=min_score,
        chunks_per_item=chunks_per_item,
    )
    total, selected = _visible_items(conn, limit)
    if len(selected) < 2:
        return RelationPlan(
            tuple(selected), (), total, 0, top_k, min_score, chunks_per_item,
        )

    sampled = _sample_chunk_ids(conn, selected, chunks_per_item)
    loaded = _load_sample_vectors(conn, sampled)
    vectorized_items, matrix = _document_centroids(selected, sampled, loaded)
    edges = () if matrix is None else _mutual_top_k_edges(
        vectorized_items, matrix, top_k=top_k, min_score=min_score,
    )
    return RelationPlan(
        items=tuple(selected),
        edges=edges,
        total_visible=total,
        vectorized=len(vectorized_items),
        top_k=top_k,
        min_score=min_score,
        chunks_per_item=chunks_per_item,
    )


def apply_relation_plan(
    conn: sqlite3.Connection,
    plan: RelationPlan,
    *,
    now: float | None = None,
) -> dict:
    """Apply a relation plan, rejecting items whose active document changed."""
    ts = time.time() if now is None else float(now)
    processed_ids = [item.item_id for item in plan.items]
    if not processed_ids:
        return {
            "processed": 0,
            "vectorized": plan.vectorized,
            "relations": 0,
            "stale_skipped": 0,
            "total_visible": plan.total_visible,
            "truncated": plan.truncated,
        }

    # Remove old derived edges touching this rebuilt subset first. Their version
    # snapshots cascade automatically. If an item changed since planning, it
    # intentionally stays relation-less until the next rebuild rather than
    # keeping a now-stale similarity edge.
    for start in range(0, len(processed_ids), _SQL_BATCH):
        batch = processed_ids[start : start + _SQL_BATCH]
        marks = ",".join("?" * len(batch))
        conn.execute(
            f"""DELETE FROM library_relations
                WHERE relation_type='related_to' AND source=?
                  AND (source_item_id IN ({marks}) OR target_item_id IN ({marks}))""",
            [RELATION_SOURCE, *batch, *batch],
        )

    expected = {item.item_id: item.input_hash for item in plan.items}
    valid: set[int] = set()
    visible = visible_content_exists("li.content_id", "vf", "vs")
    for start in range(0, len(processed_ids), _SQL_BATCH):
        batch = processed_ids[start : start + _SQL_BATCH]
        marks = ",".join("?" * len(batch))
        rows = conn.execute(
            f"""SELECT li.id, li.input_hash AS stored_input_hash,
                       c.sha256 AS content_sha, c.active_index_version,
                       dr.text_hash AS document_text_hash
                FROM library_items li
                JOIN contents c ON c.id = li.content_id
                LEFT JOIN document_representations dr
                  ON dr.content_id = c.id
                 AND dr.index_version = c.active_index_version
                WHERE li.id IN ({marks}) AND {visible}""",
            batch,
        ).fetchall()
        for row in rows:
            item_id = int(row["id"])
            current = compute_input_hash(
                str(row["content_sha"]),
                row["active_index_version"],
                row["document_text_hash"],
            )
            if (
                current == expected[item_id]
                and str(row["stored_input_hash"]) == expected[item_id]
            ):
                valid.add(item_id)

    rows = [
        (left, right, score, RELATION_SOURCE, ts)
        for left, right, score in plan.edges
        if left in valid and right in valid
    ]
    conn.executemany(
        """INSERT INTO library_relations
           (source_item_id,target_item_id,relation_type,score,source,created_at)
           VALUES (?,?,'related_to',?,?,?)
           ON CONFLICT(source_item_id,target_item_id,relation_type)
           DO UPDATE SET score=excluded.score,
                         source=excluded.source,
                         created_at=excluded.created_at""",
        rows,
    )

    version_rows = [
        (
            left,
            right,
            expected[left],
            expected[right],
            ts,
        )
        for left, right, _score, _source, _created_at in rows
    ]
    conn.executemany(
        """INSERT INTO library_relation_versions
           (source_item_id,target_item_id,relation_type,
            source_input_hash,target_input_hash,created_at)
           VALUES (?,?,'related_to',?,?,?)
           ON CONFLICT(source_item_id,target_item_id,relation_type)
           DO UPDATE SET source_input_hash=excluded.source_input_hash,
                         target_input_hash=excluded.target_input_hash,
                         created_at=excluded.created_at""",
        version_rows,
    )
    return {
        "processed": len(plan.items),
        "vectorized": plan.vectorized,
        "relations": len(rows),
        "stale_skipped": len(plan.items) - len(valid),
        "total_visible": plan.total_visible,
        "truncated": plan.truncated,
        "top_k": plan.top_k,
        "min_score": plan.min_score,
        "chunks_per_item": plan.chunks_per_item,
        "source": RELATION_SOURCE,
    }


def relation_status(conn: sqlite3.Connection) -> dict:
    """Return visibility-aware freshness information for derived relations."""
    item_visible = visible_content_exists("li.content_id", "ivf", "ivs")
    total_visible = int(
        conn.execute(
            f"SELECT COUNT(*) FROM library_items li WHERE {item_visible}"
        ).fetchone()[0]
    )

    source_visible = visible_content_exists("src.content_id", "svf", "svs")
    target_visible = visible_content_exists("dst.content_id", "tvf", "tvs")
    rows = conn.execute(
        f"""SELECT rel.source_item_id, rel.target_item_id, rel.created_at,
                   src.input_hash AS source_stored_hash,
                   dst.input_hash AS target_stored_hash,
                   src_c.sha256 AS source_content_sha,
                   src_c.active_index_version AS source_active_index_version,
                   src_dr.text_hash AS source_document_text_hash,
                   dst_c.sha256 AS target_content_sha,
                   dst_c.active_index_version AS target_active_index_version,
                   dst_dr.text_hash AS target_document_text_hash,
                   rv.source_input_hash, rv.target_input_hash,
                   rv.created_at AS version_created_at
             FROM library_relations rel
             JOIN library_items src ON src.id = rel.source_item_id
             JOIN library_items dst ON dst.id = rel.target_item_id
             JOIN contents src_c ON src_c.id = src.content_id
             JOIN contents dst_c ON dst_c.id = dst.content_id
             LEFT JOIN document_representations src_dr
               ON src_dr.content_id = src_c.id
              AND src_dr.index_version = src_c.active_index_version
             LEFT JOIN document_representations dst_dr
               ON dst_dr.content_id = dst_c.id
              AND dst_dr.index_version = dst_c.active_index_version
             LEFT JOIN library_relation_versions rv
              ON rv.source_item_id = rel.source_item_id
             AND rv.target_item_id = rel.target_item_id
             AND rv.relation_type = rel.relation_type
            WHERE rel.relation_type='related_to' AND rel.source=?
              AND {source_visible}
              AND {target_visible}""",
        (RELATION_SOURCE,),
    ).fetchall()

    valid_edges = 0
    stale_edges = 0
    covered: set[int] = set()
    latest: float | None = None
    for row in rows:
        source_current_hash = compute_input_hash(
            str(row["source_content_sha"]),
            row["source_active_index_version"],
            row["source_document_text_hash"],
        )
        target_current_hash = compute_input_hash(
            str(row["target_content_sha"]),
            row["target_active_index_version"],
            row["target_document_text_hash"],
        )
        valid = (
            row["source_input_hash"] is not None
            and row["target_input_hash"] is not None
            and str(row["source_input_hash"]) == source_current_hash
            and str(row["target_input_hash"]) == target_current_hash
            and str(row["source_stored_hash"]) == source_current_hash
            and str(row["target_stored_hash"]) == target_current_hash
        )
        if not valid:
            stale_edges += 1
            continue
        valid_edges += 1
        covered.add(int(row["source_item_id"]))
        covered.add(int(row["target_item_id"]))
        created = float(row["version_created_at"] or row["created_at"] or 0)
        latest = created if latest is None else max(latest, created)

    return {
        "source": RELATION_SOURCE,
        "total_visible": total_visible,
        "relations": valid_edges,
        "stale_relations": stale_edges,
        "covered_items": len(covered),
        "coverage": (len(covered) / total_visible) if total_visible else 0.0,
        "updated_at": latest,
        "needs_rebuild": bool(stale_edges) or (total_visible >= 2 and valid_edges == 0),
    }
