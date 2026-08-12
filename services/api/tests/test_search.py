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
    conn.execute(
        "INSERT INTO contents (id, sha256, size, parse_state) "
        "VALUES (1, 'search-fixture', 1, 'indexed')"
    )
    for cid, text in docs:
        conn.execute(
            """INSERT INTO chunks
               (id, content_id, ordinal, text, text_hash, index_version)
               VALUES (?, 1, ?, ?, ?, 1)""",
            (cid, cid - 1, text, f"hash-{cid}"),
        )
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
        """Child 词法 AND 仍不跨片；Document 软路由可以召回同文档证据。

        M2 前这个测试要求全管线为空。分层索引上线后，Document 路由的职责
        正是识别同文档的跨片问题，所以这里只锁住 Child 词法语义。
        """
        result = search(db, "庞贝蠕虫 甲烷")
        assert result["jieba"] == []
        assert result["trigram"] == []
        assert {cid for cid, _ in result["substr"]} == {1, 2}
        assert all(score == 1.0 for _cid, score in result["substr"])

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


class TestSegmentationFailureFallback:
    """分词切错词边界时的兜底。

    统计分词器必然在未登录词（专有名词、新词）上切错，这不是能靠
    换分词器或调参解决的问题。实测 jieba 把「汝窑天青釉」切成
    `汝窑天 / 青釉` —— 于是：
      · jieba 路：索引里没有「汝窑」词元 → 0 命中
      · trigram 路：「汝窑」2 字，短于 3 字符组 → 0 命中
    两路同时失效，一个明明在文档里的词完全搜不到。
    """

    @pytest.fixture
    def db(self, tmp_path):
        from app.db.database import connect, init_db
        from app.index.search import index_chunk

        conn = connect(":memory:")
        init_db(conn)
        conn.execute(
            "INSERT INTO contents (id, sha256, size) VALUES (1, 'x', 1)"
        )
        conn.execute(
            "INSERT INTO chunks (id, content_id, ordinal, text, text_hash, section_path) "
            "VALUES (1, 1, 0, ?, 'h', ?)",
            ("汝窑天青釉的玛瑙入釉说法缺乏实证。", "宋代五大名窑概述"),
        )
        index_chunk(conn, 1, "汝窑天青釉的玛瑙入釉说法缺乏实证。", "宋代五大名窑概述")
        conn.commit()
        yield conn
        conn.close()

    def test_mis_segmented_term_still_found(self, db):
        """jieba 切错边界的词必须靠子串兜底找到。"""
        assert 1 in _hits(db, "汝窑")

    def test_mis_segmented_multi_term(self, db):
        assert 1 in _hits(db, "汝窑 天青釉")

    def test_heading_text_is_searchable(self, db):
        """标题里的词必须能搜到。

        标题文字只存在 chunks.section_path，不在 text 里。
        漏掉它意味着用户搜自己文档的标题却没有结果 —— 最刺眼的检索盲区。
        """
        assert 1 in _hits(db, "五大名窑")

    def test_fallback_does_not_create_false_positives(self, db):
        """兜底不能把不存在的词也匹配上。"""
        assert _hits(db, "完全不存在的词XYZQ") == set()
