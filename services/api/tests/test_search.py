"""中文全文检索的对抗性测试。

每个用例对应一个实测踩到的坑，注释里写明它挡住什么。
"""

from __future__ import annotations

import pytest

from app.db.database import connect, init_db
from app.index.search import build_fts_query, index_chunk, search


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    docs = [
        (1, "2026 年 3 月，考察队在马里亚纳海沟西侧发现新的热液喷口群。"
            "喷口周围栖息着大量庞贝蠕虫，其体表共生菌可耐受 80 摄氏度高温。"),
        (2, "硫化物沉积层厚度达 4.2 米，甲烷浓度为周边海水的 340 倍。"),
        (3, "发票编号 HT-2024-0023，金额人民币 12800 元整。"),
        (4, "本科毕业设计：Sn58Bi 钎料的剪切强度随 Ni 含量先升后降。"),
    ]
    for cid, text in docs:
        index_chunk(conn, cid, text)
    conn.commit()
    yield conn
    conn.close()


def _hits(conn, q, limit=20):
    r = search(conn, q, limit=limit)
    return {cid for route in r.values() for cid, _ in route}


class TestQueryCompilation:
    def test_multi_term_uses_and_not_one_phrase(self):
        """多个词必须编译成 AND，不能塞进同一个短语。

        实测缺陷：原实现把整串包成一个短语，要求所有词元在原文里连续出现。
        「庞贝蠕虫 共生菌」因此返回 0 条 —— 而单独搜任一个词都能命中。
        典型的"分开能搜、合起来搜不到"。
        """
        q = build_fts_query("庞贝蠕虫 共生菌", segment=True)
        assert " AND " in q, f"多词没有用 AND 连接：{q}"
        assert q.count('"') >= 4, "每个词应各自成短语"

    def test_single_term_still_phrase(self):
        """单个词仍是短语 —— 保住多字词内部的相邻性。"""
        q = build_fts_query("庞贝蠕虫", segment=True)
        assert q.startswith('"') and q.endswith('"')
        assert " AND " not in q

    def test_empty_query(self):
        assert build_fts_query("", segment=True) == ""
        assert build_fts_query("   ", segment=True) == ""

    def test_quotes_escaped(self):
        """用户输入的引号必须转义，否则 FTS5 语法错。"""
        q = build_fts_query('带"引号"的', segment=True)
        assert '""' in q


class TestChineseSearch:
    def test_multi_term_across_sentence(self, db):
        """两个词分处不同位置也应命中（AND 而非短语）。"""
        assert 1 in _hits(db, "庞贝蠕虫 共生菌")

    def test_two_char_word(self, db):
        """双字词必须能搜到 —— trigram 只索引三字组，靠 jieba 兜。"""
        assert 1 in _hits(db, "蠕虫")

    def test_cross_chunk_and_returns_nothing(self, db):
        """AND 语义：分处不同分片的词不应命中。

        这不是 bug 而是正确行为，锁住它防止有人"修"成 OR。
        """
        assert _hits(db, "庞贝蠕虫 甲烷") == set()

    def test_hyphenated_id(self, db):
        """带连字符的编号 —— FTS5 把 `-` 当 NOT，必须被短语引号屏蔽。"""
        assert 3 in _hits(db, "HT-2024-0023")

    def test_alphanumeric_mixed(self, db):
        assert 4 in _hits(db, "Sn58Bi 剪切强度")

    def test_no_match_returns_empty(self, db):
        assert _hits(db, "完全不存在的词汇XYZQ") == set()

    def test_empty_query_safe(self, db):
        """空查询不能抛异常。"""
        assert _hits(db, "   ") == set()
