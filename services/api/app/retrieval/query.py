"""QueryPlan 辅助：比较类问题分解与文件类型提示（PLAN §5.2）。

比较类问题（"A 和 B 分别/各自…"、"A 与 B 的 X 有什么不同"）的固有难点：
两个实体的相关分片在同一条查询里竞争同一个候选池，字面信号强的实体
会把另一个实体的文档挤出文件级 Top-K。探针实测（debug run probe-1）
显示第二实体文档稳定落在第 7-13 位。

解法与 PLAN §5.3 的软路由一致：把每个实体展开成子查询，作为**额外
召回路线**参与 RRF —— 只增加入口，不排除任何既有候选（K3）。
"""

from __future__ import annotations

import re

# 触发分解的比较标记。顺序即匹配优先级：先匹配"分别/各自"类
# （谓语在标记之后），再匹配"异同/不同"类（谓语在实体与标记之间）。
_PREDICATE_AFTER = ("分别", "各自")
_PREDICATE_BEFORE = (
    "有什么不同", "有何不同", "有哪些不同", "有什么区别", "有何区别",
    "有哪些共同点和差异", "有哪些共同点", "有什么共同点", "的异同",
    "有何异同", "有哪些差异", "有什么差异",
)

_CONNECTOR = re.compile(r"\s*(?:和|与|跟|以及|、)\s*")

# 查询里明确点名的文件类型 → 扩展名集合。
# 只收显式格式词；"文件""文档"这类泛称没有区分度，不做提示。
_EXT_HINTS: dict[str, frozenset[str]] = {
    "docx": frozenset({".docx", ".doc"}),
    "word": frozenset({".docx", ".doc"}),
    "pdf": frozenset({".pdf"}),
    "markdown": frozenset({".md"}),
    "md": frozenset({".md"}),
    "txt": frozenset({".txt"}),
    "excel": frozenset({".xlsx", ".xls", ".csv"}),
    "xlsx": frozenset({".xlsx"}),
    "表格": frozenset({".xlsx", ".xls", ".csv"}),
    "ppt": frozenset({".pptx", ".ppt"}),
    "pptx": frozenset({".pptx"}),
}

# 问数值的疑问词：这类问题的答案分片几乎必然含数字，
# 作为 rerank 的答案类型特征（soft bonus，不做过滤）。
_NUMERIC_INTENT = re.compile(
    r"多少|几个|几种|几层|几条|几项|几次|版本|哪一年|年份|日期"
    r"|单价|价格|金额|比例|百分|得分|加分|分数|上限|下限|最低|最高"
)


def _split_entities(text: str) -> list[str]:
    parts = [part.strip(" ，,。？?的") for part in _CONNECTOR.split(text.strip())]
    # Single-letter/model entities are common in comparisons (“A 与 B 的区别”,
    # “甲和乙分别…”). Requiring two characters silently disabled decomposition
    # for exactly those canonical forms; connector validation below already
    # rejects empty fragments.
    entities = [part for part in parts if 1 <= len(part) <= 24]
    # 全部片段都必须成立才可信；有任何碎片说明切错了边界
    if len(entities) != len([p for p in parts if p]):
        return []
    return entities


def decompose_comparative(query: str) -> list[str]:
    """把比较类问题拆成每实体一条的子查询；非比较问题返回 []。"""
    normalized = str(query or "").strip()
    if not normalized:
        return []

    for marker in _PREDICATE_AFTER:
        index = normalized.find(marker)
        if index <= 0:
            continue
        head = normalized[:index].strip()
        predicate = normalized[index + len(marker):].strip(" ，,。？?")
        entities = _split_entities(head)
        if len(entities) < 2 or len(entities) > 3:
            continue
        return [f"{entity} {predicate}".strip() for entity in entities]

    for marker in _PREDICATE_BEFORE:
        index = normalized.find(marker)
        if index <= 0:
            continue
        head = normalized[:index].strip()
        # "A 与 B 的 X 有什么不同" —— 实体在最后一个"的"之前，谓语在其后
        predicate = ""
        split_at = head.rfind("的")
        if split_at > 0:
            predicate = head[split_at + 1:].strip()
            head = head[:split_at].strip()
        entities = _split_entities(head)
        if len(entities) < 2 or len(entities) > 3:
            continue
        return [f"{entity} {predicate}".strip() for entity in entities]

    return []


def mentioned_exts(query: str) -> frozenset[str]:
    """返回查询中显式点名的文件类型扩展名集合。"""
    lowered = str(query or "").lower()
    hinted: set[str] = set()
    for token, exts in _EXT_HINTS.items():
        if token.isascii():
            # ASCII 词要求词边界，避免 systemd/password 之类误命中
            if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered):
                hinted.update(exts)
        elif token in lowered:
            hinted.update(exts)
    return frozenset(hinted)


def wants_numeric_answer(query: str) -> bool:
    """查询是否在问一个数值/版本/日期类事实。"""
    return bool(_NUMERIC_INTENT.search(str(query or "")))
