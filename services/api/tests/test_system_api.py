"""系统状态与数据库维护 API 的契约测试。

这些用例故意从 HTTP 层验证：索引状态必须反映实际可恢复副本，数据库维护
端点必须与其余本地 API 使用同一 Bearer 鉴权。若端点尚未实现，测试应明确
以 404 失败，作为待接入契约，而不是静默跳过。
"""

from __future__ import annotations

import hashlib
import importlib
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
