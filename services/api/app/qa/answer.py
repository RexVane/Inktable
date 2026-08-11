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

from app.index.search import search as multi_search
from app.qa import llm

log = logging.getLogger("inktable.qa")

RRF_K = 60
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


@dataclass
class Answer:
    status: str              # answered / refused / fallback / not_configured
    answer: str | None
    citations: list[dict] = field(default_factory=list)
    retrieved: list[dict] = field(default_factory=list)   # fallback 时给的检索结果
    hedge: str = ""
    validation: dict = field(default_factory=dict)         # 校验过程留痕，可审计


# ---------------------------------------------------------------- 检索与装配

def _allowed_contents(conn, book_id: int | None) -> set[int] | None:
    """书内问答的范围限定（B7）。None = 不限。"""
    if book_id is None:
        return None
    rows = conn.execute(
        """SELECT DISTINCT f.content_id FROM book_members bm
           JOIN files f ON f.id = bm.file_id
           WHERE bm.book_id = ? AND f.content_id IS NOT NULL""",
        (book_id,),
    ).fetchall()
    return {r["content_id"] for r in rows}


def retrieve_context(conn, question: str, book_id: int | None = None) -> list[ContextPiece]:
    routes = multi_search(conn, question, limit=60)

    fused: dict[int, float] = {}
    for hits in routes.values():
        for rank, (cid, _s) in enumerate(hits):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    if not fused:
        return []

    ordered = sorted(fused, key=lambda k: -fused[k])
    marks = ",".join("?" * len(ordered))
    rows = {r["id"]: r for r in conn.execute(
        f"""SELECT ch.id, ch.content_id, ch.page, ch.section_path, ch.ordinal, ch.text,
                   f.id AS file_id, f.name, f.path
            FROM chunks ch JOIN files f ON f.content_id = ch.content_id
            WHERE ch.id IN ({marks})
            GROUP BY ch.id""",
        ordered,
    )}

    allowed = _allowed_contents(conn, book_id)
    picked: list = []
    per_content: dict[int, int] = {}
    for cid in ordered:
        r = rows.get(cid)
        if r is None:
            continue
        if allowed is not None and r["content_id"] not in allowed:
            continue
        # 多样性按 content_id（§12.3b ⑤ 的 v6 修正）：同一内容存了 3 处
        # 也只算一个来源，防止长文档霸占全部上下文
        if per_content.get(r["content_id"], 0) >= MAX_PER_CONTENT:
            continue
        per_content[r["content_id"]] = per_content.get(r["content_id"], 0) + 1
        picked.append(r)
        if len(picked) >= TOP_CONTEXT:
            break

    pieces: list[ContextPiece] = []
    for i, r in enumerate(picked):
        expanded = _expand_neighbors(conn, r)
        pieces.append(ContextPiece(
            tag=f"C{i + 1}",
            chunk_id=r["id"], content_id=r["content_id"],
            file_id=r["file_id"], file_name=r["name"], file_path=r["path"],
            page=r["page"], section_path=r["section_path"] or "",
            text=expanded, snippet=r["text"][:300],
        ))
    return pieces


def _expand_neighbors(conn, row) -> str:
    """B2 的动态形态：命中片 + 同内容相邻片合并送模型。

    方案原设计是预存 parent 层（相邻 3 child 合并、不参与检索）。
    这里改为**查询时按 ordinal 现取邻居**：效果相同（送模型的上下文
    完整、检索仍只用 child），但零额外存储、零重建成本 ——
    个人库的邻居查询是主键范围扫，微秒级，不值得为它维护第二层数据。
    """
    rows = conn.execute(
        """SELECT text FROM chunks
           WHERE content_id = ? AND ordinal BETWEEN ? AND ?
           ORDER BY ordinal""",
        (row["content_id"], row["ordinal"] - NEIGHBOR_SPAN,
         row["ordinal"] + NEIGHBOR_SPAN),
    ).fetchall()
    return "\n".join(r["text"] for r in rows)


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

    pieces = retrieve_context(conn, question, book_id)
    if not pieces:
        return Answer(status="refused", answer=REFUSAL)

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
                          hedge=conf.hedge, validation=validation)

        cited = _cited_tags(cleaned)
        if cited:
            by_tag = {p.tag: p for p in pieces}
            citations = [_citation_dict(by_tag[t]) for t in sorted(
                cited, key=lambda x: int(x[1:])) if t in by_tag]
            return Answer(status="answered", answer=cleaned,
                          citations=citations, hedge=conf.hedge,
                          validation=validation)

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
    )


def _citation_dict(p: ContextPiece) -> dict:
    return {
        "tag": p.tag, "chunk_id": p.chunk_id, "file_id": p.file_id,
        "file_name": p.file_name, "file_path": p.file_path,
        "page": p.page, "section_path": p.section_path,
        "snippet": p.snippet,
    }
