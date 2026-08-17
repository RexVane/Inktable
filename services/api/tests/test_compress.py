"""M4 extractive EvidenceSpan and ContextPack contracts."""

from __future__ import annotations

from app.retrieval.compress import EvidenceSource, assemble_pack, extract_spans


def _source(
    *, chunk_id: int = 1, content_id: int = 1, text: str,
    document_start: int = 100, score: float = 1.0,
) -> EvidenceSource:
    return EvidenceSource(
        chunk_id=chunk_id,
        content_id=content_id,
        section_id=10 + content_id,
        file_id=20 + content_id,
        file_name=f"doc-{content_id}.md",
        file_path=f"/tmp/doc-{content_id}.md",
        page=None,
        section_path="知识架构 › 证据压缩",
        ordinal=0,
        text=text,
        document_start_offset=document_start,
        document_end_offset=document_start + len(text),
        candidate_score=score,
    )


def test_extract_span_round_trips_chunk_and_document_offsets():
    text = "前一句与问题无关。证据压缩必须保留原文偏移。最后一句也无关。"
    source = _source(text=text, document_start=240)

    span = extract_spans("证据压缩 原文偏移", [source])[0]

    assert span.text == "证据压缩必须保留原文偏移。"
    assert text[span.start_offset:span.end_offset] == span.text
    assert span.document_start_offset == 240 + span.start_offset
    assert span.document_end_offset == 240 + span.end_offset
    document = "x" * 240 + text
    assert document[
        span.document_start_offset:span.document_end_offset
    ] == span.text


def test_extraction_never_rewrites_punctuation_or_whitespace():
    text = "表头 | 数值\n项目 A | 42\n项目 B | 17"
    spans = extract_spans("项目 A 数值", [_source(text=text)])

    assert spans
    for span in spans:
        assert span.text == text[span.start_offset:span.end_offset]


def test_context_pack_preserves_cross_document_coverage_before_score_fill():
    first = extract_spans(
        "共同问题",
        [_source(content_id=1, text="共同问题在文档一中的依据。", score=1.0)],
    )[0]
    second = extract_spans(
        "共同问题",
        [_source(content_id=2, chunk_id=2, text="共同问题在文档二中的依据。",
                 score=0.5)],
    )[0]

    pack = assemble_pack(
        [first, second], source_chars=len(first.text) + len(second.text),
        char_budget=200, max_spans=2,
    )

    assert {span.content_id for span in pack.spans} == {1, 2}


def test_identical_short_evidence_keeps_distinct_document_provenance():
    first = extract_spans("加分", [_source(content_id=1, text="35", score=1.0)])[0]
    second = extract_spans(
        "加分", [_source(content_id=2, chunk_id=2, text="35", score=0.9)],
    )[0]

    pack = assemble_pack(
        [first, second], source_chars=4, char_budget=20, max_spans=4,
    )

    assert [(span.content_id, span.text) for span in pack.spans] == [(1, "35"), (2, "35")]


def test_context_pack_respects_budget_and_reports_compression():
    source = _source(text="相关证据一句。" * 20)
    spans = extract_spans("相关证据", [source])

    pack = assemble_pack(
        spans, source_chars=len(source.text), char_budget=50, max_spans=20,
    )

    assert pack.packed_chars <= 50
    assert pack.source_chars == len(source.text)
    assert pack.compression_ratio >= 0.35
    assert pack.dropped_count > 0


def test_context_pack_keeps_one_span_per_direct_hit_before_neighbors():
    direct_a = _source(chunk_id=1, text="直接命中 A。次要句。", score=1.0)
    direct_b = _source(chunk_id=2, text="直接命中 B。关键答案 TLS 1.2。", score=0.4)
    neighbor = EvidenceSource(
        **{**direct_a.__dict__, "chunk_id": 3, "text": "高分邻居。" * 8,
           "ordinal_distance": 1},
    )
    spans = extract_spans("TLS 版本", [direct_a, direct_b, neighbor])

    pack = assemble_pack(
        spans, source_chars=sum(len(source.text) for source in (
            direct_a, direct_b, neighbor,
        )), char_budget=120, max_spans=5,
    )

    direct_ids = {
        span.chunk_id for span in pack.spans if span.ordinal_distance == 0
    }
    assert direct_ids == {1, 2}
    assert any("TLS 1.2" in span.text for span in pack.spans)


def test_context_pack_keeps_document_head_before_score_only_fill():
    high = extract_spans(
        "target", [_source(chunk_id=1, text="target high", score=1.0)],
    )[0]
    medium = extract_spans(
        "target", [_source(chunk_id=2, text="target medium", score=0.8)],
    )[0]
    head_source = _source(
        chunk_id=3,
        text="orientation one\norientation two\nprotected fact\norientation four",
        score=0.1,
    )
    head_source = EvidenceSource(
        **{**head_source.__dict__, "is_document_head": True},
    )
    head_spans = extract_spans("target", [head_source])

    pack = assemble_pack(
        [high, medium, *head_spans], source_chars=100,
        char_budget=100, max_spans=2,
    )

    assert [span.chunk_id for span in pack.spans] == [1, 3]
    assert pack.spans[1].is_fallback is True
    assert "protected fact" in pack.spans[1].text


def test_context_pack_prefers_whole_short_priority_child():
    source = _source(
        text="query fact\ncontext two\nprotected tail fact\ncontext four",
    )
    spans = extract_spans("query fact", [source])

    pack = assemble_pack(
        spans, source_chars=len(source.text), char_budget=200, max_spans=2,
    )

    assert any(span.is_fallback for span in pack.spans)
    assert any("protected tail fact" in span.text for span in pack.spans)


def test_page_sized_child_gets_exact_fallback_beyond_sentence_window_limit():
    source = _source(text="prefix\n" + "x" * 950 + "\nanswer at tail")

    spans = extract_spans("unrelated paraphrase", [source])

    fallback = next(span for span in spans if span.is_fallback)
    assert len(fallback.text) > 900
    assert fallback.text == source.text


def test_adjacent_sentence_window_preserves_reason_and_solution():
    text = (
        "现象：连接超时。"
        "原因：最低 TLS 版本设置为 TLS 1.3，客户端不兼容。"
        "解决方案：最低版本放宽为 TLS 1.2。"
    )
    spans = extract_spans("最低 TLS 版本是什么", [_source(text=text)])

    window = next(
        span for span in spans
        if "原因：" in span.text and "解决方案：" in span.text
    )
    assert window.text == text[window.start_offset:window.end_offset]
    assert "TLS 1.2" in window.text
