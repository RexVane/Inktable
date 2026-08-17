"""检索评测集 —— PLAN §18.2 / M6。

**必须在向量检索实现之前标注完成**（方案 §20：M6 先于 B3）。
否则出题会不自觉地贴合已有实现，指标失去意义 —— 这是评测集最常见的失效方式。

77 题构成：
  · 20 题单文档事实定位
  · 10 题同义改写或无关键词重合
  · 10 题同文档跨片综合
  · 10 题跨文档综合
  · 10 题元数据、文件类型和范围问题
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
    doc_hint: str | list[str] | None  # 期望命中文档的文件名片段
    answer_keywords: list[str] = field(default_factory=list)  # 答案 chunk 应含的词
    difficulty: str = "exact"     # exact / paraphrase / synthesis
    note: str = ""
    category: str = ""
    scope: str = "all"

    @property
    def doc_hints(self) -> list[str]:
        if self.doc_hint is None:
            return []
        if isinstance(self.doc_hint, str):
            return [self.doc_hint]
        return list(self.doc_hint)

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

# ---------------------------------------------------------------- v7 扩展：事实定位（补足到 20 题）

V7_FACT = [
    EvalCase("F19", "FTP 服务器默认使用哪个控制端口",
             "FTP服务器课程设计报告", ["2121"], "exact", category="single_fact"),
    EvalCase("F20", "PyFTP 使用什么 Python 版本开发",
             "FTP服务器课程设计报告", ["3.11.9"], "exact", category="single_fact"),
    EvalCase("F21", "FTP 项目支持哪两种数据连接模式",
             "FTP服务器课程设计报告", ["主动", "被动"], "exact", category="single_fact"),
    EvalCase("F22", "FTP 断点续传使用什么命令",
             "FTP服务器课程设计报告", ["REST"], "exact", category="single_fact"),
    EvalCase("F23", "FAT12 的目录项长度是多少字节",
             "操作系统实验报告", ["32 字节"], "exact", category="single_fact"),
    EvalCase("F24", "FAT12 每个 FAT 表项占多少位",
             "操作系统实验报告", ["12 位"], "exact", category="single_fact"),
    EvalCase("F25", "银行家算法判断资源不足时比较哪两个向量",
             "操作系统实验报告", ["Request", "Available"], "exact", category="single_fact"),
    EvalCase("F26", "FTP 未登录时返回什么状态码",
             "FTP服务器课程设计报告", ["530"], "exact", category="single_fact"),
    EvalCase("F27", "PyFTP 默认使用哪种并发模型",
             "FTP服务器课程设计报告", ["thread"], "exact", category="single_fact"),
    EvalCase("F28", "PyFTP 默认总根目录是什么",
             "FTP服务器课程设计报告", ["examples/ftproot"], "exact", category="single_fact"),
    EvalCase("F29", "FTP 项目要求的最低 TLS 版本是什么",
             "FTP服务器课程设计报告", ["TLS 1.2"], "exact", category="single_fact"),
    EvalCase("F30", "FTP 命令未实现时返回什么状态码",
             "FTP服务器课程设计报告", ["502"], "exact", category="single_fact"),
    EvalCase("F31", "PyFTP 按职责拆成多少个模块",
             "FTP服务器课程设计报告", ["10 个模块"], "exact", category="single_fact"),
]

# ---------------------------------------------------------------- v7 扩展：同义改写（补足到 10 题）

V7_PARAPHRASE = [
    EvalCase("P19", "网络文件传到一半断了以后怎样接着传",
             "FTP服务器课程设计报告", ["REST", "断点续传"], "paraphrase",
             "查询避开协议命令名", category="paraphrase"),
    EvalCase("P20", "怎样防止用户用相对路径跑出自己的文件目录",
             "FTP服务器课程设计报告", ["目录穿越", "chroot"], "paraphrase",
             "查询使用口语描述路径逃逸", category="paraphrase"),
]

# ---------------------------------------------------------------- v7 扩展：同文档跨片综合（补足到 10 题）

V7_SYNTHESIS = [
    EvalCase("S14", "FTP 项目如何同时保证客户端兼容性和传输安全",
             "FTP服务器课程设计报告", ["RFC 959", "TLS"], "synthesis",
             category="cross_chunk"),
    EvalCase("S15", "FTP 项目的四道安全防线分别是什么",
             "FTP服务器课程设计报告", ["目录穿越", "chroot", "登录", "FTPS"],
             "synthesis", category="cross_chunk"),
    EvalCase("S16", "FAT12 读取文件时依次会用到哪些区域和簇链信息",
             "操作系统实验报告", ["BPB", "FAT", "根目录", "数据区"],
             "synthesis", category="cross_chunk"),
    EvalCase("S17", "银行家算法收到资源请求后的完整判断流程是什么",
             "操作系统实验报告", ["Need", "Available", "试分配", "安全"],
             "synthesis", category="cross_chunk"),
    EvalCase("S18", "FTP 项目的基础功能和增强功能分别有哪些",
             "FTP服务器课程设计报告", ["认证", "上传", "断点", "并发"],
             "synthesis", category="cross_chunk"),
    EvalCase("S19", "为什么 FTP 适合作为计算机网络课程设计题目",
             "FTP服务器课程设计报告", ["协议", "并发", "可验证"],
             "synthesis", category="cross_chunk"),
    EvalCase("S20", "PyFTP 使用了哪些 Python 标准库，各自负责什么",
             "FTP服务器课程设计报告", ["socket", "selectors", "ssl", "threading"],
             "synthesis", category="cross_chunk"),
]

# ---------------------------------------------------------------- v7 扩展：跨文档综合

V7_CROSS_DOCUMENT = [
    EvalCase("X01", "Inktable 和 PyFTP 分别怎样保护本地服务边界与文件路径",
             ["PLAN", "FTP服务器课程设计报告"], [], "synthesis",
             category="cross_document"),
    EvalCase("X02", "Inktable 与墨洞项目分别如何处理本地文件和传输",
             ["PLAN", "墨洞InkHole"], [], "synthesis", category="cross_document"),
    EvalCase("X03", "操作系统实验和 FTP 项目分别用了哪些资源或并发管理机制",
             ["操作系统实验报告", "FTP服务器课程设计报告"], [], "synthesis",
             category="cross_document"),
    EvalCase("X04", "Inktable 和 FTP 项目各自如何做测试与验收",
             ["PLAN", "FTP服务器课程设计报告"], [], "synthesis",
             category="cross_document"),
    EvalCase("X05", "中文检索和 FTP 命令解析分别如何处理特殊字符带来的歧义",
             ["PLAN", "FTP服务器课程设计报告"], [], "synthesis",
             category="cross_document"),
    EvalCase("X06", "Inktable 与大湾区智慧城市调研分别如何使用 AI",
             ["PLAN", "大湾区AI智慧城市调研"], [], "synthesis",
             category="cross_document"),
    EvalCase("X07", "墨洞项目与 FTP 课程项目的文件传输定位有什么不同",
             ["墨洞InkHole", "FTP服务器课程设计报告"], [], "synthesis",
             category="cross_document"),
    EvalCase("X08", "Inktable 与 FTP 项目的本地运行架构有哪些共同点和差异",
             ["PLAN", "FTP服务器课程设计报告"], [], "synthesis",
             category="cross_document"),
    EvalCase("X09", "操作系统实验中的文件管理与 Inktable 资料治理有什么不同",
             ["操作系统实验报告", "PLAN"], [], "synthesis",
             category="cross_document"),
    EvalCase("X10", "计算机组成原理和操作系统实验分别使用了哪些核心数据结构",
             ["计算机组成原理", "操作系统实验报告"], [], "synthesis",
             category="cross_document"),
]

# ---------------------------------------------------------------- v7 扩展：元数据、类型与范围

V7_METADATA = [
    EvalCase("M01", "找出文件名中带 FTP 服务器课程设计报告的文档",
             "FTP服务器课程设计报告", [], "metadata", category="metadata"),
    EvalCase("M02", "哪份 Markdown 文件记录了杭州三日旅行规划",
             "杭州三日旅行规划", [], "metadata", category="metadata"),
    EvalCase("M03", "找出 PDF 格式的学生社团综合素质测评文件",
             "学生社团学生骨干综合素质测评", [], "metadata", category="metadata"),
    EvalCase("M04", "哪份 Markdown 文档是墨洞项目全面总结",
             "墨洞InkHole项目全面总结", [], "metadata", category="metadata"),
    EvalCase("M05", "找出 2026 年春季体育课成绩要求文档",
             "体育课成绩评定具体要求", [], "metadata", category="metadata"),
    EvalCase("M06", "哪份文档记录了 Inktable 的 M0 实测结果",
             "M0-RESULTS", [], "metadata", category="metadata"),
    EvalCase("M07", "哪份 Markdown 文件是 Inktable 的完整实施计划",
             "PLAN", [], "metadata", category="metadata"),
    EvalCase("M08", "找出计算机网络课程设计成绩单 PDF",
             "计算机网络课程设计12班-成绩单", [], "metadata", category="metadata"),
    EvalCase("M09", "哪份 DOCX 文件记录了平台网站思路",
             "平台网站思路", [], "metadata", category="metadata"),
    EvalCase("M10", "找出揭阳校区本科生综合素质测评实施细则",
             "揭阳校区本科生综合素质测评实施细则", [], "metadata", category="metadata"),
]

# ---------------------------------------------------------------- 语料漂移后新增的可回答题
#
# 这些题最初标为无依据，但当前内容哈希语料后来收录了直接答案。保留题号
# 并显式重分类，避免为了拒答指标把真实存在的证据错误标成“应拒答”。

CORPUS_DRIFT_ANSWERABLE = [
    EvalCase("U02", "宿舍热水器的使用时间限制",
             "广工大一攻略2.0", ["下午七点", "二十四点"], "exact",
             "当前攻略明确写明热水供应时段", category="single_fact"),
    EvalCase("U04", "SMTP 协议的握手流程",
             "计算机网络第8版_题目答案", ["220", "HELO", "250 OK"],
             "paraphrase", "当前教材答案包含 SMTP 三阶段",
             category="paraphrase"),
    EvalCase("U06", "图书馆借书最多能借几本",
             "广东工业大学2024年学生手册", ["30册"], "exact",
             "当前学生手册明确给出校内读者借阅上限", category="single_fact"),
    EvalCase("U09", "毕业论文查重率要求低于多少",
             "广东工业大学2025届毕业设计（论文）手册", ["20%"],
             "paraphrase", "当前毕业论文手册明确给出相似性阈值",
             category="paraphrase"),
    EvalCase("U10", "杭州地铁到浙江音乐学院怎么换乘",
             "杭州决赛区）选手参赛手册", ["地铁6 号线", "音乐学院站C 口"],
             "paraphrase", "当前参赛手册包含多出发地地铁路线",
             category="paraphrase"),
]

# ---------------------------------------------------------------- 无依据（12 题）
#
# 这些问题的答案确实不在库中。检索应当返回低分，系统应当拒答而不是硬编。
# 刻意做成"看起来很像库里有"的形态 —— 同领域、同风格、但事实不存在。

UNANSWERABLE = [
    EvalCase("U01", "钨钢刀具的最佳切削速度是多少", None, [], "exact",
             "材料学领域但库里只有钎料，无切削加工内容"),
    EvalCase("U03", "研究生复试分数线是多少", None, [], "exact",
             "同为学校文件风格，但库里无研招内容"),
    EvalCase("U05", "食堂档口的营业时间", None, [], "exact"),
    EvalCase("U07", "Redis 集群的分片策略", None, [], "exact",
             "库里有技术文档但不涉及 Redis"),
    EvalCase("U08", "汝窑天青釉的烧成温度", None, [], "exact",
             "陶瓷主题在库中完全不存在"),
    EvalCase("U11", "内存条的时序参数怎么调", None, [], "exact",
             "库里有计算机组成原理但不涉及超频"),
    EvalCase("U12", "社团经费报销的审批流程", None, [], "paraphrase",
             "同为社团文件，但只讲加分不讲经费"),
    EvalCase("U13", "图书馆图书逾期每本每天收取多少超期占用费",
             None, [], "exact", "手册要求缴费但未给出具体金额"),
    EvalCase("U14", "浙江音乐学院南区食堂早餐几点开始供应",
             None, [], "exact", "参赛手册只给出比赛期间午餐和晚餐安排"),
    EvalCase("U15", "毕业论文查重使用哪个检测系统品牌",
             None, [], "paraphrase", "手册只写学校指定检测系统，未写品牌"),
    EvalCase("U16", "SMTP 发送失败后默认间隔多少分钟重试",
             None, [], "exact", "教材说明通信过程但未给出重试间隔"),
    EvalCase("U17", "宿舍热水每立方米收费多少",
             None, [], "exact", "攻略给出供应时间但未给出热水单价"),
]

ANSWERABLE = (
    ANSWERABLE + V7_FACT + V7_PARAPHRASE + V7_SYNTHESIS
    + V7_CROSS_DOCUMENT + V7_METADATA + CORPUS_DRIFT_ANSWERABLE
)
ALL_CASES = ANSWERABLE + UNANSWERABLE


def summary() -> dict:
    from collections import Counter
    diff = Counter(c.difficulty for c in ANSWERABLE)
    categories = Counter(
        c.category or (
            "single_fact" if c.difficulty == "exact" else
            "paraphrase" if c.difficulty == "paraphrase" else
            "cross_chunk"
        )
        for c in ANSWERABLE
    )
    return {
        "total": len(ALL_CASES),
        "answerable": len(ANSWERABLE),
        "unanswerable": len(UNANSWERABLE),
        "by_difficulty": dict(diff),
        "by_category": dict(categories),
    }
