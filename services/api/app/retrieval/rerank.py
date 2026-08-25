"""Reranker protocol, local pair scorer, and explicit RRF fallback."""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.db.visibility import visible_files_condition

from app.index.search import extract_query_terms, quote_fts_query
from app.retrieval.query import wants_numeric_answer

def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


RERANK_LIMIT = _env_int("INKTABLE_RERANK_LIMIT", 80, 8, 160)
SOFT_PER_CONTENT = 12

# 级联重排的 Cross-Encoder 候选对预算。每对 50-80ms（bge-reranker-base int8、
# 384 token、14 线程实测）。取 26 的依据是 scripts/probe_cascade_depth.py：
# 融合顺序里最深的 gold 落在第 25 位（P19/P20 两道改写类问题），K 小于 25
# 就有题永远进不了二级重排。53 题里 49 题的 gold 在前 4 位，只有 4 题需要
# 这个深度 —— 但正是那 4 题在拉低 MRR/nDCG。
CASCADE_PAIRS = _env_int("INKTABLE_CASCADE_PAIRS", 26, 4, 80)
# 变体多到把 per-variant 预算压得过浅时的下限：低于这个深度，CE 连
# 融合顺序的头部都盖不住，级联就没有意义了，宁可超一点延迟。
CASCADE_MIN_HEAD = _env_int("INKTABLE_CASCADE_MIN_HEAD", 8, 4, 40)
# 尾部逐位衰减系数：只用于在 CE 分数之下维持本地打分器给出的相对顺序
CASCADE_TAIL_DECAY = 0.98

# 级联头部的分数融合权重（百分之一为单位，便于用环境变量整数扫参）。
#
# CE 不能整份取代本地打分器。实测 K=26 纯 CE（0.92·CE + 0.08·RRF）把深尾
# 全部救回来了（P20 17→8、A09 20→5、最差名次从 24 收到 9），代价是另有
# 6 道题从第 1 名掉到第 2 名 —— 其中 M08「找出…成绩单 PDF」是文件名类
# 问题，本地打分器的 filename_cov / type_match 特征本来答对，被 0.92 的
# CE 权重压掉了。两者各有不可替代的信号，所以融合而不是替换。
CASCADE_W_CROSS = _env_int("INKTABLE_CASCADE_W_CROSS", 68, 0, 100) / 100.0
CASCADE_W_LOCAL = _env_int("INKTABLE_CASCADE_W_LOCAL", 24, 0, 100) / 100.0
CASCADE_W_RRF = _env_int("INKTABLE_CASCADE_W_RRF", 8, 0, 100) / 100.0

# 本地分权重按**本次查询是否真的有词法证据**缩放。
#
# 动机：固定权重两头都讨不到好。37% 本地权重能把 M08 这类文件名问题拉回
# 第 1，但同时把 P20（"怎样防止用户用相对路径跑出自己的文件目录" → 原文写
# "路径穿越"）从第 8 名压回第 14 名 —— 改写类问题里本地打分器的
# coverage / proximity / exact 特征全为 0，它给出的分数是噪声，不是信号。
# 判据取候选池里的最大 IDF 加权词覆盖，低则把权重让给 CE，让出的部分转给
# CE 以保持总权重恒定。
#
# **诚实记录**：在 53 题的 gold 集上，这个自适应缩放**没有测出可观测收益**
# —— 自适应 CE=70/LOCAL=22 得 MRR 90.2 / nDCG 92.0，而固定 CE=68/LOCAL=24
# 得 90.3 / 92.2，差异在噪声内。默认权重取实测最好的那组（68/24），此时
# 本地权重本就不高，缩放能起作用的余地很小，基本处于休眠。机制保留是因为
# 它针对的失效模式（改写类问题上本地特征全零）有明确机理，而 53 题里只有
# 2-3 题落在这个模式上，样本量不足以判定；评测集扩大后应重新验证。
CASCADE_LEX_FULL = _env_int("INKTABLE_CASCADE_LEX_FULL", 45, 5, 100) / 100.0

