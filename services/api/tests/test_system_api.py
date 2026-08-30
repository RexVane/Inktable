"""系统状态与数据库维护 API 的契约测试。

这些用例故意从 HTTP 层验证：索引状态必须反映实际可恢复副本，数据库维护
端点必须与其余本地 API 使用同一 Bearer 鉴权。若端点尚未实现，测试应明确
以 404 失败，作为待接入契约，而不是静默跳过。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TOKEN = "system-api-test-token"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("INKTABLE_DB", str(tmp_path / "library.db"))
    monkeypatch.setenv("INKTABLE_TOKEN", TOKEN)

    from app import main as main_mod

    # 其他模块级 TestClient fixture 可能已经创建过连接；隔离本用例的数据库。
    if main_mod._db is not None:
        main_mod._db.close()
        main_mod._db = None
    if main_mod._watch is not None:
        main_mod._watch.stop()
        main_mod._watch = None
    importlib.reload(main_mod)
    with TestClient(main_mod.app) as client:
        yield client, main_mod


def test_metadata_ask_uses_sql_without_rag(api_client, monkeypatch):
    client, main_mod = api_client
    conn = main_mod.db()
    conn.execute(
        "INSERT INTO sources(name,path,kind,discovered_by,enabled,created_at) VALUES('B盘','B:\\\\','system','fixed_drive',1,?)",
        (time.time(),),
    )
    conn.execute(
        """INSERT INTO files(volume_uuid,inode,path,name,source_id,ext,size,state,detected_at)
           VALUES('v',1,'B:\\\\a.pdf','a.pdf',1,'.pdf',1,'registered',?)""",
        (time.time(),),
    )
    conn.commit()
    import app.qa.answer as answer_module
    monkeypatch.setattr(
        answer_module, "ask",
        lambda *_args, **_kwargs: pytest.fail("metadata route called RAG/LLM"),
    )

    response = client.post(
        "/ask", headers=H, json={"question": "我的 PDF 文件有多少个？"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "metadata"
    assert body["answer_source"] == "metadata"
    assert body["used_personal_files"] is False
    assert "1 个" in body["answer"]
    assert body["citations"] == []


def test_streaming_ask_finalizes_before_citations(api_client, monkeypatch):
    client, _main = api_client
    import app.qa.answer as answer_module

    def fake_ask(_conn, _question, _book_id=None, history=None,
                 event_callback=None, qa_mode="deep"):
        assert event_callback is not None
        event_callback("chat.draft", {"text": "错误草稿", "attempt": 1})
        event_callback("chat.regenerating", {"attempt": 2})
        event_callback("chat.draft", {"text": "最终答案 [C1]", "attempt": 2})
        return answer_module.Answer(
            status="answered", answer="最终答案 [C1]",
            citations=[{"tag": "C1", "file_name": "a.md"}],
            validation={"attempts": 2}, trace={"trace_id": "trace", "stages": [], "degraded": []},
        )

    monkeypatch.setattr(answer_module, "ask", fake_ask)
    response = client.post(
        "/ask/stream", headers=H, json={"question": "解释实验结论"},
    )
    assert response.status_code == 200
    text = response.text
    order = [
        text.index("event: chat.searching"),
        text.index("event: chat.draft"),
        text.index("event: chat.regenerating"),
        text.rindex("event: chat.draft"),
        text.index("event: chat.finalize"),
        text.index("event: chat.citations"),
    ]
    assert order == sorted(order)
    assert '"citations"' not in text[text.index("event: chat.finalize"):text.index("event: chat.citations")]


def test_runs_endpoint_replays_recent_trace(api_client):
    """/runs/{trace_id}：检索后可回读同一 trace；未知 id 是 404；必须带鉴权。"""
    client, _main = api_client

    search = client.post("/search", headers=H, json={"q": "任意 问题"}).json()
    trace_id = search["trace_id"]
    assert trace_id

    replay = client.get(f"/runs/{trace_id}", headers=H)
    assert replay.status_code == 200
    body = replay.json()
    assert body["trace_id"] == trace_id
    assert [stage["name"] for stage in body["stages"]] == [
        stage["name"] for stage in search["timings"]
    ]
    # trace 不含查询原文（隐私口径：只有 id/score/timing）
    assert "任意" not in str(body)

    assert client.get("/runs/does-not-exist", headers=H).status_code == 404
    assert client.get(f"/runs/{trace_id}").status_code in (401, 403)


def _insert_pending_file(
    main_mod,
    *,
    file_id: int,
    name: str,
    ext: str,
    state: str,
    original_path: Path,
    preserved_path: Path | None,
) -> int:
    """直接构造 pending content，避免扫描器读取测试中故意缺失的原件。"""
    conn = main_mod.db()
    payload = f"pending-{file_id}".encode()
    digest = hashlib.sha256(payload).hexdigest()
    content_id = conn.execute(
        "INSERT INTO contents (sha256, size, parse_state) VALUES (?, ?, 'pending')",
        (digest, len(payload)),
    ).lastrowid
    conn.execute(
        """INSERT INTO files
           (id, volume_uuid, inode, content_id, path, name, preserved_path, ext,
            size, state, mtime, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            file_id,
            f"vol-{file_id}",
            10_000 + file_id,
            content_id,
            str(original_path),
            name,
            str(preserved_path) if preserved_path is not None else None,
            ext,
            len(payload),
            state,
            time.time(),
            time.time(),
        ),
    )
    conn.commit()
    return content_id


