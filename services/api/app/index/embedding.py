"""本地嵌入服务 —— Ollama bge-m3（PLAN §16.1a，v0.3 起）。

嵌入交给本机 Ollama（默认 http://127.0.0.1:11434）：

    模型      bge-m3（BAAI）：1024 维、多语言、8192 上下文
    检测      GET /api/tags 看已拉取列表里有没有 bge-m3*，结果缓存 30 秒
    编码      POST /api/embed 批量；返回向量统一做 L2 归一化
    不可用    抛 EmbeddingUnavailable → 调用方降级纯 FTS5（降级链不变）

此前内置的 model2vec 静态嵌入（potion-multilingual-128M 裁剪版）已移除：
静态嵌入是查表加权平均，对否定、语序、上下文不敏感；bge-m3 是真正的
上下文编码器，检索质量上限高得多。代价是编码速度从 ~9000 片/秒降到
百级/秒（Apple Silicon），由 text_hash 复用 + 分批回填吸收；
Ollama 未安装或未拉模型时自动回到纯关键词检索，功能不塌。

**维度变更**：256 → 1024。chunks_vec 表按维度建，database._init_vec_table
检测到维度不符会重建表并清空 embedding_model_id，触发全量回填。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator

import numpy as np

log = logging.getLogger("inktable.embedding")

DIM = 1024
MODEL_PREFIX = "bge-m3"     # 匹配 bge-m3 / bge-m3:latest / bge-m3:567m
# 单次 HTTP 请求的文本条数。批太大单请求耗时过长（超时、阻塞索引事务），
# 太小则请求开销占比高。64 条在 Apple Silicon 上约 1-2 秒。
BATCH = 64
# 探测结果缓存时长：查询路径每次问一遍 /api/tags 太浪费
_PROBE_TTL = 30.0

_OLLAMA_URL = os.environ.get("INKTABLE_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
# 查询路径上同一句话常被向量路与精排各编一次；短 LRU 避免重复 HTTP。
# 只缓存短文本：索引回填的长分片会把查询向量挤掉。
_ENCODE_CACHE_MAX = 64
_ENCODE_CACHE_MAX_CHARS = 512


class EmbeddingUnavailable(RuntimeError):
    """模型不可用。调用方应降级为纯 FTS5 检索，而不是让功能整体失败。"""


# ---- Ollama 探测（带 TTL 缓存）----

_probe_cache: dict = {"at": 0.0, "tag": None}
_probe_lock = threading.Lock()
_encode_cache: dict[str, np.ndarray] = {}
_encode_cache_order: list[str] = []
_encode_cache_lock = threading.Lock()


def _text_cache_key(model_id: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{model_id}:{digest}"


def _cache_get(keys: list[str]) -> tuple[list[np.ndarray | None], int, int]:
    hits = 0
    rows: list[np.ndarray | None] = []
    with _encode_cache_lock:
        for key in keys:
            vec = _encode_cache.get(key)
            if vec is None:
                rows.append(None)
                continue
            hits += 1
            # 刷新 LRU 顺序
            try:
                _encode_cache_order.remove(key)
            except ValueError:
                pass
            _encode_cache_order.append(key)
            rows.append(vec.copy())
    return rows, hits, len(keys) - hits


def _cache_put(key: str, vec: np.ndarray) -> None:
    with _encode_cache_lock:
        if key not in _encode_cache:
            _encode_cache_order.append(key)
        _encode_cache[key] = vec.astype(np.float32, copy=True)
        while len(_encode_cache_order) > _ENCODE_CACHE_MAX:
            old = _encode_cache_order.pop(0)
            _encode_cache.pop(old, None)


def _http_json(path: str, payload: dict | None = None, timeout: float = 3.0) -> dict:
    req = urllib.request.Request(
        f"{_OLLAMA_URL}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _probe(force: bool = False) -> str | None:
    """返回本机 Ollama 里可用的 bge-m3 标签（如 'bge-m3:latest'），没有则 None。"""
    with _probe_lock:
        now = time.time()
        if not force and now - _probe_cache["at"] < _PROBE_TTL:
            return _probe_cache["tag"]
        tag = None
        try:
            data = _http_json("/api/tags", timeout=2.0)
            for m in data.get("models", []):
                name = str(m.get("name", ""))
                if name == MODEL_PREFIX or name.startswith(MODEL_PREFIX + ":"):
                    tag = name
                    break
        except Exception as e:  # noqa: BLE001 - 连不上就是不可用
            log.debug("Ollama 探测失败：%s", e)
        _probe_cache.update(at=now, tag=tag)
        return tag


class Embedder:
    """Ollama 嵌入客户端。接口与旧静态嵌入完全一致（encode/encode_one/encode_iter）。"""

    def __init__(self):
        tag = _probe()
        if tag is None:
            raise EmbeddingUnavailable(
                "未检测到 Ollama 的 bge-m3 模型。安装 Ollama 后执行："
                "ollama pull bge-m3"
            )
        self.tag = tag
        self.dim = DIM
        log.info("嵌入服务已连接：Ollama %s (dim=%d)", tag, DIM)

    @property
    def model_id(self) -> str:
        """写入 chunks.embedding_model_id，模型变更时据此触发重建（§12.8）。

        用基名而非完整 tag：bge-m3:latest 与 bge-m3 是同一权重，
        不应因 tag 写法差异触发全库重嵌。
        """
        return f"ollama-{MODEL_PREFIX}-d{DIM}"

    def encode(self, texts: list[str]) -> np.ndarray:
        """编码一批文本，返回 L2 归一化后的 float32 矩阵。

        归一化后余弦相似度退化为点积，检索时省一次除法（§12.2 ③）。
        内部按 BATCH 分批请求；任何一批失败都抛 EmbeddingUnavailable。
        短文本（查询）结果会进进程内 LRU，避免向量路与精排重复编码。
        """
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        keys = [
            _text_cache_key(self.model_id, text)
            if len(text) <= _ENCODE_CACHE_MAX_CHARS else ""
            for text in texts
        ]
        cached, _cache_hits, cache_misses = _cache_get(
            [key for key in keys if key]
        )
        # 把「不可缓存的长文本」也当成 miss，占位与 texts 对齐
        aligned: list[np.ndarray | None] = []
        cache_iter = iter(cached)
        for key in keys:
            if key:
                aligned.append(next(cache_iter))
            else:
                aligned.append(None)
                cache_misses += 1
        cached = aligned
        if cache_misses == 0:
            return np.stack([vec for vec in cached if vec is not None])

        miss_indices = [i for i, vec in enumerate(cached) if vec is None]
        miss_texts = [texts[i] for i in miss_indices]
        parts: list[np.ndarray] = []
        for i in range(0, len(miss_texts), BATCH):
            batch = miss_texts[i:i + BATCH]
            try:
                # 首批可能触发 Ollama 冷加载模型（数秒到数十秒），超时给足
                data = _http_json("/api/embed",
                                  {"model": self.tag, "input": batch},
                                  timeout=300.0)
            except (urllib.error.URLError, OSError, ValueError) as e:
                _probe_cache["at"] = 0.0   # 失效探测缓存，下次重新检测
                raise EmbeddingUnavailable(f"Ollama 编码失败：{e}") from e
            vecs = data.get("embeddings")
            if not isinstance(vecs, list) or len(vecs) != len(batch):
                raise EmbeddingUnavailable(
                    f"Ollama 返回 {len(vecs) if isinstance(vecs, list) else '非法'} "
                    f"条向量，预期 {len(batch)} 条"
                )
            part = np.asarray(vecs, dtype=np.float32)
            if part.ndim != 2 or part.shape[1] != self.dim:
                raise EmbeddingUnavailable(
                    f"Ollama 返回维度 {part.shape} 与预期 {self.dim} 不符"
                )
            parts.append(part)
        fresh = np.vstack(parts)
        norms = np.linalg.norm(fresh, axis=1, keepdims=True)
        # 空文本会得到零向量，除零会产生 nan
        np.maximum(norms, 1e-12, out=norms)
        fresh = fresh / norms
        for pos, index in enumerate(miss_indices):
            cached[index] = fresh[pos]
            if keys[index]:
                _cache_put(keys[index], fresh[pos])
        return np.stack([vec for vec in cached if vec is not None])

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]

    def encode_iter(
        self, items: Iterable[tuple[int, str]], batch: int = BATCH
    ) -> Iterator[list[tuple[int, np.ndarray]]]:
        """流式编码，逐批产出 (id, 向量)。调用方每批写完库就丢弃。"""
        buf_ids: list[int] = []
        buf_txt: list[str] = []

        for cid, text in items:
            buf_ids.append(cid)
            buf_txt.append(text)
            if len(buf_ids) >= batch:
                yield list(zip(buf_ids, self.encode(buf_txt)))
                buf_ids, buf_txt = [], []

        if buf_ids:
            yield list(zip(buf_ids, self.encode(buf_txt)))


_instance: Embedder | None = None
_lock = threading.Lock()


def get_embedder() -> Embedder:
    """取进程内唯一实例。模型内存驻留在 Ollama 进程里，本进程零权重开销。"""
    global _instance
    with _lock:
        if _instance is None:
            _instance = Embedder()
        return _instance


def unload() -> bool:
    """丢弃客户端实例并失效探测缓存（权重在 Ollama 侧，它自己会闲时卸载）。"""
    global _instance
    with _lock:
        if _instance is None:
            return False
        _instance = None
    _probe_cache["at"] = 0.0
    with _encode_cache_lock:
        _encode_cache.clear()
        _encode_cache_order.clear()
    log.info("嵌入客户端已重置")
    return True


def is_loaded() -> bool:
    return _instance is not None


def memory_estimate_mb() -> float:
    """本进程内的模型内存占用 —— Ollama 方案下恒为 0（权重在 Ollama 进程）。"""
    return 0.0


def is_available() -> bool:
    """Ollama 是否可用且已拉取 bge-m3。供健康检查与检索路径快速判断。"""
    return _probe() is not None


def embed_text_for(text: str, section_path: str = "") -> str:
    """构造送进模型的文本 —— 前置标题路径（§12.2 ③）。

    「见附件三」这样的片段单独看毫无语义，带上
    「采购合同 › 第二章 履约 › 2.3 保修条款」后可辨识度完全不同。
    """
    return f"{section_path}\n{text}" if section_path else text
