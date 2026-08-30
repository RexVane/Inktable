"""cc-switch 供应商导入 —— 只读它的 SQLite 配置库。

cc-switch（用户用来管理各家大模型中转的桌面工具）把供应商配置存在
`~/.cc-switch/cc-switch.db` 的 `providers` 表里，`settings_config` 是一段
JSON，格式随 app_type 而不同：

  · codex   ：{"auth": {"OPENAI_API_KEY": ...}, "config": "<TOML 字符串>"}
              TOML 里有顶层 `model = "..."` 与
              `[model_providers.*] base_url = "..."`
  · claude* ：{"env": {"ANTHROPIC_BASE_URL": ..., "ANTHROPIC_AUTH_TOKEN": ...,
              "ANTHROPIC_MODEL"/"ANTHROPIC_DEFAULT_*_MODEL*": ...}}
  · opencode：{"options": {"baseURL": ..., "apiKey": ...}, "models": {...}}
  · gemini  ：{"env": {"GEMINI_API_KEY": ..., "GEMINI_BASE_URL"/GOOGLE_* : ...}}
  · grokbuild：TOML config 里 [model.'…'] 的 base_url / api_key / model
              （纯 marketplace CLI、没有密钥的条目会跳过）

把能抽出 (endpoint, model, api_key) 的条目转成导入项，并标注
api_format（openai / anthropic），让设置页下拉框对上协议，而不是
一律当 OpenAI chat/completions。

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


def _parse_opencode(config: dict) -> tuple[str, str, str]:
    """OpenCode 供应商：options.baseURL + options.apiKey，模型取 models 的第一个键。"""
    options = config.get("options") or {}
    if not isinstance(options, dict):
        options = {}
    api_key = str(options.get("apiKey") or options.get("api_key") or "")
    endpoint = _normalize_endpoint(str(options.get("baseURL") or options.get("base_url") or ""))
    model = ""
    models = config.get("models")
    if isinstance(models, dict) and models:
        first = next(iter(models))
        entry = models.get(first) or {}
        model = str((entry.get("name") if isinstance(entry, dict) else "") or first)
    return endpoint, model, api_key


def _parse_gemini(config: dict) -> tuple[str, str, str]:
    env = config.get("env") or {}
    if not isinstance(env, dict):
        env = {}
    api_key = str(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or "")
    endpoint = _normalize_endpoint(str(
        env.get("GOOGLE_GEMINI_BASE_URL")
        or env.get("GEMINI_BASE_URL")
        or env.get("GOOGLE_GEMINI_API_BASE")
        or ""))
    model = str(env.get("GEMINI_MODEL") or env.get("GOOGLE_GEMINI_MODEL") or "")
    return endpoint, model, api_key


def _parse_grokbuild(config: dict) -> tuple[str, str, str]:
    """Grok Build 第三方供应商把密钥写在 TOML 的 [model.'…'] 段里。

    官方 marketplace 默认配置没有 api_key，解析结果为空，调用方会跳过。
    """
    toml_text = str(config.get("config") or "")
    api_key = ""
    key_match = re.search(r'^api_key\s*=\s*"([^"]+)"', toml_text, re.MULTILINE)
    if key_match:
        api_key = key_match.group(1)
    base_match = re.search(r'^base_url\s*=\s*"([^"]+)"', toml_text, re.MULTILINE)
    model_match = re.search(
        r"^\[models\][\s\S]*?^default\s*=\s*'([^']+)'", toml_text, re.MULTILINE)
    if not model_match:
        model_match = re.search(r"^\[model\.'([^']+)'\]", toml_text, re.MULTILINE)
    if not model_match:
        model_match = re.search(r'^model\s*=\s*"([^"]+)"', toml_text, re.MULTILINE)
    model = model_match.group(1) if model_match else ""
    endpoint = _normalize_endpoint(base_match.group(1) if base_match else "")
    return endpoint, model, api_key


def _parse_provider(app_type: str, config: dict) -> tuple[str, str, str, str] | None:
    """返回 (endpoint, model, api_key, api_format)；无法识别则 None。"""
    if app_type == "codex":
        endpoint, model, api_key = _parse_codex(config)
        return endpoint, model, api_key, "openai"
    if app_type.startswith("claude"):
        endpoint, model, api_key = _parse_claude(config)
        return endpoint, model, api_key, "anthropic"
    if app_type == "opencode":
        endpoint, model, api_key = _parse_opencode(config)
        return endpoint, model, api_key, "openai"
    if app_type == "gemini":
        endpoint, model, api_key = _parse_gemini(config)
        return endpoint, model, api_key, "openai"
    if app_type == "grokbuild":
        endpoint, model, api_key = _parse_grokbuild(config)
        return endpoint, model, api_key, "openai"
    return None


def read_providers(db_path: Path | str | None = None) -> dict:
    """读取 cc-switch 供应商列表。文件不存在或无法解析时优雅返回空。"""
    path = Path(db_path) if db_path else CCSWITCH_DB
    if not path.is_file():
        return {"available": False, "providers": [], "skipped": []}

    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return {"available": False, "providers": [], "skipped": []}

    providers: list[dict] = []
    skipped: list[dict] = []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT app_type, name, settings_config, is_current "
            "FROM providers ORDER BY is_current DESC, app_type, sort_index, name"
        ).fetchall()
    except sqlite3.Error:
        return {"available": False, "providers": [], "skipped": []}
    finally:
        conn.close()

    for row in rows:
        try:
            config = json.loads(row["settings_config"] or "{}")
        except (TypeError, ValueError):
            continue
        app_type = str(row["app_type"] or "")
        name = str(row["name"] or "")
        parsed = _parse_provider(app_type, config)
        if parsed is None:
            skipped.append({"app_type": app_type, "name": name,
                            "reason": "暂不支持的应用类型"})
            continue
        endpoint, model, api_key, api_format = parsed
        if not endpoint or not api_key:
            skipped.append({"app_type": app_type, "name": name,
                            "reason": "没有接口地址或密钥（CLI 默认配置会这样）"})
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
            "name": name,
            "endpoint": endpoint,
            "model": model,
            "api_format": api_format,
            "is_current": bool(row["is_current"]),
            "openai_native": api_format == "openai",
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

    return {"available": True, "providers": unique, "skipped": skipped}
