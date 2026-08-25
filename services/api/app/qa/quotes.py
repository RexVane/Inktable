"""逐字引文核验 —— 引用不只指向分片，还要指向分片里的**哪句话**。

**为什么加这一条**

Inktable 现有的四条硬校验守的是「引用编号存在且被使用」：`[C3]` 必须指向本次
上下文里真实存在的第 3 片。它守不住的是**这一片里到底有没有这句话** ——
模型可以引对了文件、却写出文件里没有的数值，而 `[C3]` 格式完全合法。这正是
`docs/eval/README.md` 里 Gold evidence citation recall 只有 68.93% 而
「精确引用率 100%」的原因：后者量的是格式，不是内容。

marginalia 的做法是让每条引用带一段 verbatim quote 并核对它在原文出现
（`quote_matches_source_text`）。这里移植同一条思路，但按 Inktable 的分片
模型落地：引文必须出现在**它所引用的那一片**里，不是"库里某处"。

**默认只诊断，不改行为**

核验结果写进 `validation.quotes`，并把 quote 挂到 citation 上（界面可以显示
「引用的是这一句」）。是否因核不上而剔除引用由 `INKTABLE_QUOTE_ENFORCE`
控制，默认关。理由：改答案行为必须先用 65 题 QA 复验，而那套复验此刻仍被
provider 可用性阻塞（见 CURRENT_STATUS 第十节）。先量，再决定要不要执行 ——
反过来做就是在没有基线的情况下动引用可靠性。
"""

from __future__ import annotations

import os
import re
import unicodedata

# 模型在正文之后追加的引文块。放在末尾而不是内联，是为了不动渲染层已有的
# [Cn] 解析 —— 内联成 [C1:"..."] 会让前端的引用正则与去重逻辑全部要改。
QUOTE_BLOCK_MARK = "===引文==="

_LINE = re.compile(r"^\s*(C\d{1,3})\s*[:：]\s*(.+?)\s*$")
# 引文最短长度。太短的片段（「是」「48.4」）在任何文本里都能命中，
# 核验就退化成了永真判据。
_MIN_QUOTE_CHARS = 6
_MAX_QUOTE_CHARS = 200


def enforcing() -> bool:
    return os.environ.get("INKTABLE_QUOTE_ENFORCE", "0") == "1"


def prompt_clause() -> str:
    """加进 system prompt 的要求。与 validate 的解析格式必须严格对应。"""
    return (
        "   · 正文写完后另起一行输出 " + QUOTE_BLOCK_MARK + "，然后每个用过的"
        "引用标记一行，格式为 `C1: 原文片段`。片段必须是你从该编号资料里"
        "**逐字复制**的一句或半句话（6 到 60 字），不得改写、不得翻译、"
        "不得拼接两处。这段内容不会显示给用户，只用于核对。\n"
    )


def _normalize(text: str) -> str:
    """核验用归一化。

    只吃掉「同一句话在不同渠道里必然产生的差异」：全角/半角、空白折叠、
    零宽字符。**不做**大小写折叠以外的语义改写 —— 归一化越宽，核验越接近
    永真，那就失去意义了。
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("​", "").replace("﻿", "")
    text = re.sub(r"\s+", "", text)
    return text.casefold()


def split_block(text: str) -> tuple[str, dict[str, str]]:
    """把回答拆成 (显示正文, {tag: 引文})。没有引文块时原样返回。"""
    idx = text.find(QUOTE_BLOCK_MARK)
    if idx < 0:
        return text, {}
    body = text[:idx].rstrip()
    quotes: dict[str, str] = {}
    for line in text[idx + len(QUOTE_BLOCK_MARK):].splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        tag, quote = m.group(1), m.group(2).strip().strip('"“”')
        if quote:
            quotes[tag] = quote[:_MAX_QUOTE_CHARS]
    return body, quotes


def verify(quotes: dict[str, str], sources: dict[str, str]) -> dict:
    """核对每条引文是否逐字出现在它所引用的那一片里。

    `sources` 是 tag → 该片原文。返回诊断记录：
      total/verified/failed 计数、per_tag 明细、以及 verified_ratio。
    """
    per_tag: dict[str, dict] = {}
    verified = 0
    for tag, quote in sorted(quotes.items()):
        source = sources.get(tag)
        if source is None:
            per_tag[tag] = {"status": "unknown_tag", "chars": len(quote)}
            continue
        if len(quote) < _MIN_QUOTE_CHARS:
            # 太短不算通过也不算失败：它证明不了任何事，单独记一类，
            # 免得把「模型偷懒给了两个字」混进"核验通过"的分子里。
            per_tag[tag] = {"status": "too_short", "chars": len(quote)}
            continue
        if _normalize(quote) in _normalize(source):
            per_tag[tag] = {"status": "ok", "chars": len(quote)}
            verified += 1
        else:
            per_tag[tag] = {"status": "not_found", "chars": len(quote)}
    total = len(per_tag)
    return {
        "total": total,
        "verified": verified,
        "failed": sum(1 for v in per_tag.values() if v["status"] == "not_found"),
        "too_short": sum(1 for v in per_tag.values() if v["status"] == "too_short"),
        "unknown_tag": sum(1 for v in per_tag.values() if v["status"] == "unknown_tag"),
        "verified_ratio": (verified / total) if total else None,
        "per_tag": per_tag,
        "enforced": enforcing(),
    }


def failing_tags(report: dict) -> set[str]:
    """核不上的编号。仅在 enforcing() 为真时由调用方用于剔除引用。"""
    return {tag for tag, info in (report.get("per_tag") or {}).items()
            if info.get("status") == "not_found"}
