"""借鉴 marginalia 的三项改进：文档主题摘要 / 调查日志 / 逐字引文核验。

三项的共同要求是**不改变既有行为的默认路径**：
- 摘要缺失时 documents_fts 的索引文本与引入该列前逐字一致；
- 调查日志只记成功回答，且绝不作为证据进入 LLM 上下文；
- 引文核验默认只诊断，`ORDO_QUOTE_ENFORCE=1` 才剔除引用。
"""

from __future__ import annotations

import sys

import pytest

from app.db.database import connect, init_db
from app.index.hierarchy import document_index_text
from app.index.search import segment_for_index
from app.qa import journal
from app.qa.quotes import (
    QUOTE_BLOCK_MARK,
    failing_tags,
    prompt_clause,
    split_block,
    verify,
)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDO_DB", str(tmp_path / "lib.db"))
    c = connect()
    init_db(c)
    yield c
    c.close()


# ---------------------------------------------------------------- A 文档摘要

def test_document_index_text_without_abstract_is_byte_identical_to_legacy():
    """摘要缺失时必须与「未引入该列」时逐字一致 —— 这是纯增量的定义。

    若这条不成立，那么在摘要回填完成之前，所有文档的检索行为都会漂移，
    而漂移的原因（多了一个空行）跟检索质量毫无关系。
    """
    legacy = segment_for_index("标题\n正文截断")
    assert document_index_text("标题", None, "正文截断") == legacy
    assert document_index_text("标题", "", "正文截断") == legacy


def test_document_index_text_puts_abstract_before_truncation():
    """摘要在前：BM25 不看位置，但人在排查索引内容时看的是顺序。"""
    text = document_index_text("标题", "主题摘要", "正文截断")
    assert text == segment_for_index("标题\n主题摘要\n正文截断")
    assert text.index("主题") < text.index("正文")


def test_abstract_columns_exist_and_default_null(conn):
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(document_representations)")}
    assert {"abstract", "abstract_model"} <= cols
    conn.execute(
        "INSERT INTO contents (id, sha256, size) VALUES (1, 'sha', 1)")
    conn.execute(
        """INSERT INTO document_representations
           (content_id, index_version, title, summary_text, full_text, text_hash)
           VALUES (1, 1, 't', 's', 'f', 'h')""")
    row = conn.execute(
        "SELECT abstract, abstract_model FROM document_representations").fetchone()
    assert row["abstract"] is None and row["abstract_model"] is None


def test_abstract_prompt_forbids_introducing_absent_concepts():
    """prompt 必须显式禁止引入文档没有的概念。

    第一版 prompt 举例「『路径穿越』也写『目录遍历』」，结果模型把这两个词
    抄进了一篇讲检索延迟的文档摘要（原文出现 0 次）。摘要进 FTS，于是查
    「路径穿越」命中不相关文档 —— 索引被静默污染。
    """
    from app.index import abstract as abstract_mod
    prompt = abstract_mod._PROMPT
    assert "路径穿越" not in prompt and "目录遍历" not in prompt
    assert "确实出现或确实讨论" in prompt


def test_abstract_strips_thinking_block():
    """推理模型的 <think> 段不能进索引 —— 那是模型的自言自语，不是文档主题。"""
    from app.index.abstract import _strip_thinking
    assert _strip_thinking("<think>让我想想</think>真正的摘要") == "真正的摘要"
    assert _strip_thinking("<think>只有思考") == ""


# ---------------------------------------------------------------- B 调查日志

def test_journal_records_only_non_empty(conn):
    with conn:
        ok = journal.record(conn, "问题", "回答", content_ids=[3, 1, 3])
        assert journal.record(conn, "", "回答") is None
        assert journal.record(conn, "问题", "") is None
    assert ok is not None
    rows = journal.recent(conn)
    assert len(rows) == 1
    # content_ids 去重并排序：同一份资料被多次引用不该在日志里重复
    assert rows[0]["content_ids"] == [1, 3]


