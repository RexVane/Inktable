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
    monkeypatch.setenv("ORDO_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("ORDO_TOKEN", TOKEN)
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


def test_windows_discovery_returns_fixed_drive_roots(monkeypatch):
    from app.discovery import sources

    monkeypatch.setattr(sources.sys, "platform", "win32")
    monkeypatch.setattr(sources, "fixed_drive_roots", lambda: [
        sources.Path("B:/"), sources.Path("C:/"), sources.Path("D:/")
    ])
    monkeypatch.setattr(sources.Path, "is_dir", lambda _self: True)
    found = sources.discover_all()
    assert [item.name for item in found] == ["B 盘", "C 盘", "D 盘"]
    assert all(item.is_drive_root for item in found)
    assert all(item.doc_count == 0 for item in found)


def test_macos_discovery_returns_volume_roots_not_apps(monkeypatch):
    from app.discovery import sources

    called = {"wechat": 0}

    def boom():
        called["wechat"] += 1
        raise AssertionError("macOS must not list IM/browser folders as sources")

    monkeypatch.setattr(sources.sys, "platform", "darwin")
    monkeypatch.setattr(sources, "macos_volume_roots", lambda: [
        sources.Path("/"), sources.Path("/Volumes/Data"),
    ])
    monkeypatch.setattr(sources.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(sources, "discover_wechat", boom)
    monkeypatch.setattr(sources, "discover_browsers", boom)
    monkeypatch.setattr(sources, "discover_system", boom)
    monkeypatch.setattr(sources, "_macos_boot_volume_name", lambda: "Macintosh HD")
    found = sources.discover_all()
    assert [item.path for item in found] == [str(sources.Path("/")), str(sources.Path("/Volumes/Data"))]
    assert [item.name for item in found] == ["Macintosh HD", "Data"]
    assert all(item.is_drive_root for item in found)
    assert all(item.discovered_by == "fixed_drive" for item in found)
    assert called["wechat"] == 0


def test_linux_discovery_returns_volume_roots_not_apps(monkeypatch):
    from app.discovery import sources

    called = {"wechat": 0}

    def boom():
        called["wechat"] += 1
        raise AssertionError("Linux must not list IM/browser folders as sources")

    monkeypatch.setattr(sources.sys, "platform", "linux")
    monkeypatch.setattr(sources, "linux_volume_roots", lambda: [
        sources.Path("/"), sources.Path("/mnt/data"),
    ])
    monkeypatch.setattr(sources.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(sources, "_linux_boot_volume_name", lambda: "系统盘")
    monkeypatch.setattr(sources, "discover_wechat", boom)
    monkeypatch.setattr(sources, "discover_browsers", boom)
    monkeypatch.setattr(sources, "discover_system", boom)
    found = sources.discover_all()
    assert [item.path for item in found] == [
        str(sources.Path("/")), str(sources.Path("/mnt/data")),
    ]
    assert [item.name for item in found] == ["系统盘", "data"]
    assert all(item.is_drive_root for item in found)
    assert all(item.discovered_by == "fixed_drive" for item in found)
    assert called["wechat"] == 0


def test_linux_volume_roots_keeps_local_disks_skips_virtual(monkeypatch):
    from app.discovery import sources

    monkeypatch.setattr(sources.sys, "platform", "linux")
    monkeypatch.setattr(sources, "_iter_linux_mounts", lambda: [
        ("/dev/nvme0n1p2", "/", "ext4"),
        ("proc", "/proc", "proc"),
        ("tmpfs", "/tmp", "tmpfs"),
        ("/dev/sdb1", "/mnt/data", "ext4"),
        ("/dev/nvme0n1p3", "/var", "ext4"),
        ("/dev/sda1", "/boot/efi", "vfat"),
        ("/dev/loop0", "/snap/foo", "squashfs"),
    ])
    monkeypatch.setattr(sources, "_linux_device_is_removable", lambda _dev: False)

    def _is_dir(self):
        return True

    def _is_mount(self):
        return str(self) in {
            str(sources.Path("/")), str(sources.Path("/mnt/data")),
        }

    monkeypatch.setattr(sources.Path, "is_dir", _is_dir)
    monkeypatch.setattr(sources.Path, "is_mount", _is_mount)
    roots = [str(p) for p in sources.linux_volume_roots()]
    assert str(sources.Path("/")) in roots
    assert str(sources.Path("/mnt/data")) in roots
    assert not any(
        item.endswith(("proc", "var")) or "snap" in item or "efi" in item
        for item in roots
    )


def test_drive_root_paths_match_windows_and_macos(monkeypatch):
    from app.watcher import policy

    monkeypatch.setattr(policy.sys, "platform", "win32")
    assert policy.is_drive_root(r"C:\\")
    assert policy.is_drive_root("D:\\")
    assert policy.is_drive_root("e:/")
    assert not policy.is_drive_root(r"C:\\Users")
    assert not policy.is_drive_root("\\\\server\\share\\")

    monkeypatch.setattr(policy.sys, "platform", "darwin")
    assert policy.is_drive_root("/")
    assert policy.is_drive_root("/Volumes/Data")
    assert policy.is_drive_root("/Volumes/Data/")
    assert not policy.is_drive_root("/Users/me")
    assert not policy.is_drive_root("/Volumes/Data/subdir")
    assert not policy.is_drive_root("/Volumes")

    monkeypatch.setattr(policy.sys, "platform", "linux")
    assert policy.is_drive_root("/")
    assert policy.is_drive_root("/mnt/data")
    assert policy.is_drive_root("/media/user/Disk")
    assert policy.is_drive_root("/run/media/me/Disk")
    assert not policy.is_drive_root("/mnt/data/subdir")
    assert not policy.is_drive_root("/home/me")
    assert not policy.is_drive_root("/usr")

    monkeypatch.setattr(
        policy,
        "_is_linux_mount",
        lambda path: path in {"/data", "/srv/archive"},
    )
    assert policy.is_drive_root("/data")
    assert policy.is_drive_root("/srv/archive")
    assert not policy.is_drive_root("/srv/not-a-mount")


def test_linux_drive_root_resolution_uses_live_mounts(monkeypatch):
    from app import main

    monkeypatch.setattr(main.sys, "platform", "linux")
    roots = [main.Path("/"), main.Path("/data"), main.Path("/srv/archive")]

    assert main._drive_root_for("/data/reports/q1.pdf", roots) == main.Path("/data")
    assert main._drive_root_for("/srv/archive/2026/a.md", roots) == main.Path("/srv/archive")
    assert main._drive_root_for("/home/me/note.md", roots) == main.Path("/")


def test_windows_wechat_custom_root_reports_direct_documents(tmp_path, monkeypatch):
    from app.discovery import sources

    root = tmp_path / "WeChat profiles"
    cache = root / "xwechat_files" / "account" / "msg" / "file"
    cache.mkdir(parents=True)
    (root / "received.pdf").write_bytes(b"pdf")
    (cache / "cached.pdf").write_bytes(b"pdf")
    monkeypatch.setattr(sources, "_windows_wechat_roots", lambda: [root])

    found = sources._discover_wechat_windows()
    root_source = next(item for item in found if item.path == str(root))
    assert root_source.name == "微信接收（根目录）"
    assert root_source.file_count == 1
    assert root_source.doc_count == 1


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


def test_disable_hides_files_but_keeps_records(client, tmp_path):
    """停用 = 摘监听 + 从浏览视图隐藏；记录保留，重新启用即恢复。

    用户在设置里停用"桌面/文稿"后，左栏与列表不应再出现它们的文件；
    但库里的记录与索引都留着 —— 重新启用无需重扫。
    """
    d = tmp_path / "src"
    _make_docs(d)
    sid = client.post("/sources/enable", headers=H,
                      json={"name": "S", "path": str(d)}).json()["source_id"]

    before = client.get("/stats", headers=H).json()
    assert before["files"] == 3
    assert [s["name"] for s in before["by_source"]] == ["S"]

    r = client.post("/sources/disable", headers=H, json={"source_id": sid})
    assert r.status_code == 200

    st = client.get("/stats", headers=H).json()
    assert st["files"] == 0, "停用来源的文件不该再计入统计"
    assert st["by_source"] == []
    assert client.get("/files", headers=H).json()["total"] == 0

    s = client.get("/sources", headers=H).json()["sources"][0]
    assert s["enabled"] is False
    assert s["watching"] is False
    assert s["file_count"] == 3, "记录必须保留，停用不等于删除"

    # 重新启用：全部回来，无需重扫
    client.post("/sources/enable", headers=H, json={"name": "S", "path": str(d)})
    assert client.get("/stats", headers=H).json()["files"] == 3
    assert client.get("/files", headers=H).json()["total"] == 3


def test_missing_files_hidden_unless_preserved(client, tmp_path):
    """磁盘上消失的文件从视图隐藏；有保全副本的仍可见（内容还在）；
    文件回到原位后重扫自动恢复可见。重新 enable = 立即重扫。"""
    import sqlite3

    d = tmp_path / "src"
    paths = _make_docs(d)   # 文档0.md 文档1.md 文档2.md
    enable = {"name": "S", "path": str(d)}
    client.post("/sources/enable", headers=H, json=enable)
    assert client.get("/files", headers=H).json()["total"] == 3

    # 文档0 消失（无保全副本）→ 从列表与统计隐藏
    kept = paths[0].read_bytes()
    paths[0].unlink()
    client.post("/sources/enable", headers=H, json=enable)
    files = client.get("/files", headers=H).json()
    assert files["total"] == 2, "无保全副本的消失文件应从列表隐藏"
    assert all(f["name"] != "文档0.md" for f in files["files"])
    assert client.get("/stats", headers=H).json()["files"] == 2

    # 文档1 消失但有保全副本 → 继续显示
    backup = tmp_path / "preserved" / "文档1.md"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(paths[1].read_bytes())
    raw = sqlite3.connect(tmp_path / "t.db", timeout=5)
    raw.execute("UPDATE files SET preserved_path = ? WHERE name = '文档1.md'",
                (str(backup),))
    raw.commit()
    raw.close()
    paths[1].unlink()
    client.post("/sources/enable", headers=H, json=enable)
    names = [f["name"] for f in client.get("/files", headers=H).json()["files"]]
    assert "文档1.md" in names, "有保全副本的消失文件必须继续可见"
    assert "文档0.md" not in names

    # 文档0 回到原位 → 重扫后自动恢复可见
    paths[0].write_bytes(kept)
    client.post("/sources/enable", headers=H, json=enable)
    names = [f["name"] for f in client.get("/files", headers=H).json()["files"]]
    assert "文档0.md" in names, "文件回归后应自动恢复可见"


def test_files_tree_lists_dirs_and_files(client, tmp_path):
    """文件树：根 = 已启用来源；子层给出下级目录（含计数）与文件。"""
    d = tmp_path / "src"
    _make_docs(d)                       # 根下 3 个 md
    _make_docs(d / "报告" / "2026", 2)   # 嵌套目录 2 个
    sid = client.post("/sources/enable", headers=H,
                      json={"name": "S", "path": str(d)}).json()["source_id"]

    roots = client.get("/files/tree", headers=H).json()["roots"]
    assert [r["name"] for r in roots] == ["S"]
    assert roots[0]["count"] == 5

    level = client.get("/files/tree", headers=H,
                       params={"dir": str(d)}).json()
    assert [x["name"] for x in level["dirs"]] == ["报告"]
    assert level["dirs"][0]["count"] == 2
    assert len(level["files"]) == 3

    sub = client.get("/files/tree", headers=H,
                     params={"dir": str(d / "报告" / "2026")}).json()
    assert sub["dirs"] == []
    assert len(sub["files"]) == 2

    # dir 过滤下的文件列表（点目录名 → 中栏递归列出）
    listed = client.get("/files", headers=H,
                        params={"dir": str(d / "报告")}).json()
    assert listed["total"] == 2

    # 停用后树根消失
    client.post("/sources/disable", headers=H, json={"source_id": sid})
    assert client.get("/files/tree", headers=H).json()["roots"] == []


def test_file_content_pagination(client, tmp_path):
    """文件查看器接口：全文分页、顺序稳定、不截断正文。"""
    import os

    d = tmp_path / "src"
    d.mkdir()
    body = "\n\n".join(f"# 第{i}节\n\n" + f"第{i}节的内容。" * 120 for i in range(6))
    (d / "长文.md").write_text(body, encoding="utf-8")
    client.post("/sources/enable", headers=H, json={"name": "S", "path": str(d)})
    client.post("/index/run", headers=H, json={"limit": 50})

    fid = client.get("/files", headers=H).json()["files"][0]["id"]
    first = client.get(f"/files/{fid}/content", headers=H,
                       params={"limit": 2}).json()
    assert first["total"] >= 4
    assert len(first["sections"]) == 2
    assert first["has_more"] is True
    assert all(s["text"] for s in first["sections"])

    rest = client.get(f"/files/{fid}/content", headers=H,
                      params={"offset": 2, "limit": 200}).json()
    assert rest["has_more"] is False
    assert 2 + len(rest["sections"]) == first["total"]

    # 未知文件 404；无内容文件返回空但不报错
    assert client.get("/files/99999/content", headers=H).status_code == 404
    _ = os  # 保留 import 以便后续用例扩展


def test_files_remove_clears_records_but_not_disk(client, tmp_path):
    """按文件移除：删记录与索引、返回磁盘路径，**绝不动磁盘文件**（H2）。"""
    d = tmp_path / "src"
    paths = _make_docs(d)
    client.post("/sources/enable", headers=H, json={"name": "S", "path": str(d)})
    client.post("/index/run", headers=H, json={"limit": 50})
    chunks_before = client.get("/index/status", headers=H).json()["chunks"]
    assert chunks_before > 0

    rows = client.get("/files", headers=H).json()["files"]
    target = next(f for f in rows if f["name"] == "文档0.md")

    r = client.post("/files/remove", headers=H,
                    json={"file_ids": [target["id"]]}).json()
    assert r["removed"] == 1
    assert r["contents_removed"] == 1        # 内容无人共享 → 一并清理
    assert r["paths"] == [target["path"]]

    assert client.get("/files", headers=H).json()["total"] == 2
    assert client.get("/index/status", headers=H).json()["chunks"] < chunks_before
    for p in paths:
        assert p.exists(), "磁盘文件被动了 —— 违反 H2"

    # 幂等：再删同一批 id 不报错
    again = client.post("/files/remove", headers=H,
                        json={"file_ids": [target["id"]]}).json()
    assert again["removed"] == 0


def test_files_remove_keeps_shared_content(client, tmp_path):
    """两个文件共享同一份内容时，删其一必须保留 chunks —— 另一个还要能搜。"""
    d = tmp_path / "src"
    d.mkdir()
    (d / "正本.md").write_text("景德镇青花瓷钴料配比记录。" * 8, encoding="utf-8")
    (d / "副本.md").write_text("景德镇青花瓷钴料配比记录。" * 8, encoding="utf-8")
    client.post("/sources/enable", headers=H, json={"name": "S", "path": str(d)})
    client.post("/index/run", headers=H, json={"limit": 50})

    rows = client.get("/files", headers=H).json()["files"]
    dup = next(f for f in rows if f["name"] == "副本.md")
    r = client.post("/files/remove", headers=H,
                    json={"file_ids": [dup["id"]]}).json()
    assert r["removed"] == 1
    assert r["contents_removed"] == 0, "内容仍被正本引用，不能清"
    assert client.get("/index/status", headers=H).json()["chunks"] > 0


def test_rebase_preserved_rewrites_prefixes(client, tmp_path):
    """数据目录迁移后，库里的保全副本绝对路径要整体换前缀。"""
    d = tmp_path / "src"
    _make_docs(d, 1)
    client.post("/sources/enable", headers=H, json={"name": "S", "path": str(d)})

    import os as _os
    import sqlite3 as _sq
    conn = _sq.connect(_os.environ["ORDO_DB"])
    conn.execute(
        "UPDATE files SET preserved_path = '/old/Ordo/preserved/S/文档0.md'"
    )
    conn.commit()
    conn.close()

    r = client.post("/system/rebase_preserved", headers=H, json={
        "old_prefix": "/old/Ordo", "new_prefix": "/new/place/Ordo",
    }).json()
    assert r["preserved_updated"] == 1

    f = client.get("/files", headers=H).json()["files"][0]
    assert f["preserved_path"] == "/new/place/Ordo/preserved/S/文档0.md"


def test_files_group_by_ext_puts_same_type_together(client, tmp_path):
    """group=ext：同扩展名连续排列，大组在前，组内新文件在上。"""
    import os
    import time as _t

    d = tmp_path / "src"
    d.mkdir()
    now = _t.time()
    for name, age in (("旧.pdf", 300), ("新.pdf", 10), ("单独.txt", 100)):
        p = d / name
        p.write_text("内容", encoding="utf-8")
        os.utime(p, (now - age, now - age))
    client.post("/sources/enable", headers=H, json={"name": "S", "path": str(d)})

    rows = client.get("/files", headers=H,
                      params={"group": "ext", "limit": 100}).json()["files"]
    names = [f["name"] for f in rows]
    # pdf 组（2 个）在 txt 组（1 个）之前；组内按 mtime 新→旧
    assert names == ["新.pdf", "旧.pdf", "单独.txt"]


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
        "hierarchy_routing", "lexical_retrieval", "embed_query",
        "deep_retrieval", "decompose", "scope", "rrf", "rerank",
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
