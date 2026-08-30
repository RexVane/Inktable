"""Versioned retrieval pipeline shared by search and QA.

M1 deliberately preserves the current behavior: four independent routes are
scoped, fused with RRF, and returned as Child candidates. Later milestones can
replace individual stages with hierarchy routing, reranking, and compression
without making the desktop search and QA paths drift apart again.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from app.db.visibility import (
    VISIBLE_FILES_COND,
    visible_content_exists,
    visible_files_condition,
)
from app.index.hierarchy import hierarchy_routes
from app.index.search import search as child_search
from app.index.search import vector_route
from app.retrieval.compress import (
    ContextPack,
    EvidenceSource,
    assemble_pack,
    extract_spans,
)
from app.retrieval.query import decompose_comparative, mentioned_exts
from app.retrieval.rerank import run_rerank

RRF_K = 60
DOCUMENT_HEAD_CHUNKS = 3


def _main_db_path(conn) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or ":memory:"
    return ":memory:"


def _parallel_subquery_search(db_path: str, subquery: str, limit: int,
                              query_vector=None):
    from app.db.database import connect
    child = connect(db_path)
    try:
        return child_search(
            child, subquery, limit=limit, include_hierarchy=False,
            vector_query=query_vector, include_substring=False,
        )
    finally:
        child.close()


# 查询嵌入是一次 Ollama HTTP 往返，本机实测约 620ms，而同一次调用编码 8 条
# 也只要 719ms —— 成本几乎全是每请求固定开销，不是算力。由此两条：
#
#   1. 全部查询变体合并成一次调用。每条子查询各发一次请求会把比较类问题的
#      嵌入开销乘上变体数。
#   2. 整个调用丢后台线程。它不依赖任何词法路线，却曾串在关键路径最前面，
#      让分层路由和四路召回（p95 合计约 1 秒）白等。
#
# 线程里只做 HTTP，不碰 SQLite —— 同一连接并发执行语句不安全。
def _start_query_embedding(variants: tuple[str, ...]):
    """后台批量编码全部查询变体。不可用时返回 None，向量路自行降级。"""
    if not variants:
        return None

    def work() -> dict[str, Any]:
        from app.index import embedding as emb
        if not emb.is_available():
            return {}
        vectors = emb.get_embedder().encode(list(variants))
        return {text: vectors[index] for index, text in enumerate(variants)}

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(work)
    except Exception:  # noqa: BLE001 - 起不了线程就退回同步路径
        pool.shutdown(wait=False)
        return None
    return (pool, future)


def _collect_query_embedding(handle) -> dict[str, Any]:
    """取回批量编码结果。任何失败都只是没有向量，检索退化为纯词法。"""
    if handle is None:
        return {}
    pool, future = handle
    try:
        return future.result()
    except Exception:  # noqa: BLE001 - 嵌入失败不能让整次检索失败
        return {}
    finally:
        pool.shutdown(wait=False)


# 子查询是补充入口，权重略低于四条主路线，避免分解误判时喧宾夺主
ROUTE_WEIGHTS = {
    "document": 0.25,
    "section": 0.5,
    "subquery": 0.8,
    # Filename matches are explicit metadata supplied by the user, not a fuzzy
    # semantic hint. Give this route enough weight to survive multi-route body
    # matches while keeping it inside the same soft RRF fusion contract.
    "filename": 1.0,
    "filename:metadata": 3.0,
}


@dataclass(frozen=True)
class QueryPlan:
    query: str
    route_limit: int = 100
    candidate_limit: int | None = None
    book_id: int | None = None


@dataclass(frozen=True)
class Candidate:
    chunk_id: int
    rrf_score: float
    route_ranks: dict[str, int] = field(default_factory=dict)
    rerank_score: float | None = None

    @property
    def final_score(self) -> float:
        return self.rerank_score if self.rerank_score is not None else self.rrf_score


@dataclass
class RetrievalTrace:
    trace_id: str
    query_hash: str
    stages: list[dict[str, Any]] = field(default_factory=list)
    routes: dict[str, int] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    deep_candidates: list[int] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    def stage(self, name: str, started: float, **details: Any) -> None:
        self.stages.append({
            "name": name,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            **details,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "query_hash": self.query_hash,
            "stages": self.stages,
            "routes": self.routes,
            "candidates": self.candidates,
            "deep_candidates": self.deep_candidates,
            "degraded": self.degraded,
        }


@dataclass(frozen=True)
class RetrievalResult:
    plan: QueryPlan
    routes: dict[str, list[tuple[int, float]]]
    candidates: list[Candidate]
    trace: RetrievalTrace


@dataclass(frozen=True)
class ContextCandidate:
    chunk_id: int
    content_id: int
    file_id: int
    file_name: str
    file_path: str
    page: int | None
    section_path: str
    ordinal: int
    text: str
    section_id: int | None = None
    document_start_offset: int | None = None
    document_end_offset: int | None = None
    relevance_score: float = 0.0
    expanded_text: str = ""
    expanded_sources: tuple[EvidenceSource, ...] = ()


def _scope_routes_to_book(conn, routes: dict[str, list[tuple[int, float]]],
                          book_id: int | None) -> dict[str, list[tuple[int, float]]]:
    if book_id is None:
        return routes

    candidate_ids = {
        chunk_id for hits in routes.values() for chunk_id, _score in hits
    }
    if not candidate_ids:
        return {name: [] for name in routes}

    marks = ",".join("?" * len(candidate_ids))
    allowed = {
        row["id"] for row in conn.execute(
            f"""SELECT DISTINCT ch.id
                FROM chunks ch
                JOIN files f ON f.content_id = ch.content_id
                LEFT JOIN sources s ON s.id = f.source_id
                JOIN book_members bm ON bm.file_id = f.id
                WHERE bm.book_id = ? AND ch.id IN ({marks})
                  AND {visible_files_condition()}
                  AND {visible_content_exists('ch.content_id')}""",
            [book_id, *candidate_ids],
        )
    }
    return {
        name: [(chunk_id, score) for chunk_id, score in hits
               if chunk_id in allowed]
        for name, hits in routes.items()
    }


def _prioritize_route_heads(
    candidates: list[Candidate], routes: dict[str, list[tuple[int, float]]],
) -> list[Candidate]:
    """Keep strong independent-route evidence inside the rerank/deep pool."""
    by_id = {candidate.chunk_id: candidate for candidate in candidates}
    ordered: list[Candidate] = []
    seen: set[int] = set()

    def add(ids) -> None:
        for chunk_id in ids:
            if chunk_id in seen or chunk_id not in by_id:
                continue
            seen.add(chunk_id)
            ordered.append(by_id[chunk_id])

    add(candidate.chunk_id for candidate in candidates[:24])
    add(chunk_id for chunk_id, _score in routes.get("vector", [])[:20])
    for name in sorted(route for route in routes if route.startswith("subquery:")):
        add(chunk_id for chunk_id, _score in routes[name][:10])
    for name, quota in (("document", 8), ("section", 8),
                        ("jieba", 8), ("trigram", 8),
                        ("filename:metadata", 8)):
        add(chunk_id for chunk_id, _score in routes.get(name, [])[:quota])
    add(candidate.chunk_id for candidate in candidates)
    return ordered


def _fuse(routes: dict[str, list[tuple[int, float]]]) -> list[Candidate]:
    scores: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}
    for route_name, hits in routes.items():
        weight = ROUTE_WEIGHTS.get(route_name)
        if weight is None:
            weight = ROUTE_WEIGHTS.get(route_name.split(":", 1)[0], 1.0)
        for rank, (chunk_id, _score) in enumerate(hits, start=1):
            scores[chunk_id] = (
                scores.get(chunk_id, 0.0) + weight / (RRF_K + rank)
            )
            ranks.setdefault(chunk_id, {})[route_name] = rank

    return [
        Candidate(chunk_id=chunk_id, rrf_score=score,
                  route_ranks=ranks.get(chunk_id, {}))
        for chunk_id, score in sorted(
            scores.items(), key=lambda item: item[1], reverse=True
        )
    ]


def run(conn, query: str, *, route_limit: int = 100,
        candidate_limit: int | None = None,
        book_id: int | None = None,
        reranker: str | None = None) -> RetrievalResult:
    """Run the stable V1 retrieval stages and return a non-persistent trace.

    ``reranker`` 只是「没人指定时用哪个重排实现」的偏好，环境变量
    `INKTABLE_RERANKER` 始终优先。重排是固定架构阶段（PLAN §0 决策 3），
    实现可替换 —— 但**搜索与问答共用同一条管线**是 v7 的明确决策，
    给两边配不同实现会让「搜到的」与「答出来的」证据顺序分叉，
    不要在没有明确决定的情况下这么做。
    """
    normalized = str(query or "").strip()
    plan = QueryPlan(query=normalized, route_limit=route_limit,
                     candidate_limit=candidate_limit, book_id=book_id)
    trace = RetrievalTrace(
        trace_id=uuid.uuid4().hex,
        query_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
    )

    started = time.perf_counter()
    subqueries = tuple(decompose_comparative(normalized))
    # 先把嵌入丢出去，再做分层路由和词法召回 —— 这两段的耗时因此被
    # 嵌入的固定往返吃掉，而不是叠加在它后面。
    # 空查询不发嵌入请求：向量路被拆出来单独调用后，不再受 child_search
    # 里「两路 FTS 表达式都为空就直接返回」那道闸的保护，空串会拿到一个
    # 无意义的向量并召回 120 条任意分片。
    embed_handle = (
        _start_query_embedding((normalized, *subqueries)) if normalized else None
    )

    hierarchy = hierarchy_routes(conn, normalized, limit=plan.route_limit)
    trace.stage(
        "hierarchy_routing", started,
        document_candidates=len(hierarchy["document"]),
        section_candidates=len(hierarchy["section"]),
    )

    started = time.perf_counter()
    # 词法四路先跑：此刻嵌入请求还在后台飞，这段时间是免费的
    routes = child_search(
        conn, normalized, limit=plan.route_limit, include_hierarchy=False,
        include_vector=False,
    )
    trace.stage(
        "lexical_retrieval", started, route_limit=plan.route_limit,
        candidate_count=sum(len(hits) for hits in routes.values()),
    )

    started = time.perf_counter()
    query_vectors = _collect_query_embedding(embed_handle)
    trace.stage("embed_query", started, variant_count=len(query_vectors))

    started = time.perf_counter()
    routes["vector"] = vector_route(
        conn, normalized, plan.route_limit,
        query_vector=query_vectors.get(normalized),
    ) if normalized else []
    routes.update(hierarchy)
    trace.routes = {name: len(hits) for name, hits in routes.items() if hits}
    child_count = sum(
        len(hits) for name, hits in routes.items()
        if name not in {"document", "section"}
    )
    trace.stage(
        "deep_retrieval", started, route_limit=plan.route_limit,
        candidate_count=child_count,
    )

    # 比较类问题分解：每个实体一条子查询，作为额外召回路线进入 RRF。
    # 只增加入口不做排除（K3）；非比较问题此阶段为空、零额外开销。
    # 每条子路线只取头部命中 —— 尾部是噪声，会把无关文件抬进融合结果。
    started = time.perf_counter()
    subquery_head = min(30, plan.route_limit)
    sub_limit = max(20, plan.route_limit // 2)
    sub_results = []
    db_path = _main_db_path(conn)
    if len(subqueries) > 1 and db_path not in {"", ":memory:"}:
        with ThreadPoolExecutor(max_workers=len(subqueries)) as pool:
            futures = [
                pool.submit(_parallel_subquery_search, db_path, query, sub_limit,
                            query_vectors.get(query))
                for query in subqueries
            ]
            sub_results = [future.result() for future in futures]
    else:
        sub_results = [
            child_search(
                conn, query, limit=sub_limit, include_hierarchy=False,
                vector_query=query_vectors.get(query), include_substring=False,
            )
            for query in subqueries
        ]
    for index, sub_routes in enumerate(sub_results):
        fused_sub = _fuse(sub_routes)
        routes[f"subquery:{index}"] = [
            (candidate.chunk_id, candidate.rrf_score)
            for candidate in fused_sub[:subquery_head]
        ]
    if subqueries:
        trace.routes = {name: len(hits) for name, hits in routes.items() if hits}
    trace.stage("decompose", started, subquery_count=len(subqueries))

    started = time.perf_counter()
    routes = _scope_routes_to_book(conn, routes, book_id)
    trace.routes = {name: len(hits) for name, hits in routes.items() if hits}
    trace.stage("scope", started, book_id=book_id,
                candidate_count=sum(trace.routes.values()))

    started = time.perf_counter()
    candidates = _prioritize_route_heads(_fuse(routes), routes)
    trace.deep_candidates = [candidate.chunk_id for candidate in candidates[:50]]
    trace.candidates = [
        {
            "chunk_id": candidate.chunk_id,
            "rrf_score": candidate.rrf_score,
            "route_ranks": candidate.route_ranks,
        }
        for candidate in candidates[:50]
    ]
    trace.stage("rrf", started, candidate_count=len(candidates))

    started = time.perf_counter()
    reranked = run_rerank(
        conn, normalized, candidates,
        subqueries=subqueries, ext_hints=mentioned_exts(normalized),
        mode=reranker,
    )
    by_id = {candidate.chunk_id: candidate for candidate in candidates}
    candidates = [
        Candidate(
            chunk_id=item.chunk_id,
            rrf_score=by_id[item.chunk_id].rrf_score,
            route_ranks=by_id[item.chunk_id].route_ranks,
            rerank_score=item.score,
        )
        for item in reranked.ranked if item.chunk_id in by_id
    ]
    if reranked.degraded:
        trace.degraded.append("rerank")
    trace.stage(
        "rerank", started, candidate_count=len(candidates),
        reranked_count=reranked.reranked_count, model_id=reranked.model_id,
        degraded=reranked.degraded,
    )
    trace.candidates = [
        {
            "chunk_id": candidate.chunk_id,
            "rrf_score": candidate.rrf_score,
            "rerank_score": candidate.rerank_score,
            "route_ranks": candidate.route_ranks,
        }
        for candidate in candidates[:50]
    ]

    selected = candidates if candidate_limit is None else candidates[:candidate_limit]
    return RetrievalResult(plan=plan, routes=routes,
                           candidates=selected, trace=trace)


def load_context_candidates(conn, retrieval: RetrievalResult, *,
                            limit: int, max_per_content: int,
                            book_id: int | None = None) -> list[ContextCandidate]:
    """Load ranked Child rows and apply the existing content diversity cap."""
    if not retrieval.candidates:
        return []

    ordered = [candidate.chunk_id for candidate in retrieval.candidates]
    marks = ",".join("?" * len(ordered))
    if book_id is None:
        row_iter = conn.execute(
            f"""WITH visible_files AS (
                    SELECT f.id, f.content_id, f.name, f.path,
                           ROW_NUMBER() OVER (
                               PARTITION BY f.content_id ORDER BY f.id
                           ) AS replica_rank
                    FROM files f
                    LEFT JOIN sources s ON s.id = f.source_id
                    WHERE {VISIBLE_FILES_COND}
                )
                SELECT ch.id, ch.content_id, ch.section_id, ch.page,
                       ch.section_path, ch.ordinal, ch.text, ch.start_offset,
                       ch.end_offset, f.id AS file_id, f.name, f.path
                FROM chunks ch
                JOIN visible_files f
                  ON f.content_id = ch.content_id AND f.replica_rank = 1
                WHERE ch.id IN ({marks})""",
            ordered,
        )
    else:
        row_iter = conn.execute(
            f"""WITH visible_book_files AS (
                    SELECT f.id, f.content_id, f.name, f.path,
                           ROW_NUMBER() OVER (
                               PARTITION BY f.content_id ORDER BY f.id
                           ) AS replica_rank
                    FROM files f
                    LEFT JOIN sources s ON s.id = f.source_id
                    JOIN book_members bm ON bm.file_id = f.id
                    WHERE bm.book_id = ? AND {VISIBLE_FILES_COND}
                )
                SELECT ch.id, ch.content_id, ch.section_id, ch.page,
                       ch.section_path, ch.ordinal, ch.text, ch.start_offset,
                       ch.end_offset, f.id AS file_id, f.name, f.path
                FROM chunks ch
                JOIN visible_book_files f
                  ON f.content_id = ch.content_id AND f.replica_rank = 1
                WHERE ch.id IN ({marks})""",
            [book_id, *ordered],
        )
    rows = {row["id"]: row for row in row_iter}

    started = time.perf_counter()
    selected: list[ContextCandidate] = []
    per_content: dict[int, int] = {}
    by_id = {candidate.chunk_id: candidate for candidate in retrieval.candidates}
    for chunk_id in ordered:
        row = rows.get(chunk_id)
        if row is None:
            continue
        content_id = row["content_id"]
        if per_content.get(content_id, 0) >= max_per_content:
            continue
        per_content[content_id] = per_content.get(content_id, 0) + 1
        selected.append(ContextCandidate(
            chunk_id=row["id"], content_id=content_id,
            file_id=row["file_id"], file_name=row["name"],
            file_path=row["path"], page=row["page"],
            section_path=row["section_path"] or "", ordinal=row["ordinal"],
            text=row["text"],
            section_id=row["section_id"],
            document_start_offset=row["start_offset"],
            document_end_offset=row["end_offset"],
            relevance_score=by_id[chunk_id].final_score,
        ))
        if len(selected) >= limit:
            break
    retrieval.trace.stage("diversify", started, selected_count=len(selected),
                          max_per_content=max_per_content)
    return selected


def expand_neighbors(conn, candidates: list[ContextCandidate], *,
                     neighbor_span: int = 1,
                     document_head_chunks: int = DOCUMENT_HEAD_CHUNKS,
                     trace: RetrievalTrace | None = None) -> list[ContextCandidate]:
    """Restore local neighbors plus a small document-head orientation window."""
    started = time.perf_counter()
    expanded: list[ContextCandidate] = []
    for candidate in candidates:
        rows = conn.execute(
            f"""SELECT id, content_id, section_id, page, section_path, ordinal,
                      text, start_offset, end_offset FROM chunks ch
               WHERE content_id = ? AND index_version = (
                   SELECT active_index_version FROM contents WHERE id = ?
               ) AND (ordinal BETWEEN ? AND ? OR ordinal < ?)
                 AND {visible_content_exists('ch.content_id')}
               ORDER BY ordinal""",
            (candidate.content_id, candidate.content_id,
             candidate.ordinal - neighbor_span,
             candidate.ordinal + neighbor_span, document_head_chunks),
        ).fetchall()
        sources = tuple(
            EvidenceSource(
                chunk_id=row["id"], content_id=row["content_id"],
                section_id=row["section_id"], file_id=candidate.file_id,
                file_name=candidate.file_name, file_path=candidate.file_path,
                page=row["page"], section_path=row["section_path"] or "",
                ordinal=row["ordinal"], text=row["text"],
                document_start_offset=row["start_offset"],
                document_end_offset=row["end_offset"],
                candidate_score=candidate.relevance_score,
                # A document head is orientation context, not hundreds of
                # semantic hops away from a hit near the end of the document.
                ordinal_distance=(
                    min(
                        abs(row["ordinal"] - candidate.ordinal),
                        neighbor_span + 1,
                    )
                    if row["ordinal"] < document_head_chunks else
                    abs(row["ordinal"] - candidate.ordinal)
                ),
                is_document_head=row["ordinal"] < document_head_chunks,
            )
            for row in rows
        )
        expanded.append(ContextCandidate(
            **{**candidate.__dict__,
               "expanded_text": "\n".join(row["text"] for row in rows),
               "expanded_sources": sources},
        ))
    if trace is not None:
        trace.stage("expand", started, selected_count=len(expanded),
                    neighbor_span=neighbor_span,
                    document_head_chunks=document_head_chunks)
    return expanded


def compress_evidence(query: str, candidates: list[ContextCandidate], *,
                      trace: RetrievalTrace | None = None):
    """Select exact query-relevant sentence and row spans."""
    started = time.perf_counter()
    raw_sources = [
        source for candidate in candidates
        for source in (candidate.expanded_sources or (EvidenceSource(
            chunk_id=candidate.chunk_id, content_id=candidate.content_id,
            section_id=candidate.section_id, file_id=candidate.file_id,
            file_name=candidate.file_name, file_path=candidate.file_path,
            page=candidate.page, section_path=candidate.section_path,
            ordinal=candidate.ordinal, text=candidate.text,
            document_start_offset=candidate.document_start_offset,
            document_end_offset=candidate.document_end_offset,
            candidate_score=candidate.relevance_score,
        ),))
    ]
    sources: list[EvidenceSource] = []
    seen: set[tuple[int, int]] = set()
    for source in raw_sources:
        key = (source.file_id, source.chunk_id)
        if key in seen:
            continue
        seen.add(key)
        sources.append(source)
    spans = extract_spans(query, sources)
    if trace is not None:
        trace.stage(
            "compress", started, source_count=len(sources), span_count=len(spans),
            source_chars=sum(len(source.text) for source in sources),
        )
    return spans


def assemble_context(spans, *, trace: RetrievalTrace | None = None,
                     char_budget: int = 3600,
                     source_chars: int | None = None) -> ContextPack:
    """Build a token-proxy budgeted ContextPack from exact EvidenceSpans."""
    started = time.perf_counter()
    original_chars = (
        source_chars if source_chars is not None
        else sum(len(span.text) for span in spans)
    )
    pack = assemble_pack(
        spans, source_chars=original_chars, char_budget=char_budget,
    )
    if trace is not None:
        trace.stage(
            "assemble", started, selected_count=len(pack.spans),
            packed_chars=pack.packed_chars, source_chars=pack.source_chars,
            dropped_count=pack.dropped_count,
        )
    return pack


def scope_routes_to_book(conn, routes: dict[str, list[tuple[int, float]]],
                         book_id: int | None) -> dict[str, list[tuple[int, float]]]:
    """Public compatibility helper for API callers during M1 migration."""
    return _scope_routes_to_book(conn, routes, book_id)
