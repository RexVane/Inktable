"""cc-switch 供应商导入的解析测试。

用临时 SQLite 构造 cc-switch 的 providers 表，覆盖：
codex TOML 解析、claude env 解析、endpoint 规范化、坏数据跳过、去重。
"""

from __future__ import annotations

import json
import sqlite3

from app.integrations.ccswitch import read_providers

CODEX_TOML = (
    'model_provider = "custom"\n'
    'model = "gpt-5.5"\n'
    "[model_providers.custom]\n"
    'name = "custom"\n'
    'base_url = "https://relay.example.com"\n'
)


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE providers (
               id TEXT, app_type TEXT, name TEXT, settings_config TEXT,
               sort_index INTEGER DEFAULT 0, is_current BOOLEAN DEFAULT 0
           )"""
    )
    conn.executemany(
        "INSERT INTO providers (id, app_type, name, settings_config, is_current) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_read_providers_parses_codex_and_claude(tmp_path):
    db = tmp_path / "cc-switch.db"
    _make_db(db, [
        ("1", "codex", "中转A", json.dumps({
            "auth": {"OPENAI_API_KEY": "sk-codex"},
            "config": CODEX_TOML,
        }), 1),
        ("2", "claude", "中转B", json.dumps({
            "env": {
                "ANTHROPIC_BASE_URL": "https://b.example.com/",
                "ANTHROPIC_AUTH_TOKEN": "sk-claude",
                "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "claude-opus-5",
            },
        }), 0),
        # claude-desktop 与 claude 同端点同密钥 → 去重
        ("3", "claude-desktop", "中转B-桌面", json.dumps({
            "env": {
                "ANTHROPIC_BASE_URL": "https://b.example.com",
                "ANTHROPIC_AUTH_TOKEN": "sk-claude",
            },
        }), 0),
        # 缺密钥 → 跳过
        ("4", "codex", "残缺", json.dumps({"config": CODEX_TOML}), 0),
        # 未知类型 → 跳过
        ("5", "gemini", "G", json.dumps({"env": {}}), 0),
        # 坏 JSON → 跳过
        ("6", "codex", "坏", "not-json", 0),
    ])

    r = read_providers(db)
    assert r["available"] is True
    assert len(r["providers"]) == 2
    skipped_types = {s["app_type"] for s in r["skipped"]}
    assert "gemini" in skipped_types

    codex = r["providers"][0]
    assert codex["name"] == "中转A"
    assert codex["endpoint"] == "https://relay.example.com/v1"
    assert codex["model"] == "gpt-5.5"
    assert "api_key" not in codex
    assert codex["provider_id"]
    assert codex["is_current"] is True
    assert codex["openai_native"] is True

    claude = r["providers"][1]
    assert claude["endpoint"] == "https://b.example.com/v1"
    assert claude["model"] == "claude-opus-5"
    assert "api_key" not in claude
    assert claude["provider_id"]
    assert claude["openai_native"] is False
    assert claude["api_format"] == "anthropic"
    assert codex["api_format"] == "openai"
    assert "sk-codex" not in json.dumps(r)
    assert "sk-claude" not in json.dumps(r)


def test_read_providers_missing_db(tmp_path):
    r = read_providers(tmp_path / "不存在.db")
    assert r == {"available": False, "providers": [], "skipped": []}


def test_read_providers_endpoint_already_v1(tmp_path):
    db = tmp_path / "cc.db"
    toml = 'model = "m1"\nbase_url = "https://api.example.com/v1"\n'
    _make_db(db, [
        ("1", "codex", "已带v1", json.dumps({
            "auth": {"OPENAI_API_KEY": "sk-x"}, "config": toml,
        }), 0),
    ])
    r = read_providers(db)
    assert r["providers"][0]["endpoint"] == "https://api.example.com/v1"


def test_read_providers_parses_opencode(tmp_path):
    db = tmp_path / "cc.db"
    _make_db(db, [
        ("1", "opencode", "LiwanLab", json.dumps({
            "npm": "@ai-sdk/openai-compatible",
            "name": "LiwanLab",
            "options": {
                "baseURL": "https://metapi.example.com/v1",
                "apiKey": "sk-open",
            },
            "models": {"DeepSeek-V4-Flash-0731": {"name": "DeepSeek-V4-Flash-0731"}},
        }), 1),
        ("2", "grokbuild", "default", json.dumps({
            "config": "[cli]\ninstaller = \"internal\"\n",
        }), 0),
    ])
    r = read_providers(db)
    assert len(r["providers"]) == 1
    oc = r["providers"][0]
    assert oc["app_type"] == "opencode"
    assert oc["endpoint"] == "https://metapi.example.com/v1"
    assert oc["model"] == "DeepSeek-V4-Flash-0731"
    assert oc["api_format"] == "openai"
    assert "sk-open" not in json.dumps(r)
    assert any(s["app_type"] == "grokbuild" for s in r["skipped"])
