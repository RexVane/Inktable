"""OpenAI-compatible LLM 客户端 —— PLAN §16.1 / §6.3。

密钥纪律（§6.3，不可协商）：
  · **只存内存**，绝不落数据库、绝不进日志、绝不出现在任何 API 响应里
  · 加密持久化在 Electron 主进程（safeStorage/Keychain），每次启动经
    stdin 或 /settings/llm 推给 sidecar
  · 云端调用仅在用户显式配置后发生（§1 约束 3）—— 没配置就是纯本地产品

用 urllib 而非引入 SDK：PyInstaller 每加一个依赖都是打包风险（§19 R2），
而 chat/completions 就是一个 POST。
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

log = logging.getLogger("inktable.llm")


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    pass


class _Config:
    def __init__(self):
        self.endpoint: str = ""
        self.api_key: str = ""
        self.model: str = ""


_cfg = _Config()
_lock = threading.Lock()


def configure(endpoint: str, api_key: str, model: str) -> None:
    """运行时配置。传空串 = 清除（回到纯本地模式）。"""
    with _lock:
        _cfg.endpoint = (endpoint or "").strip().rstrip("/")
        _cfg.api_key = (api_key or "").strip()
        _cfg.model = (model or "").strip()
    log.info("LLM %s", "已配置" if is_configured() else "已清除")  # 不打印任何值


def is_configured() -> bool:
    return bool(_cfg.endpoint and _cfg.api_key and _cfg.model)


def status() -> dict:
    """给设置界面看的状态。**绝不返回密钥本身**。"""
    return {
        "configured": is_configured(),
        "endpoint": _cfg.endpoint,
        "model": _cfg.model,
        "has_key": bool(_cfg.api_key),
    }


def chat(messages: list[dict], *, temperature: float = 0.1,
         max_tokens: int = 1500, timeout: float = 60.0) -> str:
    """一次非流式对话。返回 assistant 文本。

    非流式是刻意的（§12.4）：后置校验会改写甚至废弃答案，流式推出去
    就收不回。方案明确允许"首个版本先做非流式"，禁止的是"流式+不校验"。
    """
    if not is_configured():
        raise LLMNotConfigured("未配置模型服务（设置 → AI 问答）")

    url = f"{_cfg.endpoint}/chat/completions"
    body = json.dumps({
        "model": _cfg.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_cfg.api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        # 4xx/5xx 一律转成友好错误，**不把响应体原样透传**（可能含请求回显）
        raise LLMError(f"模型服务返回 {e.code}：{detail[:120]}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"无法连接模型服务：{e}") from e

    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError("模型服务响应格式异常") from e