def test_index_status_counts_readable_preserved_copy_for_missing_file(
    api_client, tmp_path
) -> None:
    client, main_mod = api_client
    preserved = tmp_path / "preserved" / "document.txt"
    preserved.parent.mkdir()
    preserved.write_text("保全副本仍可用于解析", encoding="utf-8")
    _insert_pending_file(
        main_mod,
        file_id=1,
        name="document.txt",
        ext=".txt",
        state="missing",
        original_path=tmp_path / "gone" / "document.txt",
        preserved_path=preserved,
    )

    response = client.get("/index/status", headers=H)

    assert response.status_code == 200
    assert response.json()["pending"] == 1


@pytest.mark.parametrize("preserved_kind", ["missing", "directory", "empty"])
def test_index_status_ignores_unusable_preserved_path(
    api_client, tmp_path, preserved_kind
) -> None:
    client, main_mod = api_client
    if preserved_kind == "missing":
        preserved = tmp_path / "does-not-exist.txt"
    elif preserved_kind == "directory":
        preserved = tmp_path / "not-a-file.txt"
        preserved.mkdir()
    else:
        preserved = None

    _insert_pending_file(
        main_mod,
        file_id=1,
        name="document.txt",
        ext=".txt",
        state="missing",
        original_path=tmp_path / "gone" / "document.txt",
        preserved_path=preserved,
    )

    response = client.get("/index/status", headers=H)

    assert response.status_code == 200
    assert response.json()["pending"] == 0


@pytest.mark.parametrize(
    "endpoint", ["/db/integrity_check", "/db/backup", "/settings/llm/test"],
)
def test_database_maintenance_endpoints_require_bearer_auth(api_client, endpoint) -> None:
    client, _main_mod = api_client

    assert client.post(endpoint).status_code == 401
    assert client.post(
        endpoint, headers={"Authorization": "Bearer wrong-token"}
    ).status_code == 401


def test_model_probe_api_is_authenticated_and_never_echoes_key(
    api_client, monkeypatch,
) -> None:
    client, _main_mod = api_client
    from app.qa import llm

    llm.configure("https://models.example.test/v1", "api-secret", "test-model")
    monkeypatch.setattr(llm, "chat", lambda *args, **kwargs: "OK")
    try:
        response = client.post("/settings/llm/test", headers=H)
    finally:
        llm.configure("", "", "")

    assert response.status_code == 200
    assert response.json()["code"] == "ready"
    assert response.json()["has_key"] is True
    assert "api-secret" not in response.text


def test_database_integrity_check_api_contract(api_client) -> None:
    client, _main_mod = api_client

    response = client.post("/db/integrity_check", headers=H)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["results"] == ["ok"]


def test_database_backup_api_creates_restorable_snapshot(api_client) -> None:
    client, main_mod = api_client
    from app.db.database import backup_is_restorable

    main_mod.db().execute(
        "INSERT INTO categories (name, sort_order) VALUES ('API 备份标记', 0)"
    )
    main_mod.db().commit()

    response = client.post("/db/backup", headers=H)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    backup_path = Path(body["path"])
    assert backup_path.is_file()
    assert backup_is_restorable(backup_path)


