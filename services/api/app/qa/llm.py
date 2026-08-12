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
import socket
import threading
import urllib.error
import urllib.request

log = logging.getLogger("inktable.llm")


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    pass


class LLMHTTPError(LLMError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"模型服务返回 {status_code}")


class LLMConnectionError(LLMError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


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


def _config_snapshot() -> tuple[str, str, str]:
    """Atomically snapshot credentials so one request cannot mix configs."""
    with _lock:
        return _cfg.endpoint, _cfg.api_key, _cfg.model


def is_configured() -> bool:
    endpoint, api_key, model = _config_snapshot()
    return bool(endpoint and api_key and model)


def status() -> dict:
    """给设置界面看的状态。**绝不返回密钥本身**。"""
    endpoint, api_key, model = _config_snapshot()
    return {
        "configured": bool(endpoint and api_key and model),
        "endpoint": endpoint,
        "model": model,
        "has_key": bool(api_key),
    }


def probe(*, timeout: float = 10.0) -> dict:
    """Run a minimal, user-triggered completion to verify the whole contract."""
    if not is_configured():
        return {
            **status(), "available": False, "code": "not_configured",
            "message": "模型尚未配置",
        }
    try:
        text = chat(
            [{"role": "user", "content": "Reply with OK."}],
            temperature=0, max_tokens=4, timeout=timeout,
        )
        if not text.strip():
            raise LLMError("模型服务返回空响应")
        return {
            **status(), "available": True, "code": "ready",
            "message": "模型连接正常",
        }
    except LLMNotConfigured:
        code, message = "not_configured", "模型尚未配置"
    except LLMHTTPError as exc:
        if exc.status_code in (401, 403):
            code, message = "auth_failed", "API 密钥无效或无权限"
        elif exc.status_code == 404:
            code, message = "not_found", "接口地址或模型名称不存在"
        elif exc.status_code == 429:
            code, message = "rate_limited", "模型服务请求过于频繁"
        elif exc.status_code in (408, 504):
            code, message = "timeout", "模型服务响应超时"
        else:
            code, message = "service_error", f"模型服务返回 HTTP {exc.status_code}"
    except LLMConnectionError as exc:
        code, message = exc.code, str(exc)
    except LLMError:
        code, message = "invalid_response", "模型服务响应格式异常"
    return {**status(), "available": False, "code": code, "message": message}


def chat(messages: list[dict], *, temperature: float = 0.1,
         max_tokens: int = 1500, timeout: float = 60.0) -> str:
    """一次非流式对话。返回 assistant 文本。

    非流式是刻意的（§12.4）：后置校验会改写甚至废弃答案，流式推出去
    就收不回。方案明确允许"首个版本先做非流式"，禁止的是"流式+不校验"。
    """
    endpoint, api_key, model = _config_snapshot()
    if not (endpoint and api_key and model):
        raise LLMNotConfigured("未配置模型服务（设置 → AI 问答）")

    url = f"{endpoint}/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise LLMHTTPError(e.code) from e
    except (TimeoutError, socket.timeout) as e:
        raise LLMConnectionError("timeout", "模型服务响应超时") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            raise LLMConnectionError("timeout", "模型服务响应超时") from e
        raise LLMConnectionError("unreachable", "无法连接模型服务") from e
    except OSError as e:
        raise LLMConnectionError("unreachable", "无法连接模型服务") from e
    except ValueError as e:
        # urllib raises ValueError for malformed/unsupported endpoint URLs.
        raise LLMConnectionError("unreachable", "无法连接模型服务") from e

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise LLMError("模型服务响应格式异常") from e

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError("模型服务响应格式异常") from e
    if content is None:
        return ""
    if not isinstance(content, str):
        raise LLMError("模型服务响应格式异常")
    return content
