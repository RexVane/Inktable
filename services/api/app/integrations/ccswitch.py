"""cc-switch 供应商导入 —— 只读它的 SQLite 配置库。

cc-switch（用户用来管理各家大模型中转的桌面工具）把供应商配置存在
`~/.cc-switch/cc-switch.db` 的 `providers` 表里，`settings_config` 是一段
JSON，格式随 app_type 而不同：

  · codex   ：{"auth": {"OPENAI_API_KEY": ...}, "config": "<TOML 字符串>"}
              TOML 里有顶层 `model = "..."` 与
              `[model_providers.*] base_url = "..."`
  · claude* ：{"env": {"ANTHROPIC_BASE_URL": ..., "ANTHROPIC_AUTH_TOKEN": ...,
              "ANTHROPIC_MODEL"/"ANTHROPIC_DEFAULT_*_MODEL*": ...}}

Inktable 的问答走 OpenAI 兼容接口（endpoint + /chat/completions），
这里把两种格式尽力转换成 (endpoint, model, api_key) 三元组；
Anthropic 中转是否兼容 OpenAI 协议因站而异，由用户「检测连接」验证。

**只读**：以 read-only URI 打开，绝不写 cc-switch 的库。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

CCSWITCH_DB = Path.home() / ".cc-switch" / "cc-switch.db"

# claude 类配置里可能承载模型名的 env 键，按优先级取第一个非空
_CLAUDE_MODEL_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
)


def _normalize_endpoint(base_url: str) -> str:
    """把 base_url 规范成 Inktable 期望的 `https://host[/...]/v1` 形式。"""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def _parse_codex(config: dict) -> tuple[str, str, str]:
    api_key = str((config.get("auth") or {}).get("OPENAI_API_KEY") or "")
    toml_text = str(config.get("config") or "")
    model_match = re.search(r'^model\s*=\s*"([^"]+)"', toml_text, re.MULTILINE)
    base_match = re.search(r'^base_url\s*=\s*"([^"]+)"', toml_text, re.MULTILINE)
    model = model_match.group(1) if model_match else ""
    endpoint = _normalize_endpoint(base_match.group(1) if base_match else "")
    return endpoint, model, api_key


def _parse_claude(config: dict) -> tuple[str, str, str]:
    env = config.get("env") or {}
    api_key = str(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "")
    endpoint = _normalize_endpoint(str(env.get("ANTHROPIC_BASE_URL") or ""))
    model = ""
    for key in _CLAUDE_MODEL_KEYS:
        if env.get(key):
            model = str(env[key])
            break
    return endpoint, model, api_key


def read_providers(db_path: Path | str | None = None) -> dict:
    """读取 cc-switch 供应商列表。文件不存在或无法解析时优雅返回空。"""
    path = Path(db_path) if db_path else CCSWITCH_DB
    if not path.is_file():
        return {"available": False, "providers": []}

    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return {"available": False, "providers": []}

    providers: list[dict] = []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT app_type, name, settings_config, is_current "
            "FROM providers ORDER BY is_current DESC, app_type, sort_index, name"
        ).fetchall()
    except sqlite3.Error:
        return {"available": False, "providers": []}
    finally:
        conn.close()

    for row in rows:
        try:
            config = json.loads(row["settings_config"] or "{}")
        except (TypeError, ValueError):
            continue
        app_type = str(row["app_type"] or "")
        if app_type == "codex":
            endpoint, model, api_key = _parse_codex(config)
        elif app_type.startswith("claude"):
            endpoint, model, api_key = _parse_claude(config)
        else:
            continue  # gemini/opencode 等格式各异，先不猜
        if not endpoint or not api_key:
            continue
        # Never return the secret through the sidecar HTTP API. Keep an opaque
        # process-memory handle so a later explicit import request can select
        # it without placing the key in renderer memory/DOM/devtools.
        provider_id = hashlib.sha256(
            f"{endpoint}\0{api_key}".encode("utf-8")
        ).hexdigest()[:24]
        providers.append({
            "provider_id": provider_id,
            "app_type": app_type,
            "name": str(row["name"] or ""),
            "endpoint": endpoint,
            "model": model,
            "is_current": bool(row["is_current"]),
            # Anthropic 中转不保证兼容 OpenAI 协议，前端提示用户检测连接
            "openai_native": app_type == "codex",
        })

    # 去重：公开句柄已包含 secret 指纹但不可逆，不需要向响应附带 key。
    seen: set[str] = set()
    unique: list[dict] = []
    for provider in providers:
        key = provider["provider_id"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(provider)

    return {"available": True, "providers": unique}
