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
    assert d["trace"]["trace_id"]
    assert d["trace_id"] == d["trace"]["trace_id"]
    assert d["timings"] == d["trace"]["stages"]
    assert [stage["name"] for stage in d["trace"]["stages"]] == [
        "hierarchy_routing", "deep_retrieval", "decompose", "scope", "rrf",
        "rerank",
    ]
    assert "汝窑 天青釉" not in str(d["trace"])


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


def test_files_filters_and_pagination_are_server_side(client, tmp_path):
    """renderer 的来源/类型/重复与分页参数必须由服务端组合执行。"""
    a, b = tmp_path / "来源A", tmp_path / "来源B"
    a.mkdir()
    b.mkdir()
    shared = "共享内容：三星堆青铜神树的分枝结构记录。" * 8
    (a / "原件.txt").write_text(shared, encoding="utf-8")
    (a / "说明.md").write_text("# 说明\n\n来源 A 的独有文档。", encoding="utf-8")
    (b / "副本.txt").write_text(shared, encoding="utf-8")
    (b / "其他.txt").write_text("来源 B 的独有文档。", encoding="utf-8")

    sid_a = client.post(
        "/sources/enable", headers=H, json={"name": "A", "path": str(a)}
    ).json()["source_id"]
    client.post(
        "/sources/enable", headers=H, json={"name": "B", "path": str(b)}
    )

    # scanner 默认忽略无扩展名文件；直接补一条历史/手工登记数据，验证
    # ``ext=`` 与 ext 参数缺省是两个不同协议状态。
    no_ext = a / "README"
    no_ext.write_text("无扩展名文件", encoding="utf-8")
    from app import main as main_mod
    conn = main_mod.db()
    now = time.time() + 10
    conn.execute(
        """INSERT INTO files
           (volume_uuid, inode, path, name, source_id, ext, size, state, mtime, detected_at)
           VALUES ('manual-test', 999999, ?, 'README', ?, '', ?, 'registered', ?, ?)""",
        (str(no_ext), sid_a, no_ext.stat().st_size, now, now),
    )
    conn.commit()

    by_source = client.get(
        "/files", headers=H, params={"source": "A", "limit": 100}
    ).json()
    assert by_source["total"] == 3
    assert {f["name"] for f in by_source["files"]} == {"原件.txt", "说明.md", "README"}
    assert all(f["source_name"] == "A" and f["source_id"] == sid_a
               for f in by_source["files"])

    by_ext = client.get(
        "/files", headers=H, params={"ext": ".md", "limit": 100}
    ).json()
    assert [f["name"] for f in by_ext["files"]] == ["说明.md"]

    no_ext_result = client.get(
        "/files", headers=H, params={"ext": "", "limit": 100}
    ).json()
    assert [f["name"] for f in no_ext_result["files"]] == ["README"]

    duplicates = client.get(
        "/files", headers=H, params={"duplicate": True, "limit": 100}
    ).json()
    assert duplicates["total"] == 2
    assert {f["name"] for f in duplicates["files"]} == {"原件.txt", "副本.txt"}

    full = by_source["files"]
    first = client.get(
        "/files", headers=H,
        params={"source": "A", "limit": 1, "offset": 0},
    ).json()
    second = client.get(
        "/files", headers=H,
        params={"source": "A", "limit": 1, "offset": 1},
    ).json()
    assert first["total"] == second["total"] == 3
    assert [first["files"][0]["id"], second["files"][0]["id"]] == [
        full[0]["id"], full[1]["id"],
    ]

    assert client.get("/files", headers=H, params={"limit": 0}).status_code == 422
    assert client.get("/files", headers=H, params={"limit": 501}).status_code == 422
    assert client.get("/files", headers=H, params={"offset": -1}).status_code == 422


def test_file_detail_returns_metadata_and_active_passages(client, tmp_path):
    source_dir = tmp_path / "资料"
    source_dir.mkdir()
    path = source_dir / "研究笔记.md"
    path.write_text(
        "# 陶瓷研究\n\n## 配方\n\n景德镇青花瓷使用钴料。" * 20,
        encoding="utf-8",
    )
    source_id = client.post(
        "/sources/enable", headers=H,
        json={"name": "研究资料", "path": str(source_dir)},
    ).json()["source_id"]
    client.post("/index/run", headers=H, json={"limit": 50})
    file_id = client.get("/files", headers=H).json()["files"][0]["id"]

    category_id = client.post(
        "/categories", headers=H, json={"name": "陶瓷"},
    ).json()["id"]
    client.post(
        "/files/classify", headers=H,
        json={"file_ids": [file_id], "category_id": category_id},
    )
    book_id = client.post(
        "/books", headers=H, json={"name": "青花瓷研究"},
    ).json()["id"]
    client.post(
        "/books/add", headers=H,
        json={"book_id": book_id, "file_ids": [file_id]},
    )
    from app import main as main_mod
    conn = main_mod.db()
    tag_id = conn.execute(
        "INSERT INTO tags (name, color) VALUES ('重点', '#b91c1c')"
    ).lastrowid
    conn.execute(
        "INSERT INTO file_tags (file_id, tag_id) VALUES (?, ?)",
        (file_id, tag_id),
    )
    conn.commit()

    response = client.get(f"/files/{file_id}/detail", headers=H)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"file", "document", "sections", "truncated"}
    file = body["file"]
    assert file["id"] == file_id
    assert file["name"] == "研究笔记.md"
    assert file["path"] == str(path)
    assert file["mime"] == "text/markdown"
    assert file["state"] == "registered"
    assert file["source"] == {
        "id": source_id, "name": "研究资料", "path": str(source_dir),
        "kind": "manual", "discovered_by": "manual", "volatile": False,
        "enabled": True,
    }
    assert file["category"]["name"] == "陶瓷"
    assert file["tags"] == [{"id": tag_id, "name": "重点", "color": "#b91c1c"}]
    assert file["books"] == [{"id": book_id, "name": "青花瓷研究"}]
    assert file["content"]["parse_state"] == "indexed"
    assert file["content"]["active_index_version"] == 1
    assert body["document"]["title"] == "陶瓷研究"
    assert body["document"]["index_version"] == 1
    assert body["sections"]
    assert all("钴料" in passage["text"] for passage in body["sections"])
    assert all(passage["locator"] for passage in body["sections"])


