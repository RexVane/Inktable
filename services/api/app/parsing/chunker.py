"""分片 —— 把 ParsedDoc 切成可检索的 chunk（PLAN §12.2c）。

三条规则来自方案，每条都有具体理由：

1. **只在 Block 边界切，绝不切断句子**
   切碎的句子既检索不准，展示给用户看也是断头话。

2. **表格整体成片**
   表格切开后行与表头分离，"张三 | 85 | 优秀"脱离表头就没有意义。

3. **相邻小块合并到目标长度**
   单个段落往往太短（几十字），单独成片会让向量语义稀薄；
   合并到 ~500 字是检索质量与定位精度的平衡点。

**每个 chunk 携带 locator**，这是引用能跳回原文的前提（§12.6）。
合并多个 Block 时取第一个 Block 的 locator —— 用户跳过去看到的是这段的开头。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.parsing.base import Block, BlockKind, Locator, ParsedDoc

# 目标片长（字符）。中文按字符计，与 token 大致 1:1
TARGET_CHARS = 500
MAX_CHARS = 1000     # 硬上限，超过强制切分
MIN_CHARS = 80       # 短于此的尾片并入前一片，避免产生语义稀薄的碎片


@dataclass
class Chunk:
    text: str
    ordinal: int
    locator: Locator = field(default_factory=Locator)
    section_path: str = ""      # 标题路径，嵌入时前置（§12.2d）
    kind: str = "text"          # text / table / code

    @property
    def text_hash(self) -> str:
        """内容哈希 —— 增量更新的 diff 主键（§12.5）。

        用它而不是 section_path：文档开头插一段会让所有标题编号平移，
        按路径匹配会退化成全量重嵌入。
        """
        return hashlib.sha1(self.text.encode("utf-8")).hexdigest()

    @property
    def embed_text(self) -> str:
        """送给嵌入模型的文本 —— 前置标题路径（§12.2d）。

        「见附件三」这样的片段单独看毫无语义，带上
        「采购合同 › 第二章 履约 › 2.3 保修条款」后可辨识度完全不同。
        """
        return f"{self.section_path}\n\n{self.text}" if self.section_path else self.text


def chunk_document(doc: ParsedDoc) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf: list[Block] = []
    buf_len = 0

    def flush():
        nonlocal buf, buf_len
        if not buf:
            return
        text = "\n\n".join(b.text for b in buf).strip()
        if text:
            chunks.append(
                Chunk(
                    text=text,
                    ordinal=len(chunks),
                    locator=buf[0].locator,
                    section_path=" › ".join(buf[0].locator.heading_path),
                )
            )
        buf = []
        buf_len = 0

    for block in doc.blocks:
        # 表格与代码整体成片，不与其他块合并
        if block.kind in (BlockKind.TABLE, BlockKind.CODE):
            flush()
            for piece in _split_oversized(block.text):
                chunks.append(
                    Chunk(
                        text=piece,
                        ordinal=len(chunks),
                        locator=block.locator,
                        section_path=" › ".join(block.locator.heading_path),
                        kind="table" if block.kind == BlockKind.TABLE else "code",
                    )
                )
            continue

        # 标题不单独成片 —— 它已经在 section_path 里了，单独成片会产生
        # 一堆只有几个字的无用 chunk。但它标志着新章节开始，先冲掉缓冲。
        if block.kind == BlockKind.HEADING:
            flush()
            continue

        # 单个超长段落：先冲掉缓冲，再单独切
        if len(block.text) > MAX_CHARS:
            flush()
            for piece in _split_oversized(block.text):
                chunks.append(
                    Chunk(
                        text=piece,
                        ordinal=len(chunks),
                        locator=block.locator,
                        section_path=" › ".join(block.locator.heading_path),
                    )
                )
            continue

        # 加进来会超长 → 先冲掉
        if buf_len + len(block.text) > TARGET_CHARS and buf:
            flush()

        buf.append(block)
        buf_len += len(block.text)

    flush()

    # 尾片太短就并入前一片，避免语义稀薄的碎片
    if len(chunks) >= 2 and len(chunks[-1].text) < MIN_CHARS and chunks[-1].kind == "text":
        tail = chunks.pop()
        chunks[-1].text += "\n\n" + tail.text

    return chunks


def _split_oversized(text: str) -> list[str]:
    """切分超长文本。优先在句号处切，退而求其次在换行处，最后硬切。"""
    if len(text) <= MAX_CHARS:
        return [text]

    pieces: list[str] = []
    rest = text
    while len(rest) > MAX_CHARS:
        window = rest[:MAX_CHARS]
        # 在后半段找断点，避免切出过短的片
        cut = max(
            window.rfind("。", MAX_CHARS // 2),
            window.rfind("\n", MAX_CHARS // 2),
            window.rfind(". ", MAX_CHARS // 2),
        )
        if cut < MAX_CHARS // 2:
            cut = MAX_CHARS  # 找不到断点就硬切
        else:
            cut += 1
        pieces.append(rest[:cut].strip())
        rest = rest[cut:]
    if rest.strip():
        pieces.append(rest.strip())
    return pieces
