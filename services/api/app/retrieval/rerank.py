"""Reranker protocol, local pair scorer, and explicit RRF fallback."""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.index.search import extract_query_terms, quote_fts_query
from app.retrieval.query import wants_numeric_answer

RERANK_LIMIT = 80
SOFT_PER_CONTENT = 12

# 词邻近度：两个查询词在原文中相距多少字符以内算"在讲同一件事"
PROXIMITY_WINDOW = 40

# 跨内容近重复：不同 content 的两个分片向量余弦超过该值视为同一段话
# 的副本（文件被多次复制/另存导致），只保留最高分的那份不受影响。
# 阈值来自实测：同段落的编辑副本余弦 0.965-0.973，相关但不同的
# 文档只有 0.61 —— 0.95 在两者之间留有充分裕量。
NEARDUP_COSINE = 0.95
NEARDUP_PENALTY = 0.80

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

    model_id = "local-static-v3"

    def __init__(self, subqueries: tuple[str, ...] = (),
                 ext_hints: frozenset[str] = frozenset(),
                 term_idf: dict[str, float] | None = None):
        # 比较类问题的子查询：分片只要答对其中一个实体就算相关，
        # 语义/覆盖特征取所有查询变体的最大值。
        self.subqueries = tuple(subqueries)
        self.ext_hints = frozenset(ext_hints)
        # 词 → IDF。缺省为空 dict，覆盖特征退化为均匀权重。
        self.term_idf = dict(term_idf or {})

    def _weighted_coverage(self, terms: list[str], haystack: str) -> float:
        """IDF 加权词覆盖：命中「TLS」应比命中「项目」重要得多。"""
        if not terms:
            return 0.0
        weights = [max(self.term_idf.get(term, 1.0), 1e-6) for term in terms]
        hit = sum(w for term, w in zip(terms, weights) if term in haystack)
        return hit / sum(weights)

    @staticmethod
    def _proximity(terms: list[str], haystack: str) -> float:
        """命中词的邻近度：答案句里的查询词彼此靠近，泛泛提及则分散。"""
        positions = sorted(
            pos for pos in (haystack.find(term) for term in terms) if pos >= 0
        )
        if len(positions) < 2:
            return 0.0
        near = sum(
            1 for a, b in zip(positions, positions[1:])
            if b - a <= PROXIMITY_WINDOW
        )
        return near / (len(positions) - 1)

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
        # 比较类问题的词法特征只看实体子查询：全查询覆盖会奖励
        # "两个实体都顺带提到"的综述文档，压过真正回答问题的实体文档
        lexical_terms = variant_terms[1:] if len(variant_terms) > 1 else variant_terms
        numeric_intent = wants_numeric_answer(query)
        max_rrf = max((item.rrf_score for item in candidates), default=1.0)

        outputs: list[RerankOutput] = []
        for index, item in enumerate(candidates):
            haystack = f"{item.section_path}\n{item.text}".lower()
            coverage = max(
                self._weighted_coverage(terms, haystack)
                for terms in lexical_terms
            )
            proximity = max(
                self._proximity(terms, haystack) for terms in lexical_terms
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
                self._weighted_coverage(terms, name_hay)
                for terms in lexical_terms
            ) if name_hay else 0.0
            score = (
                0.40 * semantic_score
                + 0.32 * coverage
                + 0.18 * rrf_score
                + 0.05 * proximity
                + 0.04 * exact
                + 0.05 * numeric
                + 0.06 * type_match
                + 0.08 * filename_cov
            )
            outputs.append(RerankOutput(item.chunk_id, score))

        # 跨内容近重复软降权：同一段话的多个文件副本只有最高分那份
        # 保持原分，其余按比例降权（text_hash 只拦得住完全同文，编辑过
        # 的副本要靠向量相似度识别）。软惩罚，不淘汰任何候选（K3）。
        scores = {output.chunk_id: output.score for output in outputs}
        order = sorted(range(len(candidates)),
                       key=lambda i: -scores[candidates[i].chunk_id])
        kept: list[int] = []
        for i in order:
            is_dup = any(
                candidates[j].content_id != candidates[i].content_id
                and float(vectors[i] @ vectors[j]) >= NEARDUP_COSINE
                for j in kept
            )
            if is_dup:
                scores[candidates[i].chunk_id] *= NEARDUP_PENALTY
            else:
                kept.append(i)
        outputs = [RerankOutput(output.chunk_id, scores[output.chunk_id])
                   for output in outputs]
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


# 词 → 全库文档频次缓存。库内容变化（分片总数变了）时整体失效。
_DF_CACHE: dict[str, int] = {}
_DF_CACHE_TOTAL = -1


def _term_idf(conn, terms: set[str]) -> dict[str, float]:
    """查询词的全库 IDF，按本次查询内的最大值归一化到 (0, 1]。"""
    global _DF_CACHE_TOTAL
    if not terms:
        return {}
    total = conn.execute("SELECT count(*) c FROM chunks").fetchone()["c"] or 1
    if total != _DF_CACHE_TOTAL:
        _DF_CACHE.clear()
        _DF_CACHE_TOTAL = total
    idf: dict[str, float] = {}
    for term in terms:
        df = _DF_CACHE.get(term)
        if df is None:
            try:
                df = conn.execute(
                    "SELECT count(*) c FROM chunks_fts WHERE chunks_fts MATCH ?",
                    (quote_fts_query(term),),
                ).fetchone()["c"]
            except Exception:
                df = 0
            _DF_CACHE[term] = df
        idf[term] = math.log1p((total - df + 0.5) / (df + 0.5))
    peak = max(idf.values(), default=1.0) or 1.0
    return {term: value / peak for term, value in idf.items()}


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
    all_terms = set(extract_query_terms(query))
    for subquery in subqueries:
        all_terms.update(extract_query_terms(subquery))
    reranker: Reranker = LocalStaticReranker(
        subqueries=subqueries, ext_hints=ext_hints,
        term_idf=_term_idf(conn, all_terms),
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
