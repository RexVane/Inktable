"""中文全文检索的对抗性测试。

每个用例对应一个实测踩到的坑，注释里写明它挡住什么。
"""

from __future__ import annotations

import numpy as np
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


def test_unfiltered_vector_search_falls_back_to_knn_without_matrix(
    tmp_path, monkeypatch,
):
    """矩阵不可用时必须退回 KNN，且绝不在查询路径上做逐行导出。

    逐行从 vec0 取向量实测约 5ms/行（71378 条 369.7 秒）。矩阵路径改为整块
    读影子表后这个代价没了，但影子表是 sqlite-vec 的内部布局，扩展升级后
    可能不再匹配 —— 那时必须退回 KNN 慢一点，而不是把语义检索变成零结果，
    更不能回落到逐行导出。
    """
    from app.index import vector as vec

    conn = connect(str(tmp_path / "no-matrix.db"))
    init_db(conn)
    try:
        rng = np.random.default_rng(5)
        vec.upsert(conn, [
            (chunk_id, rng.normal(size=vec.DIM).astype(np.float32))
            for chunk_id in range(1, 6)
        ])
        conn.commit()
        vec._invalidate_cache()

        monkeypatch.setattr(vec, "_shadow_bulk_vectors", lambda *_a, **_k: None)
        monkeypatch.setattr(
            vec, "_search_knn",
            lambda _conn, _query, limit, candidate_ids: [(limit, 0.9)],
        )
        assert vec.search(
            conn, np.ones(vec.DIM, dtype=np.float32), limit=7,
        ) == [(7, 0.9)]
    finally:
        conn.close()
        vec._invalidate_cache()


def test_unfiltered_vector_search_uses_matrix_when_available(tmp_path, monkeypatch):
    """矩阵可用时无过滤主路径必须走矩阵乘，不再每次付 KNN 的全表扫描。

    这条用例跑的是真实的影子表重建路径（不打桩），所以它同时守住了
    「整块读出来的向量顺序与 rowid 对得上」。
    """
    from app.index import vector as vec

    conn = connect(str(tmp_path / "matrix.db"))
    init_db(conn)
    try:
        vectors = np.zeros((3, vec.DIM), dtype=np.float32)
        vectors[0, 0] = 1.0
        vectors[1, 1] = 1.0
        vectors[2, 0], vectors[2, 1] = 0.6, 0.8
        vec.upsert(conn, list(zip([11, 22, 33], vectors)))
        conn.commit()
        vec._invalidate_cache()

        monkeypatch.setattr(
            vec, "_search_knn",
            lambda *_a, **_k: pytest.fail("矩阵可用时不应退回 KNN"),
        )
        # Route selection should not depend on the host's momentary free
        # memory while Ollama/backfill is running beside the test suite.
        monkeypatch.setattr(vec, "_matrix_budget_allows", lambda _rows: True)
        query = np.zeros(vec.DIM, dtype=np.float32)
        query[1] = 1.0
        hits = vec.search(conn, query, limit=2)
        assert [chunk_id for chunk_id, _score in hits] == [22, 33]
        assert hits[0][1] == pytest.approx(1.0, abs=1e-5)
        assert hits[1][1] == pytest.approx(0.8, abs=1e-5)
    finally:
        conn.close()
        vec._invalidate_cache()


def test_vector_search_above_legacy_limit_uses_matrix_when_budget_allows(monkeypatch):
    """The former 100k row cutoff must not force a multi-second KNN scan."""
    from app.index import vector as vec

    monkeypatch.setattr(vec, "count", lambda _conn: vec.INMEM_LIMIT + 1)
    monkeypatch.setattr(vec, "_matrix_budget_allows", lambda _rows: True)
    monkeypatch.setattr(
        vec, "_search_inmem", lambda *_args, **_kwargs: [(17, 0.99)],
    )
    monkeypatch.setattr(
        vec, "_search_knn", lambda *_args, **_kwargs: pytest.fail("不应走 KNN"),
    )

    hits = vec.search(object(), np.ones(vec.DIM, dtype=np.float32), limit=5)
    assert hits == [(17, 0.99)]


def test_matrix_budget_respects_available_memory_and_explicit_cap(monkeypatch):
    from app.index import vector as vec

    monkeypatch.delenv("INKTABLE_VECTOR_NO_MATRIX", raising=False)
    monkeypatch.setenv("INKTABLE_VECTOR_CACHE_MB", "1024")
    monkeypatch.setenv("INKTABLE_VECTOR_CACHE_RESERVE_MB", "512")
    monkeypatch.setattr(vec, "_available_memory_bytes", lambda: 2 * 1024**3)
    assert vec._matrix_budget_allows(100_001)

    monkeypatch.setattr(vec, "_available_memory_bytes", lambda: 700 * 1024**2)
    assert not vec._matrix_budget_allows(100_001)

    monkeypatch.setattr(vec, "_available_memory_bytes", lambda: 4 * 1024**3)
    monkeypatch.setenv("INKTABLE_VECTOR_CACHE_MB", "256")
    assert not vec._matrix_budget_allows(100_001)


def test_candidate_vectors_reuse_existing_cache_above_legacy_limit(monkeypatch):
    from app.index import vector as vec

    ids = np.asarray([11, 22, 33], dtype=np.int64)
    matrix = np.eye(3, vec.DIM, dtype=np.float32)
    monkeypatch.setattr(vec, "_cached_matrix", lambda _conn: (ids, matrix))

    got_ids, got = vec._load_inmem_matrix(object(), [33, 11])
    assert got_ids.tolist() == [11, 33]
    assert np.array_equal(got, matrix[[0, 2]])


