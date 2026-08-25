from __future__ import annotations

from datetime import datetime
import time

from app.db.database import connect, init_db
from app.qa.metadata import answer_metadata


def _db_with_files(*files: tuple[str, str, float]):
    conn = connect(":memory:")
    init_db(conn)
    root = "/tmp/meta"
    conn.execute(
        "INSERT INTO sources(name,path,kind,discovered_by,enabled,created_at) "
        "VALUES('B盘',?,'system','fixed_drive',1,?)",
        (root, time.time()),
    )
    for inode, (name, ext, mtime) in enumerate(files, start=1):
        conn.execute(
            """INSERT INTO files
               (volume_uuid,inode,path,name,source_id,ext,size,state,mtime,detected_at)
               VALUES('v',?,?,?,?,?,1,'registered',?,?)""",
            (inode, root + "/" + name, name, 1, ext, mtime, mtime),
        )
    conn.commit()
    return conn


def test_metadata_count_does_not_need_rag():
    conn = _db_with_files(("a.pdf", ".pdf", time.time()))
    result = answer_metadata(conn, "我的 PDF 文件有多少个？")
    assert result is not None
    assert result.query_kind == "metadata_count"
    assert "1 个" in result.answer
    conn.close()


def test_content_questions_are_not_hijacked_by_metadata_route():
    conn = _db_with_files(("a.pdf", ".pdf", time.time()))
    for question in (
        "这份 PDF 里讲了多少种情况？",
        "最近那个方案里提到多少种方法？",
        "本周的会议纪要说了什么？",
        "这份文档中为什么这样设计？",
    ):
        assert answer_metadata(conn, question) is None, question
    conn.close()


def test_recent_applies_a_real_30_day_window():
    now = datetime(2026, 8, 19, 12).timestamp()
    conn = _db_with_files(
        ("recent.pdf", ".pdf", now - 5 * 86400),
        ("old.pdf", ".pdf", now - 60 * 86400),
    )
    result = answer_metadata(conn, "最近的 PDF 文件有多少个？", now=now)
    assert result is not None
    assert "1 个" in result.answer
    conn.close()


def test_week_ranges_use_calendar_mondays_not_rolling_windows():
    # 2026-08-19 is Wednesday. “本周” starts Mon 2026-08-17; “上周” is
    # Mon 10th through Sun 16th. A rolling 7-day implementation would include
    # the previous Thursday and fail this contract.
    now = datetime(2026, 8, 19, 12).timestamp()
    conn = _db_with_files(
        ("this-mon.pdf", ".pdf", datetime(2026, 8, 17, 9).timestamp()),
        ("last-thu.pdf", ".pdf", datetime(2026, 8, 13, 9).timestamp()),
        ("two-weeks.pdf", ".pdf", datetime(2026, 8, 9, 23).timestamp()),
    )
    this_week = answer_metadata(conn, "本周的 PDF 文件有多少个？", now=now)
    last_week = answer_metadata(conn, "上周的 PDF 文件有多少个？", now=now)
    assert this_week is not None and "1 个" in this_week.answer
    assert last_week is not None and "1 个" in last_week.answer
    conn.close()


def test_latest_files_returns_a_list_not_the_all_time_count():
    now = datetime(2026, 8, 19, 12).timestamp()
    conn = _db_with_files(
        ("new.md", ".md", now - 60),
        ("old.md", ".md", now - 90 * 86400),
    )
    result = answer_metadata(conn, "最新的文件", now=now)
    assert result is not None
    assert result.query_kind == "metadata_list"
    assert "new.md" in result.answer
    assert "old.md" not in result.answer
    conn.close()
