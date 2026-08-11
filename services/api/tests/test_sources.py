"""来源管理端点测试。

重点是删除语义：停用保留索引、移除清理索引，但**两者都绝不动磁盘文件**。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INKTABLE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("INKTABLE_TOKEN", TOKEN)
    # main 在 import 时读环境变量，必须在设好之后再导入
    import importlib

    from app import main as main_mod
    importlib.reload(main_mod)
    with TestClient(main_mod.app) as c:
        yield c


H = {"Authorization": f"Bearer {TOKEN}"}


def _make_docs(d, n=3):
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        p = d / f"文档{i}.md"
        p.write_text(f"# 标题{i}\n\n景德镇青花瓷的钴料配比第{i}号试验记录。\n",
                     encoding="utf-8")
        paths.append(p)
    return paths


def test_list_sources_reports_live_state(client, tmp_path):
    """列表要带实时状态：目录还在不在、是否正在监听。"""
    d = tmp_path / "src"
    _make_docs(d)
    client.post("/sources/enable", headers=H, json={"name": "S", "path": str(d)})

    r = client.get("/sources", headers=H)
    assert r.status_code == 200
    s = r.json()["sources"][0]
    assert s["name"] == "S"
    assert s["enabled"] is True
    assert s["exists"] is True
    assert s["file_count"] == 3


def test_disable_keeps_files(client, tmp_path):
    """停用只摘监听，已收录的文件必须留着。"""
    d = tmp_path / "src"
    _make_docs(d)
    sid = client.post("/sources/enable", headers=H,
                      json={"name": "S", "path": str(d)}).json()["source_id"]

    before = client.get("/stats", headers=H).json()["files"]
    r = client.post("/sources/disable", headers=H, json={"source_id": sid})
    assert r.status_code == 200

    after = client.get("/stats", headers=H).json()["files"]
    assert after == before, "停用不该删除已收录的文件"

    s = client.get("/sources", headers=H).json()["sources"][0]
    assert s["enabled"] is False
    assert s["watching"] is False


def test_remove_clears_index_but_not_disk(client, tmp_path):
    """移除清库，但磁盘上的原文件一个都不能少 —— §1 约束 1。"""
    d = tmp_path / "src"
    paths = _make_docs(d)
    sid = client.post("/sources/enable", headers=H,
                      json={"name": "S", "path": str(d)}).json()["source_id"]
    client.post("/index/run", headers=H, json={"limit": 50})

    assert client.get("/index/status", headers=H).json()["chunks"] > 0

    r = client.post("/sources/remove", headers=H, json={"source_id": sid})
    assert r.status_code == 200
    assert r.json()["files_removed"] == 3

    st = client.get("/stats", headers=H).json()
    assert st["files"] == 0
    # 分片必须一起清掉，否则搜索会命中已消失的内容
    assert client.get("/index/status", headers=H).json()["chunks"] == 0
    assert client.get("/sources", headers=H).json()["sources"] == []

    # 磁盘文件完好
    for p in paths:
        assert p.exists(), f"{p.name} 被误删了"
        assert "景德镇青花瓷" in p.read_text(encoding="utf-8")


def test_remove_orphan_content_only(client, tmp_path):
    """两个来源含同一份内容时，移除其中一个不能清掉共享的 content。

    contents 是共享的（§9）：内容相同的文件只存一份分片。
    删来源时只能清"已无人引用"的 content，否则另一个来源的搜索会失效。
    """
    d1, d2 = tmp_path / "a", tmp_path / "b"
    for d in (d1, d2):
        d.mkdir()
        (d / "同文.md").write_text("# 同一份内容\n\n汝窑天青釉的玛瑙入釉说法缺乏实证。\n",
                                  encoding="utf-8")

    sid1 = client.post("/sources/enable", headers=H,
                       json={"name": "A", "path": str(d1)}).json()["source_id"]
    client.post("/sources/enable", headers=H, json={"name": "B", "path": str(d2)})
    client.post("/index/run", headers=H, json={"limit": 50})

    st = client.get("/stats", headers=H).json()
    assert st["files"] == 2 and st["contents"] == 1, "相同内容应共享 content"
    chunks_before = client.get("/index/status", headers=H).json()["chunks"]

    client.post("/sources/remove", headers=H, json={"source_id": sid1})

    st = client.get("/stats", headers=H).json()
    assert st["files"] == 1
    chunks_after = client.get("/index/status", headers=H).json()["chunks"]
    assert chunks_after == chunks_before, "共享 content 的分片被误删"

    # 另一个来源的内容仍可搜到
    d = client.post("/search", headers=H, json={"q": "汝窑 天青釉"}).json()
    assert d["total"] == 1


def test_add_source_manual(client, tmp_path):
    """手动添加目录（§7.6）—— NAS、外置盘等自动发现覆盖不到的场景。"""
    d = tmp_path / "手动"
    _make_docs(d, 2)
    r = client.post("/sources/add", headers=H, json={"path": str(d)})
    assert r.status_code == 200
    assert r.json()["stats"]["registered"] == 2
    assert client.get("/sources", headers=H).json()["sources"][0]["name"] == "手动"


def test_add_nonexistent_rejected(client, tmp_path):
    r = client.post("/sources/add", headers=H, json={"path": str(tmp_path / "无此目录")})
    assert r.status_code == 400


def test_remove_unknown_source(client):
    r = client.post("/sources/remove", headers=H, json={"source_id": 999})
    assert r.status_code == 404
