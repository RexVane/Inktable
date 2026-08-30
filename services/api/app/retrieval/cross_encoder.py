"""Local ONNX Cross-Encoder reranker with an explicit asset boundary.

刻意**没有**模块级的 `MODEL_ID`。原先有一个，注册表化之后它就变成了陷阱：
`health.py` 一直在 `from ... import MODEL_ID`，于是实际跑着 mMiniLM 时
`/health` 仍报 bge —— 唯一对外说出「用的是哪个 CE」的地方在撒谎，而且
不会报错。要问哪个模型生效，只能走 `active_spec()`。
"""

from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.db.database import APP_DIR


@dataclass(frozen=True)
class ModelSpec:
    """一个可选的 CE 模型及其资产指纹。

    做成注册表而不是硬编码一套常量，是因为「换更便宜的 CE」是已知的下一步
    优化方向（见 docs/RETRIEVAL-PERF.md §5.8），而换模型必须能与旧模型同库
    对照评测 —— 硬编码就只能改代码切换，没法并排跑。
    """

    key: str
    model_id: str
    repo: str
    model_file: str
    model_remote: str
    model_sha256: str
    model_size: int
    tokenizer_sha256: str
    tokenizer_size: int
    tokenizer_file: str = "tokenizer.json"
    tokenizer_remote: str = "tokenizer.json"
    note: str = ""


MODELS: dict[str, ModelSpec] = {
    "bge-base": ModelSpec(
        key="bge-base",
        model_id="bge-reranker-base-int8-onnx",
        repo="onnx-community/bge-reranker-base-ONNX",
        model_file="model_int8.onnx",
        model_remote="onnx/model_int8.onnx",
        model_sha256=(
            "46a1bb4cf46ff1e300d27589d620141fbf04fc0eaf8e7bb6dea5e044475ff387"
        ),
        model_size=279_252_659,
        tokenizer_sha256=(
            "14917dd757b81bc44d4af6b028367351702656670c1954e055dabdfcf21593cf"
        ),
        tokenizer_size=17_082_798,
        note="12 层 / hidden 768，279MB",
    ),
    "mminilm-l12-h384": ModelSpec(
        key="mminilm-l12-h384",
        model_id="mmarco-mminilmv2-l12-h384-quint8",
        repo="cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        model_file="model_quint8.onnx",
        # avx2 版而不是 avx512_vnni：本机是 i7-14700HX，消费级 Raptor Lake
        # 没有 AVX-512（Intel 从 12 代起在消费线禁用），选 avx512 变体会退化。
        model_remote="onnx/model_quint8_avx2.onnx",
        model_sha256=(
            "6c2513767fb63d008a4377bef7a7a3555433d9436342bb53e35a3a72ffc52d4b"
        ),
        model_size=118_620_016,
        tokenizer_sha256=(
            "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626"
        ),
        tokenizer_size=17_082_660,
        note="12 层 / hidden 384，119MB；mMARCO 多语言蒸馏",
    ),
}


def active_spec() -> ModelSpec:
    """当前生效的 CE 模型。

    显式指定（`ORDO_RERANK_MODEL`）优先；否则**按偏好顺序挑已装上的那个**。

    「挑已装上的」这条不能省：若默认写死成新模型，已经装了旧模型的用户会因为
    新模型不存在而让 `is_available()` 返回 False，`auto` 静默退回一级本地打分器
    —— 表现是「升级之后检索变差了」，而没有任何报错。
    """
    key = os.environ.get("ORDO_RERANK_MODEL", "").strip()
    if key:
        return MODELS.get(key, MODELS[_PREFERRED[0]])
    for candidate in _PREFERRED:
        if _spec_installed(MODELS[candidate]):
            return MODELS[candidate]
    return MODELS[_PREFERRED[0]]


def _spec_installed(spec: ModelSpec) -> bool:
    root = _spec_dir(spec)
    return (root / spec.model_file).is_file() and (root / spec.tokenizer_file).is_file()


