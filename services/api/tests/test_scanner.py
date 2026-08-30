"""扫描器对抗性测试。

不只验证"能跑通"，而是主动构造会打破设计约束的场景。
每个测试对应方案里的一条硬约束或一个性能关键项。
"""

from __future__ import annotations

import mimetypes
import os
import time

import pytest

from app.db.database import connect, init_db
from app.watcher import scanner
from app.watcher.scanner import scan_source, should_skip_path


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


def test_ingest_extension_whitelist():
    """Only the agreed text/office formats enter the library."""
    for ext in (".txt", ".docx", ".pdf", ".md", ".csv", ".html", ".htm"):
        assert scanner.classify_ext(ext) == "fulltext"
    for ext in (".doc", ".xlsx", ".xls", ".pptx", ".png", ".zip", ".py", ".css"):
        assert scanner.classify_ext(ext) == "ignore"


def test_parse_html_extracts_chat_text():
    """HTML 解析：抽正文、丢 script/style、<br> 断行、保留标题。"""
    import tempfile
    from pathlib import Path

    from app.parsing.parsers import parse_html

    html = (
        "<html><head><title>与张三的聊天记录</title>"
        "<style>.x{color:red}</style></head><body>"
        "<div class='msg'>张三：封阳台报价 680 元每平米</div>"
        "<div class='msg'>我：好的<br>那验收呢</div>"
        "<script>track()</script></body></html>"
    )
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "chat.html"
        p.write_text(html, encoding="utf-8")
        doc = parse_html(p)
    text = "\n".join(b.text for b in doc.blocks)
    assert "封阳台报价 680 元每平米" in text
    assert "那验收呢" in text
    assert "track()" not in text and "color:red" not in text
    assert doc.title == "与张三的聊天记录"


@pytest.fixture
def source(db, tmp_path):
    db.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, created_at) "
        "VALUES ('test', ?, 'manual', 'manual', ?)",
        (str(tmp_path), time.time()),
    )
    db.commit()
    return 1


def test_rename_keeps_identity(db, source, tmp_path, monkeypatch):
    """文件改名后必须复用原记录，且不重新哈希（PLAN §8、§12.1）。

    这是索引模式的核心性能保证：用户整理一次 Downloads 目录，
    若身份追踪失效就要全库重新解析 + 重新嵌入。
    """
    (tmp_path / "a.txt").write_text("内容")
    scan_source(db, source, tmp_path)
    before = db.execute("SELECT id, content_id FROM files").fetchone()

    (tmp_path / "a.txt").rename(tmp_path / "b.txt")
    monkeypatch.setattr(scanner, "hash_file", lambda _p: pytest.fail("纯改名不应重哈希"))
    stats = scan_source(db, source, tmp_path)
    after = db.execute("SELECT id, content_id, name FROM files").fetchone()

    assert after["id"] == before["id"], "改名后 file_id 变了，索引会断"
    assert after["content_id"] == before["content_id"], "改名触发了重新哈希"
    assert after["name"] == "b.txt"
    assert stats.path_updated == 1
    assert stats.registered == 0, "改名被误判为新文件"


def test_same_size_subsecond_edit_is_rehashed(db, source, tmp_path, monkeypatch):
    """同大小编辑即使 mtime 只变化不足一秒，也不能走 unchanged 快路径。"""
    f = tmp_path / "quick.txt"
    f.write_text("AAAA", encoding="utf-8")
    scan_source(db, source, tmp_path)
    before = db.execute("SELECT content_id, mtime FROM files").fetchone()

    f.write_text("BBBB", encoding="utf-8")  # 同字节数、同 inode
    # 用显式时间戳消除调度抖动，可靠复现旧实现的 1 秒容差缺陷。
    edited_mtime = before["mtime"] + 0.25
    import os
    os.utime(f, (edited_mtime, edited_mtime))

    original_hash = scanner.hash_file
    calls = 0

    def counted_hash(path):
        nonlocal calls
        calls += 1
        return original_hash(path)

    monkeypatch.setattr(scanner, "hash_file", counted_hash)
    stats = scan_source(db, source, tmp_path)
    after = db.execute("SELECT content_id, mtime FROM files").fetchone()

    assert calls == 1
    assert stats.content_updated == 1
    assert stats.unchanged == 0
    assert after["content_id"] != before["content_id"]
    assert 0 < after["mtime"] - before["mtime"] < 1


