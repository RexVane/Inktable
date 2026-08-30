"""模型槽位配置 —— 知识问答（qa）/ 知识馆整理（library）/ 向量（embedding）。

三个槽位彼此独立，各自可配 provider（openai = OpenAI 兼容接口 / ollama =
本地 Ollama）、接口地址、密钥与模型名。**密钥只进内存**（§6.3）：持久化由
Electron 主进程用 safeStorage 加密保存，sidecar 每次启动后收到推送。

未收到推送时（headless CLI、测试）回退到环境变量，行为与拆槽位之前一致：
    library → INKTABLE_LIBRARY_MODEL / INKTABLE_ABSTRACT_MODEL / INKTABLE_OLLAMA_URL
    embedding → INKTABLE_OLLAMA_URL（模型固定 bge-m3 系）
qa 槽位不走这里 —— 它是 app.qa.llm 的全局配置（/settings/llm 原路径保留）。
"""

from __future__ import annotations

import json
import os
import threading

from app.config.endpoints import EndpointPolicyError, normalize_model_endpoint

SLOTS = ("library", "embedding")
PROVIDERS = ("openai", "ollama")

_DEFAULT_OLLAMA_URL = os.environ.get(
    "INKTABLE_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


class SlotConfigError(ValueError):
    """槽位配置不合法（协议、地址、缺模型名等）。"""


_configs: dict[str, dict] = {}
_lock = threading.Lock()


def _validate_endpoint(endpoint: str, *, allow_local: bool = True) -> str:
    """Compatibility wrapper around the one shared endpoint policy."""
    del allow_local
    try:
        return normalize_model_endpoint(endpoint)
    except EndpointPolicyError as exc:
        raise SlotConfigError(str(exc)) from exc


def configure(slot: str, provider: str, endpoint: str,
              api_key: str = "", model: str = "") -> dict:
    """写入一个槽位的配置并返回脱敏 status。抛 SlotConfigError 表示不合法。"""
    if slot not in SLOTS:
        raise SlotConfigError(f"未知槽位：{slot}（可用：{'/'.join(SLOTS)}）")
    if provider not in PROVIDERS:
        raise SlotConfigError(f"未知 provider：{provider}（可用：{'/'.join(PROVIDERS)}）")
    if slot == "embedding" and provider != "ollama":
        # 云端嵌入的维度/身份管理（换模型即换向量空间）是独立工程，
        # 首版只开放本地 Ollama；维度由 /settings/models/test 探测并展示。
        raise SlotConfigError("向量槽位目前只支持本地 Ollama（provider=ollama）")
    if provider == "openai":
        url = _validate_endpoint(endpoint, allow_local=False)
        if not (api_key or "").strip():
            raise SlotConfigError("OpenAI 兼容接口必须填写 API 密钥")
    else:
        url = _validate_endpoint(endpoint or _DEFAULT_OLLAMA_URL)
    if not (model or "").strip():
        raise SlotConfigError("必须填写模型名")
    cfg = {"provider": provider, "endpoint": url,
           "api_key": (api_key or "").strip(), "model": (model or "").strip()}
    with _lock:
        _configs[slot] = cfg
    return status(slot)


def clear(slot: str) -> None:
    if slot in SLOTS:
        with _lock:
            _configs.pop(slot, None)


def get(slot: str) -> dict | None:
    with _lock:
        cfg = _configs.get(slot)
        return dict(cfg) if cfg else None


def effective(slot: str) -> dict | None:
    """当前生效的配置；未配置时回退环境变量（仅 ollama 形态）。"""
    cfg = get(slot)
    if cfg:
        return cfg
    if slot == "library":
        model = os.environ.get(
            "INKTABLE_LIBRARY_MODEL",
            os.environ.get("INKTABLE_ABSTRACT_MODEL", "qwen3:8b"))
        return {"provider": "ollama", "endpoint": _DEFAULT_OLLAMA_URL,
                "api_key": "", "model": model}
    if slot == "embedding":
        return {"provider": "ollama", "endpoint": _DEFAULT_OLLAMA_URL,
                "api_key": "", "model": "bge-m3"}
    return None


def status(slot: str) -> dict:
    """给设置界面看的状态。**绝不返回密钥本身**。"""
    cfg = get(slot)
    if cfg is None:
        return {"configured": False, "provider": None,
                "endpoint": "", "model": "", "has_key": False}
    return {"configured": True, "provider": cfg["provider"],
            "endpoint": cfg["endpoint"], "model": cfg["model"],
            "has_key": bool(cfg["api_key"])}


def all_status() -> dict:
    return {slot: status(slot) for slot in SLOTS}


def dumps_for_electron() -> str:
    """诊断用；不含密钥。"""
    return json.dumps(all_status(), ensure_ascii=False)