def test_file_detail_is_bounded_to_active_index(client, tmp_path):
    source_dir = tmp_path / "大文档"
    source_dir.mkdir()
    (source_dir / "内容.txt").write_text("首段内容。" * 100, encoding="utf-8")
    client.post(
        "/sources/enable", headers=H,
        json={"name": "大文档", "path": str(source_dir)},
    )
    client.post("/index/run", headers=H, json={"limit": 50})
    file_id = client.get("/files", headers=H).json()["files"][0]["id"]

    from app import main as main_mod
    conn = main_mod.db()
    content = conn.execute(
        """SELECT f.content_id, c.active_index_version
           FROM files f JOIN contents c ON c.id = f.content_id WHERE f.id = ?""",
        (file_id,),
    ).fetchone()
    active = content["active_index_version"]
    conn.execute(
        """UPDATE chunks SET text = ?, bbox = 'not-json'
           WHERE id = (SELECT id FROM chunks WHERE content_id = ?
                       AND index_version = ? ORDER BY ordinal LIMIT 1)""",
        ("甲" * 4500, content["content_id"], active),
    )
    for ordinal in range(100, 130):
        conn.execute(
            """INSERT INTO chunks
               (content_id, layer, ordinal, text, text_hash, index_version)
               VALUES (?, 'child', ?, ?, ?, ?)""",
            (content["content_id"], ordinal, f"追加片段 {ordinal}",
             f"extra-{ordinal}", active),
        )
    conn.execute(
        """INSERT INTO chunks
           (content_id, layer, ordinal, text, text_hash, index_version)
           VALUES (?, 'child', 0, '旧版本不应展示', 'inactive', ?)""",
        (content["content_id"], active + 1),
    )
    conn.commit()

    body = client.get(f"/files/{file_id}/detail", headers=H).json()

    assert len(body["sections"]) == 24
    assert body["truncated"] is True
    assert len(body["sections"][0]["text"]) == 4000
    assert body["sections"][0]["text_truncated"] is True
    assert body["sections"][0]["locator"] == {}
    assert all("旧版本不应展示" not in item["text"] for item in body["sections"])


def test_file_detail_requires_auth_and_has_stable_not_found(client):
    assert client.get("/files/1/detail").status_code == 401
    response = client.get("/files/999999/detail", headers=H)
    assert response.status_code == 404
    assert response.json() == {"detail": "文件不存在"}


def test_search_book_scope_uses_member_file_for_shared_content(client, tmp_path):
    """书内全文搜索既排除书外 content，也不能把共享 content 映射到书外副本。"""
    outside, inside = tmp_path / "书外", tmp_path / "书内"
    outside.mkdir()
    inside.mkdir()
    shared = "天穹编号 QX-771 的校验规则记录在青铜神树档案中。" * 8
    (outside / "书外原件.txt").write_text(shared, encoding="utf-8")
    (outside / "纯书外.txt").write_text(
        "天穹编号 QX-771 的另一份书外研究记录。" * 8, encoding="utf-8"
    )
    (inside / "书内副本.txt").write_text(shared, encoding="utf-8")

    client.post(
        "/sources/enable", headers=H,
        json={"name": "书外来源", "path": str(outside)},
    )
    client.post(
        "/sources/enable", headers=H,
        json={"name": "书内来源", "path": str(inside)},
    )
    client.post("/index/run", headers=H, json={"limit": 50})

    file_rows = client.get("/files", headers=H, params={"limit": 100}).json()["files"]
    member_id = next(f["id"] for f in file_rows if f["name"] == "书内副本.txt")
    book_id = client.post(
        "/books", headers=H, json={"name": "限定书"}
    ).json()["id"]
    client.post(
        "/books/add", headers=H,
        json={"book_id": book_id, "file_ids": [member_id]},
    )

    result = client.post(
        "/search", headers=H,
        json={"q": "天穹编号 QX-771", "limit": 40, "book_id": book_id},
    )

    assert result.status_code == 200
    body = result.json()
    assert body["total"] == 1
    assert [f["name"] for f in body["files"]] == ["书内副本.txt"]
    assert body["files"][0]["file_id"] == member_id

    empty_book = client.post(
        "/books", headers=H, json={"name": "空书"}
    ).json()["id"]
    empty = client.post(
        "/search", headers=H,
        json={"q": "天穹编号 QX-771", "book_id": empty_book},
    ).json()
    assert empty["total"] == 0 and empty["files"] == []