def test_index_version_api_lists_and_activates_completed_version(
    api_client, tmp_path, monkeypatch,
) -> None:
    client, main_mod = api_client
    from app.index import embedding as emb
    from app.index.pipeline import index_content

    path = tmp_path / "versioned.md"
    content_id = _insert_pending_file(
        main_mod,
        file_id=1,
        name=path.name,
        ext=".md",
        state="registered",
        original_path=path,
        preserved_path=None,
    )
    monkeypatch.setattr(emb, "is_available", lambda: False)
    path.write_text("# 第一版\n\n" + "旧版知识。" * 80, encoding="utf-8")
    index_content(main_mod.db(), content_id, path)
    main_mod.db().commit()
    path.write_text("# 第二版\n\n" + "新版知识。" * 80, encoding="utf-8")
    index_content(main_mod.db(), content_id, path)
    main_mod.db().commit()

    listed = client.get(
        "/index/versions", headers=H, params={"content_id": content_id},
    )

    assert listed.status_code == 200
    body = listed.json()
    assert body["active_version"] == 2
    assert [item["status"] for item in body["versions"]] == [
        "active", "superseded",
    ]

    activated = client.post(
        "/index/activate", headers=H,
        json={"content_id": content_id, "version": 1},
    )

    assert activated.status_code == 200
    assert activated.json()["previous_version"] == 2
    assert activated.json()["active_version"] == 1


@pytest.mark.parametrize("endpoint", [
    "/index/versions?content_id=1", "/index/activate",
])
def test_index_version_endpoints_require_auth(api_client, endpoint) -> None:
    client, _main_mod = api_client
    if endpoint == "/index/activate":
        response = client.post(endpoint, json={"content_id": 1, "version": 1})
    else:
        response = client.get(endpoint)
    assert response.status_code == 401


# ---------------------------------------------------------------- 模型槽位


@pytest.fixture
def clean_model_slots():
    from app.config import models as model_slots

    model_slots.clear("library")
    model_slots.clear("embedding")
    yield
    model_slots.clear("library")
    model_slots.clear("embedding")


# ---------------------------------------------------------------- 回答档位（快速 / 深度）


def test_ask_rejects_unknown_mode(api_client):
    client, _main_mod = api_client
    response = client.post(
        "/ask", headers=H,
        json={"question": "汝窑的烧成温度是多少？", "mode": "bogus"},
    )
    assert response.status_code == 422


def test_ask_mode_passed_through_to_pipeline(api_client, monkeypatch):
    client, main_mod = api_client
    import app.qa.answer as answer_module

    captured = {}

    class _FakeAnswer:
        status = "answered"
        answer = "好"
        citations: list = []
        retrieved: list = []
        hedge = ""
        validation = {"qa_mode": "deep"}
        mode = "knowledge"
        trace = {}

    def fake_ask(conn, q, book_id=None, history=None,
                 event_callback=None, qa_mode="deep"):
        captured["qa_mode"] = qa_mode
        return _FakeAnswer()

    monkeypatch.setattr(answer_module, "ask", fake_ask)

    quick = client.post("/ask", headers=H,
                        json={"question": "汝窑的烧成温度是多少？", "mode": "quick"})
    assert quick.status_code == 200
    assert captured["qa_mode"] == "quick"

    default = client.post("/ask", headers=H,
                          json={"question": "两淮盐政的稽核制度？"})
    assert default.status_code == 200
    assert captured["qa_mode"] == "deep"


def test_model_slots_set_get_clear(api_client, clean_model_slots):
    client, _main_mod = api_client
    # 向量槽位只接受本地 Ollama
    bad = client.post("/settings/models", headers=H, json={
        "slot": "embedding", "provider": "openai",
        "endpoint": "https://api.example.com/v1", "api_key": "sk-x", "model": "m"})
    assert bad.status_code == 422

    saved = client.post("/settings/models", headers=H, json={
        "slot": "library", "provider": "openai",
        "endpoint": "https://api.example.com/v1", "api_key": "sk-slot-key",
        "model": "organizer-1"})
    assert saved.status_code == 200
    assert saved.json()["configured"] is True
    assert saved.json()["model"] == "organizer-1"

    slots = client.get("/settings/models", headers=H).json()["slots"]
    assert set(slots) == {"qa", "library", "embedding"}
    assert slots["library"]["provider"] == "openai"
    assert "sk-slot-key" not in json.dumps(slots)

    cleared = client.post("/settings/models", headers=H, json={
        "slot": "library", "clear": True})
    assert cleared.status_code == 200
    assert cleared.json()["configured"] is False