def test_rename_or_atomic_replace_to_ignored_extension_detaches_old_content(
    db, source, tmp_path, monkeypatch
):
    """改名或随后原子替换到 ignore 扩展后，旧全文不能继续挂在该文件上。"""
    original = tmp_path / "searchable.txt"
    original.write_text("曾经可以全文检索的内容", encoding="utf-8")
    scan_source(db, source, tmp_path)
    before = db.execute("SELECT id, content_id FROM files").fetchone()
    assert before["content_id"] is not None

    ignored = tmp_path / "searchable.unknown"
    original.rename(ignored)
    monkeypatch.setattr(scanner, "hash_file", lambda _p: pytest.fail("ignore 扩展不应读取内容"))
    stats = scan_source(db, source, tmp_path)
    after = db.execute(
        "SELECT id, content_id, path, name, ext, mime, state, inode FROM files"
    ).fetchone()

    assert after["id"] == before["id"]
    assert after["content_id"] is None
    assert after["path"] == str(ignored)
    assert after["name"] == ignored.name
    assert after["ext"] == ".unknown"
    assert after["state"] == "ignored"
    assert stats.skipped_ext == 1

    # 同一路径再被新 inode 原子替换，路径兜底也必须继续保持 ignored，
    # 不能因为 identity 变化又把旧全文重新挂回文件。
    old_inode = after["inode"]
    replacement = tmp_path / ".incoming"
    replacement.write_text("新的未知类型内容", encoding="utf-8")
    replacement.replace(ignored)
    scan_source(db, source, tmp_path)
    replaced = db.execute(
        "SELECT id, content_id, inode, state, ext FROM files"
    ).fetchone()
    assert replaced["id"] == before["id"]
    assert replaced["inode"] != old_inode
    assert replaced["content_id"] is None
    assert replaced["state"] == "ignored"
    assert replaced["ext"] == ".unknown"


def test_cross_source_move_refreshes_ownership_and_metadata(db, tmp_path, monkeypatch):
    """跨来源移动仍复用 inode，并把归属及派生字段一起刷新。"""
    source_a_root = tmp_path / "first"
    source_b_root = tmp_path / "second"
    source_a_root.mkdir()
    source_b_root.mkdir()
    source_a = db.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, created_at) "
        "VALUES ('first', ?, 'manual', 'manual', ?)",
        (str(source_a_root), time.time()),
    ).lastrowid
    source_b = db.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, created_at) "
        "VALUES ('second', ?, 'manual', 'manual', ?)",
        (str(source_b_root), time.time()),
    ).lastrowid
    db.commit()

    original = source_a_root / "before.txt"
    original.write_text("同一份内容", encoding="utf-8")
    scan_source(db, source_a, source_a_root)
    before = db.execute(
        "SELECT id, content_id FROM files WHERE path = ?", (str(original),)
    ).fetchone()

    moved = source_b_root / "after.md"
    original.rename(moved)
    # inode 命中不应重新读取/哈希内容；若调用便说明退化为重新入库。
    monkeypatch.setattr(scanner, "hash_file", lambda _p: pytest.fail("跨来源移动不应重哈希"))
    stats = scan_source(db, source_b, source_b_root)
    after = db.execute(
        "SELECT id, content_id, source_id, path, name, ext, mime FROM files"
    ).fetchone()

    assert after["id"] == before["id"]
    assert after["content_id"] == before["content_id"]
    assert after["source_id"] == source_b
    assert after["path"] == str(moved)
    assert after["name"] == "after.md"
    assert after["ext"] == ".md"
    assert after["mime"] == mimetypes.guess_type(moved.name)[0]
    assert stats.path_updated == 1

    # 旧来源随后扫描时不得把已转属的记录误标 missing。
    scan_source(db, source_a, source_a_root)
    final = db.execute(
        "SELECT source_id, state FROM files WHERE id = ?", (after["id"],)
    ).fetchone()
    assert final["source_id"] == source_b
    assert final["state"] == "registered"


