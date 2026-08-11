"""本地嵌入服务 —— PLAN §16.1a。

选型：`potion-multilingual-128M` 经词表裁剪（scripts/trim_embedding_model.py）。
model2vec 是**静态嵌入**（查表 + 平均），无神经网络推理：
依赖只有 numpy + tokenizers，PyInstaller 友好，编码约 9000 片/秒。

实测三方对比（30 题评测集，docs/eval/）：

    FTS5 三路（基线）      Recall@5 83.3%     0 MB
    potion-base-8M                 55.6%    30 MB   ← 英文词表，中文不够
    potion-multilingual-128M       88.9%   489 MB
    ↑ 裁剪 + float16               94.4%   111 MB   ← 采用

**内存纪律**（这一节是被实测教训写出来的）：

开发时我一次性把 3098 个分片全部载入编码，同时还开着三个模型对比 ——
峰值 2-3 GB，把开发机内存打爆。模型本身不重（单个约 420 MB 常驻），
问题在于「同时多实例」+「一次性全量」。所以：

  · **进程内单实例**，用 get_embedder() 取，不要各处 StaticModel.from_pretrained
  · **流式分批**，encode_iter() 逐批 yield，调用方边算边写库，不攒在内存里
  · **不缓存输入文本** —— 文本从数据库流式取，用完即弃
  · **按需加载**：不调用 get_embedder() 就不占那 420 MB。
    这是给个人电脑用的软件，轻度用户只用关键词搜索时不该付这个成本。

个人电脑（16 GB）的内存账，实测推算：

    库规模        向量(float32)   说明
     3,000 片        3 MB        当前开发库
    20,000 片       20 MB        一年日常使用
   100,000 片       98 MB        重度使用，仍可全量载入内存
   500,000 片      488 MB        改用 sqlite-vec 磁盘索引，按需读

10 万分片以内全量内存扫描最快也最省事；超过再切 sqlite-vec
（它已经打进包了，§4.3）。模型的 420 MB 是固定开销，与库规模无关。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path

import numpy as np

log = logging.getLogger("inktable.embedding")

DIM = 256
# 每批条数。批太小则 tokenizer 调用开销占比高，太大则中间数组吃内存。
# 256 条 × 平均 500 字符 ≈ 单批峰值几 MB，安全。
BATCH = 256


def _candidate_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("INKTABLE_EMBED_MODEL", ""))]
    if getattr(sys, "frozen", False):
        # 打包后：sidecar 在 Contents/Resources/sidecar/，模型在 Resources/models/
        exe = Path(sys.executable).resolve()
        dirs.append(exe.parent.parent / "models" / "potion-zh-trimmed")
    dirs += [
        Path(__file__).resolve().parents[4] / "models" / "potion-zh-trimmed",
        Path.home() / "Documents/Agent/Inktable/models/potion-zh-trimmed",
    ]
    return dirs


class EmbeddingUnavailable(RuntimeError):
    """模型不可用。调用方应降级为纯 FTS5 检索，而不是让功能整体失败。"""


def _resolve_model_dir() -> Path:
    for p in _candidate_dirs():
        if p and (p / "model.safetensors").is_file():
            return p
    raise EmbeddingUnavailable(
        "找不到嵌入模型。运行 scripts/trim_embedding_model.py 生成，"
        "或设置 INKTABLE_EMBED_MODEL 指向模型目录"
    )


class Embedder:
    """嵌入模型封装。**进程内应当只有一个实例**（见模块 docstring 的内存纪律）。"""

    def __init__(self, model_dir: Path | None = None):
        from model2vec import StaticModel

        self.model_dir = model_dir or _resolve_model_dir()
        self._model = StaticModel.from_pretrained(str(self.model_dir))
        self.dim = int(self._model.dim)
        if self.dim != DIM:
            log.warning("模型维度 %d 与预期 %d 不一致", self.dim, DIM)
        log.info("嵌入模型已加载：%s (dim=%d)", self.model_dir.name, self.dim)

    @property
    def model_id(self) -> str:
        """写入 chunks.embedding_model_id，模型变更时据此触发重建（§12.8）。"""
        return f"{self.model_dir.name}-d{self.dim}"

    def encode(self, texts: list[str]) -> np.ndarray:
        """编码一批文本，返回 L2 归一化后的 float32 矩阵。

        归一化后余弦相似度退化为点积，检索时省一次除法（§12.2 ③）。
        """
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        v = self._model.encode(texts, batch_size=BATCH)
        v = np.asarray(v, dtype=np.float32)
        norms = np.linalg.norm(v, axis=1, keepdims=True)
        # 空文本或全未知 token 会得到零向量，除零会产生 nan
        np.maximum(norms, 1e-12, out=norms)
        return v / norms

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]

    def encode_iter(
        self, items: Iterable[tuple[int, str]], batch: int = BATCH
    ) -> Iterator[list[tuple[int, np.ndarray]]]:
        """流式编码，逐批产出 (id, 向量)。

        调用方应当每批写完库就丢弃 —— 全量攒在内存里正是把开发机
        打爆的原因。20 万分片全量编码的结果向量就有 195 MB，
        加上文本驻留会更多。
        """
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
    """取进程内唯一实例。首次调用约 1.6 秒（加载 111 MB 权重，常驻约 420 MB）。

    **按需加载**：不调用就不占内存。语义检索关闭时整个进程里没有这块开销。
    """
    global _instance
    with _lock:
        if _instance is None:
            _instance = Embedder()
        return _instance


def unload() -> bool:
    """释放模型内存。

    面向个人电脑：用户关闭语义检索、或长时间只用关键词搜索时，
    没必要一直占着 420 MB。下次需要会自动重新加载（1.6 秒）。
    """
    global _instance
    with _lock:
        if _instance is None:
            return False
        _instance = None
    import gc
    gc.collect()
    log.info("嵌入模型已卸载")
    return True


def is_loaded() -> bool:
    return _instance is not None


def memory_estimate_mb() -> float:
    """已加载模型的粗略内存占用，供状态展示。"""
    if _instance is None:
        return 0.0
    # 权重 float32 + tokenizer 结构（实测约 200 MB）
    return _instance._model.embedding.nbytes / 1048576 + 200


def is_available() -> bool:
    """模型是否可用。不加载权重，只看文件在不在 —— 供健康检查用。"""
    try:
        _resolve_model_dir()
        return True
    except EmbeddingUnavailable:
        return False


def embed_text_for(text: str, section_path: str = "") -> str:
    """构造送进模型的文本 —— 前置标题路径（§12.2 ③）。

    「见附件三」这样的片段单独看毫无语义，带上
    「采购合同 › 第二章 履约 › 2.3 保修条款」后可辨识度完全不同。
    """
    return f"{section_path}\n{text}" if section_path else text