def test_model_slot_list_uses_saved_credentials(api_client, clean_model_slots, monkeypatch):
    client, main_mod = api_client
    from app.config import models as model_slots

    # 假 openai /models 服务器
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            resp = json.dumps({"data": [{"id": "m-1"}, {"id": "m-2"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        saved = client.post("/settings/models", headers=H, json={
            "slot": "library", "provider": "openai",
            "endpoint": f"http://127.0.0.1:{srv.server_port}/v1",
            "api_key": "sk-from-save", "model": "m-1"})
        assert saved.status_code == 200
        # 表单不带密钥（密钥不回显到前端）→ 服务端用已保存的密钥拉列表
        listed = client.post("/settings/models/list", headers=H, json={
            "slot": "library"})
        assert listed.status_code == 200
        assert listed.json()["models"] == ["m-1", "m-2"]
        assert model_slots.get("library")["api_key"] == "sk-from-save"
    finally:
        srv.shutdown()


def test_model_slot_list_never_reuses_key_for_another_origin(
    api_client, clean_model_slots, monkeypatch,
):
    client, _main_mod = api_client
    from app.config import llm_client

    saved = client.post("/settings/models", headers=H, json={
        "slot": "library",
        "provider": "openai",
        "endpoint": "https://api.example.com/v1",
        "api_key": "origin-bound-secret",
        "model": "m-1",
    })
    assert saved.status_code == 200
    monkeypatch.setattr(
        llm_client,
        "list_models",
        lambda **_kwargs: pytest.fail("cross-origin request must not receive saved key"),
    )

    response = client.post("/settings/models/list", headers=H, json={
        "slot": "library",
        "provider": "openai",
        "endpoint": "https://other.example/v1",
    })

    assert response.status_code == 422
    assert "API 密钥" in response.json()["detail"]
    assert "origin-bound-secret" not in response.text


def test_model_slot_list_requires_endpoint(api_client, clean_model_slots):
    """openai 格式没地址必须 422；ollama 格式空地址回退默认本机地址。"""
    client, _main_mod = api_client
    r = client.post("/settings/models/list", headers=H,
                    json={"slot": "library", "provider": "openai"})
    assert r.status_code == 422
    ok = client.post("/settings/models/list", headers=H,
                     json={"slot": "embedding", "provider": "ollama"})
    # 本机 Ollama 在跑 → 200；没在跑 → 502；都不应再报"地址为空"
    assert ok.status_code in (200, 502)


# ---------------------------------------------------------------- 原文查看器字节端点


def test_files_raw_serves_registered_bytes(api_client, tmp_path):
    client, main_mod = api_client
    conn = main_mod.db()
    source = conn.execute(
        "INSERT INTO sources(name,path,kind,discovered_by,enabled,created_at) "
        "VALUES('B盘','B:\\\\','system','fixed_drive',1,?)", (time.time(),))
    conn.commit()
    source_id = source.lastrowid
    doc = tmp_path / "讲义.pdf"
    doc.write_bytes(b"%PDF-1.4 test-bytes")
    fid = conn.execute(
        """INSERT INTO files(volume_uuid,inode,path,name,source_id,ext,size,state,detected_at)
           VALUES('v',1,?,'讲义.pdf',?,'.pdf',?,'registered',?)""",
        (str(doc), source_id, doc.stat().st_size, time.time())).lastrowid
    conn.commit()

    resp = client.get(f"/files/{fid}/raw", headers=H)
    assert resp.status_code == 200
    assert b"%PDF-1.4 test-bytes" in resp.content
    assert resp.headers["content-type"].startswith("application/pdf")

    assert client.get("/files/999999/raw", headers=H).status_code == 404
    assert client.get(f"/files/{fid}/raw").status_code == 401


def test_qa_slot_accepts_ollama_provider(api_client, clean_model_slots):
    client, _main_mod = api_client
    r = client.post("/settings/models", headers=H, json={
        "slot": "qa", "provider": "ollama",
        "endpoint": "http://127.0.0.1:11434", "model": "qwen3:8b"})
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "ollama"
    assert body["configured"] is True      # 本地 Ollama 免密钥也算已配置
    assert client.get("/settings/llm", headers=H).json()["provider"] == "ollama"
