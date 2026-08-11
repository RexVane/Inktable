"""各格式解析器 —— 全部产出统一的 ParsedDoc（PLAN §12.2）。

设计原则：**解析失败不能拖垮流水线**。单个文档加密、损坏、格式异常时，
返回已解析出的部分 + warnings，而不是抛异常让整批索引中断。
只有完全无法读取才抛 ParseError。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.parsing.base import (
    Block,
    BlockKind,
    Locator,
    ParsedDoc,
    ParseError,
    UnsupportedFormat,
)

# 单文档解析上限：超大文档解析会吃满内存且价值递减
MAX_CHARS = 5_000_000


def parse_pdf(path: Path) -> ParsedDoc:
    import pymupdf as fitz

    blocks: list[Block] = []
    warnings: list[str] = []

    try:
        doc = fitz.open(path)
    except Exception as e:
        raise ParseError(f"无法打开 PDF：{e}") from e

    try:
        if doc.needs_pass:
            raise ParseError("PDF 已加密")

        total = 0
        for pno in range(doc.page_count):
            try:
                page = doc.load_page(pno)
                text = page.get_text("text")
            except Exception:
                warnings.append(f"第 {pno + 1} 页解析失败，已跳过")
                continue

            for para in _split_paragraphs(text):
                blocks.append(
                    Block(
                        kind=BlockKind.PARAGRAPH,
                        text=para,
                        locator=Locator(page=pno + 1),
                    )
                )
                total += len(para)

            if total > MAX_CHARS:
                warnings.append(f"文档过长，只索引了前 {pno + 1} 页")
                break

        page_count = doc.page_count
        title = (doc.metadata or {}).get("title") or None
    finally:
        doc.close()

    # 扫描件（纯图片 PDF）没有文本层 —— 这不是错误，但用户需要知道
    if not blocks:
        warnings.append("未提取到文本，可能是扫描件（暂不支持 OCR）")

    return ParsedDoc(blocks=blocks, title=title, page_count=page_count, warnings=warnings)


def parse_docx(path: Path) -> ParsedDoc:
    import docx  # python-docx

    try:
        doc = docx.Document(str(path))
    except Exception as e:
        raise ParseError(f"无法打开 DOCX：{e}") from e

    blocks: list[Block] = []
    heading_path: list[str] = []

    for idx, para in enumerate(doc.paragraphs):
        text = para.text.strip()
        if not text:
            continue

        style = (para.style.name or "").lower() if para.style else ""
        level = _heading_level(style)

        if level:
            heading_path = heading_path[: level - 1] + [text]
            blocks.append(
                Block(
                    kind=BlockKind.HEADING,
                    text=text,
                    level=level,
                    locator=Locator(para_index=idx, heading_path=list(heading_path)),
                )
            )
        else:
            kind = BlockKind.LIST_ITEM if "list" in style else BlockKind.PARAGRAPH
            blocks.append(
                Block(
                    kind=kind,
                    text=text,
                    locator=Locator(para_index=idx, heading_path=list(heading_path)),
                )
            )

    # 表格单独成块，保留行结构（分片器会整表保留，见 §12.2c）
    for t_idx, table in enumerate(doc.tables):
        rows = []
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            blocks.append(
                Block(
                    kind=BlockKind.TABLE,
                    text="\n".join(rows),
                    locator=Locator(heading_path=list(heading_path)),
                )
            )

    title = blocks[0].text if blocks and blocks[0].kind == BlockKind.HEADING else None
    return ParsedDoc(blocks=blocks, title=title)


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_FENCE = re.compile(r"^```")


def parse_markdown(path: Path) -> ParsedDoc:
    text = _read_text(path)
    blocks: list[Block] = []
    heading_path: list[str] = []
    buf: list[str] = []
    in_code = False
    code_buf: list[str] = []

    def flush_para():
        if buf:
            content = "\n".join(buf).strip()
            if content:
                blocks.append(
                    Block(
                        kind=BlockKind.PARAGRAPH,
                        text=content,
                        locator=Locator(heading_path=list(heading_path)),
                    )
                )
            buf.clear()

    for line in text.splitlines():
        if _MD_FENCE.match(line):
            if in_code:
                blocks.append(
                    Block(
                        kind=BlockKind.CODE,
                        text="\n".join(code_buf),
                        locator=Locator(heading_path=list(heading_path)),
                    )
                )
                code_buf.clear()
            else:
                flush_para()
            in_code = not in_code
            continue

        if in_code:
            code_buf.append(line)
            continue

        m = _MD_HEADING.match(line)
        if m:
            flush_para()
            level = len(m.group(1))
            title = m.group(2).strip()
            heading_path = heading_path[: level - 1] + [title]
            blocks.append(
                Block(
                    kind=BlockKind.HEADING,
                    text=title,
                    level=level,
                    locator=Locator(heading_path=list(heading_path)),
                )
            )
        elif not line.strip():
            flush_para()
        else:
            buf.append(line)

    if in_code and code_buf:  # 代码块未闭合
        blocks.append(
            Block(kind=BlockKind.CODE, text="\n".join(code_buf),
                  locator=Locator(heading_path=list(heading_path)))
        )
    flush_para()

    title = blocks[0].text if blocks and blocks[0].kind == BlockKind.HEADING else None
    return ParsedDoc(blocks=blocks, title=title)


def parse_text(path: Path) -> ParsedDoc:
    text = _read_text(path)
    blocks = [
        Block(kind=BlockKind.PARAGRAPH, text=p, locator=Locator(para_index=i))
        for i, p in enumerate(_split_paragraphs(text))
    ]
    return ParsedDoc(blocks=blocks)


PARSERS = {
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".md": parse_markdown,
    ".markdown": parse_markdown,
    ".txt": parse_text,
    ".rtf": parse_text,
}


def parse(path: Path | str) -> ParsedDoc:
    p = Path(path)
    parser = PARSERS.get(p.suffix.lower())
    if parser is None:
        raise UnsupportedFormat(f"不支持的格式：{p.suffix}")
    return parser(p)


# ---------------------------------------------------------------- 内部工具

def _read_text(path: Path) -> str:
    """读文本文件。中文文档常见 GBK 编码，UTF-8 失败时回退。"""
    raw = path.read_bytes()[: MAX_CHARS * 4]
    for enc in ("utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _split_paragraphs(text: str) -> list[str]:
    """按空行切段，并合并被硬换行拆散的行。

    PDF 提取的文本常在固定宽度处换行，直接按行切会把一句话切碎。
    """
    parts = re.split(r"\n\s*\n", text)
    out = []
    for part in parts:
        merged = re.sub(r"\n(?![\s•\-\d])", "", part).strip()
        if merged:
            out.append(merged)
    return out


def _heading_level(style_name: str) -> int:
    """从 DOCX 样式名判断标题级别。中英文样式名都要认。"""
    m = re.search(r"heading\s*(\d)", style_name)
    if m:
        return int(m.group(1))
    m = re.search(r"标题\s*(\d)", style_name)
    if m:
        return int(m.group(1))
    if style_name in ("title", "标题"):
        return 1
    return 0