def test_hash_failure_keeps_old_content_and_retries(db, source, tmp_path, monkeypatch):
    """临时读取失败不得解除旧索引，下一次扫描必须重新尝试哈希。"""
    f = tmp_path / "doc.txt"
    f.write_text("稳定的旧内容", encoding="utf-8")
    scan_source(db, source, tmp_path)
    old = db.execute("SELECT id, content_id FROM files").fetchone()

    # 改为不同大小，确保首次扫描进入内容更新路径；随后保持相同 metadata，
    # 用来验证 retry 标记不会被 fast path 当成 unchanged。
    f.write_text("这是需要重新读取并重新建立内容哈希的新版本", encoding="utf-8")
    original_hash = scanner.hash_file
    calls = 0

    def flaky_hash(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("临时拒绝读取")
        return original_hash(path)

    monkeypatch.setattr(scanner, "hash_file", flaky_hash)
    first = scan_source(db, source, tmp_path)
    failed = db.execute(
        "SELECT content_id, state, error_code, retry_count FROM files WHERE id = ?", (old["id"],)
    ).fetchone()
    assert first.errors == 1
    assert first.unchanged == 0
    assert failed["content_id"] == old["content_id"], "读取失败不应错误解除旧 content"
    assert failed["state"] == "failed"
    assert failed["error_code"] == "hash_failed"
    assert failed["retry_count"] == 1

    second = scan_source(db, source, tmp_path)
    retried = db.execute(
        "SELECT content_id, state, error_code, retry_count FROM files WHERE id = ?", (old["id"],)
    ).fetchone()
    assert calls == 2, "失败文件被当成 unchanged，未在下一次扫描重试"
    assert second.content_updated == 1
    assert retried["content_id"] != old["content_id"]
    assert retried["state"] == "registered"
    assert retried["error_code"] is None
    assert retried["retry_count"] == 0


def test_path_fallback_reuses_row_after_atomic_replace(db, source, tmp_path):
    """同一路径被原子替换（新 inode）时复用原 file_id，而不是插入重复路径。"""
    target = tmp_path / "atomic.txt"
    target.write_text("旧版本", encoding="utf-8")
    scan_source(db, source, tmp_path)
    before = db.execute(
        "SELECT id, inode, content_id FROM files WHERE path = ?", (str(target),)
    ).fetchone()

    replacement = tmp_path / ".replacement"
    replacement.write_text("原子替换后的全新内容，长度也不同", encoding="utf-8")
    replacement.replace(target)
    stats = scan_source(db, source, tmp_path)
    after = db.execute(
        "SELECT id, inode, content_id FROM files WHERE path = ?", (str(target),)
    ).fetchone()

    assert db.execute("SELECT count(*) c FROM files").fetchone()["c"] == 1
    assert after["id"] == before["id"]
    assert after["inode"] != before["inode"]
    assert after["content_id"] != before["content_id"]
    assert stats.content_updated == 1


def test_live_filter_uses_same_exclusion_and_depth_rules(tmp_path):
    """实时监听与全量扫描必须使用同一套排除/深度规则。"""
    assert should_skip_path(
        tmp_path / "node_modules" / "pkg" / "README.md", tmp_path
    )
    deep = tmp_path
    for i in range(scanner.MAX_DEPTH):
        deep /= f"d{i}"
    assert should_skip_path(deep / "too-deep.txt", tmp_path)


def test_identical_content_shares_row(db, source, tmp_path):
    """内容相同的文件共享一条 contents（PLAN §9 contents 表）。

    这决定了重复文件不会重复解析、重复向量化。
    """
    (tmp_path / "x.txt").write_text("一样的内容")
    (tmp_path / "y.txt").write_text("一样的内容")
    scan_source(db, source, tmp_path)

    files = db.execute("SELECT count(*) c FROM files").fetchone()["c"]
    contents = db.execute("SELECT count(*) c FROM contents").fetchone()["c"]
    assert files == 2
    assert contents == 1, "重复内容没有去重，会重复嵌入"


def test_content_change_detected(db, source, tmp_path):
    """内容变化必须被检测到并重新登记（PLAN §12.5 增量更新的前提）。"""
    f = tmp_path / "doc.txt"
    f.write_text("原内容")
    scan_source(db, source, tmp_path)
    old = db.execute("SELECT content_id FROM files").fetchone()["content_id"]

    time.sleep(1.1)  # 跨过 mtime 的 1 秒容差
    f.write_text("改过的内容")
    stats = scan_source(db, source, tmp_path)
    new = db.execute("SELECT content_id FROM files").fetchone()["content_id"]

    assert new != old, "内容变了但没检测到，索引会陈旧"
    assert stats.content_updated == 1


def test_common_dir_names_are_not_blanket_excluded(db, source, tmp_path):
    """通用名目录不能仅凭名称误杀；真正代码项目由项目标记识别。"""
    for dirname in ("build", "vendor", "dist", "target", "tmp"):
        d = tmp_path / dirname
        d.mkdir()
        (d / f"{dirname}.txt").write_text(dirname + " 里的文件")
    (tmp_path / "real.txt").write_text("真实文件")

    scan_source(db, source, tmp_path)

    total = db.execute("SELECT count(*) c FROM files").fetchone()["c"]
    assert total == 6
    for dirname in ("build", "vendor", "dist", "target", "tmp"):
        hit = db.execute(
            "SELECT count(*) c FROM files WHERE path LIKE ?",
            (f"%{dirname}{os.sep}{dirname}.txt%",),
        ).fetchone()["c"]
        assert hit == 1, f"{dirname} 目录内的文件未被收录"


def test_node_modules_not_descended(db, source, tmp_path):
    """依赖目录里的 README 也不是用户资料，整棵树必须剪掉。"""
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("module.exports = 1")
    (nm / "package.json").write_text("{}")
    (nm / "README.md").write_text("# 这是一个包")
    (tmp_path / "real.txt").write_text("真实文件")

    scan_source(db, source, tmp_path)

    rows = db.execute("SELECT path FROM files").fetchall()
    assert [r["path"] for r in rows] == [str(tmp_path / "real.txt")]


def test_hidden_dirs_are_not_descended(db, source, tmp_path):
    """隐藏目录整棵跳过，不能让其中恰好带文档后缀的文件泄漏。"""
    gitdir = tmp_path / ".git" / "objects"
    gitdir.mkdir(parents=True)
    (gitdir / "leaked.txt").write_text("不应出现")
    (tmp_path / "note.txt").write_text("正常文件")

    scan_source(db, source, tmp_path)

    total = db.execute("SELECT count(*) c FROM files").fetchone()["c"]
    assert total == 1, f"应只收录 note.txt，实际 {total}"


def test_package_dirs_not_descended(db, source, tmp_path):
    """.app / .pages 等 package 目录不能泄漏内部资源。"""
    pkg = tmp_path / "note.pages"
    pkg.mkdir()
    (pkg / "index.xml").write_text("<xml/>")
    (pkg / "preview.png").write_bytes(b"png")
    (pkg / "data.bin").write_bytes(b"\x00")

    scan_source(db, source, tmp_path)
    inside = db.execute(
        "SELECT count(*) c FROM files WHERE path LIKE ?",
        (f"%note.pages{os.sep}%",),
    ).fetchone()["c"]
    assert inside == 0, "package 内部资源不应入库"


def test_code_project_tree_is_pruned_but_explicit_root_is_allowed(db, source, tmp_path):
    """整盘来源排除代码项目；显式选择项目根时仍可收项目文档。"""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]")
    (project / "README.md").write_text("# 包说明")
    (tmp_path / "personal.txt").write_text("个人资料")

    scan_source(db, source, tmp_path)
    assert db.execute("SELECT count(*) c FROM files").fetchone()["c"] == 1

    explicit = db.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, enabled, created_at) "
        "VALUES ('project', ?, 'manual', 'manual', 1, ?)",
        (str(project), time.time()),
    ).lastrowid
    db.commit()
    scan_source(db, explicit, project)
    row = db.execute(
        "SELECT source_id FROM files WHERE path = ?", (str(project / "README.md"),)
    ).fetchone()
    assert row["source_id"] == explicit


