"""B6 问答管线对抗测试 —— 用假 OpenAI 服务器打穿四条后置校验。

不 mock ask() 内部：起真实 HTTP 服务器按脚本回话，
连 urllib 请求路径、鉴权头、超时处理一起测。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.db.database import connect, init_db
from app.index.pipeline import index_pending
from app.qa import llm
from app.qa.answer import REFUSAL, ask
from app.watcher.scanner import scan_source


class _FakeLLM(BaseHTTPRequestHandler):
    """按 scripts 队列依次回话；记录收到的请求供断言。"""

    scripts: list[str] = []
    seen: list[dict] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeLLM.seen.append({
            "auth": self.headers.get("Authorization", ""),
            "messages": body.get("messages", []),
        })
        text = _FakeLLM.scripts.pop(0) if _FakeLLM.scripts else "空脚本"
        resp = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": text}}]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):  # 安静
        pass


@pytest.fixture(scope="module")
def fake_server():
    srv = HTTPServer(("127.0.0.1", 0), _FakeLLM)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


@pytest.fixture
def scripted(fake_server):
    """配置 llm 指向假服务器；返回设定脚本的函数。"""
    llm.configure(fake_server, "sk-test-fake", "fake-model")
    _FakeLLM.scripts = []
    _FakeLLM.seen = []

    def set_scripts(*texts):
        _FakeLLM.scripts = list(texts)

    yield set_scripts
    llm.configure("", "", "")


@pytest.fixture
def db(tmp_path):
    conn = connect(":memory:")
    init_db(conn)
    conn.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, created_at) "
        "VALUES ('S', ?, 'manual', 'manual', ?)", (str(tmp_path), time.time()),
    )
    (tmp_path / "瓷器.txt").write_text(
        "汝窑天青釉的烧成温度在一千二百度上下，还原气氛决定呈色。" * 6, encoding="utf-8")
    (tmp_path / "盐政.txt").write_text(
        "两淮盐政的稽核制度在乾隆年间经历了三次重大调整与改革。" * 6, encoding="utf-8")
    scan_source(conn, 1, tmp_path)
    index_pending(conn, limit=10)
    yield conn
    conn.close()


def test_not_configured(db):
    llm.configure("", "", "")
    a = ask(db, "汝窑的烧成温度？")
    assert a.status == "not_configured"
    assert a.answer is None


def test_answered_with_citations(db, scripted):
    scripted("汝窑的烧成温度在一千二百度上下 [C1]。")
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "answered"
    assert "[C1]" in a.answer
    assert a.citations and a.citations[0]["file_name"] == "瓷器.txt"
    assert a.citations[0]["snippet"]           # 引用带原文片段
    assert a.validation["attempts"] == 1


def test_fabricated_citation_stripped(db, scripted):
    """虚构的 [C9]（上下文里没有）必须被剔除（§12.4 ①）。"""
    scripted("烧成温度一千二百度 [C1]，另据业内共识 [C9] 无需还原气氛。")
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "answered"
    assert "[C9]" not in a.answer, "虚构引用没被剔除"
    assert "[C1]" in a.answer
    assert a.validation["fabricated_removed"] == 1


def test_zero_citation_regenerates(db, scripted):
    """非拒答却零引用 → 判幻觉，重新生成一次（§12.4 ②）。"""
    scripted("烧成温度大约一千二百度。",                 # 第一次：无引用
             "烧成温度在一千二百度上下 [C1]。")           # 重试：带引用
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "answered"
    assert a.validation["attempts"] == 2
    assert len(_FakeLLM.seen) == 2
    # 重试请求里带了纠正指令
    retry_msgs = _FakeLLM.seen[1]["messages"]
    assert any("没有任何 [Cn] 引用" in m["content"] for m in retry_msgs)


def test_persistent_zero_citation_falls_back(db, scripted):
    """二次仍零引用 → 降级为检索结果列表，**不输出自然语言答案**（§12.4 ③）。"""
    scripted("大概一千二百度吧。", "应该是一千二百度左右。")
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "fallback"
    assert a.answer is None, "降级后不该有自然语言答案"
    assert a.retrieved, "降级必须给出检索到的原文"
    assert a.validation.get("fallback") is True


def test_refusal_passthrough(db, scripted):
    scripted(REFUSAL)
    a = ask(db, "月球背面的氦三储量是多少")
    assert a.status == "refused"
    assert a.answer == REFUSAL


def test_refusal_with_smuggled_facts_truncated(db, scripted):
    """拒答句里夹带事实 → 截断到拒答句（§12.4 ④）：拒答不许夹带。"""
    scripted(REFUSAL + "不过一般来说氦三储量约有一百万吨。")
    a = ask(db, "月球背面的氦三储量是多少")
    assert a.status == "refused"
    assert a.answer == REFUSAL
    assert a.validation["truncated_refusal"] is True


def test_book_scoping(db, scripted):
    """书内问答只用书内文件的内容（B7 × B6）。"""
    fid = db.execute("SELECT id FROM files WHERE name = '盐政.txt'").fetchone()["id"]
    db.execute("INSERT INTO books (name, created_at) VALUES ('盐政研究', ?)", (time.time(),))
    db.execute("INSERT INTO book_members (book_id, file_id, added_at) VALUES (1, ?, ?)",
               (fid, time.time()))
    db.commit()

    scripted("资料只有盐政内容 [C1]。")
    a = ask(db, "汝窑的烧成温度是多少", book_id=1)
    # 上下文只能来自书内文件 —— 引用必然指向盐政.txt
    for c in a.citations:
        assert c["file_name"] == "盐政.txt", "书外内容泄进了书内问答"


def test_api_key_sent_but_never_echoed(db, scripted):
    """密钥出现在对外请求头里（这是用途），绝不出现在 status 里（§6.3）。"""
    scripted("温度一千二百度 [C1]。")
    ask(db, "汝窑的烧成温度是多少")
    assert _FakeLLM.seen[0]["auth"] == "Bearer sk-test-fake"
    st = llm.status()
    assert "sk-test-fake" not in json.dumps(st), "密钥被回显"
    assert st["has_key"] is True


def test_llm_classify_respects_valid_ids(db, scripted):
    """B1：模型只能选既有分类 id；越界 id 与库外 file_id 一律跳过（§16.1）。"""
    import json as _json

    from app.organize.classify import create_category
    from app.qa.classify_llm import llm_classify_unclassified

    cat = create_category(db, "陶瓷研究")
    db.commit()
    fid = db.execute("SELECT id FROM files WHERE name='瓷器.txt'").fetchone()["id"]

    scripted(_json.dumps([
        {"file_id": fid, "category_id": cat},      # 合法
        {"file_id": fid + 999, "category_id": cat},  # 库外文件 → 跳过
        {"file_id": fid, "category_id": 9999},        # 未知分类 → 跳过
    ]))
    r = llm_classify_unclassified(db)
    assert r["classified"] == 1

    row = db.execute("SELECT category_id, confirmed_by_user FROM files WHERE id=?",
                     (fid,)).fetchone()
    assert row["category_id"] == cat
    assert row["confirmed_by_user"] == 0     # by='llm'，用户仍可随时改

    # 发出去的内容只含文件名与正文开头，不含完整正文
    sent = _FakeLLM.seen[-1]["messages"][1]["content"]
    assert "瓷器.txt" in sent
    assert len(sent) < 4000


def test_llm_classify_garbage_json(db, scripted):
    from app.qa.classify_llm import llm_classify_unclassified

    scripted("我觉得这些文件都挺好的，不太好分类呢。")
    r = llm_classify_unclassified(db)
    assert r["classified"] == 0
    assert "error" in r
