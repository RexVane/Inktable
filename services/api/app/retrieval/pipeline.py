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
from dataclasses import dataclass, field
from typing import Any

from app.index.hierarchy import hierarchy_routes
from app.index.search import search as child_search
from app.retrieval.query import decompose_comparative, mentioned_exts
from app.retrieval.rerank import run_rerank
from app.retrieval.compress import (
    ContextPack,
    EvidenceSource,
    assemble_pack,
    extract_spans,
)

RRF_K = 60
# 子查询是补充入口，权重略低于四条主路线，避免分解误判时喧宾夺主
ROUTE_WEIGHTS = {"document": 0.25, "section": 0.5, "subquery": 0.8}


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
                JOIN book_members bm ON bm.file_id = f.id
                WHERE bm.book_id = ? AND ch.id IN ({marks})""",
            [book_id, *candidate_ids],
        )
    }
    return {
        name: [(chunk_id, score) for chunk_id, score in hits
               if chunk_id in allowed]
        for name, hits in routes.items()
    }


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
        book_id: int | None = None) -> RetrievalResult:
    """Run the stable V1 retrieval stages and return a non-persistent trace."""
    normalized = str(query or "").strip()
    plan = QueryPlan(query=normalized, route_limit=route_limit,
                     candidate_limit=candidate_limit, book_id=book_id)
    trace = RetrievalTrace(
        trace_id=uuid.uuid4().hex,
        query_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
    )

    started = time.perf_counter()
    hierarchy = hierarchy_routes(conn, normalized, limit=plan.route_limit)
    trace.stage(
        "hierarchy_routing", started,
        document_candidates=len(hierarchy["document"]),
        section_candidates=len(hierarchy["section"]),
    )

    started = time.perf_counter()
    routes = child_search(
        conn, normalized, limit=plan.route_limit, include_hierarchy=False,
    )
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
    subqueries = tuple(decompose_comparative(normalized))
    subquery_head = min(30, plan.route_limit)
    for index, subquery in enumerate(subqueries):
        sub_routes = child_search(
            conn, subquery, limit=max(20, plan.route_limit // 2),
            include_hierarchy=False,
        )
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
    candidates = _fuse(routes)
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
            f"""SELECT ch.id, ch.content_id, ch.section_id, ch.page,
                       ch.section_path, ch.ordinal, ch.text, ch.start_offset,
                       ch.end_offset, f.id AS file_id, f.name, f.path
                FROM chunks ch JOIN files f ON f.content_id = ch.content_id
                WHERE ch.id IN ({marks})
                GROUP BY ch.id""",
            ordered,
        )
    else:
        row_iter = conn.execute(
            f"""SELECT ch.id, ch.content_id, ch.section_id, ch.page,
                       ch.section_path, ch.ordinal, ch.text, ch.start_offset,
                       ch.end_offset, f.id AS file_id, f.name, f.path
                FROM chunks ch
                JOIN files f ON f.content_id = ch.content_id
                JOIN book_members bm ON bm.file_id = f.id
                WHERE bm.book_id = ? AND ch.id IN ({marks})
                GROUP BY ch.id""",
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
                     trace: RetrievalTrace | None = None) -> list[ContextCandidate]:
    """Restore local Child neighbors after ranking and diversity selection."""
    started = time.perf_counter()
    expanded: list[ContextCandidate] = []
    for candidate in candidates:
        rows = conn.execute(
            """SELECT id, content_id, section_id, page, section_path, ordinal,
                      text, start_offset, end_offset FROM chunks
               WHERE content_id = ? AND index_version = (
                   SELECT active_index_version FROM contents WHERE id = ?
               ) AND ordinal BETWEEN ? AND ?
               ORDER BY ordinal""",
            (candidate.content_id, candidate.content_id,
             candidate.ordinal - neighbor_span,
             candidate.ordinal + neighbor_span),
        ).fetchall()
        sources = tuple(EvidenceSource(
            chunk_id=row["id"], content_id=row["content_id"],
            section_id=row["section_id"], file_id=candidate.file_id,
            file_name=candidate.file_name, file_path=candidate.file_path,
            page=row["page"], section_path=row["section_path"] or "",
            ordinal=row["ordinal"], text=row["text"],
            document_start_offset=row["start_offset"],
            document_end_offset=row["end_offset"],
            candidate_score=candidate.relevance_score,
            ordinal_distance=abs(row["ordinal"] - candidate.ordinal),
        ) for row in rows)
        expanded.append(ContextCandidate(
            **{**candidate.__dict__,
               "expanded_text": "\n".join(row["text"] for row in rows),
               "expanded_sources": sources},
        ))
    if trace is not None:
        trace.stage("expand", started, selected_count=len(expanded),
                    neighbor_span=neighbor_span)
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
