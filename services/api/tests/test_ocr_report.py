"""扫描件 OCR 兜底与每周报告的测试。

OCR 不真调 osascript（CI/无权限环境不可依赖），全部经 monkeypatch
锁定接线正确性：无文本 PDF → OCR 路径 → 分块可检索；开关与重试端点语义。
"""

from __future__ import annotations

import re
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app.parsing import ocr, ocr_windows, parsers

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDO_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("ORDO_TOKEN", TOKEN)
    import importlib

    from app import main as main_mod
    importlib.reload(main_mod)
    with TestClient(main_mod.app) as c:
        yield c


def _blank_pdf(path):
    """生成一页没有文本层的 PDF（等价扫描件）。"""
    import pymupdf as fitz

    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()


def test_pdf_ocr_fallback_extracts_text(tmp_path, monkeypatch):
    pdf = tmp_path / "扫描.pdf"
    _blank_pdf(pdf)
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(
        ocr, "ocr_pdf",
        lambda path: ([(1, "扫描出来的合同条款文本。")], []),
    )
    ocr.set_runtime_enabled(True)

    doc = parsers.parse_pdf(pdf)
    assert doc.blocks, "OCR 文本必须进入 blocks"
    assert doc.blocks[0].text == "扫描出来的合同条款文本。"
    assert doc.blocks[0].locator.page == 1
    assert any("OCR 提取" in w for w in doc.warnings)


def test_pdf_ocr_respects_disabled(tmp_path, monkeypatch):
    pdf = tmp_path / "扫描.pdf"
    _blank_pdf(pdf)
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(
        ocr, "ocr_pdf",
        lambda path: ([(1, "不该出现的文本")], []),
    )
    ocr.set_runtime_enabled(False)
    try:
        doc = parsers.parse_pdf(pdf)
        assert not doc.blocks
        assert any("未启用或不可用" in w for w in doc.warnings)
    finally:
        ocr.set_runtime_enabled(True)


def test_retry_scanned_requeues_and_indexes(client, tmp_path, monkeypatch):
    """扫描件先标 no_text；开 OCR 后重试 → 重新解析并可检索。"""
    d = tmp_path / "src"
    d.mkdir()
    _blank_pdf(d / "扫描合同.pdf")

    # 第一轮：OCR 不可用 → no_text
    monkeypatch.setattr(ocr, "is_available", lambda: False)
    client.post("/sources/enable", headers=H, json={"name": "S", "path": str(d)})
    client.post("/index/run", headers=H, json={"limit": 10})
    assert client.get("/index/status", headers=H).json()["chunks"] == 0

    # 没有可重试项之外的情况：先确认确实有 no_text 的 pdf
    r0 = client.post("/index/retry_scanned", headers=H).json()
    assert r0["requeued"] == 1

    # 第二轮：OCR 可用 → 补出文本
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(
        ocr, "ocr_pdf",
        lambda path: ([(1, "青花瓷钴料配比试验记录。")], []),
    )
    client.post("/index/run", headers=H, json={"limit": 10})
    assert client.get("/index/status", headers=H).json()["chunks"] > 0

    # 幂等：已修复后再重试无事发生
    assert client.post("/index/retry_scanned", headers=H).json()["requeued"] == 0


def test_retry_scanned_re_registers_hash_failed_file(client, tmp_path):
    """A real hash_failed row must execute the scanner retry path, not 500."""
    import sqlite3

    source = tmp_path / "src"
    source.mkdir()
    document = source / "可恢复.md"
    document.write_text("哈希读取恢复后的知识正文。", encoding="utf-8")
    client.post(
        "/sources/enable",
        headers=H,
        json={"name": "S", "path": str(source)},
    )

    raw = sqlite3.connect(tmp_path / "t.db")
    raw.execute(
        "UPDATE files SET state = 'failed', error_code = 'hash_failed' WHERE path = ?",
        (str(document),),
    )
    raw.commit()
    raw.close()

    result = client.post("/index/retry_scanned", headers=H)
    assert result.status_code == 200
    assert result.json()["hash_retried"] == 1
    assert result.json()["hash_recovered"] == 1
    assert result.json()["hash_failed"] == 0

    raw = sqlite3.connect(tmp_path / "t.db")
    state, error_code = raw.execute(
        "SELECT state, error_code FROM files WHERE path = ?", (str(document),)
    ).fetchone()
    raw.close()
    assert state != "failed"
    assert error_code is None