def test_broad_source_never_steals_nested_source_files(db, tmp_path):
    """嵌套来源采用最长路径归属，广域补扫不能把文件抢回去。"""
    broad_root = tmp_path / "drive"
    nested_root = broad_root / "QQ profiles"
    nested_root.mkdir(parents=True)
    outside = broad_root / "outside.txt"
    inside = nested_root / "lesson.pdf"
    outside.write_text("盘根资料")
    inside.write_bytes(b"%PDF-1.4 lesson")

    broad_id = db.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, enabled, created_at) "
        "VALUES ('drive', ?, 'manual', 'manual', 1, ?)",
        (str(broad_root), time.time()),
    ).lastrowid
    nested_id = db.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, enabled, created_at) "
        "VALUES ('QQ', ?, 'im', 'manual', 1, ?)",
        (str(nested_root), time.time()),
    ).lastrowid
    db.commit()

    scan_source(db, nested_id, nested_root)
    scan_source(db, broad_id, broad_root)
    assert db.execute(
        "SELECT source_id FROM files WHERE path = ?", (str(inside),)
    ).fetchone()["source_id"] == nested_id

    # 模拟旧版本已把它错归到广域来源；下一次广域补扫也必须主动修正。
    db.execute("UPDATE files SET source_id = ? WHERE path = ?", (broad_id, str(inside)))
    db.commit()
    scan_source(db, broad_id, broad_root)
    assert db.execute(
        "SELECT source_id FROM files WHERE path = ?", (str(inside),)
    ).fetchone()["source_id"] == nested_id


