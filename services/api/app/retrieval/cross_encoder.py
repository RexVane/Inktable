"""Local ONNX Cross-Encoder reranker with an explicit asset boundary."""

from __future__ import annotations

import math
import os
import threading
from pathlib import Path

import numpy as np

from app.db.database import APP_DIR


MODEL_ID = "bge-reranker-base-int8-onnx"
MODEL_REPO = "onnx-community/bge-reranker-base-ONNX"
MODEL_FILE = "model_int8.onnx"
TOKENIZER_FILE = "tokenizer.json"
MODEL_SHA256 = "46a1bb4cf46ff1e300d27589d620141fbf04fc0eaf8e7bb6dea5e044475ff387"
TOKENIZER_SHA256 = "14917dd757b81bc44d4af6b028367351702656670c1954e055dabdfcf21593cf"
MODEL_SIZE = 279_252_659
TOKENIZER_SIZE = 17_082_798


class CrossEncoderUnavailable(RuntimeError):
    pass


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def model_dir() -> Path:
    override = os.environ.get("INKTABLE_RERANK_MODEL_DIR", "").strip()
    return Path(override).expanduser() if override else APP_DIR / "models" / MODEL_ID


def is_available() -> bool:
    root = model_dir()
    return (root / MODEL_FILE).is_file() and (root / TOKENIZER_FILE).is_file()


class OnnxCrossEncoder:
    model_id = MODEL_ID

    def __init__(self, root: Path | None = None):
        root = root or model_dir()
        model_path = root / MODEL_FILE
        tokenizer_path = root / TOKENIZER_FILE
        if not model_path.is_file() or not tokenizer_path.is_file():
            raise CrossEncoderUnavailable(
                "Cross-Encoder model is not installed; run scripts/install_reranker.py"
            )
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise CrossEncoderUnavailable("Cross-Encoder runtime is unavailable") from exc

        options = ort.SessionOptions()
        # 28 核实测（20 对候选、384 token）：8 线程 1370ms、14 线程 1024ms、
        # 28 线程 1336ms —— 拉满核数反而变慢，int8 GEMM 在这个尺寸上会被
        # 线程同步开销吃掉。上限取 14，够用且不超订。
        options.intra_op_num_threads = _env_int(
            "INKTABLE_RERANK_THREADS",
            max(1, min((os.cpu_count() or 4) // 2, 14)),
            1, 64,
        )
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        try:
            max_length = int(os.environ.get("INKTABLE_RERANK_MAX_TOKENS", "384"))
        except ValueError:
            max_length = 384
        self.tokenizer.enable_truncation(max_length=max(64, min(max_length, 512)))
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        self.input_names = {item.name for item in self.session.get_inputs()}
        try:
            batch_size = int(os.environ.get("INKTABLE_RERANK_BATCH", "8"))
        except ValueError:
            batch_size = 8
        self.batch_size = max(1, min(batch_size, 64))

    def score(self, query: str, documents: list[str]) -> np.ndarray:
        if not documents:
            return np.empty(0, dtype=np.float32)
        scores = np.empty(len(documents), dtype=np.float32)
        # Tokenizers pads each batch to its longest sequence. Length bucketing
        # avoids paying 384 tokens for every short metadata/table candidate.
        order = sorted(range(len(documents)), key=lambda index: len(documents[index]))
        for start in range(0, len(order), self.batch_size):
            indices = order[start:start + self.batch_size]
            batch = [documents[index] for index in indices]
            encodings = self.tokenizer.encode_batch([(query, document) for document in batch])
            feeds: dict[str, np.ndarray] = {
                "input_ids": np.asarray([item.ids for item in encodings], dtype=np.int64),
                "attention_mask": np.asarray(
                    [item.attention_mask for item in encodings], dtype=np.int64
                ),
            }
            if "token_type_ids" in self.input_names:
                feeds["token_type_ids"] = np.asarray(
                    [item.type_ids for item in encodings], dtype=np.int64
                )
            feeds = {name: value for name, value in feeds.items() if name in self.input_names}
            logits = np.asarray(self.session.run(None, feeds)[0], dtype=np.float32).reshape(-1)
            scores[indices] = logits
        return scores


_runtime: OnnxCrossEncoder | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> OnnxCrossEncoder:
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = OnnxCrossEncoder()
    return _runtime


def sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1.0 + exp)
