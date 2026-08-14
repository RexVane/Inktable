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

from app.qa import llm
from app.retrieval.pipeline import (
    assemble_context,
    compress_evidence,
    expand_neighbors,
    load_context_candidates,
)
from app.retrieval.pipeline import (
    run as run_retrieval,
)

log = logging.getLogger("inktable.qa")

# 证据预算：早期为小上下文模型定的保守值（6 片 / 3600 字）会让回答"准确但单薄"。
# 现代模型上下文普遍 256K–1M，把证据放开到 28 片 / 每文件 8 片 / 邻居各扩 3 片，
# 装配字符预算 48000（约几万 token，占 256K 上下文的零头）——
# 让模型看到几乎全部相关原文，才能答得全。
TOP_CONTEXT = 28
MAX_PER_CONTENT = 8
NEIGHBOR_SPAN = 3
CONTEXT_CHAR_BUDGET = 48000
REFUSAL = "未在文件库中找到足够依据"

_CITE = re.compile(r"\[C(\d{1,3})\]")   # 证据可达三位数条目，两位会漏 C100+


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
    mode: str = "knowledge"  # knowledge=依据文件库 / general=通用回答（未引用资料）


# ---------------------------------------------------------------- 检索与装配

def retrieve_context(conn, question: str, book_id: int | None = None,
                     *, return_trace: bool = False):
    # 候选池放宽到 120：更大的证据预算需要更多候选来喂满，
    # 否则"讲全"受限于召回而不是预算
    retrieval = run_retrieval(
        conn, question, route_limit=120, candidate_limit=120, book_id=book_id,
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
        char_budget=CONTEXT_CHAR_BUDGET,
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
    vector_hits = retrieval.routes.get("vector") or []
    top_vector_cosine = float(vector_hits[0][1]) if vector_hits else 0.0
    if not return_trace:
        return pieces
    trace = retrieval.trace.to_dict()
    trace["top_vector_cosine"] = top_vector_cosine
    return pieces, trace


# ---------------------------------------------------------------- Prompt

GENERAL_MARK = "【通用】"


def build_messages(question: str, pieces: list[ContextPiece]) -> list[dict]:
    """资料在前、问题在后，问题后补一行约束复述（§12.4 ⑤ recency 槽位）。

    模型先做意图路由：与本地资料相关 → 严格依据资料并逐句引用；
    寒暄/常识/写作等通用问题 → 以 GENERAL_MARK 开头自然回答。
    路由标记在带内声明，后置校验据此分流 —— 知识库回答仍走全部硬校验，
    通用回答在界面明确标注"未引用文件库"，来源永远可辨。
    """
    ctx_lines = []
    for p in pieces:
        loc = f"第{p.page}页" if p.page else (p.section_path or "")
        ctx_lines.append(f"[{p.tag}] 《{p.file_name}》{('·' + loc) if loc else ''}\n{p.text}")
    ctx = "\n\n".join(ctx_lines) if ctx_lines else "（本次没有检索到相关资料）"

    system = (
        "你是个人知识库的问答助手。先判断用户的问题是否需要依据【资料】"
        "（用户本地文件的内容）作答：\n"
        "1) 问题涉及用户的文件、资料内容，或你在【资料】中看到了相关信息 → "
        "只依据【资料】回答。要求：\n"
        "   · 先给结论——第一句直接回答问题的核心，再展开细节与依据。\n"
        "   · 尽量全面——把【资料】中所有与问题相关的信息都整合进来，"
        "不要只挑一条就收尾；有多个要点、数字、条件、步骤时逐条列出。\n"
        "   · 结构清晰——信息较多时用分点或小标题组织，便于阅读；"
        "对比、清单类信息可用表格呈现。\n"
        "   · 行文连贯——把同一主题的事实融合成通顺的叙述，按逻辑重新组织，"
        "不要按资料出现顺序一条证据复述一句；不要反复使用"
        "「资料显示」「资料还记载」这类套话，直接陈述内容本身。\n"
        "   · 如实呈现——不同资料相互矛盾时，并列说明各自的记载并分别引用；"
        "资料带日期或版本时以较新者为准，并点明时间。\n"
        "   · 每个事实性陈述后紧跟引用标记（如 [C1]，可多个），"
        "格式示例：「烧成温度在一千二百度上下 [C1]，呈色由还原气氛决定 [C2]。」\n"
        f"   · 资料确实不足以回答时，只输出「{REFUSAL}」这一句话，"
        "不得用你自己的知识补充、不得推测。\n"
        f"2) 与本地资料无关的通用问题（寒暄、常识、写作、翻译、代码等）→ "
        f"回答第一行以{GENERAL_MARK}开头，然后正常、充分地回答，不使用引用标记。\n"
        "拿不准时优先按第 1 类处理。始终使用与问题相同的语言回答。"
    )
    user = (
        f"【资料】\n{ctx}\n\n【问题】\n{question}\n\n"
        f"（提醒：涉及资料则**尽量全面地整合所有相关信息**、逐句带 [Cn] 引用，"
        f"资料不足才只答「{REFUSAL}」；与资料无关的通用问题以{GENERAL_MARK}开头直接回答）"
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


# ---------------------------------------------------------------- 句级自动归因

_SENT_SPLIT = re.compile(r"(?<=[。！？；\n])")
# 数字是幻觉重灾区：句中的阿拉伯数字与中文数字串必须逐个出现在归因证据里
_NUM_TOKEN = re.compile(r"[0-9]+(?:\.[0-9]+)?%?|[零一二两三四五六七八九十百千万亿]{2,}")
_AUTO_CITE_MIN_OVERLAP = 0.55
_SUBSTANTIVE_LEN = 8


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def auto_cite(text: str, pieces: list[ContextPiece]) -> tuple[str, int] | None:
    """模型没写 [Cn] 时的后置归因兜底。

    很多中转/推理模型不遵守引用格式，直接降级成"只给片段"会让问答
    大部分时间答非所问（用户视角：知识库问答不返回内容）。这里逐句
    与证据做字符 bigram 重叠归因：**每个实质句都能落到某条证据上**
    （重叠 ≥ 0.55 且句中所有数字串都出现在该证据里）才接受，任何一句
    落不下就整体放弃、回到原有降级路径 —— 底线不变：无依据不成答案。
    """
    sentences = [s for s in _SENT_SPLIT.split(text) if s.strip()]
    if not sentences:
        return None
    corpus = [(p, _bigrams(p.text), p.text) for p in pieces]
    out: list[str] = []
    attributed = 0
    for sentence in sentences:
        core = sentence.strip()
        if len(core) < _SUBSTANTIVE_LEN:
            out.append(sentence)   # 连接语/短语不强制归因，也不注引用
            continue
        grams = _bigrams(core)
        if not grams:
            out.append(sentence)
            continue
        best_piece, best_text, best_ratio = None, "", 0.0
        for piece, piece_grams, piece_text in corpus:
            ratio = len(grams & piece_grams) / len(grams)
            if ratio > best_ratio:
                best_piece, best_text, best_ratio = piece, piece_text, ratio
        if best_piece is None or best_ratio < _AUTO_CITE_MIN_OVERLAP:
            return None
        if any(token not in best_text for token in _NUM_TOKEN.findall(core)):
            return None   # 数字对不上 → 疑似幻觉，整体放弃
        match = re.match(r"^(.*?)([。！？；\s]*)$", sentence, re.DOTALL)
        out.append(f"{match.group(1)} [{best_piece.tag}]{match.group(2)}")
        attributed += 1
    if attributed == 0:
        return None
    return "".join(out), attributed


# ---------------------------------------------------------------- 多轮浓缩与检索改写

def _condense_question(question: str, history: list[dict]) -> str:
    """把依赖对话上下文的追问改写成可独立检索的问题（conversational condensing）。

    「那第二个呢？」「它多少钱？」这类追问单独拿去检索毫无意义 ——
    这是多轮问答体验僵硬的最大来源，也是业界公认收益最高的单点改写。
    只在有历史时才多打一次轻量模型调用；改写失败回退原问题，不阻塞。
    """
    turns = [t for t in (history or []) if str(t.get("q") or "").strip()][-4:]
    if not turns:
        return question
    lines = []
    for turn in turns:
        lines.append(f"用户：{str(turn.get('q') or '').strip()}")
        answer_text = str(turn.get("a") or "").strip()
        if answer_text:
            lines.append(f"助手：{answer_text[:200]}")
    try:
        rewritten = llm.chat(
            [{"role": "system", "content":
              "把用户的新问题改写成不依赖对话历史、可独立用于检索的完整问题。"
              "只输出改写后的问题本身，不要任何解释或前缀。"
              "如果新问题本身已经独立完整，原样输出。"},
             {"role": "user", "content":
              "【对话历史】\n" + "\n".join(lines) + f"\n\n【新问题】\n{question}"}],
            temperature=0, max_tokens=200, timeout=15,
        ).strip()
    except llm.LLMError:
        return question
    if not rewritten or len(rewritten) > max(len(question) * 5, 160):
        return question   # 模型跑偏输出长篇 → 弃用改写
    return rewritten


def _rewrite_for_retrieval(question: str) -> str | None:
    """拒答后的检索查询改写（bounded self-reflection 的轻量版，只试一次）。

    模型说"资料不足"未必是库里没有 —— 常见原因是提问用词和文档原文
    对不上。换成更可能出现在文档里的关键词重试一次，命中率立涨。
    """
    try:
        rewritten = llm.chat(
            [{"role": "system", "content":
              "在个人文件库中没有检索到这个问题的答案。请把它改写成更可能"
              "命中文档原文的检索查询：改用文档里可能出现的关键词、同义词，"
              "展开缩写。只输出改写后的查询本身，不要解释。"},
             {"role": "user", "content": question}],
            temperature=0, max_tokens=120, timeout=15,
        ).strip()
    except llm.LLMError:
        return None
    if not rewritten or rewritten == question or len(rewritten) > 160:
        return None
    return rewritten


# ---------------------------------------------------------------- 主入口

def ask(conn: sqlite3.Connection, question: str, book_id: int | None = None,
        history: list[dict] | None = None) -> Answer:
    if not llm.is_configured():
        return Answer(status="not_configured", answer=None,
                      hedge="尚未配置模型服务，可先使用搜索")

    validation: dict = {"attempts": 0}
    # 多轮追问先浓缩成独立问题再检索；生成仍然用用户原话
    search_query = _condense_question(question, history or [])
    if search_query != question:
        validation["condensed_query"] = search_query

    pieces, trace = retrieve_context(conn, search_query, book_id, return_trace=True)
    # 没有检索到资料也继续 —— 模型可路由为通用回答；涉及资料则按提示拒答

    from app.index.confidence import assess
    top_cosine = float(trace.get("top_vector_cosine") or 0.0)
    conf = assess(conn, search_query, top_cosine)

    # 回答长度档位（设置 → 通用配置）："auto" = 不传上限，跟随所选模型
    from app.db.database import get_setting
    raw_limit = get_setting(conn, "answer_max_tokens", "auto")
    try:
        answer_tokens: int | None = None if raw_limit == "auto" else int(raw_limit)
    except ValueError:
        answer_tokens = None

    answer = _generate(question, pieces, conf, trace, validation,
                       max_tokens=answer_tokens)

    # 拒答 ≠ 库里没有：换检索关键词重试一次（有界，最多一轮）。
    # 重试用独立的 validation 副本 —— 重试失败被丢弃时，
    # 不能污染首轮已经留痕的校验记录。
    if answer.status == "refused":
        rewritten = _rewrite_for_retrieval(search_query)
        if rewritten:
            retry_pieces, retry_trace = retrieve_context(
                conn, rewritten, book_id, return_trace=True)
            if retry_pieces:
                retry_validation = dict(validation)
                retry_validation["retrieval_retry"] = rewritten
                retry = _generate(question, retry_pieces, conf,
                                  retry_trace, retry_validation,
                                  max_tokens=answer_tokens)
                if retry.status == "answered":
                    return retry
    return answer


def _generate(question: str, pieces: list[ContextPiece], conf,
              trace: dict, validation: dict,
              max_tokens: int | None = None) -> Answer:
    """一轮完整生成：装配 → LLM →（通用路由 / 四条硬校验 / 自动归因）。"""
    messages = build_messages(question, pieces)

    for attempt in (1, 2):
        validation["attempts"] = attempt
        try:
            # max_tokens=None → 不传上限，跟随所选模型的默认输出上限。
            # 长上下文 + 详尽回答在中转服务上经常超过 60 秒，超时放宽到 180
            raw = llm.chat(messages, max_tokens=max_tokens, timeout=180)
        except llm.LLMError as exc:
            # 模型调用失败不该炸整个请求：降级为检索结果并说明原因
            validation["error"] = str(exc)
            return Answer(
                status="fallback", answer=None,
                retrieved=[_citation_dict(p) for p in pieces],
                hedge=f"模型调用失败（{exc}），以下是检索到的原文片段",
                validation=validation, trace=trace,
            )
        # 通用路由：模型判断与本地资料无关，直接自然回答（界面标注来源）
        raw_text = raw.strip()
        if raw_text.startswith(GENERAL_MARK):
            general = _CITE.sub("", raw_text[len(GENERAL_MARK):]).strip()
            if general:
                validation["mode"] = "general"
                return Answer(status="answered", answer=general,
                              validation=validation, trace=trace,
                              mode="general")
            # 只有标记没有内容 → 按无效输出走原有重试/降级

        cleaned, rec = validate(raw, pieces)
        validation.update(rec)

        if cleaned == REFUSAL:
            return Answer(status="refused", answer=REFUSAL,
                          hedge=conf.hedge, validation=validation, trace=trace)

        cited = _cited_tags(cleaned)
        if not cited:
            # 模型不守引用格式 ≠ 答案无依据：先尝试句级自动归因，
            # 全部实质句都能落到证据上才接受（auto_cited 留痕可审计）
            attributed = auto_cite(cleaned, pieces)
            if attributed:
                cleaned, cited_count = attributed
                validation["auto_cited"] = cited_count
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
