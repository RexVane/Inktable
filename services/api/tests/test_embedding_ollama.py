"""Ollama 嵌入集成测试 —— 起真实 HTTP 假服务器，不 mock urllib。

覆盖：模型探测与缓存、批量编码与归一化、服务缺失时的降级、
以及换模型导致的向量表维度迁移。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

from app.index import embedding as emb


class _FakeOllama(BaseHTTPRequestHandler):
    """可脚本化的假 Ollama：/api/tags 返回模型列表，/api/embed 返回向量。"""

    models: list[str] = ["bge-m3:latest"]
    embed_requests: list[dict] = []
    dim: int = emb.DIM

    def do_GET(self):
        if self.path == "/api/tags":
            body = json.dumps(
                {"models": [{"name": n} for n in self.models]}
            ).encode()
            self._reply(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/embed":
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            _FakeOllama.embed_requests.append(req)
            texts = req.get("input", [])
            vecs = []
            for i, _t in enumerate(texts):
                v = [0.0] * self.dim
                v[i % self.dim] = 3.0   # 未归一化，验证客户端会归一化
                vecs.append(v)
            self._reply(json.dumps({"embeddings": vecs}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def _reply(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_ollama(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _FakeOllama)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _FakeOllama.models = ["bge-m3:latest"]
    _FakeOllama.embed_requests = []
    _FakeOllama.dim = emb.DIM
    monkeypatch.setattr(emb, "_OLLAMA_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(emb, "_instance", None)
    emb._probe_cache.update(at=0.0, tag=None)
    yield srv
    srv.shutdown()
    emb._probe_cache.update(at=0.0, tag=None)


def test_probe_finds_bge_m3(fake_ollama):
    assert emb.is_available() is True
    assert emb._probe() == "bge-m3:latest"


def test_probe_misses_without_model(fake_ollama):
    _FakeOllama.models = ["llama3:8b"]
    emb._probe_cache.update(at=0.0, tag=None)
    assert emb.is_available() is False
    with pytest.raises(emb.EmbeddingUnavailable):
        emb.Embedder()


def test_probe_unreachable_is_unavailable(monkeypatch):
    monkeypatch.setattr(emb, "_OLLAMA_URL", "http://127.0.0.1:1")   # 无人监听
    emb._probe_cache.update(at=0.0, tag=None)
    assert emb.is_available() is False
    emb._probe_cache.update(at=0.0, tag=None)


def test_probe_result_is_cached(fake_ollama):
    assert emb.is_available() is True
    _FakeOllama.models = []          # 服务端变了，但缓存未过期
    assert emb.is_available() is True
    emb._probe_cache.update(at=0.0, tag=None)
    assert emb.is_available() is False


def test_encode_normalizes_and_batches(fake_ollama, monkeypatch):
    monkeypatch.setattr(emb, "BATCH", 2)
    m = emb.get_embedder()
    v = m.encode(["a", "b", "c"])
    assert v.shape == (3, emb.DIM)
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)
    # BATCH=2 → 3 条文本分两个请求
    assert [len(r["input"]) for r in _FakeOllama.embed_requests] == [2, 1]
    assert all(r["model"] == "bge-m3:latest" for r in _FakeOllama.embed_requests)


def test_encode_failure_raises_unavailable(fake_ollama, monkeypatch):
    m = emb.get_embedder()
    # 构造完成后服务"挂掉"：编码请求指向无人监听的端口
    monkeypatch.setattr(emb, "_OLLAMA_URL", "http://127.0.0.1:1")
    with pytest.raises(emb.EmbeddingUnavailable):
        m.encode(["文本"])


def test_model_id_is_stable_across_tags(fake_ollama):
    _FakeOllama.models = ["bge-m3:567m"]
    emb._probe_cache.update(at=0.0, tag=None)
    m = emb.Embedder()
    assert m.tag == "bge-m3:567m"
    assert m.model_id == f"ollama-bge-m3-d{emb.DIM}"   # tag 不进 model_id


def test_vec_table_dim_migration(monkeypatch):
    """换模型维度后：旧表重建、embedding_model_id 清空、待回填。"""
    from app.db.database import connect, init_db

    conn = connect(":memory:")
    init_db(conn)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'chunks_vec'"
    ).fetchone()
    if row is None:
        pytest.skip("sqlite-vec 不可用")
    assert f"float[{emb.DIM}]" in row["sql"]

    # 伪造一张旧 256 维表 + 一条已嵌入记录
    conn.execute("DROP TABLE chunks_vec")
    conn.execute("CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[256])")
    conn.execute(
        "INSERT INTO contents (sha256, size, embedding_model_id) "
        "VALUES ('h', 1, 'legacy-d256')"
    )
    conn.execute(
        "INSERT INTO chunks (content_id, ordinal, text, text_hash, embedding_model_id) "
        "VALUES (1, 0, 't', 'th', 'legacy-d256')"
    )
    conn.commit()

    from app.db.database import _init_vec_table
    _init_vec_table(conn)

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'chunks_vec'"
    ).fetchone()
    assert f"float[{emb.DIM}]" in row["sql"]
    assert conn.execute(
        "SELECT count(*) c FROM chunks WHERE embedding_model_id IS NOT NULL"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT count(*) c FROM contents WHERE embedding_model_id IS NOT NULL"
    ).fetchone()["c"] == 0
    conn.close()
