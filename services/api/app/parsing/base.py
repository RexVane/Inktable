"""统一文档中间表示（PLAN §12.2）。

四种格式（PDF / DOCX / Markdown / 纯文本）解析后都收敛成同一个 ParsedDoc，
下游的分片、嵌入、引用映射只面对这一种结构 —— 加新格式不动下游。

**引用映射的地基在这里**：每个 Block 都带 locator，记录它在原文档中的位置
（PDF 的页码、DOCX 的段落序号、Markdown 的标题路径）。分片时 locator 会
传递给 chunk，最终让答案里的引用能跳回原文档的准确位置（§12.6）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class BlockKind(str, Enum):
    """块类型。分片器据此决定切分策略（§12.2c）。"""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    CODE = "code"


@dataclass
class Locator:
    """块在原文档中的位置。用于引用回跳（§12.6）。

    不同格式填不同字段，UI 按可用字段决定跳转方式：
    - PDF：page（1-based）
    - DOCX：para_index
    - Markdown：heading_path
    """

    page: int | None = None
    para_index: int | None = None
    heading_path: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict = {}
        if self.page is not None:
            d["page"] = self.page
        if self.para_index is not None:
            d["para_index"] = self.para_index
        if self.heading_path:
            d["heading_path"] = self.heading_path
        return d

    def describe(self) -> str:
        """人类可读的位置描述，直接显示在引用角标旁。"""
        if self.page is not None:
            return f"第 {self.page} 页"
        if self.heading_path:
            return " › ".join(self.heading_path)
        if self.para_index is not None:
            return f"第 {self.para_index + 1} 段"
        return ""


@dataclass
class Block:
    """文档中的一个语义块。"""

    kind: BlockKind
    text: str
    locator: Locator = field(default_factory=Locator)
    level: int = 0  # 仅 HEADING 有意义：1 = h1


@dataclass
class ParsedDoc:
    """解析结果。"""

    blocks: list[Block]
    title: str | None = None
    page_count: int | None = None
    # 解析器遇到的非致命问题（加密页、损坏图片等）。写入 contents.parse_warnings，
    # 让用户知道"这份文档只解析出了一部分"，而不是静默给出不完整的答案。
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.text.strip())

    @property
    def char_count(self) -> int:
        return sum(len(b.text) for b in self.blocks)


class ParseError(Exception):
    """解析失败。调用方据此把 file 置为 failed 并记录原因。"""


class UnsupportedFormat(ParseError):
    """没有对应解析器。"""