def test_qa_answer_length_setting(client):
    """回答长度档位：默认 auto；可设数字；非法值拒绝。"""
    assert client.get("/settings/qa", headers=H).json()["answer_max_tokens"] == "auto"

    r = client.post("/settings/qa", headers=H,
                    json={"answer_max_tokens": "131072"}).json()
    assert r["answer_max_tokens"] == "131072"
    assert client.get("/settings/qa", headers=H).json()["answer_max_tokens"] == "131072"

    assert client.post("/settings/qa", headers=H,
                       json={"answer_max_tokens": "abc"}).status_code == 400
    assert client.post("/settings/qa", headers=H,
                       json={"answer_max_tokens": "10"}).status_code == 400

    client.post("/settings/qa", headers=H, json={"answer_max_tokens": "auto"})


def test_ocr_setting_toggle(client):
    st = client.get("/settings/ocr", headers=H).json()
    assert st["enabled"] is True   # 默认开
    assert st["engine"] in {"macos-vision", "windows-media-ocr", "unavailable"}
    assert st["engine_label"]
    client.post("/settings/ocr", headers=H, json={"enabled": False})
    assert client.get("/settings/ocr", headers=H).json()["enabled"] is False
    client.post("/settings/ocr", headers=H, json={"enabled": True})


def test_windows_ocr_image_normalizes_text(tmp_path, monkeypatch):
    image = tmp_path / "任意 名称.png"
    image.write_bytes(b"not-read-by-the-mock")
    monkeypatch.setattr(ocr_windows, "is_available", lambda: True)

    def fake_run(command, *, image_path=None, timeout):
        assert command == ocr_windows._ENCODED_SCRIPT
        assert image_path == image
        assert timeout == ocr_windows.OCR_PAGE_TIMEOUT
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="个 人 知 识 库\r\nINKTABLE  OCR", stderr="",
        )

    monkeypatch.setattr(ocr_windows, "_run_powershell", fake_run)
    assert ocr_windows.ocr_image(image) == "个人知识库\nINKTABLE OCR"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows OCR integration")
def test_windows_system_ocr_reads_image_only_pdf(tmp_path):
    if not ocr_windows.is_available():
        pytest.skip("Windows OCR language pack unavailable")

    import pymupdf as fitz

    source = fitz.open()
    page = source.new_page(width=595, height=842)
    page.insert_text((72, 180), "INKTABLE OCR 2048", fontsize=32)
    pix = page.get_pixmap(dpi=200, alpha=False)
    png = tmp_path / "ocr-source.png"
    pix.save(str(png))
    source.close()

    scan = fitz.open()
    page = scan.new_page(width=595, height=842)
    page.insert_image(page.rect, filename=str(png))
    pdf = tmp_path / "image-only.pdf"
    scan.save(str(pdf))
    scan.close()

    check = fitz.open(pdf)
    try:
        assert check[0].get_text("text") == ""
    finally:
        check.close()

    parsed = parsers.parse_pdf(pdf)
    recognized = re.sub(r"\W+", "", " ".join(b.text for b in parsed.blocks)).upper()
    assert "INKTABLEOCR2048" in recognized
    assert any("已通过 OCR" in warning for warning in parsed.warnings)


def test_weekly_report_generates_and_caches(client, tmp_path, monkeypatch):
    from app.qa import report as report_mod

    monkeypatch.setattr(report_mod, "REPORTS_DIR", tmp_path / "reports")

    d = tmp_path / "src"
    d.mkdir()
    (d / "本周笔记.md").write_text("# 笔记\n\n景德镇钴料配比。\n", encoding="utf-8")
    client.post("/sources/enable", headers=H, json={"name": "S", "path": str(d)})
    client.post("/index/run", headers=H, json={"limit": 10})

    first = client.get("/reports/weekly", headers=H).json()
    assert first["generated"] is True
    assert "知识库周报" in first["markdown"]
    assert "本周重点文档" in first["markdown"]
    assert "本周笔记.md" in first["markdown"]
    assert time.strftime("%G-W%V") == first["week"]

    cached = client.get("/reports/weekly", headers=H).json()
    assert cached["generated"] is False
    assert cached["markdown"] == first["markdown"]

    regen = client.get("/reports/weekly", headers=H,
                       params={"force": "true"}).json()
    assert regen["generated"] is True
