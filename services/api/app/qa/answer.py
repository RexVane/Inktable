"""问答管线 —— PLAN §12.3b / §12.4 / B6。

    问题 → 多路检索 → 多样性约束 → 邻居扩展(B2) → [Cn] 锚点装配
         → LLM → 四条后置校验 → 引用映射

**prompt 是建议，校验才是执行**（§12.4）：引用正确率的保证不在提示词里，
在生成之后的四条硬校验里 —— 每条的失败路径都有明确归宿，
最坏降级为"只给检索结果不给答案"，绝不把无依据的话包装成答案。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field

from app.retrieval.pipeline import (
    assemble_context,
    compress_evidence,
    expand_neighbors,
    load_context_candidates,
    run as run_retrieval,
)
from app.qa import llm

log = logging.getLogger("inktable.qa")

TOP_CONTEXT = 6          # 送入模型的资料片数（§12.4：放太多会 lost in the middle）
MAX_PER_CONTENT = 3      # 多样性：同一内容最多几片（§12.3b ⑤，v6 修正为按 content）
NEIGHBOR_SPAN = 1        # B2：命中片向前后各扩展几片（parent = 邻居合并的动态形态）
REFUSAL = "未在文件库中找到足够依据"

_CITE = re.compile(r"\[C(\d{1,2})\]")


@dataclass
class ContextPiece:
    tag: str                 # C1..Cn
    chunk_id: int
    content_id: int
    file_id: int
    file_name: str
    file_path: str
    page: int | None
    section_path: str
    text: str                # 扩展后的文本（命中片 + 邻居）
    snippet: str             # 命中片原文（引用展示用）
    span_id: str = ""
    start_offset: int = 0
    end_offset: int = 0
    document_start_offset: int | None = None
    document_end_offset: int | None = None


@dataclass
class Answer:
    status: str              # answered / refused / fallback / not_configured
    answer: str | None
    citations: list[dict] = field(default_factory=list)
    retrieved: list[dict] = field(default_factory=list)   # fallback 时给的检索结果
    hedge: str = ""
    validation: dict = field(default_factory=dict)         # 校验过程留痕，可审计
    trace: dict = field(default_factory=dict)              # 非持久化检索 trace


# ---------------------------------------------------------------- 检索与装配

def retrieve_context(conn, question: str, book_id: int | None = None,
                     *, return_trace: bool = False):
    retrieval = run_retrieval(
        conn, question, route_limit=60, candidate_limit=60, book_id=book_id,
    )
    if not retrieval.candidates:
        return ([], retrieval.trace.to_dict()) if return_trace else []
    candidates = load_context_candidates(
        conn, retrieval, limit=TOP_CONTEXT,
        max_per_content=MAX_PER_CONTENT, book_id=book_id,
    )
    candidates = expand_neighbors(
        conn, candidates, neighbor_span=NEIGHBOR_SPAN, trace=retrieval.trace,
    )
    source_chars = sum(len(source.text) for source in {
        (source.file_id, source.chunk_id): source
        for candidate in candidates for source in candidate.expanded_sources
    }.values())
    spans = compress_evidence(question, candidates, trace=retrieval.trace)
    pack = assemble_context(
        spans, trace=retrieval.trace, source_chars=source_chars,
    )
    pieces = [
        ContextPiece(
            tag=f"C{i + 1}", chunk_id=span.chunk_id,
            content_id=span.content_id, file_id=span.file_id,
            file_name=span.file_name, file_path=span.file_path,
            page=span.page, section_path=span.heading_path,
            text=span.text, snippet=span.text,
            span_id=span.span_id, start_offset=span.start_offset,
            end_offset=span.end_offset,
            document_start_offset=span.document_start_offset,
            document_end_offset=span.document_end_offset,
        )
        for i, span in enumerate(pack.spans)
    ]
    return (pieces, retrieval.trace.to_dict()) if return_trace else pieces


# ---------------------------------------------------------------- Prompt

def build_messages(question: str, pieces: list[ContextPiece]) -> list[dict]:
    """资料在前、问题在后，问题后补一行约束复述（§12.4 ⑤ recency 槽位）。"""
    ctx_lines = []
    for p in pieces:
        loc = f"第{p.page}页" if p.page else (p.section_path or "")
        ctx_lines.append(f"[{p.tag}] 《{p.file_name}》{('·' + loc) if loc else ''}\n{p.text}")
    ctx = "\n\n".join(ctx_lines)

    system = (
        "你是本地文件库的问答助手。只能依据【资料】回答，"
        "每个事实性陈述后必须紧跟引用标记（如 [C1]，可多个）。"
        f"资料不足以回答时，只输出「{REFUSAL}」这一句话，"
        "不得用你自己的知识补充，不得推测。"
    )
    user = (
        f"【资料】\n{ctx}\n\n【问题】\n{question}\n\n"
        f"（提醒：只依据上方资料，逐句带 [Cn] 引用；资料不足则只答「{REFUSAL}」）"
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


# ---------------------------------------------------------------- 后置校验

def validate(text: str, pieces: list[ContextPiece]) -> tuple[str, dict]:
    """四条硬校验（§12.4）。返回 (清洗后的文本, 校验记录)。

    校验记录进 Answer.validation —— 出问题时能看到到底哪条被触发，
    而不是对着一个"看起来正常"的答案猜。
    """
    record: dict = {"fabricated_removed": 0, "truncated_refusal": False}
    valid_tags = {p.tag for p in pieces}

    # ① 虚构引用剔除：编号不在本次上下文里的 [Cn] 直接删
    def _strip(m: re.Match) -> str:
        if f"C{m.group(1)}" in valid_tags:
            return m.group(0)
        record["fabricated_removed"] += 1
        return ""

    cleaned = _CITE.sub(_strip, text).strip()

    # ④ 拒答句里混入具体事实 → 截断到拒答句（拒答就是拒答，不许夹带）
    if REFUSAL in cleaned and cleaned != REFUSAL:
        record["truncated_refusal"] = True
        cleaned = REFUSAL

    return cleaned, record


def _cited_tags(text: str) -> set[str]:
    return {f"C{m.group(1)}" for m in _CITE.finditer(text)}


# ---------------------------------------------------------------- 主入口

def ask(conn: sqlite3.Connection, question: str, book_id: int | None = None) -> Answer:
    if not llm.is_configured():
        return Answer(status="not_configured", answer=None,
                      hedge="尚未配置模型服务，可先使用搜索")

    pieces, trace = retrieve_context(conn, question, book_id, return_trace=True)
    if not pieces:
        return Answer(status="refused", answer=REFUSAL, trace=trace)

    from app.index.confidence import assess
    conf = assess(conn, question, 0.0)

    messages = build_messages(question, pieces)
    validation: dict = {"attempts": 0}

    for attempt in (1, 2):
        validation["attempts"] = attempt
        raw = llm.chat(messages)
        cleaned, rec = validate(raw, pieces)
        validation.update(rec)

        if cleaned == REFUSAL:
            return Answer(status="refused", answer=REFUSAL,
                          hedge=conf.hedge, validation=validation, trace=trace)

        cited = _cited_tags(cleaned)
        if cited:
            by_tag = {p.tag: p for p in pieces}
            citations = [_citation_dict(by_tag[t]) for t in sorted(
                cited, key=lambda x: int(x[1:])) if t in by_tag]
            return Answer(status="answered", answer=cleaned,
                          citations=citations, hedge=conf.hedge,
                          validation=validation, trace=trace)

        # ② 非拒答却零引用 = 无依据的断言 → 重新生成一次（§12.4）
        if attempt == 1:
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content":
                             "你上一条回答没有任何 [Cn] 引用。请重新回答：每个事实"
                             f"陈述后紧跟引用标记；资料不足则只答「{REFUSAL}」。"})
            log.info("零引用，触发重新生成")

    # ③ 二次仍零引用 → 降级：只给检索结果，不给自然语言答案（§12.4）
    validation["fallback"] = True
    return Answer(
        status="fallback", answer=None,
        retrieved=[_citation_dict(p) for p in pieces],
        hedge="模型未能给出带依据的回答，以下是检索到的原文片段",
        validation=validation,
        trace=trace,
    )


def _citation_dict(p: ContextPiece) -> dict:
    return {
        "tag": p.tag, "chunk_id": p.chunk_id, "file_id": p.file_id,
        "file_name": p.file_name, "file_path": p.file_path,
        "page": p.page, "section_path": p.section_path,
        "snippet": p.snippet,
        "span_id": p.span_id,
        "start_offset": p.start_offset,
        "end_offset": p.end_offset,
        "document_start_offset": p.document_start_offset,
        "document_end_offset": p.document_end_offset,
    }
