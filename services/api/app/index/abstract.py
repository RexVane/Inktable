"""Document 层主题摘要 —— 本机 Ollama 生成（检索用，非展示用）。

**为什么需要它**：`documents_fts` 索引的是 `title + summary_text`，而
`summary_text = full_text[:1000]` 是**截断，不是摘要**。截断只带正文开头的
词，于是「哪份资料讲了检索延迟优化」这类**主题类**查询在文档路上找不到目标
—— 除非那些词恰好出现在前 1000 字里。2026-08-21 的 document-first 实验
（改用文档头部重排）把 nDCG 刷到 90.1% 却把 Gold Evidence Recall@50 砸到
51.0%，原因正是「头部不代表文档」。摘要是同一个想法的有原则版本：让文档
层带上**主题词汇**，而不是开头的词汇。

**设计约束**

1. 不替换 `summary_text`。它是确定性截断，零依赖、永远可用；摘要是
   `abstract` 列，可为 NULL。两者一起进 FTS，摘要在前。
   → LLM 不可用时行为与改动前**逐字一致**，降级路径天然存在，不需要开关。
2. 摘要是**给检索器读的**，不是给人读的：要求覆盖主题、专有名词、别名与
   同义说法，不要求文笔通顺，也不要求完整句子。展示层不使用它。
3. 走「知识馆整理」槽位（app.config.models）：默认本机 Ollama；用户把该槽位
   显式指到 OpenAI 兼容接口时才出网 —— 与富化同一份配置、同一个选择，
   不存在"索引时悄悄发给问答云端供应商"的路径。
4. 输入截断到 `_INPUT_CHARS`。整篇 6.4MB 的文档发进去只会撑爆上下文，
   而摘要需要的主题信号在开头 + 结构里已经足够。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

from app.config import llm_client, models as model_slots

log = logging.getLogger("inktable.abstract")

# 摘要模型与嵌入模型分开配：嵌入是 bge-m3（不能对话），摘要要一个 chat 模型。
_MODEL = os.environ.get("INKTABLE_ABSTRACT_MODEL", "qwen3:8b")

# 送进模型的正文上限。5372 字是本机库的平均文档长度，取 6000 覆盖多数文档
# 全文；更长的文档靠「开头 + 结构」已能定主题。
_INPUT_CHARS = int(os.environ.get("INKTABLE_ABSTRACT_INPUT_CHARS", "6000"))
# 摘要输出上限（token）。摘要进 FTS 是为了带词，不是为了成文，200 足够。
_MAX_TOKENS = int(os.environ.get("INKTABLE_ABSTRACT_MAX_TOKENS", "220"))
_TIMEOUT = float(os.environ.get("INKTABLE_ABSTRACT_TIMEOUT", "120"))

_PROMPT = """你在为一个本地文件检索系统建立索引。请为下面的文档写一段检索用摘要。

要求：
1. 覆盖文档的主题、领域、涉及的专有名词与技术术语。
2. 若文档里的概念有常见别名或同义说法，一并写出。
3. 写明文档的体裁（论文/报告/笔记/配置/日志/合同等）与它在回答什么问题。
4. **只使用这篇文档里确实出现或确实讨论的概念。** 不要引入文档没有涉及的
   词，哪怕它看起来相关 —— 索引里多一个无关词，就会让查那个词的人被引到
   这篇不相关的文档。
5. 只输出摘要正文，不要标题、不要编号、不要「本文」之类的开场。
6. 不超过 150 字。不要复述开头段落，要概括全文。

文档标题：{title}

文档正文：
{body}"""


class AbstractUnavailable(RuntimeError):
    """摘要服务不可用（未装模型、连不上、超时）。调用方应保留 NULL。"""


def _post(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "Inktable/0.3"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _cfg() -> dict:
    """摘要跟随「知识馆整理」槽位；未配置时回退环境变量（ollama 形态）。"""
    return model_slots.effective("library") or {
        "provider": "ollama", "endpoint": model_slots.discover_ollama_url(),
        "api_key": "", "model": _MODEL,
    }


def model_available() -> bool:
    """摘要模型是否就绪。ollama 查 /api/tags；openai 看配置是否完整。"""
    cfg = _cfg()
    if cfg["provider"] == "openai":
        return bool(cfg["endpoint"] and cfg["model"] and cfg["api_key"])
    try:
        req = urllib.request.Request(f"{cfg['endpoint']}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 - 连不上就是不可用
        log.debug("摘要模型探测失败：%s", exc)
        return False
    want = cfg["model"].split(":")[0]
    for model in data.get("models", []):
        name = str(model.get("name", ""))
        if name == cfg["model"] or name.split(":")[0] == want:
            return True
    return False


def _strip_thinking(text: str) -> str:
    """去掉推理模型的 <think> 段。

    qwen3 一族即使传了 `think: false` 也可能吐出思考块；把它写进索引等于
    把模型的自言自语当成文档主题词。"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"^\s*<think>.*", "", text, flags=re.S)
    return text.strip()


def generate(title: str, body: str) -> str:
    """为一篇文档生成检索用摘要。失败抛 AbstractUnavailable。"""
    prompt = _PROMPT.format(
        title=(title or "未命名文档")[:200],
        body=(body or "")[:_INPUT_CHARS],
    )
    started = time.time()
    cfg = _cfg()
    if cfg["provider"] == "openai":
        try:
            text = llm_client.generate_text(
                prompt, cfg=cfg, max_tokens=_MAX_TOKENS, timeout=_TIMEOUT)
        except llm_client.LLMClientError as exc:
            raise AbstractUnavailable(str(exc)) from exc
        if not text:
            raise AbstractUnavailable("模型返回空摘要")
        log.debug("摘要生成 %.1fs / %d 字", time.time() - started, len(text))
        return text
    try:
        data = _post(f"{cfg['endpoint']}/api/generate", {
            "model": cfg["model"],
            "prompt": prompt,
            "stream": False,
            # 推理链对摘要没有价值，只会吃掉 token 预算并拖慢回填。
            "think": False,
            "options": {
                # 摘要要稳定可复现：同一篇文档重跑应给出同一段索引文本，
                # 否则增量重建会无谓地让 FTS 抖动。
                "temperature": 0.0,
                "num_predict": _MAX_TOKENS,
            },
        }, timeout=_TIMEOUT)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AbstractUnavailable(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise AbstractUnavailable(f"响应不是 JSON：{exc}") from exc

    text = _strip_thinking(str(data.get("response") or ""))
    if not text:
        raise AbstractUnavailable("模型返回空摘要")
    log.debug("摘要生成 %.1fs / %d 字", time.time() - started, len(text))
    return text
