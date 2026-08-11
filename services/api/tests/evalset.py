"""检索评测集 —— PLAN §18.2 / M6。

**必须在向量检索实现之前标注完成**（方案 §20：M6 先于 B3）。
否则出题会不自觉地贴合已有实现，指标失去意义 —— 这是评测集最常见的失效方式。

30 题构成：
  · 18 题有依据（answerable）—— 答案确实在库中某个 chunk 里
  · 12 题无依据（unanswerable）—— 答案不在库中，用于标定拒答门限

为什么无依据题要 12 而不是方案初版的 5：5 个样本标不出可靠阈值，
4/5 与 5/5 只差一题，"拒答率 ≥0.80" 这个数字本身没有意义（§12.3c）。

出题原则：
  · **查询词与原文表述不重合** —— 否则测的是字符串匹配，不是语义检索
  · 覆盖三类检索难度：精确术语、同义改写、跨段综合
  · ground_truth 用 content_id + 关键词双重锚定，不用 chunk_id
    （chunk_id 会随分片策略变化而失效，方案 §12.5 改 diff 主键时就会变）
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvalCase:
    qid: str
    query: str
    # None 表示答案不在库中（应当拒答）
    doc_hint: str | None          # 期望命中文档的文件名片段
    answer_keywords: list[str] = field(default_factory=list)  # 答案 chunk 应含的词
    difficulty: str = "exact"     # exact / paraphrase / synthesis
    note: str = ""

    @property
    def answerable(self) -> bool:
        return self.doc_hint is not None


# ---------------------------------------------------------------- 有依据（18 题）

ANSWERABLE = [
    # --- 精确术语：查询词在原文出现，测基础召回 ---
    EvalCase("A01", "银行家算法用到哪些数据结构",
             "操作系统实验报告", ["Available", "Max", "Allocation"], "exact"),
    EvalCase("A02", "Sn58Bi 钎料加入纳米镍后抗剪强度峰值是多少",
             "毕业论文", ["48.4", "0.5%"], "exact"),
    EvalCase("A03", "PyFTP 实现了哪三种并发模型",
             "FTP服务器课程设计报告", ["thread", "process", "select"], "exact"),
    EvalCase("A04", "学生社团负责人三星级优秀可以加多少分",
             "学生社团学生骨干综合素质测评", ["35"], "exact"),
    EvalCase("A05", "冯诺依曼机工作方式的基本特点",
             "计算机组成原理期末考试", ["冯", "诺依曼"], "exact"),

    # --- 同义改写：用词与原文不重合，这才是向量检索存在的理由 ---
    EvalCase("A06", "宿舍怎么交电费",
             "宿舍预付费用电", ["充值", "电费"], "paraphrase",
             "原文写'预付费''充值'，查询用口语'交电费'"),
    EvalCase("A07", "低熔点无铅焊料为什么会性能下降",
             "毕业论文", ["富Bi", "偏析", "IMC"], "paraphrase",
             "原文术语'共晶钎料''金属间化合物'，查询用通俗说法"),
    EvalCase("A08", "怎么避免进程互相等待资源卡死",
             "操作系统实验报告", ["死锁", "安全"], "paraphrase",
             "原文'死锁'，查询完全避开该词"),
    EvalCase("A09", "决赛在哪个城市哪所学校举办",
             "选手参赛手册", ["杭州", "浙江音乐学院"], "paraphrase"),
    EvalCase("A10", "网站配色和字体该怎么选",
             "平台网站思路", ["思源", "色系"], "paraphrase",
             "原文'整体视觉色系''字体建议'"),

    # --- 跨段综合：答案需要多个片段，测上下文装配 ---
    EvalCase("A11", "这个 FTP 项目一共写了多少行代码、测试通过多少项",
             "FTP服务器课程设计报告", ["2700", "38"], "synthesis"),
    EvalCase("A12", "镍含量从 0.1% 加到 2% 强度怎么变化",
             "毕业论文", ["47.6", "38.7"], "synthesis",
             "需要串起多个含量点的数据"),
    EvalCase("A13", "社团成员加分需要满足什么前提条件",
             "学生社团学生骨干综合素质测评", ["时长", "活动"], "synthesis"),

    # --- 技术文档：验证 Markdown 与代码类内容的检索 ---
    EvalCase("A14", "墨洞项目的定位是什么",
             "墨洞InkHole", ["定位"], "exact"),
    EvalCase("A15", "Inktable 为什么不用移动文件的方式整理",
             "PLAN", ["索引模式", "移动"], "paraphrase",
             "方案 §2.1 的论证"),
    EvalCase("A16", "中文全文检索为什么需要两个索引",
             "PLAN", ["jieba", "trigram"], "paraphrase"),
    EvalCase("A17", "补码零的表示形式有什么特点",
             "计算机组成原理期末考试", ["补码"], "exact"),
    EvalCase("A18", "祠堂三维模型建议怎么拆分文件",
             "平台网站思路", ["glb", "拆"], "paraphrase"),
]

# ---------------------------------------------------------------- 无依据（12 题）
#
# 这些问题的答案确实不在库中。检索应当返回低分，系统应当拒答而不是硬编。
# 刻意做成"看起来很像库里有"的形态 —— 同领域、同风格、但事实不存在。

UNANSWERABLE = [
    EvalCase("U01", "钨钢刀具的最佳切削速度是多少", None, [], "exact",
             "材料学领域但库里只有钎料，无切削加工内容"),
    EvalCase("U02", "宿舍热水器的使用时间限制", None, [], "exact",
             "同为宿舍主题，但原通知只讲电费"),
    EvalCase("U03", "研究生复试分数线是多少", None, [], "exact",
             "同为学校文件风格，但库里无研招内容"),
    EvalCase("U04", "SMTP 协议的握手流程", None, [], "paraphrase",
             "库里有 FTP 协议实现，无 SMTP"),
    EvalCase("U05", "食堂档口的营业时间", None, [], "exact"),
    EvalCase("U06", "图书馆借书最多能借几本", None, [], "exact"),
    EvalCase("U07", "Redis 集群的分片策略", None, [], "exact",
             "库里有技术文档但不涉及 Redis"),
    EvalCase("U08", "汝窑天青釉的烧成温度", None, [], "exact",
             "陶瓷主题在库中完全不存在"),
    EvalCase("U09", "毕业论文查重率要求低于多少", None, [], "paraphrase",
             "库里有毕业论文正文，但无查重规定"),
    EvalCase("U10", "杭州地铁到浙江音乐学院怎么换乘", None, [], "paraphrase",
             "手册提到浙音但无交通指引"),
    EvalCase("U11", "内存条的时序参数怎么调", None, [], "exact",
             "库里有计算机组成原理但不涉及超频"),
    EvalCase("U12", "社团经费报销的审批流程", None, [], "paraphrase",
             "同为社团文件，但只讲加分不讲经费"),
]

ALL_CASES = ANSWERABLE + UNANSWERABLE


def summary() -> dict:
    from collections import Counter
    diff = Counter(c.difficulty for c in ANSWERABLE)
    return {
        "total": len(ALL_CASES),
        "answerable": len(ANSWERABLE),
        "unanswerable": len(UNANSWERABLE),
        "by_difficulty": dict(diff),
    }
