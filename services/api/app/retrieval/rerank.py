"""Reranker protocol, local pair scorer, and explicit RRF fallback."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.index.search import extract_query_terms
from app.retrieval.query import wants_numeric_answer

RERANK_LIMIT = 80
SOFT_PER_CONTENT = 12

# 同文档冗余惩罚：一个分片对查询词没有任何新增覆盖时的降权系数。
# 探针实测（probe-1）F20 的金文档前三名全部在重复"如何启动"这一个
# 方面，而含答案的"版本"分片被挤到文档内第 4 —— 评测与搜索界面
# 都只取每文档前 3 个分片。
REDUNDANCY_PENALTY = 0.92


@dataclass(frozen=True)
class RerankInput:
    chunk_id: int
    content_id: int
    text: str
    section_path: str
    rrf_score: float
    ext: str = ""
    file_name: str = ""


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

    model_id = "local-static-v2"

    def __init__(self, subqueries: tuple[str, ...] = (),
                 ext_hints: frozenset[str] = frozenset()):
        # 比较类问题的子查询：分片只要答对其中一个实体就算相关，
        # 语义/覆盖特征取所有查询变体的最大值。
        self.subqueries = tuple(subqueries)
        self.ext_hints = frozenset(ext_hints)

    def rerank(
        self, query: str, candidates: list[RerankInput],
    ) -> list[RerankOutput]:
        from app.index import embedding as emb

        if not emb.is_available():
            raise emb.EmbeddingUnavailable("本地精排模型不可用")
        model = emb.get_embedder()
        variants = [query, *self.subqueries]
        query_vectors = np.stack([model.encode_one(v) for v in variants])
        texts = [
            emb.embed_text_for(item.text, item.section_path)
            for item in candidates
        ]
        vectors = model.encode(texts)
        # 每个候选对所有查询变体的最大余弦
        semantic = (vectors @ query_vectors.T).max(axis=1)
        variant_terms = [extract_query_terms(v) for v in variants]
        numeric_intent = wants_numeric_answer(query)
        max_rrf = max((item.rrf_score for item in candidates), default=1.0)

        outputs: list[RerankOutput] = []
        for index, item in enumerate(candidates):
            haystack = f"{item.section_path}\n{item.text}".lower()
            coverage = max(
                (sum(1 for term in terms if term in haystack) / len(terms))
                if terms else 0.0
                for terms in variant_terms
            )
            exact = 1.0 if any(
                v.strip().lower() in haystack for v in variants
            ) else 0.0
            semantic_score = (float(semantic[index]) + 1.0) / 2.0
            rrf_score = item.rrf_score / max(max_rrf, 1e-12)
            # 答案类型特征：问数值的查询里，含数字的分片更可能是答案所在
            numeric = 1.0 if (
                numeric_intent and any(ch.isdigit() for ch in item.text)
            ) else 0.0
            # 类型提示特征：查询显式点名了文件格式（"哪份 DOCX…"）
            type_match = 1.0 if (
                self.ext_hints and item.ext.lower() in self.ext_hints
            ) else 0.0
            # 文件名特征："哪份文件记录了 X" 这类元数据问题的答案文件
            # 往往名字就含查询词，正文反而未必复述
            name_hay = item.file_name.lower()
            filename_cov = max(
                (sum(1 for term in terms if term in name_hay) / len(terms))
                if terms else 0.0
                for terms in variant_terms
            ) if name_hay else 0.0
            score = (
                0.45 * semantic_score
                + 0.30 * coverage
                + 0.20 * rrf_score
                + 0.05 * exact
                + 0.05 * numeric
                + 0.06 * type_match
                + 0.08 * filename_cov
            )
            outputs.append(RerankOutput(item.chunk_id, score))
        return sorted(outputs, key=lambda item: item.score, reverse=True)


def _demote_redundant_coverage(
    query: str, candidates: list[RerankInput], ranked: list[RerankOutput],
) -> list[RerankOutput]:
    """同文档内按查询词覆盖去冗：不带来新覆盖的分片轻度降权。

    评测与搜索界面都只展示每文档得分最高的少数分片。若它们全部
    覆盖同一批查询词（同一方面），互补的答案分片就会被挤出。
    这一步不淘汰任何候选（K3），只在文档内部做覆盖多样化。
    """
    terms = extract_query_terms(query)
    if not terms:
        return ranked
    texts = {item.chunk_id: f"{item.section_path}\n{item.text}".lower()
             for item in candidates}
    contents = {item.chunk_id: item.content_id for item in candidates}
    covered: dict[int, set[str]] = {}
    adjusted: list[RerankOutput] = []
    for item in ranked:
        haystack = texts.get(item.chunk_id)
        if haystack is None:
            adjusted.append(item)
            continue
        content_id = contents[item.chunk_id]
        hits = {term for term in terms if term in haystack}
        seen = covered.setdefault(content_id, set())
        if seen and not (hits - seen):
            adjusted.append(RerankOutput(
                item.chunk_id, item.score * REDUNDANCY_PENALTY,
            ))
            continue
        seen.update(hits)
        adjusted.append(item)
    return sorted(adjusted, key=lambda item: item.score, reverse=True)


def _load_inputs(conn, candidates) -> tuple[list[RerankInput], list[int]]:
    if not candidates:
        return [], []
    ordered_ids = [candidate.chunk_id for candidate in candidates]
    marks = ",".join("?" * len(ordered_ids))
    rows = {
        row["id"]: row for row in conn.execute(
            f"""SELECT ch.id, ch.content_id, ch.text, ch.section_path,
                       ch.text_hash,
                       (SELECT f.ext FROM files f
                        WHERE f.content_id = ch.content_id LIMIT 1) AS ext,
                       (SELECT f.name FROM files f
                        WHERE f.content_id = ch.content_id LIMIT 1) AS file_name
                FROM chunks ch JOIN contents c ON c.id = ch.content_id
                WHERE ch.id IN ({marks})
                  AND ch.index_version = c.active_index_version""",
            ordered_ids,
        )
    }
    selected: list[RerankInput] = []
    selected_ids: set[int] = set()
    per_content: dict[int, int] = {}
    seen_text_hash: set[str] = set()
    by_id = {candidate.chunk_id: candidate for candidate in candidates}
    for chunk_id in ordered_ids:
        row = rows.get(chunk_id)
        if row is None:
            continue
        content_id = row["content_id"]
        if per_content.get(content_id, 0) >= SOFT_PER_CONTENT:
            continue
        # 跨内容的完全同文分片只精排排名最高的一份：同一份报告的
        # 多个文件副本会把候选池和文件级 Top-K 全部占满（probe-2 的
        # X06 实测 8 份近重复副本霸占前 8 名）。被跳过的副本保留在
        # remainder 里以 RRF 分参与，不会被淘汰。
        text_hash = row["text_hash"] or ""
        if text_hash and text_hash in seen_text_hash:
            continue
        if len(selected) >= RERANK_LIMIT:
            break
        per_content[content_id] = per_content.get(content_id, 0) + 1
        if text_hash:
            seen_text_hash.add(text_hash)
        selected_ids.add(chunk_id)
        selected.append(RerankInput(
            chunk_id=chunk_id,
            content_id=content_id,
            text=row["text"],
            section_path=row["section_path"] or "",
            rrf_score=by_id[chunk_id].rrf_score,
            ext=row["ext"] or "",
            file_name=row["file_name"] or "",
        ))
    remainder = [chunk_id for chunk_id in ordered_ids if chunk_id not in selected_ids]
    return selected, remainder


def run_rerank(conn, query: str, candidates, *,
               subqueries: tuple[str, ...] = (),
               ext_hints: frozenset[str] = frozenset()) -> RerankResult:
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
    reranker: Reranker = LocalStaticReranker(
        subqueries=subqueries, ext_hints=ext_hints,
    )
    degraded = False
    try:
        ranked = reranker.rerank(query, selected)
        ranked = _demote_redundant_coverage(query, selected, ranked)
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