def _spec_dir(spec: ModelSpec) -> Path:
    override = os.environ.get("ORDO_RERANK_MODEL_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return APP_DIR / "models" / spec.model_id


# 偏好顺序：小模型在前。实测（真实库 65 题，同一套门控配置）两者质量打平
# （R@5 都是 96.2%、nDCG 90.2% / 90.3%），但 mMiniLM 每对约 17ms 对 69ms、
# 磁盘 119MB 对 279MB —— 在弱 CPU 上这个差距决定 Rerank P95 过不过门槛。
#
# 但要记住 bge 更**稳**：把头部换成纯融合序 K=26 时 bge 仍是 96.2%，
# mMiniLM 掉到 94.3%。也就是说 mMiniLM 依赖「向量配额把头部收窄」这个条件。
# 评测集扩大后若 mMiniLM 退化，用 ORDO_RERANK_MODEL=bge-base 一个变量切回。
_PREFERRED = ("mminilm-l12-h384", "bge-base")


class CrossEncoderUnavailable(RuntimeError):
    pass


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def model_dir() -> Path:
    return _spec_dir(active_spec())


DEFAULT_MAX_TOKENS = 192


def resolved_max_tokens() -> int:
    """单对候选截到多少 token。

    做成函数是为了只有一处默认值：`/health` 要把生效值报出来（发布产物是否
    真的含发布配置只能靠它看出来），若两边各写一个 192，改一处就会静默错配。
    """
    return _env_int("ORDO_RERANK_MAX_TOKENS", DEFAULT_MAX_TOKENS, 64, 512)


def is_available() -> bool:
    """任意一个注册模型装上了就算可用。

    不是「默认模型装上了」—— 那会让装了旧模型的用户在换默认之后静默降级。
    """
    if os.environ.get("ORDO_RERANK_MODEL", "").strip():
        return _spec_installed(active_spec())
    return any(_spec_installed(spec) for spec in MODELS.values())


class OnnxCrossEncoder:
    # 实例上会被 __init__ 覆盖成实际加载的那个模型；类属性只是兜底，
    # 且刻意留空而不是写死某一个 id —— 见模块顶部关于 MODEL_ID 的说明。
    model_id = ""

    def __init__(self, root: Path | None = None):
        spec = active_spec()
        self.spec = spec
        self.model_id = spec.model_id
        root = root or model_dir()
        model_path = root / spec.model_file
        tokenizer_path = root / spec.tokenizer_file
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
            "ORDO_RERANK_THREADS",
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
        # 192 而不是 384，理由是**质量**而不是速度：65 题上 nDCG 89.5% → 90.3%、
        # MRR 86.9% → 88.1%，Recall@5 保持 96.2%，5 次独立运行逐位复现。
        #
        # 别把它当省时手段。我原先在这里写过「纯打分 1,798ms → 1,434ms，省 20%」,
        # 那是错的:probe_ce_cost.py 当时每换一组环境变量就多留一个活着的 ONNX
        # session,各带 14 条线程,越往后越超订;同时桌面应用正在后台扫描。静默
        # 机器上重测（20 对，bge）:tok384 33.8ms/对、tok192 35.2ms/对 —— 截到
        # 192 对速度基本没有影响。详见 docs/RETRIEVAL-PERF.md §5.8 的更正。
        #
        # 这与 §5.1「截短正文会掉质量」不矛盾：那次动的是 `_focus_window` 的窗口
        # 宽度（420→300 字，换了一个居中位置不同的窗口），这里保持 420 字窗口
        # 不变，只截 tokenizer。窗口本就以查询词为中心，前半段已含最密集的证据；
        # 再往后是稀释信号的长尾。
        max_length = resolved_max_tokens()
        self.tokenizer.enable_truncation(max_length=max_length)
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        self.input_names = {item.name for item in self.session.get_inputs()}
        try:
            batch_size = int(os.environ.get("ORDO_RERANK_BATCH", "8"))
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