def test_agent_config_docs_skipped_anywhere():
    """第一层：agent / 编辑器机器配置在任何位置都不是知识，无条件挡掉。

    实测真实库里 skill.md 一项就有 265 个副本。
    """
    for name in ["SKILL.md", "skill.md", "AGENTS.md", "CLAUDE.md",
                 "GEMINI.md", "cursorrules.md", "copilot-instructions.md"]:
        assert scanner.should_skip_file_name(name), f"agent 配置未被挡住：{name}"


def test_repo_boilerplate_skipped_only_inside_code_projects(tmp_path):
    """第二层：仓库样板只在代码项目内部才算噪声。

    同一个 README.md 在用户资料目录里可能是他自己写的说明，在 git 仓库里
    就是样板。所以这一层必须结合路径判断，不能只看文件名。

    为什么不能直接对整盘打开代码项目剪枝：实测那样会连 OneDrive / WPSDrive
    里的个人简历、证件、升学材料一起判成"代码项目内"而排除 —— 6266 个可见
    文件里 5409 个会消失。所以只挡样板文件名，不剪整棵项目树。
    """
    root = tmp_path / "drive"
    project = root / "repo"
    project.mkdir(parents=True)
    (project / "package.json").write_text("{}", encoding="utf-8")
    notes = root / "资料"
    notes.mkdir()

    # 仓库里的样板：挡
    for name in ["README.md", "README.en.md", "README_CN.md", "CHANGELOG.md",
                 "CONTRIBUTING.md", "NOTICE.txt"]:
        assert scanner.should_skip_path(project / name, root, check_markers=False), \
            f"仓库样板未被挡住：{name}"

    # 同名文件在普通资料目录里：保留
    for name in ["README.md", "CHANGELOG.md"]:
        assert not scanner.should_skip_path(notes / name, root, check_markers=False), \
            f"误杀资料目录里的同名文件：{name}"

    # 仓库里的真实内容文件：保留（只挡样板名，不剪整棵树）
    for name in ["架构设计.md", "readme笔记.md", "market_report.md",
                 "auth-credential-semantics.md"]:
        assert not scanner.should_skip_path(project / name, root, check_markers=False), \
            f"误杀仓库内的真实内容：{name}"

    # 文件名判定不该误伤用户资料
    for name in ["理论笔记.md", "个人简历.docx", "操作系统实验报告.pdf",
                 "我的readme总结.md", "readme使用说明.md"]:
        assert not scanner.should_skip_file_name(name), f"误杀真实资料：{name}"


