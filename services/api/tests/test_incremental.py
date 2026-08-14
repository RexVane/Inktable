"""增量嵌入复用测试 —— §12.5 的验收方式。

方案原话：一份 200 页的合同改了一条条款，重嵌入代价应从 200 页降到 1 片。
这里用 30 片验证同一件事：**编码器只收到真正变化的那 1 条文本**。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from app.db.database import connect, init_db
from app.index import embedding as emb
from app.index import vector as vec
from app.index.pipeline import index_pending
from app.watcher.scanner import scan_source


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def source(db, tmp_path):
    db.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, created_at) "
        "VALUES ('S', ?, 'manual', 'manual', ?)",
        (str(tmp_path), time.time()),
    )
    db.commit()
    return 1


def _fake_encode(texts: list[str]) -> np.ndarray:
    """按文本内容决定论生成归一化向量 —— 复用逻辑只关心「谁被编码」。"""
    out = np.zeros((len(texts), emb.DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        rng = np.random.default_rng(abs(hash(t)) % (2 ** 32))
        v = rng.standard_normal(emb.DIM).astype(np.float32)
        out[i] = v / np.linalg.norm(v)
    return out


@pytest.fixture
def encode_log(monkeypatch):
    """伪造 Ollama 可用，并记录每次编码收到的文本数。"""
    calls: list[int] = []

    def spy(self, texts):
        calls.append(len(texts))
        return _fake_encode(texts)

    monkeypatch.setattr(emb, "_probe", lambda force=False: "bge-m3:latest")
    monkeypatch.setattr(emb.Embedder, "encode", spy)
    monkeypatch.setattr(emb, "_instance", None)
    return calls


def _make_doc(path, n=30, changed: int | None = None):
    """n 段互不相同的长段落，每段独立成片（超过分片目标长度）。"""
    paras = []
    for i in range(n):
        body = f"第{i}号段落记录了钧窑窑变工艺的第{i}组实验数据，" * 12
        if i == changed:
            body = f"第{i}号段落已被彻底改写：新增了还原气氛控制的补充说明。" * 12
        paras.append(body)
    path.write_text("\n\n".join(paras), encoding="utf-8")


def test_edit_one_para_encodes_one_chunk(db, source, tmp_path, encode_log):
    """§12.5 验收：30 片改 1 片 → 编码器只收到 1 条文本。"""
    f = tmp_path / "长文.txt"
    _make_doc(f, n=30)
    scan_source(db, source, tmp_path)
    r1 = index_pending(db, limit=10)
    assert r1["indexed"] == 1
    n_chunks = r1["chunks"]
    assert n_chunks >= 25, f"分片数异常：{n_chunks}"
    first_encoded = sum(encode_log)
    assert first_encoded >= n_chunks  # 首次全量编码（含健康检查等杂项调用）

    encode_log.clear()
    time.sleep(1.1)                    # 跨过 mtime 容差
    _make_doc(f, n=30, changed=7)      # 只改第 7 段
    scan_source(db, source, tmp_path)  # 内容变化 → 新 content
    r2 = index_pending(db, limit=10)
    assert r2["indexed"] == 1

    assert sum(encode_log) == 1, (
        f"改 1 片却编码了 {sum(encode_log)} 条 —— 增量复用失效"
    )
    # 旧 content 应被清扫，索引三处无残留
    assert r2["orphans_cleaned"] == 1
    assert db.execute("SELECT count(*) c FROM contents").fetchone()["c"] == 1
    # 每个分片都有向量（复用的 + 新编的）
    n_now = db.execute("SELECT count(*) c FROM chunks").fetchone()["c"]
    assert vec.count(db) == n_now


def test_duplicate_file_zero_encoding(db, source, tmp_path, encode_log):
    """内容完全相同的第二个文件：contents 去重，零编码零新分片。"""
    _make_doc(tmp_path / "a.txt", n=10)
    scan_source(db, source, tmp_path)
    index_pending(db, limit=10)
    encode_log.clear()

    import shutil
    shutil.copy2(tmp_path / "a.txt", tmp_path / "副本.txt")
    scan_source(db, source, tmp_path)
    r = index_pending(db, limit=10)

    assert sum(encode_log) == 0, "相同内容被重复编码"
    assert r["indexed"] == 0          # content 已 indexed，无待处理


def test_same_paragraph_across_files_reused(db, source, tmp_path, encode_log):
    """跨文件的相同段落也复用 —— 内容寻址的副产品。"""
    shared = "两淮盐政的稽核制度在乾隆年间经历了三次重大调整，" * 15
    (tmp_path / "甲.txt").write_text(shared + "\n\n甲文件独有的一段结尾内容。" * 10,
                                     encoding="utf-8")
    scan_source(db, source, tmp_path)
    index_pending(db, limit=10)
    encode_log.clear()

    (tmp_path / "乙.txt").write_text(shared + "\n\n乙文件完全不同的收尾段落文字。" * 10,
                                     encoding="utf-8")
    scan_source(db, source, tmp_path)
    index_pending(db, limit=10)

    # 乙有两片：共享段（复用）+ 独有段（编码）→ 只编 1 条
    assert sum(encode_log) == 1, f"共享段落未复用（编码了 {sum(encode_log)} 条）"


def test_search_still_correct_after_edit(db, source, tmp_path, encode_log):
    """编辑后：旧分片彻底消失，新内容可搜。

    注意向量路**按设计**永远返回最近邻（哪怕不相关 —— 相关性把关在
    置信度标注层，§12.3c 永不硬拒答）。所以这里的不变量不是
    "搜旧词返回空"，而是：
      ① 已删除的旧 chunk id 绝不出现在任何一路
      ② 词法三路（真正做字面匹配的）对旧词全空
    """
    f = tmp_path / "笔记.txt"
    f.write_text("初版结论：龙泉窑梅子青釉需要一点二毫米的釉层厚度。" * 8, encoding="utf-8")
    scan_source(db, source, tmp_path)
    index_pending(db, limit=10)
    old_chunk_ids = {r["id"] for r in db.execute("SELECT id FROM chunks")}

    time.sleep(1.1)
    f.write_text("修订结论：耀州窑刻花青瓷的橄榄绿呈色源自氧化亚铁。" * 8, encoding="utf-8")
    scan_source(db, source, tmp_path)
    index_pending(db, limit=10)

    from app.index.search import search
    r_old = search(db, "梅子青釉 釉层厚度")
    hit_ids = {c for v in r_old.values() for c, _ in v}
    assert hit_ids & old_chunk_ids == set(), "已删除的旧分片仍出现在结果里"
    for route in ("jieba", "trigram", "substr"):
        assert r_old[route] == [], f"词法路 {route} 命中了已删除的内容"

    r_new = search(db, "耀州窑 橄榄绿")
    assert any(r_new[k] for k in ("jieba", "trigram")), "新版内容词法搜不到"
