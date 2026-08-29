"""统一模型客户端 —— library / embedding 槽位共用。

按槽位配置的 provider 分派：
    openai  → POST {endpoint}/chat/completions（Bearer 密钥，取 message.content）
    ollama  → POST {endpoint}/api/generate（think 关闭，取 response）

推理模型的思考内容在两种协议下都不进正文：openai 思考在独立的
reasoning/reasoning_content 字段（解析只取 content），ollama 侧剥 <think> 块。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)


class LLMClientError(RuntimeError):
    """模型调用失败（连不上、超时、HTTP 错误、空响应）。"""


# 同 qa/llm.py：中转站拦截 Python-urllib 默认 UA，必须自报家门
USER_AGENT = "Inktable/0.3"


def _strip_thinking(text: str) -> str:
    text = _THINK_BLOCK.sub("", text)
    return re.sub(r"^\s*<think>.*", "", text).strip()


def _request(url: str, payload: dict | None, *, headers: dict | None = None,
             timeout: float = 120.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None else None,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                 **(headers or {})},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise LLMClientError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMClientError(f"连接失败：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise LLMClientError(f"响应不是 JSON：{exc}") from exc


def generate_text(prompt: str, *, cfg: dict, json_mode: bool = False,
                  max_tokens: int = 900, timeout: float = 120.0) -> str:
    """一次非流式补全，返回清洗后的正文。cfg 来自 config.models。"""
    if cfg.get("provider") == "openai":
        headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg.get("api_key") else {}
        payload: dict = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "stream": False,
        }
        data = _request(f"{cfg['endpoint']}/chat/completions", payload,
                        headers=headers, timeout=timeout)
        try:
            message = data["choices"][0].get("message") or {}
            text = str(message.get("content") or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMClientError(f"响应缺少 choices.message：{str(data)[:120]}") from exc
    else:
        payload = {
            "model": cfg["model"],
            "prompt": prompt,
            "stream": False,
            "think": False,
            # Ollama 的 JSON 模式约束语法；openai 侧不用（中转支持参差），
            # 输出合法性由调用方严格校验兜底。
            **({"format": "json"} if json_mode else {}),
            "options": {"temperature": 0.0, "num_predict": max_tokens},
        }
        data = _request(f"{cfg['endpoint']}/api/generate", payload, timeout=timeout)
        text = str(data.get("response") or "")

    text = _strip_thinking(text)
    if not text:
        raise LLMClientError("模型返回空结果")
    return text


def probe_chat(cfg: dict, timeout: float = 15.0) -> dict:
    """小请求确认模型真的能回话。返回 {ok, message, latency_ms}。"""
    started = time.time()
    try:
        text = generate_text("请只回复两个字：正常", cfg=cfg,
                             max_tokens=256, timeout=timeout)
    except LLMClientError as exc:
        return {"ok": False, "message": str(exc), "latency_ms": 0}
    return {"ok": True, "message": f"模型回复「{text[:40]}」",
            "latency_ms": int((time.time() - started) * 1000)}


def list_models(*, provider: str, endpoint: str, api_key: str = "",
                timeout: float = 10.0) -> list[str]:
    """拉取可选模型名。openai 走 /models，ollama 走 /api/tags。"""
    if provider == "openai":
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        data = _request(f"{endpoint}/models", None, headers=headers,
                        timeout=timeout)
        entries = data.get("data") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise LLMClientError("响应缺少 models 列表")
        names = [str(e.get("id") or "") for e in entries
                 if isinstance(e, dict) and e.get("id")]
    else:
        data = _request(f"{endpoint}/api/tags", None, timeout=timeout)
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            raise LLMClientError("响应缺少 models 列表")
        names = [str(m.get("name") or "") for m in models
                 if isinstance(m, dict) and m.get("name")]
    return sorted(dict.fromkeys(names))
