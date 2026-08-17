from __future__ import annotations

import time

from app.db.database import connect, init_db
from app.qa.metadata import answer_metadata


def test_metadata_count_does_not_need_rag():
    conn = connect(':memory:'); init_db(conn)
    root = '/tmp/meta'
    conn.execute("INSERT INTO sources(name,path,kind,discovered_by,enabled,created_at) VALUES('B盘',?,'system','fixed_drive',1,?)", (root,time.time()))
    conn.execute("INSERT INTO files(volume_uuid,inode,path,name,source_id,ext,size,state,detected_at) VALUES('v',1,?, 'a.pdf',1,'.pdf',1,'registered',?)",(root+'/a.pdf',time.time()))
    conn.commit()
    result=answer_metadata(conn,'我的 PDF 文件有多少个？')
    assert result is not None
    assert result.query_kind == 'metadata_count'
    assert '1 个' in result.answer
    conn.close()