def test_install_trees_pruned_even_when_project_pruning_is_off(db, source, tmp_path):
    """安装目录与代码项目**分开门控**：整盘收录关掉代码项目剪枝，但仍剪安装树。

    整盘剪代码项目会误删用户写在带 .git 目录里的真笔记（实测），所以那条关掉。
    但安装树没有这种歧义 —— 没人把个人资料放进 Git 安装目录或 Android SDK，
    而 INSTALL_MARKERS 认的是 git.exe / adb.exe 这类具体可执行文件，不是通用
    目录名。实测只开安装目录剪枝可去掉 1456 个可见文件（Android gradle 缓存
    604、Git mingw64 378…），其中 .pdf 与 .docx 各 0 个。
    """
    root = tmp_path / "drive"
    # 安装树：靠 cmd/git.exe 这个具体标记识别
    install = root / "git" / "Git"
    (install / "cmd").mkdir(parents=True)
    (install / "cmd" / "git.exe").write_text("", encoding="utf-8")
    (install / "usr").mkdir()
    (install / "usr" / "手册.txt").write_text("安装目录里的手册", encoding="utf-8")
    # 代码项目：整盘收录时**不**剪，用户可能把真笔记写在里面
    project = root / "myproject"
    project.mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")
    (project / "设计笔记.md").write_text("用户自己写的笔记", encoding="utf-8")
    # 普通资料
    (root / "资料.txt").write_text("普通资料", encoding="utf-8")

    found = {
        path.name
        for path, _skipped in scanner.iter_files(root, prune_projects=False)
        if path is not None
    }
    assert "资料.txt" in found
    assert "设计笔记.md" in found, "整盘收录不该剪掉代码项目里的真笔记"
    assert "手册.txt" not in found, "安装树应当被剪掉，即使代码项目剪枝是关的"


def test_macos_boot_skip_covers_other_volumes():
    """Walking ``/`` must not recurse into ``/Volumes`` (other disks)."""
    assert "volumes" in scanner.MAC_BOOT_SKIP_DIRS
    assert "system" in scanner.MAC_BOOT_SKIP_DIRS


def test_linux_boot_skip_covers_other_volumes():
    """Walking Linux ``/`` must not recurse into ``/mnt`` / ``/proc``."""
    assert "mnt" in scanner.LINUX_BOOT_SKIP_DIRS
    assert "media" in scanner.LINUX_BOOT_SKIP_DIRS
    assert "proc" in scanner.LINUX_BOOT_SKIP_DIRS
    assert "home" not in scanner.LINUX_BOOT_SKIP_DIRS


def test_scan_never_modifies_files(db, source, tmp_path):
    """扫描绝不改动原文件 —— PLAN §1 第一条不可协商约束。"""
    paths = []
    for i in range(5):
        p = tmp_path / f"f{i}.txt"
        p.write_text(f"内容{i}")
        paths.append(p)
    before = {p: (p.stat().st_size, p.stat().st_mtime) for p in paths}

    scan_source(db, source, tmp_path)

    for p in paths:
        assert p.exists(), f"{p.name} 被删除了"
        assert (p.stat().st_size, p.stat().st_mtime) == before[p], f"{p.name} 被改动了"


def test_stats_reconcile(db, source, tmp_path):
    """统计必须对账：scanned == 各分项之和。

    对不上意味着有文件被静默丢弃 —— 这类 bug 在生产里极难发现。
    """
    (tmp_path / "a.txt").write_text("1")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "c.o").write_bytes(b"obj")  # 白名单外，应被忽略

    stats = scan_source(db, source, tmp_path)
    assert stats.scanned == stats.accounted, (
        f"统计对不上：scanned={stats.scanned} accounted={stats.accounted}"
    )


def test_rescan_is_idempotent(db, source, tmp_path):
    """重复扫描不产生重复记录。"""
    (tmp_path / "a.txt").write_text("内容")
    scan_source(db, source, tmp_path)
    n1 = db.execute("SELECT count(*) c FROM files").fetchone()["c"]

    stats = scan_source(db, source, tmp_path)
    n2 = db.execute("SELECT count(*) c FROM files").fetchone()["c"]

    assert n1 == n2 == 1
    assert stats.unchanged == 1
    assert stats.registered == 0