# 送进 CE 的单个候选文本上限（字符）。分片正文实测 p50 383 字、p90 956 字，
# 而 CE 截断在 384 token —— 长分片是从**开头**截的，答案落在后半段就直接
# 看不到。改为按查询词密度取窗口：同样的 token 预算下信息量更高，长文的
# 批次成本也随最长序列一起降下来。
CASCADE_FOCUS_CHARS = _env_int("INKTABLE_CASCADE_FOCUS_CHARS", 420, 120, 4000)


def _focus_window(text: str, terms: list[str], budget: int) -> str:
    """截取查询词最密集的一段，而不是从开头硬切。

    CE 的截断是从头开始的，对 p90 的 956 字分片等于丢掉后六成正文。
    这里在字符级滑窗里选覆盖不同查询词最多的位置，平局取更靠前的窗口
    （文档靠前的内容通常更概括）。没有任何查询词命中时退回开头，
    与原行为一致。
    """
    if len(text) <= budget or not terms:
        return text[:budget]
    lowered = text.lower()
    hits: list[tuple[int, str]] = []
    for term in terms:
        start = lowered.find(term)
        while start >= 0:
            hits.append((start, term))
            start = lowered.find(term, start + 1)
            if len(hits) > 512:  # 极端重复文本下的保护
                break
    if not hits:
        return text[:budget]
    hits.sort()
    best_start, best_cover = 0, -1
    for index, (position, _term) in enumerate(hits):
        # 以每个命中为窗口左端，统计窗口内覆盖的不同查询词数
        window_end = position + budget
        covered = {
            term for other, term in hits[index:]
            if other < window_end
        }
        if len(covered) > best_cover:
            best_cover = len(covered)
            best_start = position
    # 让命中稍微居中，前面留一点上下文，避免正好从关键词切断句子
    start = max(0, min(best_start - budget // 4, len(text) - budget))
    return text[start:start + budget]


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
                 term_idf: dict[str, float] | None = None,
                 chunk_vectors: dict[int, np.ndarray] | None = None):
        # 比较类问题的子查询：分片只要答对其中一个实体就算相关，
        # 语义/覆盖特征取所有查询变体的最大值。
        self.subqueries = tuple(subqueries)
        self.ext_hints = frozenset(ext_hints)
        # 词 → IDF。缺省为空 dict，覆盖特征退化为均匀权重。
        self.term_idf = dict(term_idf or {})
        # chunk_id → 已入库向量。嵌入阶段算过的不再现场编码 ——
        # bge-m3 现场编码 80 条候选要 6-14 秒，是检索耗时的大头。
        self.chunk_vectors = chunk_vectors or {}

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

        # 本地打分器不依赖现场编码也能工作：Ollama 不可用时语义特征退化为
        # 中性值，词法/元数据特征（IDF 覆盖、邻近、精确、数值、类型、文件名）
        # 全部照常计算。检索不该因为精排模型掉线就整体降级回 RRF ——
        # 这批特征本身就是确定性本地信号（"local static" 的由来）。
        semantic: np.ndarray | None = None
        vectors: np.ndarray | None = None
        try:
            model = emb.get_embedder()
            variants = [query, *self.subqueries]
            # 变体合并成一次批量编码（逐条 encode_one 是 N 次 HTTP 往返）
            query_vectors = model.encode(variants)
            # 候选向量优先取索引时已入库的，只对缺失的（刚入库还没回填）现场编码
            rows: list[np.ndarray | None] = []
            missing: list[int] = []
            for index, item in enumerate(candidates):
                v = self.chunk_vectors.get(item.chunk_id)
                rows.append(v)
                if v is None:
                    missing.append(index)
            if missing:
                encoded = model.encode([
                    emb.embed_text_for(candidates[i].text, candidates[i].section_path)
                    for i in missing
                ])
                for pos, i in enumerate(missing):
                    rows[i] = encoded[pos]
            vectors = np.stack(rows)
            # 每个候选对所有查询变体的最大余弦
            semantic = (vectors @ query_vectors.T).max(axis=1)
        except emb.EmbeddingUnavailable:
            semantic = None
            vectors = None
        variant_terms = [extract_query_terms(v) for v in [query, *self.subqueries]]
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
                v.strip().lower() in haystack for v in [query, *self.subqueries]
            ) else 0.0
            semantic_score = (
                (float(semantic[index]) + 1.0) / 2.0 if semantic is not None else 0.0
            )
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
        # 向量不可用（Ollama 掉线）时跳过本步 —— 无向量就没有近重复判据。
        scores = {output.chunk_id: output.score for output in outputs}
        if vectors is not None:
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


