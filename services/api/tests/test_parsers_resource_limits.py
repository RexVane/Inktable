from __future__ import annotations

import zipfile

import pytest

from app.parsing import parsers
from app.parsing.base import ParseError


def _archive(path, entries: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)


def test_docx_rejects_excessive_entry_count(tmp_path, monkeypatch) -> None:
    path = tmp_path / "many.docx"
    _archive(path, [
        ("word/document.xml", b"<w:document/>") ,
        ("word/a.xml", b"a"),
        ("word/b.xml", b"b"),
    ])
    monkeypatch.setattr(parsers, "DOCX_MAX_ENTRIES", 2)

    with pytest.raises(ParseError, match="条目过多"):
        parsers._validate_docx_archive(path)


def test_docx_rejects_entry_total_and_compression_bombs(tmp_path, monkeypatch) -> None:
    entry = tmp_path / "entry.docx"
    _archive(entry, [("word/document.xml", b"x" * 256)])
    monkeypatch.setattr(parsers, "DOCX_MAX_ENTRY_BYTES", 128)
    with pytest.raises(ParseError, match="单个条目"):
        parsers._validate_docx_archive(entry)

    total = tmp_path / "total.docx"
    _archive(total, [
        ("word/document.xml", b"x" * 80),
        ("word/styles.xml", b"y" * 80),
    ])
    monkeypatch.setattr(parsers, "DOCX_MAX_ENTRY_BYTES", 1_000)
    monkeypatch.setattr(parsers, "DOCX_MAX_TOTAL_BYTES", 100)
    with pytest.raises(ParseError, match="解压总量"):
        parsers._validate_docx_archive(total)

    ratio = tmp_path / "ratio.docx"
    _archive(ratio, [("word/document.xml", b"z" * 4096)])
    monkeypatch.setattr(parsers, "DOCX_MAX_ENTRY_BYTES", 10_000)
    monkeypatch.setattr(parsers, "DOCX_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(parsers, "_DOCX_RATIO_MIN_BYTES", 1)
    monkeypatch.setattr(parsers, "DOCX_MAX_COMPRESSION_RATIO", 2)
    with pytest.raises(ParseError, match="压缩比异常"):
        parsers._validate_docx_archive(ratio)


def test_docx_rejects_traversal_and_missing_main_document(tmp_path) -> None:
    traversal = tmp_path / "traversal.docx"
    _archive(traversal, [
        ("word/document.xml", b"<w:document/>"),
        ("../outside.xml", b"bad"),
    ])
    with pytest.raises(ParseError, match="非法或重复"):
        parsers._validate_docx_archive(traversal)

    missing = tmp_path / "missing.docx"
    _archive(missing, [("word/styles.xml", b"<w:styles/>")])
    with pytest.raises(ParseError, match="缺少 word/document.xml"):
        parsers._validate_docx_archive(missing)


def test_docx_text_output_is_bounded_by_shared_character_limit(
    tmp_path, monkeypatch,
) -> None:
    import docx

    path = tmp_path / "bounded.docx"
    document = docx.Document()
    document.add_paragraph("甲" * 500)
    document.save(path)
    monkeypatch.setattr(parsers, "MAX_CHARS", 100)

    parsed = parsers.parse_docx(path)

    assert parsed.char_count == 100
    assert parsed.text == "甲" * 100
    assert parsed.warnings == ["文档过长，只索引前 100 个字符"]
