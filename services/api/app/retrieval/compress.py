"""Extractive evidence compression with exact source offsets."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.index.search import extract_query_terms

# 单段上限放宽到 ~900：保留成段的叙述而不是切成零碎句子，
# 让"把某事讲全"类问题拿到连贯上下文（现代模型上下文管够）。
MAX_SPAN_CHARS = 900
DEFAULT_CONTEXT_CHARS = 3600
DEFAULT_MAX_SPANS = 120
# 每个来源文档最多贡献几段证据。早期为小预算定的 3 会让单个文档"讲不透"，
# 放宽到 6 —— 配合更大的装配预算，回答更全面。
MAX_SPANS_PER_SOURCE = 6


@dataclass(frozen=True)
class EvidenceSource:
    chunk_id: int
    content_id: int
    section_id: int | None
    file_id: int
    file_name: str
    file_path: str
    page: int | None
    section_path: str
    ordinal: int
    text: str
    document_start_offset: int | None
    document_end_offset: int | None
    candidate_score: float
    ordinal_distance: int = 0


@dataclass(frozen=True)
class EvidenceSpan:
    span_id: str
    chunk_id: int
    content_id: int
    section_id: int | None
    file_id: int
    file_name: str
    file_path: str
    page: int | None
    heading_path: str
    start_offset: int
    end_offset: int
    document_start_offset: int | None
    document_end_offset: int | None
    text: str
    relevance_score: float
    ordinal_distance: int = 0


@dataclass(frozen=True)
class ContextPack:
    spans: tuple[EvidenceSpan, ...]
    source_chars: int
    packed_chars: int
    dropped_count: int

    @property
    def compression_ratio(self) -> float:
        if self.source_chars <= 0:
            return 0.0
        return 1.0 - self.packed_chars / self.source_chars


def _segments(text: str) -> list[tuple[int, int]]:
    """Split on sentence/row boundaries while retaining exact delimiters."""
    if not text:
        return []
    boundaries = [0]
    for match in re.finditer(r"(?:[。！？!?；;]+|\n+)", text):
        boundaries.append(match.end())
    if boundaries[-1] != len(text):
        boundaries.append(len(text))

    spans: list[tuple[int, int]] = []
    for left, right in zip(boundaries, boundaries[1:]):
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left >= right:
            continue
        cursor = left
        while right - cursor > MAX_SPAN_CHARS:
            window = text[cursor:cursor + MAX_SPAN_CHARS]
            cut = max(
                window.rfind("，", MAX_SPAN_CHARS // 2),
                window.rfind(", ", MAX_SPAN_CHARS // 2),
                window.rfind(" ", MAX_SPAN_CHARS // 2),
            )
            size = cut + 1 if cut >= MAX_SPAN_CHARS // 2 else MAX_SPAN_CHARS
            spans.append((cursor, cursor + size))
            cursor += size
        if cursor < right:
            spans.append((cursor, right))
    return spans


def extract_spans(query: str, sources: list[EvidenceSource]) -> list[EvidenceSpan]:
    """Score exact sentence/row spans; no generated text may enter evidence."""
    terms = extract_query_terms(query)
    max_candidate = max((source.candidate_score for source in sources), default=1.0)
    spans: list[EvidenceSpan] = []
    for source in sources:
        source_spans: list[EvidenceSpan] = []
        candidate_score = source.candidate_score / max(max_candidate, 1e-12)
        segments = _segments(source.text)
        windows: list[tuple[int, int]] = list(segments)
        for index in range(len(segments)):
            for width in (2, 3):
                if index + width > len(segments):
                    continue
                start = segments[index][0]
                end = segments[index + width - 1][1]
                if end - start <= MAX_SPAN_CHARS:
                    windows.append((start, end))
        for start, end in dict.fromkeys(windows):
            text = source.text[start:end]
            haystack = text.lower()
            coverage = (
                sum(1 for term in terms if term in haystack) / len(terms)
                if terms else 0.0
            )
            exact = 1.0 if query.strip().lower() in haystack else 0.0
            proximity = 1.0 / (1.0 + source.ordinal_distance)
            coherence = 1.0 if (
                ("原因：" in text or "原因:" in text)
                and ("解决方案：" in text or "解决方案:" in text)
            ) else 0.0
            answer_cue = 1.0 if (
                any(word in query for word in ("什么", "多少", "哪个", "哪种"))
                and ("=" in text or "为 " in text or "是 " in text)
            ) else 0.0
            relevance = (
                0.55 * coverage + 0.30 * candidate_score
                + 0.10 * proximity + 0.05 * exact
                + 0.12 * coherence + 0.08 * answer_cue
            )
            document_start = (
                source.document_start_offset + start
                if source.document_start_offset is not None else None
            )
            document_end = (
                source.document_start_offset + end
                if source.document_start_offset is not None else None
            )
            source_spans.append(EvidenceSpan(
                span_id=f"ch{source.chunk_id}:{start}-{end}",
                chunk_id=source.chunk_id,
                content_id=source.content_id,
                section_id=source.section_id,
                file_id=source.file_id,
                file_name=source.file_name,
                file_path=source.file_path,
                page=source.page,
                heading_path=source.section_path,
                start_offset=start,
                end_offset=end,
                document_start_offset=document_start,
                document_end_offset=document_end,
                text=text,
                relevance_score=relevance,
                ordinal_distance=source.ordinal_distance,
            ))
        source_spans.sort(key=lambda span: span.relevance_score, reverse=True)
        chosen: list[EvidenceSpan] = []
        for span in source_spans:
            if any(
                span.start_offset >= kept.start_offset
                and span.end_offset <= kept.end_offset
                for kept in chosen
            ):
                continue
            chosen.append(span)
            if len(chosen) >= MAX_SPANS_PER_SOURCE:
                break
        spans.extend(chosen)
    return sorted(spans, key=lambda span: span.relevance_score, reverse=True)


def assemble_pack(
    spans: list[EvidenceSpan], *, source_chars: int,
    char_budget: int = DEFAULT_CONTEXT_CHARS,
    max_spans: int = DEFAULT_MAX_SPANS,
) -> ContextPack:
    """Pack diverse exact spans within a deterministic character budget."""
    selected: list[EvidenceSpan] = []
    selected_ids: set[str] = set()
    normalized_texts: set[str] = set()
    used = 0

    def add(span: EvidenceSpan) -> bool:
        nonlocal used
        normalized = re.sub(r"\s+", "", span.text).lower()
        if not normalized or normalized in normalized_texts:
            return False
        if used + len(span.text) > char_budget:
            return False
        if len(selected) >= max_spans:
            return False
        selected.append(span)
        selected_ids.add(span.span_id)
        normalized_texts.add(normalized)
        used += len(span.text)
        return True

    # First pass preserves cross-document coverage before filling by score.
    seen_contents: set[int] = set()
    for span in spans:
        if span.content_id not in seen_contents and add(span):
            seen_contents.add(span.content_id)

    # Directly retrieved Children outrank their expansion-only neighbors. Keep
    # one exact span per hit before spending the remaining budget on context.
    seen_hit_chunks: set[int] = set()
    for span in spans:
        if (span.ordinal_distance == 0 and span.chunk_id not in seen_hit_chunks
                and add(span)):
            seen_hit_chunks.add(span.chunk_id)
    for span in spans:
        if span.span_id not in selected_ids:
            add(span)

    return ContextPack(
        spans=tuple(selected),
        source_chars=source_chars,
        packed_chars=used,
        dropped_count=max(0, len(spans) - len(selected)),
    )


def best_span(
    query: str, source: EvidenceSource,
) -> EvidenceSpan:
    """Return one exact display span, falling back to the whole source text."""
    spans = extract_spans(query, [source])
    if spans:
        return spans[0]
    return EvidenceSpan(
        span_id=f"ch{source.chunk_id}:0-{len(source.text)}",
        chunk_id=source.chunk_id,
        content_id=source.content_id,
        section_id=source.section_id,
        file_id=source.file_id,
        file_name=source.file_name,
        file_path=source.file_path,
        page=source.page,
        heading_path=source.section_path,
        start_offset=0,
        end_offset=len(source.text),
        document_start_offset=source.document_start_offset,
        document_end_offset=source.document_end_offset,
        text=source.text,
        relevance_score=source.candidate_score,
        ordinal_distance=source.ordinal_distance,
    )