class CrossEncoderReranker:
    """True pairwise scorer backed by the pinned local ONNX model."""

    def __init__(self, subqueries: tuple[str, ...] = ()):
        from app.retrieval.cross_encoder import get_runtime

        self.runtime = get_runtime()
        self.model_id = self.runtime.model_id
        self.subqueries = tuple(subqueries)

    def rerank(
        self, query: str, candidates: list[RerankInput],
    ) -> list[RerankOutput]:
        from app.retrieval.cross_encoder import sigmoid

        documents = [
            "\n".join(part for part in (
                item.file_name, item.section_path, item.text,
            ) if part)
            for item in candidates
        ]
        variants = (query, *self.subqueries)
        raw_by_variant = [self.runtime.score(variant, documents) for variant in variants]
        raw = np.stack(raw_by_variant).max(axis=0)
        max_rrf = max((item.rrf_score for item in candidates), default=1.0)
        outputs = []
        for item, logit in zip(candidates, raw):
            pair_score = sigmoid(float(logit))
            rrf_score = item.rrf_score / max(max_rrf, 1e-12)
            outputs.append(RerankOutput(
                item.chunk_id,
                0.92 * pair_score + 0.08 * rrf_score,
            ))
        return sorted(outputs, key=lambda item: item.score, reverse=True)


