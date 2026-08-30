"""标签折算的边界 —— app/library/vocab.py。

用例全部来自真实语料的实测输出（本机 qwen3:8b + 用户 226 篇已整理文档的
词表），不是构造的例子：

- 该折的：模型按名字输出时几乎不复用已有词，词表有「博客」它写「博客记录」、
  有「单片机」它写「单片机编程」、有「服务」它写「服务基类」。不折算就等于
  把 v2 的「词表膨胀」原样换回来。
- 不该折的：「学费标准」和「学习计划」只共享一个「学」字。用单字重叠做度量
  会把二者折在一起 —— 这就是本模块用二元组而不是单字的原因。
"""

from __future__ import annotations

from app.library import vocab


def test_normalize_folds_width_case_and_noise() -> None:
    assert vocab.normalize("ＡＩ工具") == vocab.normalize("AI工具")
    assert vocab.normalize("Web Agent") == vocab.normalize("web-agent")
    assert vocab.normalize("  机器 学习 ") == vocab.normalize("机器-学习")
    assert vocab.normalize(None) == ""


def test_identical_after_normalization_scores_one() -> None:
    assert vocab.similarity("CMake 配置", "cmake配置") == 1.0


def test_qualifier_suffix_folds_back_to_the_base_word() -> None:
    """模型爱加限定词：博客→博客记录、单片机→单片机编程。这些必须折回去。

    分数全部来自 30 篇离线对照的实际输出。
    """
    for proposed, existing in [("博客记录", "博客"), ("单片机编程", "单片机"),
                               ("服务基类", "服务"), ("报名表格", "报名表"),
                               ("招生报名表", "报名表"), ("AI科研工具", "AI科研"),
                               ("插件生命周期", "生命周期管理")]:
        score = vocab.similarity(proposed, existing)
        assert score >= vocab.MATCH_THRESHOLD, f"{proposed}→{existing} 得分 {score}"


def test_single_shared_character_is_not_a_match() -> None:
    """「学费标准」与「学习计划」只共享「学」。单字度量会把它们折在一起。"""
    assert vocab.similarity("学费标准", "学习计划") < vocab.MATCH_THRESHOLD
    assert vocab.similarity("足球联赛", "网络协议") < vocab.MATCH_THRESHOLD
    # 更具体的主题不该被吞进宽泛的旧词
    assert vocab.similarity("子代理机制", "代理系统") < vocab.MATCH_THRESHOLD


def test_shared_two_char_morpheme_alone_is_not_a_match() -> None:
    """回归：这是第一版用「最长公共子串」时真实发生的一批误折。

    中文四字词大量是「限定词 + 两字词素」，共享同一词素时最长公共子串比恰好
    是 0.50，于是全部越过阈值。改用包含关系后二者互不包含，Dice 也只有 0.33。
    每一对都来自 30 篇离线对照的实际输出。
    """
    for proposed, existing in [
        ("功能设计", "电路设计"),      # 共享「设计」
        ("账户管理", "文献管理"),      # 共享「管理」
        ("配置管理", "文献管理"),
        ("主题管理", "文献管理"),
        ("任务分发", "任务进度"),      # 共享「任务」
        ("编码代理", "编码规范"),
        ("组件规范", "编码规范"),
        ("系统提示", "代理系统"),
        ("个人申请", "个人博客"),
        ("资源清理", "免费资源"),
        ("数据结构", "树形结构"),
    ]:
        score = vocab.similarity(proposed, existing)
        assert score < vocab.MATCH_THRESHOLD, f"{proposed}→{existing} 误折，得分 {score}"


def test_reordered_morphemes_still_match() -> None:
    """字序不同但词素重合 —— 最长公共子串抓不到，二元组 Dice 抓得到。"""
    assert vocab.similarity("配置构建", "构建配置") >= vocab.MATCH_THRESHOLD


def test_resolve_picks_the_nearest_existing_tag() -> None:
    vocabulary = [
        {"id": 3, "name": "博客"},
        {"id": 7, "name": "嵌入式开发"},
        {"id": 9, "name": "网络协议"},
    ]
    tag_id, score = vocab.resolve("博客记录", vocabulary)
    assert tag_id == 3 and score >= vocab.MATCH_THRESHOLD


def test_resolve_returns_none_when_nothing_is_close() -> None:
    """够不着就该建新词 —— 这正是修好「标签不符内容」的那一半。

    足球比赛记录在这份技术词表里没有任何对应，硬塞一个已有 id 就是
    v3 的病根（实测它会返回「调试日志 / 配置接口」）。
    """
    vocabulary = [
        {"id": 1, "name": "嵌入式开发"},
        {"id": 2, "name": "调试日志"},
        {"id": 3, "name": "配置接口"},
    ]
    tag_id, score = vocab.resolve("足球联赛", vocabulary)
    assert tag_id is None
    assert score < vocab.MATCH_THRESHOLD


def test_resolve_is_deterministic_on_ties() -> None:
    """同分取 id 小的：同一篇文档重跑必须落到同一个标签上。"""
    vocabulary = [{"id": 40, "name": "构建配置"}, {"id": 12, "name": "构建配置"}]
    assert vocab.resolve("构建配置", vocabulary)[0] == 12
    assert vocab.resolve("构建配置", list(reversed(vocabulary)))[0] == 12


def test_resolve_skips_malformed_rows_instead_of_raising() -> None:
    vocabulary = [{"name": "缺 id"}, {"id": "x", "name": "坏 id"},
                  {"id": 5, "name": "博客"}]
    assert vocab.resolve("博客记录", vocabulary)[0] == 5
