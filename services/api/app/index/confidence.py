"""拒答判定 —— PLAN §12.3c。

**为什么拒答不能交给模型判断**：`AbstentionBench`（arXiv:2506.09038）
的结论是推理型 LLM 在无解问题上系统性失败，RL 微调反而把模型推向
「猜一个」而非弃权 —— 换更强的模型救不了拒答率。唯一可靠的机制是
在生成之前用检索信号判定，分不够就根本不让模型进入生成。

**RRF 分数不可阈值化**（方案明确警告过，实测证实）：
`score = Σ 1/(k+rank)` 是纯排名函数，绝对值与查询难度无关。
一个库里完全没有答案的问题，它的 top-1 RRF 分数和完美命中的问题
**一样高**（都是 1/61）。所以必须用有绝对含义的信号。

## 实测结论：当前信号无法可靠判定拒答

在 30 题评测集（18 有依据 / 12 无依据）上试过三个信号：

    信号                有依据均值   无依据均值   最优准确率
    向量最高余弦            0.587      0.532      73.3%
    命中片实词覆盖率         0.539      0.391      70.0%
    全库实词覆盖率           0.933      0.740      80.0%

「全库实词覆盖率」看起来最好，但**逐题核查后发现它的原理站不住**：

    「杭州地铁到浙江音乐学院怎么换乘」→ 覆盖率 100%
      "地铁" 全库 8 片、"换乘" 5 片 —— 词都在，只是没有一处在回答这个问题
    「Redis 集群的分片策略」→ 覆盖率 100%
      "Redis" 5 片、"分片" 25 片 —— 都来自技术文档，与 Redis 无关

在文档多样的真实库里，几乎任何常见词都能找到出处。**覆盖率高不代表
这些词凑在一起回答了问题** —— 这个信号在小语料上看似有效，实际是巧合。

要求零误拒（有依据题一个都不能拒）时，三个信号都只能拒对 1/12。

## 当前策略：不拒答，但如实标注置信度

强行拒答会误伤真实问题（误拒的代价比多给结果高得多：用户看到
「未找到依据」就不会再往下找了）。所以：

  · **永不硬拒答** —— 检索到什么就给什么
  · 置信度低时**明确标注**「相关度一般，请自行判断」
  · 把哪些实词在库中稀少（<3 片）如实告诉用户，让他自己判断

这不是达标，是**如实反映当前能力**。方案 §18.2 的「正确拒答率 ≥80%」
在纯检索信号下达不到；它需要 §12.4 的生成后校验（让模型读完资料
再判断"这些资料能否回答问题"），那是 B6 的范围。

先把这个缺口记在明处，而不是调参数凑一个好看的数字。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.index.search import _is_meaningful, _needs_split, segment_for_query

# 判"高置信"的门槛。低于此值只降级标注，不拒答（见模块 docstring）。
HIGH_COSINE = 0.60
# 实词在全库出现少于这么多片，视为"库里几乎没这方面内容"
RARE_THRESHOLD = 3


@dataclass
class Confidence:
    level: str            # high / medium
    cosine: float         # 向量最高余弦
    rare_words: list[str] # 库中稀少的实词 —— 告诉用户可能缺什么

    @property
    def should_answer(self) -> bool:
        """当前策略下恒为真 —— 不硬拒答，见模块 docstring。"""
        return True

    @property
    def hedge(self) -> str:
        """给用户看的提示。空串表示无需提示。"""
        if self.level == "high":
            return ""
        if self.rare_words:
            return (f"文件库里关于「{'、'.join(self.rare_words[:3])}」的内容很少，"
                    f"以下结果可能不完全对应你的问题")
        return "以下结果与你的问题相关度一般，请自行判断"


def content_words(query: str) -> list[str]:
    """抽取查询里的实词，去掉停用词与单字。"""
    words: list[str] = []
    for term in query.split():
        if _needs_split(term):
            words += [w for w in segment_for_query(term).split() if _is_meaningful(w)]
        else:
            words.append(term)
    return list(dict.fromkeys(words))


def assess(conn: sqlite3.Connection, query: str, top_cosine: float = 0.0) -> Confidence:
    """判定这次检索的置信度。

    top_cosine 由调用方从向量路传入（避免重复编码）。
    向量路不可用时传 0 —— 此时一律标为 medium，因为没有语义信号可依据。
    """
    level = "high" if top_cosine >= HIGH_COSINE else "medium"

    rare: list[str] = []
    if level == "medium":
        # 只在低置信时才查，避免高置信路径上多跑一堆 LIKE
        for w in content_words(query):
            if len(w) < 2:
                continue
            n = conn.execute(
                "SELECT count(*) c FROM (SELECT 1 FROM chunks WHERE text LIKE ? LIMIT ?)",
                (f"%{w}%", RARE_THRESHOLD),
            ).fetchone()["c"]
            if n < RARE_THRESHOLD:
                rare.append(w)

    return Confidence(level, top_cosine, rare)