class CascadeReranker:
    """两级重排：融合顺序截断头部交 Cross-Encoder，尾部留给本地打分器。

    Cross-Encoder 每对候选 50-80ms（bge-reranker-base int8，384 token，
    28 核实测），1.5 秒的 rerank 门槛只够 20 对左右，扫不完 `_load_inputs`
    给出的 80 个候选。所以它只精排融合顺序的前 K 位 —— 截断多深由
    `scripts/probe_cascade_depth.py` 实测确定：K 必须覆盖全部 gold，
    否则二级重排再准也救不回落在 K 之外的题。

    预算按**候选对数**而不是固定 K 来限。比较类问题会带 1-2 条子查询，
    CrossEncoder 对每个变体各打一遍分，K 不随变体数收缩的话最坏情况
    直接翻三倍，P95 就是被这种少数查询顶穿的。

    尾部候选不参与 CE，统一压到 CE 最低分之下：既然 K 已按实测覆盖 gold，
    让尾部有机会盖过 CE 排序的候选只会引入噪声。
    """

    def __init__(self, local: LocalStaticReranker,
                 subqueries: tuple[str, ...] = (),
                 pairs_budget: int = 0):
        from app.retrieval.cross_encoder import get_runtime

        self.runtime = get_runtime()
        self.local = local
        self.subqueries = tuple(subqueries)
        self.pairs_budget = pairs_budget or CASCADE_PAIRS
        self.model_id = f"cascade:{local.model_id}+{self.runtime.model_id}"

    def _head_size(self, variant_count: int, total: int) -> int:
        # 变体越多，单个候选越贵，头部就要越浅，保住最坏情况的延迟
        per_variant = max(1, self.pairs_budget // max(1, variant_count))
        return max(1, min(CASCADE_MIN_HEAD, total) if per_variant < CASCADE_MIN_HEAD
                   else min(per_variant, total))

    def rerank(
        self, query: str, candidates: list[RerankInput],
    ) -> list[RerankOutput]:
        from app.retrieval.cross_encoder import sigmoid

        if not candidates:
            return []
        # 一级：本地打分器排全部候选，给尾部一个有意义的顺序
        local_ranked = self.local.rerank(query, candidates)
        local_scores = {item.chunk_id: item.score for item in local_ranked}

        variants = (query, *self.subqueries)
        head_size = self._head_size(len(variants), len(candidates))
        # 头部按**融合顺序**截取，不按本地分：本地打分器在改写类问题上
        # 会把 gold 压下去（覆盖/邻近特征全为 0），用它截断等于把 CE
        # 最该救的那批题提前淘汰掉。
        head = candidates[:head_size]
        tail = candidates[head_size:]

        # 文件名与标题路径始终完整保留 —— 它们短，且是元数据类问题的
        # 主要信号；只有正文按查询词密度取窗口。
        focus_terms: list[str] = []
        for variant in variants:
            for term in extract_query_terms(variant):
                if term not in focus_terms:
                    focus_terms.append(term)
        documents = [
            "\n".join(part for part in (
                item.file_name,
                item.section_path,
                _focus_window(item.text, focus_terms, CASCADE_FOCUS_CHARS),
            ) if part)
            for item in head
        ]
        raw = np.stack([
            self.runtime.score(variant, documents) for variant in variants
        ]).max(axis=0)

        max_rrf = max((item.rrf_score for item in candidates), default=1.0)
        max_local = max(
            (local_scores.get(item.chunk_id, 0.0) for item in head), default=1.0,
        )
        haystacks = [
            f"{item.section_path}\n{item.text}".lower() for item in head
        ]
        # 本次查询的词法置信度：候选池里最强的 IDF 加权词覆盖。
        # 纯字符串操作，相对 CE 的每对数十毫秒可忽略。
        lexical_confidence = max(
            (self.local._weighted_coverage(focus_terms, haystack)
             for haystack in haystacks),
            default=0.0,
        )
        trust_local = min(1.0, lexical_confidence / max(CASCADE_LEX_FULL, 1e-6))
        w_local = CASCADE_W_LOCAL * trust_local
        # 让出的本地权重转给 CE，总权重恒定
        w_cross = CASCADE_W_CROSS + (CASCADE_W_LOCAL - w_local)

        outputs: list[RerankOutput] = []
        for item, logit in zip(head, raw):
            pair_score = sigmoid(float(logit))
            rrf_score = item.rrf_score / max(max_rrf, 1e-12)
            local_score = local_scores.get(item.chunk_id, 0.0) / max(max_local, 1e-12)
            outputs.append(RerankOutput(
                item.chunk_id,
                w_cross * pair_score
                + w_local * local_score
                + CASCADE_W_RRF * rrf_score,
            ))
        outputs.sort(key=lambda item: item.score, reverse=True)

        # 尾部整体压到 CE 最低分之下，保持本地分给出的相对顺序
        if tail:
            floor = outputs[-1].score if outputs else 1.0
            tail_ranked = sorted(
                tail, key=lambda item: -local_scores.get(item.chunk_id, 0.0),
            )
            span = max(floor, 1e-6)
            for offset, item in enumerate(tail_ranked, start=1):
                outputs.append(RerankOutput(
                    item.chunk_id, span * CASCADE_TAIL_DECAY ** offset,
                ))
        return outputs


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
                       (SELECT vf.ext FROM files vf
                        LEFT JOIN sources vs ON vs.id = vf.source_id
                        WHERE vf.content_id = ch.content_id
                          AND {visible_files_condition('vf', 'vs')}
                        ORDER BY vf.id LIMIT 1) AS ext,
                       (SELECT vf.name FROM files vf
                        LEFT JOIN sources vs ON vs.id = vf.source_id
                        WHERE vf.content_id = ch.content_id
                          AND {visible_files_condition('vf', 'vs')}
                        ORDER BY vf.id LIMIT 1) AS file_name
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


def _load_vectors(conn, chunk_ids: list[int]) -> dict[int, np.ndarray]:
    """批量取候选分片已入库的向量（chunks_vec），供重排复用。

    走 vector.vectors_for：它优先从整库矩阵缓存里切片。逐行查 vec0 约 5ms/行，
    80 个候选就是 400ms —— 那是重排耗时的大头，而不是打分本身。
    缓存里的向量已按行归一化，NEARDUP_COSINE 的点积因此就是真余弦。
    """
    if not chunk_ids:
        return {}
    try:
        from app.index import vector as vec

        return vec.vectors_for(conn, chunk_ids)
    except Exception:  # noqa: BLE001 - 向量表缺失时重排自行编码或降级
        return {}


def run_rerank(conn, query: str, candidates, *,
               subqueries: tuple[str, ...] = (),
               ext_hints: frozenset[str] = frozenset(),
               mode: str | None = None) -> RerankResult:
    started = time.perf_counter()
    # 环境变量优先于调用方偏好：评测与诊断要能全局钉死一种重排实现，
    # 而调用方偏好只是「没人指定时用哪个」。
    mode = (
        os.environ.get("INKTABLE_RERANKER", "").strip().lower()
        or (mode or "").strip().lower()
        or "auto"
    )
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

    def local_reranker() -> LocalStaticReranker:
        all_terms = set(extract_query_terms(query))
        for subquery in subqueries:
            all_terms.update(extract_query_terms(subquery))
        return LocalStaticReranker(
            subqueries=subqueries,
            ext_hints=ext_hints,
            term_idf=_term_idf(conn, all_terms),
            chunk_vectors=_load_vectors(
                conn, [item.chunk_id for item in selected],
            ),
        )

    degraded = False
    ranked: list[RerankOutput] | None = None
    model_id = ""
    cross_modes = {"cross", "cross-encoder", "onnx"}
    cascade_modes = {"cascade", "two-stage"}
    if mode in cascade_modes:
        try:
            reranker: Reranker = CascadeReranker(
                local_reranker(), subqueries=subqueries,
            )
            ranked = reranker.rerank(query, selected)
            model_id = reranker.model_id
        except Exception:
            # CE 不可用/模型未安装时退回一级本地打分器，而不是退到 RRF：
            # 级联的一级本身就是当前生产默认，降级后质量不该跌破它
            degraded = True
    if mode in cross_modes:
        try:
            reranker = CrossEncoderReranker(subqueries=subqueries)
            ranked = reranker.rerank(query, selected)
            model_id = reranker.model_id
        except Exception:
            degraded = True
    if ranked is None and mode not in {"off", "rrf", "disabled"}:
        try:
            reranker = local_reranker()
            ranked = reranker.rerank(query, selected)
            model_id = reranker.model_id
            degraded = degraded or mode in cross_modes or mode in cascade_modes
        except Exception:
            ranked = None
    if ranked is None:
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

    ranked = _demote_redundant_coverage(query, selected, ranked)

    by_id = {candidate.chunk_id: candidate for candidate in candidates}
    # Candidates excluded only by the rerank input caps still participate as a
    # tail. Keep that tail deterministic and RRF-ordered rather than preserving
    # incidental SQL/load order.
    ranked.extend(
        RerankOutput(chunk_id, by_id[chunk_id].rrf_score)
        for chunk_id in sorted(
            (cid for cid in remainder if cid in by_id),
            key=lambda cid: (-by_id[cid].rrf_score, cid),
        )
    )
    return RerankResult(
        ranked=ranked,
        model_id=model_id,
        degraded=degraded,
        duration_ms=(time.perf_counter() - started) * 1000,
        reranked_count=len(selected),
    )