def test_journal_search_and_delete_keep_fts_in_sync(conn):
    with conn:
        jid = journal.record(conn, "银行家算法用到哪些数据结构", "用到四个结构")
    assert [r["question"] for r in journal.search(conn, "银行家算法")]
    with conn:
        assert journal.delete(conn, jid) is True
    # contentless FTS 必须显式删；漏了就会搜到已删除的记录
    assert journal.search(conn, "银行家算法") == []
    assert journal.delete(conn, 999) is False


def test_journal_related_requires_real_overlap(conn):
    with conn:
        journal.record(conn, "银行家算法用到哪些数据结构", "四个结构")
    assert journal.related(conn, "银行家算法用到哪些数据结构")
    # 只共享「什么」「是」这类虚词的不算问过类似问题，否则用户会学会忽略提示
    assert journal.related(conn, "什么是死锁") == []
    assert journal.related(conn, "今天天气怎么样") == []
    assert journal.related(conn, "") == []


def test_journal_clear_empties_both_tables(conn):
    with conn:
        journal.record(conn, "问题一", "回答一")
        journal.record(conn, "问题二", "回答二")
        assert journal.clear(conn) == 2
    assert journal.recent(conn) == []
    assert journal.search(conn, "问题") == []


def test_journal_is_never_used_as_retrieval_evidence():
    """journal 不得进入检索/上下文装配。

    它存的是模型自己的输出。作为证据喂回去，模型可能引用「自己上次说的
    话」而不是原文，H8 的证据链就断了 —— 而四条后置校验看到的是一个格式
    完全合法的引用，发现不了这种断裂。
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for rel in ["retrieval/pipeline.py", "retrieval/compress.py",
                "qa/answer.py", "index/search.py"]:
        src = (root / rel).read_text(encoding="utf-8")
        assert "journal" not in src, f"{rel} 不应引用 journal"


# ---------------------------------------------------------------- D 引文核验

def test_split_block_extracts_quotes_and_removes_them_from_body():
    raw = ("事实一 [C1]\n事实二 [C2]\n" + QUOTE_BLOCK_MARK
           + "\nC1: 逐字原文片段一\nC2: 逐字原文片段二\n")
    body, quotes = split_block(raw)
    # 引文块自带 `C1:` 字样，留在正文里会被当成引用标记
    assert QUOTE_BLOCK_MARK not in body
    assert "逐字原文片段一" not in body
    assert quotes == {"C1": "逐字原文片段一", "C2": "逐字原文片段二"}


def test_split_block_without_marker_is_passthrough():
    raw = "事实一 [C1]"
    assert split_block(raw) == (raw, {})


def test_verify_flags_hallucinated_and_too_short_quotes():
    sources = {"C1": "用到 Available、Max、Allocation、Need 四个数据结构。",
               "C2": "另一段完全不同的原文。"}
    report = verify({"C1": "Available、Max、Allocation",
                     "C2": "这句话原文里没有",
                     "C3": "无主之引"}, sources)
    per = report["per_tag"]
    assert per["C1"]["status"] == "ok"
    assert per["C2"]["status"] == "not_found"
    assert per["C3"]["status"] == "unknown_tag"
    assert report["verified"] == 1 and report["failed"] == 1
    assert failing_tags(report) == {"C2"}


def test_verify_too_short_quote_is_neither_pass_nor_fail():
    """两个字的引文在任何文本里都能命中，核验会退化成永真判据。

    所以它单独归一类，不能计进「核验通过」的分子 —— 否则指标会因为模型
    偷懒而变好看。
    """
    report = verify({"C1": "是"}, {"C1": "这里是一段原文。"})
    assert report["per_tag"]["C1"]["status"] == "too_short"
    assert report["verified"] == 0 and report["failed"] == 0


def test_verify_normalizes_width_and_whitespace_only():
    """归一化只吃渠道差异（全角、空白、零宽），不做语义改写。

    归一化越宽，核验越接近永真。这里守住边界：换个词就必须核不上。
    """
    assert verify({"C1": "Available、Max"},
                  {"C1": "用到　Available 、 Max"})["per_tag"]["C1"]["status"] == "ok"
    assert verify({"C1": "Available、Min"},
                  {"C1": "用到 Available、Max"})["per_tag"]["C1"]["status"] == "not_found"


def test_quote_enforcement_is_off_by_default(monkeypatch):
    """默认只诊断：改答案行为必须先有 65 题 QA 复验的基线。"""
    monkeypatch.delenv("ORDO_QUOTE_ENFORCE", raising=False)
    from app.qa import quotes
    assert quotes.enforcing() is False
    monkeypatch.setenv("ORDO_QUOTE_ENFORCE", "1")
    assert quotes.enforcing() is True


def test_prompt_clause_matches_parser_format():
    """prompt 里承诺的格式必须与解析器实际接受的一致。

    这两处分开写就必然漂移：prompt 改成「引文：」而解析器还认 `C1:`，
    核验会静默地全部变成 total=0，看起来像「没有问题」。
    """
    clause = prompt_clause()
    assert QUOTE_BLOCK_MARK in clause
    assert "C1: 原文片段" in clause
    body, quotes = split_block(f"x [C1]\n{QUOTE_BLOCK_MARK}\nC1: 原文片段\n")
    assert quotes == {"C1": "原文片段"}


# ---------------------------------------------------- Windows 云占位检测修复

def test_windows_cloud_placeholder_is_detected():
    """`is_dataless` 原先只看 macOS 的 st_flags，Windows 上恒为 False。

    后果不是「少一个优化」：云端未下载的文件一路走到读取，`open()` 抛
    `OSError [Errno 22]`，被记成 `hash_failed`。真实库里 624 个 WPS 云盘文件
    （326 PDF + 257 DOCX）就是这样静默丢掉的 —— 用户看到的是「索引坏了」，
    实际是「文件没下载」，两者处置完全不同。
    """
    from app.domain.identity import (
        FILE_ATTRIBUTE_OFFLINE,
        FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
        FILE_ATTRIBUTE_RECALL_ON_OPEN,
        is_cloud_placeholder,
    )

    class FakeStat:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    if sys.platform == "win32":
        # 实测那个 WPS 文件正是 0x400020 = RECALL_ON_DATA_ACCESS | ARCHIVE
        assert is_cloud_placeholder(FakeStat(st_file_attributes=0x400020)) is True
        for bit in (FILE_ATTRIBUTE_OFFLINE, FILE_ATTRIBUTE_RECALL_ON_OPEN,
                    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
            assert is_cloud_placeholder(FakeStat(st_file_attributes=bit)) is True
        # 普通本地文件（ARCHIVE 位）不是占位
        assert is_cloud_placeholder(FakeStat(st_file_attributes=0x20)) is False
        # 缺字段不能抛，也不能误判成占位
        assert is_cloud_placeholder(FakeStat()) is False
    else:
        from app.domain.identity import SF_DATALESS
        assert is_cloud_placeholder(FakeStat(st_flags=SF_DATALESS)) is True
        assert is_cloud_placeholder(FakeStat(st_flags=0)) is False
        assert is_cloud_placeholder(FakeStat()) is False


def test_hash_failed_records_are_not_short_circuited_as_unchanged():
    """failed 记录必须在下次扫描被重试，否则占位检测修好了也没用。

    `register_file` 的「未变化」短路条件里显式排除了 `HASH_FAILED`；这条
    断言守住它 —— 少了它，那 624 条会因为 size/mtime 没变而被永远跳过。
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "watcher" / "scanner.py").read_text(encoding="utf-8")
    assert 'row["error_code"] != HASH_FAILED' in src