def test_matrix_cache_rebuild_is_single_flight(tmp_path, monkeypatch):
    """Startup warmup and the first request must not build two full matrices."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from app.index import vector as vec

    path = tmp_path / "single-flight.db"
    setup = connect(path)
    init_db(setup)
    vec.upsert(setup, [(1, np.ones(vec.DIM, dtype=np.float32))])
    setup.commit()
    setup.close()

    first = connect(path)
    second = connect(path)
    original = vec._shadow_bulk_vectors
    calls = 0
    calls_lock = threading.Lock()

    def counted(conn, dim=vec.DIM):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.1)
        return original(conn, dim)

    monkeypatch.setattr(vec, "_shadow_bulk_vectors", counted)
    monkeypatch.setattr(vec, "_matrix_budget_allows", lambda _rows: True)
    vec._invalidate_cache()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(vec._cached_matrix, [first, second]))
        assert calls == 1
        assert all(result is not None for result in results)
    finally:
        first.close()
        second.close()
        vec._invalidate_cache()


def test_matrix_cache_detects_external_same_row_replacement(tmp_path, monkeypatch):
    """data_version invalidates a cache even when count/max(rowid) stay equal."""
    from app.index import vector as vec

    path = tmp_path / "data-version.db"
    reader = connect(path)
    init_db(reader)
    old = np.zeros(vec.DIM, dtype=np.float32)
    old[0] = 1.0
    vec.upsert(reader, [(7, old)])
    reader.commit()
    monkeypatch.setattr(vec, "_matrix_budget_allows", lambda _rows: True)
    vec._invalidate_cache()
    assert vec._cached_matrix(reader)[1][0, 0] == pytest.approx(1.0)

    writer = connect(path)
    new = np.zeros(vec.DIM, dtype=np.float32)
    new[1] = 1.0
    writer.execute("DELETE FROM chunks_vec WHERE rowid = 7")
    writer.execute(
        "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
        (7, vec._serialize(new)),
    )
    writer.commit()
    writer.close()
    try:
        rebuilt = vec._cached_matrix(reader)[1]
        assert rebuilt[0, 0] == pytest.approx(0.0)
        assert rebuilt[0, 1] == pytest.approx(1.0)
    finally:
        reader.close()
        vec._invalidate_cache()


def test_shadow_bulk_vectors_match_vec0_rows(tmp_path):
    """整块读影子表重建的向量，必须与 vec0 逐行读出的逐字节一致。

    这条路径踩的是 sqlite-vec 的内部布局（向量按每块 1024 条打包成一个
    blob），换来 327 倍的重建速度：71378 条从 369.7 秒降到 1.1 秒。代价是
    布局假设一旦随扩展升级失效，必须能被测出来 —— 而不是给出错位的向量，
    让语义检索静默排错。
    """
    from app.index import vector as vec

    conn = connect(str(tmp_path / "shadow.db"))
    init_db(conn)
    try:
        rng = np.random.default_rng(3)
        rows = [
            (chunk_id, rng.normal(size=vec.DIM).astype(np.float32))
            for chunk_id in range(1, 40)
        ]
        vec.upsert(conn, rows)
        conn.commit()

        built = vec._shadow_bulk_vectors(conn)
        assert built is not None, "影子表布局不符预期"
        ids, matrix = built
        assert sorted(ids.tolist()) == [chunk_id for chunk_id, _ in rows]
        for chunk_id, _expected in rows:
            position = int(np.flatnonzero(ids == chunk_id)[0])
            stored = np.frombuffer(
                conn.execute(
                    "SELECT embedding FROM chunks_vec WHERE rowid = ?", (chunk_id,),
                ).fetchone()[0],
                dtype=np.float32,
            )
            assert np.array_equal(matrix[position], stored)
    finally:
        conn.close()


def test_shadow_bulk_vectors_respect_deletions(tmp_path):
    """删掉的 rowid 不能出现在重建结果里 —— 靠 validity 位图，不是靠行数。

    位图读错会让已删除分片的向量继续参与检索，表现为搜到打不开的结果。
    """
    from app.index import vector as vec

    conn = connect(str(tmp_path / "shadow-del.db"))
    init_db(conn)
    try:
        rng = np.random.default_rng(11)
        vec.upsert(conn, [
            (chunk_id, rng.normal(size=vec.DIM).astype(np.float32))
            for chunk_id in range(1, 20)
        ])
        vec.delete(conn, [2, 5, 17])
        conn.commit()

        built = vec._shadow_bulk_vectors(conn)
        assert built is not None
        ids, matrix = built
        assert {2, 5, 17}.isdisjoint(ids.tolist())
        assert len(ids) == vec.count(conn) == 16
        assert matrix.shape == (16, vec.DIM)
    finally:
        conn.close()


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

    def test_explicit_filename_query_has_an_independent_recall_route(self, db):
        db.execute(
            """INSERT INTO files
               (volume_uuid, inode, content_id, path, name, ext, size, state, detected_at)
               VALUES ('v', 1, 1, '/docs/site.docx', '平台网站思路(1).docx',
                       '.docx', 1, 'registered', 1)"""
        )
        db.commit()

        result = search(db, "哪份 DOCX 文件记录了平台网站思路")

        assert result["filename:metadata"]
        assert result["filename:metadata"][0][0] == 1


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
