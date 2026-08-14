"""信息层分类测试（A6）。

锁死的核心语义：
  · 分类纯虚拟 —— 归类操作绝不触碰磁盘
  · 规则永不覆盖用户的手动决定
  · 回流学习：归一个 → 生成规则 → 回溯存量
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INKTABLE_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("INKTABLE_TOKEN", TOKEN)
    import importlib

    from app import main as main_mod
    importlib.reload(main_mod)
    with TestClient(main_mod.app) as c:
        yield c


def _seed(client, tmp_path, names=("a.pdf", "b.pdf", "c.txt")):
    d = tmp_path / "src"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / n).write_text(f"内容 {n}", encoding="utf-8")
    client.post("/sources/enable", headers=H, json={"name": "S", "path": str(d)})
    rows = client.get("/files", headers=H, params={"limit": 100}).json()["files"]
    return d, {f["name"]: f["id"] for f in rows}


def test_category_tree_and_assign(client, tmp_path):
    d, ids = _seed(client, tmp_path)
    work = client.post("/categories", headers=H, json={"name": "工作"}).json()["id"]
    sub = client.post("/categories", headers=H,
                      json={"name": "合同", "parent_id": work}).json()["id"]

    r = client.post("/files/classify", headers=H,
                    json={"file_ids": [ids["a.pdf"]], "category_id": sub})
    assert r.json()["assigned"] == 1

    tree = client.get("/categories", headers=H).json()
    flat = {e["name"]: e for e in tree["tree"]}
    assert flat["合同"]["depth"] == 1
    assert flat["合同"]["file_count"] == 1
    assert flat["工作"]["total_count"] == 1     # 父分类含子树
    assert tree["unclassified"] == 2

    # 按分类过滤（含子分类）
    files = client.get("/files", headers=H,
                       params={"category_id": work}).json()["files"]
    assert [f["name"] for f in files] == ["a.pdf"]


def test_assign_never_touches_disk(client, tmp_path):
    """归类是信息层操作 —— 磁盘一个字节都不能动（v6 定稿）。"""
    d, ids = _seed(client, tmp_path)
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in d.iterdir()}
    cat = client.post("/categories", headers=H, json={"name": "X"}).json()["id"]
    client.post("/files/classify", headers=H,
                json={"file_ids": list(ids.values()), "category_id": cat})

    assert not list(d.glob("X")), "磁盘上出现了分类目录 —— 违背 v6 定稿"
    for p, sig in before.items():
        assert (p.stat().st_size, p.stat().st_mtime_ns) == sig


def test_learn_rule_backfills(client, tmp_path):
    """回流学习：归类一个 pdf + learn_rule → 存量 pdf 全部自动归类。"""
    d, ids = _seed(client, tmp_path)
    cat = client.post("/categories", headers=H, json={"name": "文档"}).json()["id"]

    r = client.post("/files/classify", headers=H, json={
        "file_ids": [ids["a.pdf"]], "category_id": cat, "learn_rule": True,
    }).json()
    assert r["rule_created"] == 1
    assert r["backfilled"] == 1  # b.pdf 被回溯；c.txt 扩展名不同不动

    files = client.get("/files", headers=H,
                       params={"category_id": cat}).json()["files"]
    assert sorted(f["name"] for f in files) == ["a.pdf", "b.pdf"]


def test_rule_applies_to_new_files(client, tmp_path):
    """规则生成后，**新登记**的同类文件自动归类。"""
    d, ids = _seed(client, tmp_path)
    cat = client.post("/categories", headers=H, json={"name": "文档"}).json()["id"]
    client.post("/files/classify", headers=H, json={
        "file_ids": [ids["a.pdf"]], "category_id": cat, "learn_rule": True,
    })

    (d / "新来的.pdf").write_text("新内容", encoding="utf-8")
    client.post("/sources/enable", headers=H, json={"name": "S", "path": str(d)})

    files = client.get("/files", headers=H,
                       params={"category_id": cat}).json()["files"]
    assert "新来的.pdf" in [f["name"] for f in files]


def test_rule_never_overrides_user(client, tmp_path):
    """用户手动归过类的文件，规则回填不许动。"""
    d, ids = _seed(client, tmp_path)
    cat_a = client.post("/categories", headers=H, json={"name": "A"}).json()["id"]
    cat_b = client.post("/categories", headers=H, json={"name": "B"}).json()["id"]

    # 用户把 b.pdf 手动放进 A
    client.post("/files/classify", headers=H,
                json={"file_ids": [ids["b.pdf"]], "category_id": cat_a})
    # 然后用 a.pdf 学了一条 "pdf → B" 的规则
    client.post("/files/classify", headers=H, json={
        "file_ids": [ids["a.pdf"]], "category_id": cat_b, "learn_rule": True,
    })

    b = [f for f in client.get("/files", headers=H, params={"limit": 100}).json()["files"]
         if f["name"] == "b.pdf"][0]
    in_a = client.get("/files", headers=H, params={"category_id": cat_a}).json()["files"]
    assert "b.pdf" in [f["name"] for f in in_a], "规则覆盖了用户的手动归类"


def test_delete_nonempty_rejected(client, tmp_path):
    d, ids = _seed(client, tmp_path)
    cat = client.post("/categories", headers=H, json={"name": "占用"}).json()["id"]
    client.post("/files/classify", headers=H,
                json={"file_ids": [ids["a.pdf"]], "category_id": cat})
    r = client.post("/categories/delete", headers=H, json={"category_id": cat})
    assert r.status_code == 400

    # 移走后可删
    client.post("/files/classify", headers=H,
                json={"file_ids": [ids["a.pdf"]], "category_id": None})
    assert client.post("/categories/delete", headers=H,
                       json={"category_id": cat}).status_code == 200


def test_unclassified_filter(client, tmp_path):
    d, ids = _seed(client, tmp_path)
    cat = client.post("/categories", headers=H, json={"name": "X"}).json()["id"]
    client.post("/files/classify", headers=H,
                json={"file_ids": [ids["a.pdf"]], "category_id": cat})
    rest = client.get("/files", headers=H,
                      params={"unclassified": True, "limit": 100}).json()["files"]
    assert sorted(f["name"] for f in rest) == ["b.pdf", "c.txt"]


def test_auto_ext_classify_groups_unclassified(client, tmp_path):
    # 无扩展名文件在扫描阶段就被忽略，不入库 —— 只测有扩展名的归组
    _d, _ids = _seed(client, tmp_path, names=("a.pdf", "b.PDF", "c.txt"))
    r = client.post("/classify/auto_ext", headers=H).json()
    assert r["classified"] == 3
    assert r["rules_created"] >= 2   # .pdf / .txt

    cats = client.get("/categories", headers=H).json()["tree"]
    names = {c["name"] for c in cats}
    assert "按扩展名" in names
    assert ".pdf" in names
    assert ".txt" in names

    tree = client.get("/categories", headers=H).json()
    assert tree["unclassified"] == 0

    # 大小写不同的扩展名归入同一分类
    pdf_id = next(c["id"] for c in cats if c["name"] == ".pdf")
    files = client.get("/files", headers=H,
                       params={"category_id": pdf_id}).json()["files"]
    assert sorted(f["name"] for f in files) == ["a.pdf", "b.PDF"]
