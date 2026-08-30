"""QueryPlan 辅助：比较类分解、类型提示与数值意图。"""

from __future__ import annotations

from app.retrieval.query import (
    decompose_comparative,
    mentioned_exts,
    wants_numeric_answer,
)


def test_decompose_predicate_after_marker():
    subqueries = decompose_comparative("Ordo 和 PyFTP 分别怎样保护本地服务边界与文件路径")
    assert subqueries == [
        "Ordo 怎样保护本地服务边界与文件路径",
        "PyFTP 怎样保护本地服务边界与文件路径",
    ]


def test_decompose_each_entity_keeps_own_predicate():
    subqueries = decompose_comparative("计算机组成原理和操作系统实验分别使用了哪些核心数据结构")
    assert subqueries == [
        "计算机组成原理 使用了哪些核心数据结构",
        "操作系统实验 使用了哪些核心数据结构",
    ]


def test_decompose_difference_marker_with_predicate_before():
    subqueries = decompose_comparative("墨洞项目与 FTP 课程项目的文件传输定位有什么不同")
    assert subqueries == [
        "墨洞项目 文件传输定位",
        "FTP 课程项目 文件传输定位",
    ]


def test_decompose_ignores_plain_questions():
    assert decompose_comparative("银行家算法用到哪些数据结构") == []
    assert decompose_comparative("宿舍怎么交电费") == []
    assert decompose_comparative("") == []


def test_decompose_accepts_single_character_entities():
    # 型号/代号经常就是一个字符，A/B 与甲/乙比较都应正常分解。
    assert decompose_comparative("A 和 B 分别是什么") == ["A 是什么", "B 是什么"]


def test_mentioned_exts_uses_word_boundaries():
    assert mentioned_exts("哪份 DOCX 文件记录了平台网站思路") == frozenset({".docx", ".doc"})
    assert mentioned_exts("找一下 md 格式的笔记") == frozenset({".md"})
    # systemd / password 不能误命中 md / word
    assert mentioned_exts("systemd 的配置在哪") == frozenset()
    assert mentioned_exts("wifi password 是多少") == frozenset()
    assert mentioned_exts("上周的表格发我") == frozenset({".xlsx", ".xls", ".csv"})


def test_wants_numeric_answer():
    assert wants_numeric_answer("PyFTP 按职责拆成多少个模块")
    assert wants_numeric_answer("FTP 项目要求的最低 TLS 版本是什么")
    assert not wants_numeric_answer("宿舍怎么交电费")
