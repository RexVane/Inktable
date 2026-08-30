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
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

SLOTS = ("library", "embedding")
PROVIDERS = ("openai", "ollama")

# 官方默认口。探测失败时的展示/回退值，**不是** import 时从环境变量拍下来的
# 快照 —— 环境变量每次调用都重读，否则测试里 setenv 和 sidecar 启动后才出现
# 的 Ollama 都看不见。
_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

# Ollama 的官方默认端口是 11434，但 Windows 上它常落在 Hyper-V/WSL 的动态保留段
# 里，装机时只能改口（本机是 18434）。硬按平台写死任一个都会错一半人，所以这里
# **探测**：显式 INKTABLE_OLLAMA_URL 优先，否则按候选顺序拿第一个应答的。
# 结果缓存在进程内 —— 设置界面与嵌入模块由此共用同一个答案，不再各写一个常量。
_OLLAMA_PORT_CANDIDATES = (11434, 18434)
_DISCOVER_MISS_TTL = 30.0
_discover_lock = threading.Lock()
_discovered_ollama: str | None = None   # 成功探到的活地址，进程内粘住
_discover_miss_at: float = 0.0          # 最近一次「候选全死」的时间；0 = 无


def reset_discovery() -> None:
    """测试用：清掉探测缓存，让下一次 discover_ollama_url 重新打候选口。"""
    global _discovered_ollama, _discover_miss_at
    with _discover_lock:
        _discovered_ollama = None
        _discover_miss_at = 0.0


def _explicit_ollama_url() -> str:
    return (os.environ.get("INKTABLE_OLLAMA_URL") or "").strip().rstrip("/")


def _ollama_alive(url: str, timeout: float) -> bool:
    """打 /api/tags：200 才算真能拉模型列表，不只是有东西在监听。"""
    try:
        req = urllib.request.Request(
            f"{url}/api/tags", headers={"User-Agent": "Inktable/0.3"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def discover_ollama_url(*, timeout: float = 0.6) -> str:
    """当前这台机器上 Ollama 的实际地址；探测不到时返回官方默认值。

    成功探到的地址进程内粘住（Ollama 不会中途换口）。失败**不**当成成功缓存
    —— sidecar 常比 Ollama 先起来，把 11434 冻死会让之后启动的 18434 永远
    找不到。失败只记住 30 秒，避免设置页两个槽位各打一轮候选口。
    """
    global _discovered_ollama, _discover_miss_at
    explicit = _explicit_ollama_url()
    if explicit:
        return explicit
    with _discover_lock:
        if _discovered_ollama is not None:
            return _discovered_ollama
        now = time.monotonic()
        if _discover_miss_at and now - _discover_miss_at < _DISCOVER_MISS_TTL:
            return _DEFAULT_OLLAMA_URL
        for port in _OLLAMA_PORT_CANDIDATES:
            url = f"http://127.0.0.1:{port}"
            if _ollama_alive(url, timeout):
                _discovered_ollama = url
                _discover_miss_at = 0.0
                return url
        _discover_miss_at = now
        return _DEFAULT_OLLAMA_URL


class SlotConfigError(ValueError):
    """槽位配置不合法（协议、地址、缺模型名等）。"""


_configs: dict[str, dict] = {}
_lock = threading.Lock()


def _validate_endpoint(endpoint: str, *, allow_local: bool = True) -> str:
    """只接受绝对 http(s) 地址且不得内嵌凭据（密钥走 Authorization 头）。"""
    value = (endpoint or "").strip().rstrip("/")
    if not value:
        raise SlotConfigError("接口地址不能为空")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SlotConfigError("接口地址必须是有效的 http:// 或 https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise SlotConfigError("接口地址不能包含用户名或密码")
    if not allow_local and parsed.scheme == "http" and parsed.hostname not in (
        "localhost", "127.0.0.1", "::1",
    ):
        # 云端明文 http 会把密钥与库内容裸奔在网络上
        raise SlotConfigError("云端接口必须使用 https://（本地服务可用 http）")
    return value


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
        url = _validate_endpoint(endpoint or discover_ollama_url())
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
        return {"provider": "ollama", "endpoint": discover_ollama_url(),
                "api_key": "", "model": model}
    if slot == "embedding":
        return {"provider": "ollama", "endpoint": discover_ollama_url(),
                "api_key": "", "model": "bge-m3"}
    return None


def status(slot: str) -> dict:
    """给设置界面看的状态。**绝不返回密钥本身**。

    `default_ollama` 是本机探测到的 Ollama 地址：界面拿它当空表单的预填值，
    这样 Mac 与 Windows 看到的都是各自机器上真实可用的那个口。
    """
    default_ollama = discover_ollama_url()
    cfg = get(slot)
    if cfg is None:
        return {"configured": False, "provider": None,
                "endpoint": "", "model": "", "has_key": False,
                "default_ollama": default_ollama}
    return {"configured": True, "provider": cfg["provider"],
            "endpoint": cfg["endpoint"], "model": cfg["model"],
            "has_key": bool(cfg["api_key"]),
            "default_ollama": default_ollama}


def all_status() -> dict:
    return {slot: status(slot) for slot in SLOTS}


def dumps_for_electron() -> str:
    """诊断用；不含密钥。"""
    return json.dumps(all_status(), ensure_ascii=False)
