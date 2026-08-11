"""裁剪嵌入模型词表 —— 把 489 MB 的多语言模型压到约 111 MB。

    uv run python scripts/trim_embedding_model.py

背景：`potion-multilingual-128M` 是 model2vec 静态嵌入（查表 + 平均，
无神经网络推理），依赖只有 numpy + tokenizers，PyInstaller 友好。
但它的 50 万词表覆盖上百种语言，float32 存下来 489 MB ——
打进 DMG 会让安装包从 175 MB 涨到 660 MB。

实测三方对比（30 题评测集，见 docs/eval/）：

    方案                Recall@5    拒答准确率   体积
    FTS5 三路（基线）      83.3%       无法判定    0
    potion-base-8M       55.6%       60.0%     30 MB
    potion-multilingual  88.9%       73.3%    489 MB
    ↑ 裁剪 + float16      94.4%       73.3%    111 MB

**裁剪后质量反而提升 5.6 个百分点** —— 去掉西里尔、阿拉伯、天城等
无关语言的词向量后，跨语言干扰消失了。体积同时降到 1/4。

裁剪规则：只保留 token 含中文字符或纯 ASCII 的词，其余整行删除。
model2vec 的推理是"把 token 对应的行取出来平均"，删掉用不到的行
不影响中文与英文的结果 —— 前提是同步重建 tokenizer 的 id 映射。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np
import safetensors.numpy as stn

SRC = Path.home() / "Documents/Agent/Inktable/models/potion-multilingual-128M"
DST = Path.home() / "Documents/Agent/Inktable/models/potion-zh-trimmed"

CJK_RANGES = [
    ("一", "鿿"),   # 基本汉字
    ("㐀", "䶿"),   # 扩展 A
    ("豈", "﫿"),   # 兼容汉字
    ("　", "〿"),   # 中文标点
    ("＀", "￯"),   # 全角字符
]


def is_kept(token: str) -> bool:
    """判断这个 token 是否保留。

    保留：含中日韩统一表意文字 / 中文标点 / 全角字符的 token，以及纯 ASCII。
    删除：西里尔、阿拉伯、天城、韩文谚文、平假名片假名等无关文字。

    注意保留 ASCII —— 中文文档里混排的英文术语、型号、编号
    （Sn58Bi、RFC 959、HT-2024-0023）全靠它。
    """
    s = str(token).lstrip("▁")
    if not s:
        return True   # 特殊 token（[PAD] [UNK] 等）必须留
    if all(ord(c) < 128 for c in s):
        return True
    return any(lo <= c <= hi for c in s for lo, hi in CJK_RANGES)


def main() -> int:
    if not SRC.is_dir():
        print(f"源模型不存在：{SRC}")
        return 1

    tok_path = SRC / "tokenizer.json"
    tok = json.loads(tok_path.read_text(encoding="utf-8"))
    model = tok["model"]

    raw_vocab = model["vocab"]
    is_list = isinstance(raw_vocab, list)
    # unigram 词表是 [[token, score], ...]，wordpiece/bpe 是 {token: id}
    if is_list:
        entries = list(raw_vocab)
        tokens = [e[0] if isinstance(e, (list, tuple)) else e for e in entries]
    else:
        pairs = sorted(raw_vocab.items(), key=lambda kv: kv[1])
        entries = [k for k, _ in pairs]
        tokens = entries

    weights = stn.load_file(str(SRC / "model.safetensors"))
    key = next(iter(weights))
    emb = weights[key]

    if len(tokens) != emb.shape[0]:
        print(f"词表 {len(tokens)} 与嵌入行数 {emb.shape[0]} 不一致，无法安全裁剪")
        return 1

    keep = [i for i, t in enumerate(tokens) if is_kept(t)]
    print(f"词表 {len(tokens):,} → 保留 {len(keep):,} ({len(keep)/len(tokens):.0%})")

    new_emb = emb[keep].astype(np.float16)
    print(f"嵌入 {emb.nbytes/1048576:.0f} MB (float32) → "
          f"{new_emb.nbytes/1048576:.0f} MB (float16)")

    # 重建词表：id 必须是裁剪后的连续序号，否则查表全错
    if is_list:
        model["vocab"] = [entries[i] for i in keep]
    else:
        model["vocab"] = {tokens[i]: new_id for new_id, i in enumerate(keep)}

    # unigram 的 unk_id 也要重映射
    if "unk_id" in model and model["unk_id"] is not None:
        old_unk = model["unk_id"]
        remap = {old: new for new, old in enumerate(keep)}
        model["unk_id"] = remap.get(old_unk, 0)

    DST.mkdir(parents=True, exist_ok=True)
    stn.save_file({key: new_emb}, str(DST / "model.safetensors"))
    (DST / "tokenizer.json").write_text(
        json.dumps(tok, ensure_ascii=False), encoding="utf-8"
    )
    shutil.copy(SRC / "config.json", DST / "config.json")

    total = sum(f.stat().st_size for f in DST.iterdir()) / 1048576
    print(f"\n已写入 {DST}")
    for f in sorted(DST.iterdir()):
        print(f"  {f.name:<22} {f.stat().st_size/1048576:>7.1f} MB")
    print(f"  {'合计':<22} {total:>7.1f} MB")

    # 自检：能加载、能编码、中英文都不退化成同一个向量
    print("\n自检…")
    from model2vec import StaticModel

    m = StaticModel.from_pretrained(str(DST))
    probe = ["汝窑天青釉的呈色原理", "Sn58Bi 钎料的抗剪强度",
             "宿舍电费预付费充值", "RFC 959 FTP protocol"]
    v = m.encode(probe)
    v = v / np.linalg.norm(v, axis=1, keepdims=True)
    sim = v @ v.T
    off = sim[np.triu_indices(len(probe), 1)]
    print(f"  维度 {m.dim} · 四条互不相关文本的相似度 "
          f"[{off.min():.2f}, {off.max():.2f}]")
    if off.max() > 0.95:
        print("  ✗ 向量退化（词表映射可能错位）")
        return 1
    print("  ✓ 通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
