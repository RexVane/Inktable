"""模型槽位契约测试 —— library / embedding 槽位与双协议客户端。

用假 HTTP 服务器打通真实请求路径：openai（/chat/completions + /models）、
ollama（/api/generate + /api/tags）。云端的 https 与密钥约束在这里钉死。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.config import llm_client, models as model_slots
from app.library import enrichment


class _OpenAIFake(BaseHTTPRequestHandler):
    """openai 兼容假服务器：GET /models 给列表；POST /chat/completions 按脚本回话。"""

    scripts: list[str] = []
    seen: list[dict] = []

    def do_GET(self):
        if self.path.endswith("/models"):
            resp = json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _OpenAIFake.seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "body": body,
        })
        text = _OpenAIFake.scripts.pop(0) if _OpenAIFake.scripts else "好的"
        resp = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": text}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass


class _OllamaFake(BaseHTTPRequestHandler):
    """ollama 假服务器：/api/tags 给模型列表；/api/generate、/api/embed 可脚本化。"""

    tags: list[dict] = []
    generate_responses: list[dict] = []
    embed_vectors: list[list[float]] = []
    seen: list[dict] = []

    def do_GET(self):
        if self.path.endswith("/api/tags"):
            resp = json.dumps({"models": _OllamaFake.tags}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _OllamaFake.seen.append({"path": self.path, "body": body})
        if self.path.endswith("/api/generate"):
            data = (_OllamaFake.generate_responses.pop(0)
                    if _OllamaFake.generate_responses else {"response": "完成"})
            resp = json.dumps(data).encode()
        elif self.path.endswith("/api/embed"):
            vec = _OllamaFake.embed_vectors.pop(0) if _OllamaFake.embed_vectors \
                else [0.1] * 1024
            resp = json.dumps({"embeddings": [vec]}).encode()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass


@pytest.fixture
def openai_server():
    _OpenAIFake.scripts = []
    _OpenAIFake.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _OpenAIFake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


@pytest.fixture
def ollama_server():
    _OllamaFake.tags = [{"name": "fake-emb:latest"}]
    _OllamaFake.generate_responses = []
    _OllamaFake.embed_vectors = []
    _OllamaFake.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _OllamaFake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def clean_slots():
    model_slots.clear("library")
    model_slots.clear("embedding")
    model_slots.reset_discovery()
    yield
    model_slots.clear("library")
    model_slots.clear("embedding")
    model_slots.reset_discovery()


# ---------------------------------------------------------------- 配置校验

def test_cloud_endpoint_requires_https():
    with pytest.raises(model_slots.SlotConfigError, match="https"):
        model_slots.configure("library", "openai",
                              "http://api.example.com/v1", "sk-x", "m1")


def test_openai_provider_requires_key():
    with pytest.raises(model_slots.SlotConfigError, match="密钥"):
        model_slots.configure("library", "openai",
                              "https://api.example.com/v1", "", "m1")


def test_embedding_slot_rejects_openai():
    with pytest.raises(model_slots.SlotConfigError, match="Ollama"):
        model_slots.configure("embedding", "openai",
                              "https://api.example.com/v1", "sk-x", "m1")


def test_status_never_contains_key(openai_server):
    model_slots.configure("library", "openai", openai_server,
                          "sk-super-secret", "m1")
    dumped = json.dumps(model_slots.all_status())
    assert "sk-super-secret" not in dumped
    st = model_slots.status("library")
    assert st["configured"] is True and st["provider"] == "openai"
    assert st["has_key"] is True


def test_library_env_fallback(monkeypatch):
    monkeypatch.setenv("INKTABLE_LIBRARY_MODEL", "env-model-7b")
    cfg = model_slots.effective("library")
    assert cfg["provider"] == "ollama"
    assert cfg["model"] == "env-model-7b"


# ------------------------------------------------------------ Ollama 地址探测
#
# 官方默认端口 11434 在 Windows 上常落进 Hyper-V/WSL 的动态保留段，装机时只能
# 改口。按平台写死任一个都会错一半人，所以探测：显式 env 优先，否则取第一个
# 应答的候选口。界面拿 status()["default_ollama"] 当空表单预填值。

def test_discover_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("INKTABLE_OLLAMA_URL", "http://127.0.0.1:6553")
    assert model_slots.discover_ollama_url() == "http://127.0.0.1:6553"


def test_discover_picks_second_candidate_when_first_is_dead(
        monkeypatch, ollama_server):
    """第一个候选口不通就换下一个 —— 这正是 Windows 上 11434 → 18434 的形状。"""
    live_port = int(ollama_server.rsplit(":", 1)[1])
    monkeypatch.delenv("INKTABLE_OLLAMA_URL", raising=False)
    # 1 号是保证没人听的口，2 号是真服务器
    monkeypatch.setattr(model_slots, "_OLLAMA_PORT_CANDIDATES", (6553, live_port))
    assert model_slots.discover_ollama_url(timeout=0.05) == (
        f"http://127.0.0.1:{live_port}")


def test_discover_falls_back_to_official_default_when_nothing_answers(monkeypatch):
    monkeypatch.delenv("INKTABLE_OLLAMA_URL", raising=False)
    monkeypatch.setattr(model_slots, "_OLLAMA_PORT_CANDIDATES", (6553, 6554))
    assert model_slots.discover_ollama_url(timeout=0.05) == (
        model_slots._DEFAULT_OLLAMA_URL)


def test_discover_does_not_cache_a_miss(monkeypatch, ollama_server):
    """失败不能冻成成功：sidecar 常比 Ollama 先起，随后 18434 必须还能探到。"""
    live_port = int(ollama_server.rsplit(":", 1)[1])
    monkeypatch.delenv("INKTABLE_OLLAMA_URL", raising=False)
    monkeypatch.setattr(model_slots, "_OLLAMA_PORT_CANDIDATES", (6553, 6554))
    assert model_slots.discover_ollama_url(timeout=0.05) == (
        model_slots._DEFAULT_OLLAMA_URL)
    monkeypatch.setattr(model_slots, "_DISCOVER_MISS_TTL", 0.0)
    monkeypatch.setattr(model_slots, "_OLLAMA_PORT_CANDIDATES", (6553, live_port))
    assert model_slots.discover_ollama_url(timeout=0.05) == (
        f"http://127.0.0.1:{live_port}")


def test_status_reports_default_ollama_for_unconfigured_slot(monkeypatch):
    """界面靠这个字段预填地址；缺了它就会退回自己硬写的常量。"""
    monkeypatch.delenv("INKTABLE_OLLAMA_URL", raising=False)
    monkeypatch.setattr(model_slots, "_discovered_ollama",
                        "http://127.0.0.1:18434")
    model_slots.clear("embedding")
    st = model_slots.status("embedding")
    assert st["configured"] is False
    assert st["default_ollama"] == "http://127.0.0.1:18434"


# ---------------------------------------------------------------- 双协议客户端

def test_llm_client_openai_path(openai_server):
    _OpenAIFake.scripts = ["  最终回答  "]
    out = llm_client.generate_text("你好", cfg={
        "provider": "openai", "endpoint": openai_server,
        "api_key": "sk-fake", "model": "m1"})
    assert out == "最终回答"
    sent = _OpenAIFake.seen[0]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["auth"] == "Bearer sk-fake"
    assert sent["body"]["model"] == "m1"
    assert sent["body"]["messages"][0]["content"] == "你好"


def test_llm_client_ollama_path(ollama_server):
    _OllamaFake.generate_responses = [{"response": "<think>内心戏</think>结果"}]
    out = llm_client.generate_text("你好", cfg={
        "provider": "ollama", "endpoint": ollama_server,
        "api_key": "", "model": "fake-emb:latest"}, json_mode=True)
    assert out == "结果"
    sent = _OllamaFake.seen[0]
    assert sent["path"] == "/api/generate"
    assert sent["body"]["format"] == "json"
    assert sent["body"]["model"] == "fake-emb:latest"


def test_list_models_both_protocols(openai_server, ollama_server):
    assert llm_client.list_models(provider="openai", endpoint=openai_server,
                                  api_key="sk-x") == ["model-a", "model-b"]
    assert llm_client.list_models(provider="ollama", endpoint=ollama_server) \
        == ["fake-emb:latest"]


# ---------------------------------------------------------------- 富化槽位分派

def test_enrichment_follows_library_slot(openai_server):
    model_slots.configure("library", "openai", openai_server,
                          "sk-fake", "m1")
    st = enrichment.status()
    assert st["provider"] == "openai" and st["cloud"] is True
    assert enrichment.model_available() is True

    _OpenAIFake.scripts = [json.dumps({
        "summary": "摘要", "language": "zh", "category_id": 1, "tag_ids": []},
        ensure_ascii=False)]
    raw = enrichment._configured_generate("生成 JSON")
    assert "摘要" in raw
    sent = _OpenAIFake.seen[0]
    assert sent["auth"] == "Bearer sk-fake"
    assert sent["body"]["max_tokens"] == 900


def test_enrichment_ollama_slot_matches_configured_model(ollama_server):
    model_slots.configure("library", "ollama", ollama_server, "", "fake-emb")
    assert enrichment.model_available() is True
    model_slots.configure("library", "ollama", ollama_server, "", "absent-model")
    assert enrichment.model_available() is False
