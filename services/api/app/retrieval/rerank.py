"""Reranker protocol, local pair scorer, and explicit RRF fallback."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.index.search import extract_query_terms

RERANK_LIMIT = 80
SOFT_PER_CONTENT = 12


@dataclass(frozen=True)
class RerankInput:
    chunk_id: int
    content_id: int
    text: str
    section_path: str
    rrf_score: float


@dataclass(frozen=True)
class RerankOutput:
    chunk_id: int
    score: float


@dataclass(frozen=True)
class RerankResult:
    ranked: list[RerankOutput]
    model_id: str
    degraded: bool
    duration_ms: float
    reranked_count: int


class Reranker(Protocol):
    model_id: str

    def rerank(
        self, query: str, candidates: list[RerankInput],
    ) -> list[RerankOutput]: ...


class RrfOnlyReranker:
    model_id = "rrf-only"

    def rerank(
        self, query: str, candidates: list[RerankInput],
    ) -> list[RerankOutput]:
        del query
        return [
            RerankOutput(chunk_id=item.chunk_id, score=item.rrf_score)
            for item in candidates
        ]


class LocalStaticReranker:
    """Fast local bi-encoder pair scorer using the already bundled model.

    This is deliberately named static, not cross-encoder: it benchmarks an
    available zero-dependency local path while the model selection gate remains
    open for a true cross-encoder candidate.
    """

    model_id = "local-static-v1"

    def rerank(
        self, query: str, candidates: list[RerankInput],
    ) -> list[RerankOutput]:
        from app.index import embedding as emb

        if not emb.is_available():
            raise emb.EmbeddingUnavailable("本地精排模型不可用")
        model = emb.get_embedder()
        query_vector = model.encode_one(query)
        texts = [
            emb.embed_text_for(item.text, item.section_path)
            for item in candidates
        ]
        vectors = model.encode(texts)
        semantic = vectors @ query_vector
        terms = extract_query_terms(query)
        max_rrf = max((item.rrf_score for item in candidates), default=1.0)

        outputs: list[RerankOutput] = []
        for index, item in enumerate(candidates):
            haystack = f"{item.section_path}\n{item.text}".lower()
            coverage = (
                sum(1 for term in terms if term in haystack) / len(terms)
                if terms else 0.0
            )
            exact = 1.0 if query.strip().lower() in haystack else 0.0
            semantic_score = (float(semantic[index]) + 1.0) / 2.0
            rrf_score = item.rrf_score / max(max_rrf, 1e-12)
            score = (
                0.45 * semantic_score
                + 0.30 * coverage
                + 0.20 * rrf_score
                + 0.05 * exact
            )
            outputs.append(RerankOutput(item.chunk_id, score))
        return sorted(outputs, key=lambda item: item.score, reverse=True)


def _load_inputs(conn, candidates) -> tuple[list[RerankInput], list[int]]:
    if not candidates:
        return [], []
    ordered_ids = [candidate.chunk_id for candidate in candidates]
    marks = ",".join("?" * len(ordered_ids))
    rows = {
        row["id"]: row for row in conn.execute(
            f"""SELECT ch.id, ch.content_id, ch.text, ch.section_path
                FROM chunks ch JOIN contents c ON c.id = ch.content_id
                WHERE ch.id IN ({marks})
                  AND ch.index_version = c.active_index_version""",
            ordered_ids,
        )
    }
    selected: list[RerankInput] = []
    selected_ids: set[int] = set()
    per_content: dict[int, int] = {}
    by_id = {candidate.chunk_id: candidate for candidate in candidates}
    for chunk_id in ordered_ids:
        row = rows.get(chunk_id)
        if row is None:
            continue
        content_id = row["content_id"]
        if per_content.get(content_id, 0) >= SOFT_PER_CONTENT:
            continue
        if len(selected) >= RERANK_LIMIT:
            break
        per_content[content_id] = per_content.get(content_id, 0) + 1
        selected_ids.add(chunk_id)
        selected.append(RerankInput(
            chunk_id=chunk_id,
            content_id=content_id,
            text=row["text"],
            section_path=row["section_path"] or "",
            rrf_score=by_id[chunk_id].rrf_score,
        ))
    remainder = [chunk_id for chunk_id in ordered_ids if chunk_id not in selected_ids]
    return selected, remainder


def run_rerank(conn, query: str, candidates) -> RerankResult:
    started = time.perf_counter()
    mode = os.environ.get("INKTABLE_RERANKER", "local").strip().lower()
    fallback = RrfOnlyReranker()
    if mode in {"off", "rrf", "disabled"}:
        selected = [
            RerankInput(
                chunk_id=item.chunk_id, content_id=0, text="",
                section_path="", rrf_score=item.rrf_score,
            )
            for item in candidates
        ]
        ranked = fallback.rerank(query, selected)
        return RerankResult(
            ranked=ranked, model_id=fallback.model_id, degraded=True,
            duration_ms=(time.perf_counter() - started) * 1000,
            reranked_count=0,
        )

    selected, remainder = _load_inputs(conn, candidates)
    reranker: Reranker = LocalStaticReranker()
    degraded = False
    try:
        ranked = reranker.rerank(query, selected)
        model_id = reranker.model_id
    except Exception:
        all_inputs = [
            RerankInput(
                chunk_id=item.chunk_id, content_id=0, text="",
                section_path="", rrf_score=item.rrf_score,
            )
            for item in candidates
        ]
        return RerankResult(
            ranked=fallback.rerank(query, all_inputs),
            model_id=fallback.model_id,
            degraded=True,
            duration_ms=(time.perf_counter() - started) * 1000,
            reranked_count=0,
        )

    by_id = {candidate.chunk_id: candidate for candidate in candidates}
    ranked.extend(
        RerankOutput(chunk_id, by_id[chunk_id].rrf_score)
        for chunk_id in remainder if chunk_id in by_id
    )
    return RerankResult(
        ranked=ranked,
        model_id=model_id,
        degraded=degraded,
        duration_ms=(time.perf_counter() - started) * 1000,
        reranked_count=len(selected),
    )
