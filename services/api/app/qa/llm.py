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
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

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


def _validate_endpoint(endpoint: str) -> str:
    """Accept only absolute HTTP(S) endpoints with no embedded credentials.

    The API key is forwarded as an Authorization header to this host. Without
    validation, a compromised renderer could configure an arbitrary scheme or
    a URL containing misleading credentials and make the sidecar forward the
    user's key there. Local HTTP endpoints remain supported for Ollama-style
    providers; HTTPS is required only by product policy, not by this parser.
    """
    value = (endpoint or "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LLMError("模型接口地址必须是有效的 http:// 或 https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise LLMError("模型接口地址不能包含用户名或密码")
    return value


def configure(endpoint: str, api_key: str, model: str) -> None:
    """运行时配置。传空串 = 清除（回到纯本地模式）。"""
    value = _validate_endpoint(endpoint)
    with _lock:
        _cfg.endpoint = value
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


def probe(*, timeout: float = 20.0) -> dict:
    """真实响应测试：发一条最小对话请求，必须拿回模型生成的文本才算通过。

    这不是 TCP/HTTP 连通性检查 —— 鉴权失效、模型名不存在、中转站不兼容
    OpenAI 协议、推理模型只产思考不产正文，这些只有真实补全才能暴露。
    成功时把模型的实际回复和耗时一并返回给界面展示。

    max_tokens 给足 512：推理型模型会先消耗思考预算，只给 4 个 token
    会出现"接口通但回复为空"的假阴性。
    """
    if not is_configured():
        return {
            **status(), "available": False, "code": "not_configured",
            "message": "模型尚未配置",
        }
    started = time.monotonic()
    try:
        for attempt in range(5):
            try:
                text = chat(
                    [{"role": "user", "content": "这是一次连接测试。请只回复两个字：确认"}],
                    temperature=0, max_tokens=512, timeout=timeout,
                )
                break
            except LLMConnectionError as exc:
                if attempt == 2 or exc.code != "unreachable":
                    raise
                # A freshly started local provider can be bound before its
                # serving thread begins accepting connections.
                time.sleep(0.05 * (attempt + 1))
        latency_ms = int((time.monotonic() - started) * 1000)
        reply = " ".join(text.split())
        if not reply:
            raise LLMError("接口连通但模型没有返回文本")
        if len(reply) > 60:
            reply = reply[:60] + "…"
        return {
            **status(), "available": True, "code": "ready",
            "reply": reply, "latency_ms": latency_ms,
            "message": f"连接正常 · {latency_ms / 1000:.1f}s 实际回复「{reply}」",
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
        code, message = ("invalid_response",
                         "接口连通但没有拿到有效回复（检查模型名，或该中转不兼容 OpenAI 协议）")
    return {**status(), "available": False, "code": code, "message": message}


def _urlopen_before_deadline(req, deadline: float):
    """Open and receive headers without letting urllib exceed the deadline."""
    result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
    timed_out = threading.Event()

    def _open() -> None:
        response = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("absolute request deadline expired")
            response = urllib.request.urlopen(req, timeout=remaining)
            if timed_out.is_set():
                response.close()
                return
            result.put_nowait((True, response))
        except BaseException as exc:  # preserve urllib's HTTPError/URLError exactly
            if response is not None:
                try:
                    response.close()
                except Exception:  # noqa: BLE001 - best-effort late-response cleanup
                    pass
            if not timed_out.is_set():
                try:
                    result.put_nowait((False, exc))
                except queue.Full:
                    pass

    worker = threading.Thread(target=_open, name="llm-urlopen", daemon=True)
    worker.start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        timed_out.set()
        raise TimeoutError("absolute request deadline expired")
    try:
        ok, value = result.get(timeout=remaining)
    except queue.Empty as exc:
        timed_out.set()
        raise TimeoutError("absolute request deadline expired") from exc
    if ok:
        return value
    raise value


def _underlying_socket(response):
    """Best-effort dig for the raw socket behind a urllib/http.client response."""
    for attribute_path in (("fp", "raw", "_sock"), ("fp", "_sock"), ("_sock",)):
        target = response
        try:
            for name in attribute_path:
                target = getattr(target, name)
        except AttributeError:
            continue
        if hasattr(target, "shutdown") and hasattr(target, "fileno"):
            return target
    return None


def _arm_deadline_close(response, deadline: float) -> tuple[threading.Timer, threading.Event]:
    """Interrupt a blocking urllib response when the absolute deadline expires.

    urllib's timeout is inactivity-based and can be defeated by a provider
    dripping bytes forever.

    ``response.close()`` alone is **not** enough and was measured failing: with a
    real socket dripping one byte every 250 ms, a 1 s deadline still took 50 s,
    because CPython's ``BufferedReader`` holds its lock inside the blocked read
    and ``close()`` waits for that lock instead of interrupting it. Shutting the
    underlying socket down forces the blocked ``recv`` to return immediately, so
    the deadline becomes real rather than advisory.

    Returns the timer and a ``fired`` event the callers check to classify the
    resulting OSError. Relying on ``time.monotonic() >= deadline`` alone is
    racy: the timer can trip a hair before that comparison reads true on
    coarse-resolution clocks, misfiling a deadline close as ``unreachable``.
    The flag makes "our timer closed this response" authoritative.
    """
    fired = threading.Event()

    def _close() -> None:
        fired.set()
        sock = _underlying_socket(response)
        if sock is not None:
            # SHUT_RDWR unblocks a recv that is parked in the kernel; do this
            # before close() so the reader thread cannot stay wedged.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001 - already closed/reset is fine
                pass
        try:
            response.close()
        except Exception:  # noqa: BLE001 - best-effort interrupt of a blocked read
            pass

    timer = threading.Timer(max(0.0, deadline - time.monotonic()), _close)
    timer.daemon = True
    timer.start()
    return timer, fired


def chat_stream(messages: list[dict], *, temperature: float = 0.1,
                max_tokens: int | None = 1500, timeout: float = 60.0):
    """Yield OpenAI-compatible SSE delta text for a draft answer.

    `urlopen(timeout=...)` is a per-socket-operation timeout, not a total
    request deadline. Check monotonic time between SSE lines as well so a
    provider dripping one byte at a time cannot hold an ask forever.
    """
    endpoint, api_key, model = _config_snapshot()
    if not (endpoint and api_key and model):
        raise LLMNotConfigured("未配置模型服务（设置 → AI 问答）")
    payload: dict = {
        "model": model, "messages": messages,
        "temperature": temperature, "stream": True,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    req = urllib.request.Request(
        f"{endpoint}/chat/completions",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    deadline = time.monotonic() + timeout
    try:
        with _urlopen_before_deadline(req, deadline) as resp:
            timer, deadline_fired = _arm_deadline_close(resp, deadline)
            try:
                for raw_line in resp:
                    if deadline_fired.is_set() or time.monotonic() > deadline:
                        raise LLMConnectionError("timeout", "模型服务响应超时")
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        event = json.loads(data)
                        content = event["choices"][0].get("delta", {}).get("content")
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        continue
                    if isinstance(content, str) and content:
                        yield content
            except OSError as exc:
                if deadline_fired.is_set() or time.monotonic() >= deadline:
                    raise LLMConnectionError("timeout", "模型服务响应超时") from exc
                raise
            finally:
                timer.cancel()
            if deadline_fired.is_set() or time.monotonic() >= deadline:
                raise LLMConnectionError("timeout", "模型服务响应超时")
            return
    except urllib.error.HTTPError as e:
        raise LLMHTTPError(e.code) from e
    except TimeoutError as e:
        raise LLMConnectionError("timeout", "模型服务响应超时") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            raise LLMConnectionError("timeout", "模型服务响应超时") from e
        raise LLMConnectionError("unreachable", "无法连接模型服务") from e
    except OSError as e:
        if time.monotonic() >= deadline:
            raise LLMConnectionError("timeout", "模型服务响应超时") from e
        raise LLMConnectionError("unreachable", "无法连接模型服务") from e
    except ValueError as e:
        raise LLMConnectionError("unreachable", "无法连接模型服务") from e


def chat(messages: list[dict], *, temperature: float = 0.1,
         max_tokens: int | None = 1500, timeout: float = 60.0) -> str:
    """一次非流式对话。返回 assistant 文本。

    max_tokens=None 表示**不传该字段**，由所选模型使用自己的默认输出
    上限（现代模型普遍是几万到几十万，让模型自己决定最合理）。

    非流式是刻意的（§12.4）：后置校验会改写甚至废弃答案，流式推出去
    就收不回。方案明确允许"首个版本先做非流式"，禁止的是"流式+不校验"。
    """
    endpoint, api_key, model = _config_snapshot()
    if not (endpoint and api_key and model):
        raise LLMNotConfigured("未配置模型服务（设置 → AI 问答）")

    url = f"{endpoint}/chat/completions"
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    body = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        with _urlopen_before_deadline(req, deadline) as resp:
            timer, deadline_fired = _arm_deadline_close(resp, deadline)
            try:
                while True:
                    if deadline_fired.is_set() or time.monotonic() > deadline:
                        raise LLMConnectionError("timeout", "模型服务响应超时")
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except OSError as exc:
                if deadline_fired.is_set() or time.monotonic() >= deadline:
                    raise LLMConnectionError("timeout", "模型服务响应超时") from exc
                raise
            finally:
                timer.cancel()
            if deadline_fired.is_set() or time.monotonic() >= deadline:
                raise LLMConnectionError("timeout", "模型服务响应超时")
        raw = b"".join(chunks)
    except urllib.error.HTTPError as e:
        raise LLMHTTPError(e.code) from e
    except TimeoutError as e:
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
