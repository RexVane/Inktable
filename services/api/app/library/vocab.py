"""标签词表匹配 —— 把模型说出的主题词折算回已有标签。

**为什么需要这一层**（实测结论，不是设计偏好）：

qwen3:8b 做不了「从编号列表里按 id 挑标签」。给它 40 行 `id: 名称` 再要
`tag_ids`，它返回的是语法合法、语义随机的整数 —— 实测三篇毫不相干的文档
（足球联赛记录 / 学费缴费通知 / 一句头脑风暴便条）都挑了 id 103，另有两篇
都挑了 29，其余是连号的 104、105。而 `_parse_result` 只校验「id 在允许集合
里吗」，于是垃圾原样落库：足球比赛记录被打上「调试日志 / 配置接口」。
同一批文档改成「给我标签名」，同一个模型立刻返回「足球联赛 / 比赛记录」。

结论是分工反了：**模型擅长读懂内容并命名主题，不擅长在受控词表里做匹配**。
匹配是确定性的字符串工作，应该由代码做。本模块就是那一半。

代价与取舍：模型按名字输出时**几乎不会复用已有词**（实测主题确实在词表里的
四篇，复用 0/11：词表有「博客」它写「博客记录」，有「单片机」它写
「单片机编程」）。所以不折算就会把「词表膨胀」原样换回来 —— 这正是本模块
存在的理由，而不是可选的优化。

折算用二元组 Dice 而不是单字重叠：单字会把「学费标准」折到「学习计划」
（共享「学」），二元组下二者交集为空。见 `tests/test_library_vocab.py`。
"""

from __future__ import annotations

import re
import unicodedata

# 折算阈值。0.5 是按真实词表标定的：
#   博客记录 → 博客        0.50  折
#   单片机编程 → 单片机     0.60  折
#   服务基类 → 服务        0.50  折
#   子代理机制 → 代理系统   0.40  不折（确实是更具体的另一个主题）
#   学费标准 → 学习计划     0.25  不折（只共享一个「学」字）
MATCH_THRESHOLD = 0.5

# 最短公共子串：CJK 下单字重合是噪声（「学」「文」「数」到处都是）。
MIN_COMMON_CHARS = 2

_NOISE = re.compile(r"[\s_\-·./\\]+")


def normalize(name: object) -> str:
    """折算键：NFKC 归一 + 折叠大小写 + 去掉空格/连字符/点等噪声字符。

    与 `enrichment._norm_tag` 同义，这里独立实现是为了让本模块可单独测试、
    且不产生反向依赖（enrichment 会 import 本模块）。
    """
    text = unicodedata.normalize("NFKC", str(name or "")).strip().casefold()
    return _NOISE.sub("", text)


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _dice(a: str, b: str) -> float:
    """二元组 Dice 系数。中文没有词边界，二元组是最便宜的「共享词素」近似。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def similarity(proposed: str, existing: str) -> float:
    """0..1。规范化后完全相同为 1.0。

    取两种度量的较大者：

    - **包含**：短的那个是长的那个的子串时，得分 = 短/长。抓「A 是 B 加了
      限定词」这一类（博客记录 ⊃ 博客、单片机编程 ⊃ 单片机、报名表格 ⊃ 报名表）。
    - **二元组 Dice**：抓字序不同但词素重合（配置构建 ↔ 构建配置）。

    **为什么不用最长公共子串**（实测栽过）：中文四字词大量是「限定词+两字
    词素」，任意两个共享同一词素的四字词，最长公共子串比恰好是 0.50 —— 于是
    「功能设计→电路设计」「账户管理→文献管理」「任务分发→任务进度」全部被
    误折。包含关系把这一类干净地挡在外面（二者互不包含），而真正的限定词
    扩展仍然通过。
    """
    a, b = normalize(proposed), normalize(existing)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    ratio = 0.0
    if len(short) >= MIN_COMMON_CHARS and short in long_:
        ratio = len(short) / len(long_)
    return max(ratio, _dice(a, b))


def resolve(proposed: str, vocabulary: list[dict], *,
            threshold: float = MATCH_THRESHOLD) -> tuple[int | None, float]:
    """把一个模型提出的名字折算到词表里最近的标签。

    `vocabulary` 是 `[{"id": int, "name": str}, ...]`（**全量**词表，不是送进
    提示词的那 40 个 —— 折算能看见的越多越好，这不花模型的钱）。
    返回 `(tag_id, score)`；没有够近的返回 `(None, 最高分)`，调用方据此建新词。

    同分取 id 小的：结果必须可复现，否则同一篇文档重跑会落到不同标签上。
    """
    best_id: int | None = None
    best_score = 0.0
    for row in vocabulary:
        try:
            tag_id = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        score = similarity(proposed, str(row.get("name") or ""))
        better = score > best_score
        tie = score == best_score and best_id is not None and tag_id < best_id
        if better or tie:
            best_score = score
            best_id = tag_id
    if best_score >= threshold:
        return best_id, best_score
    return None, best_score
