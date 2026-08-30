"""B6 问答管线对抗测试 —— 用假 OpenAI 服务器打穿四条后置校验。

不 mock ask() 内部：起真实 HTTP 服务器按脚本回话，
连 urllib 请求路径、鉴权头、超时处理一起测。
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.db.database import connect, init_db
from app.index.pipeline import index_pending
from app.qa import answer as answer_module
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
            "path": self.path,
            "messages": body.get("messages", []),
            "max_tokens": body.get("max_tokens", "ABSENT"),
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


class _StatusLLM(BaseHTTPRequestHandler):
    status_code = 401

    def do_POST(self):
        # 必须先读完请求体再关闭连接：Windows 下带着未读的 POST body 关
        # socket 会发 RST（WinError 10053）而不是 FIN，客户端读响应状态行
        # 时被中止，probe 被间歇性误判成 unreachable（修 flaky）。
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        self.send_response(self.status_code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class _RawLLM(BaseHTTPRequestHandler):
    body = b"not-json"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def fake_server():
    srv = HTTPServer(("127.0.0.1", 0), _FakeLLM)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


@pytest.fixture
def scripted(fake_server, monkeypatch):
    """配置 llm 指向假服务器；返回设定脚本的函数。"""
    llm.configure(fake_server, "sk-test-fake", "fake-model")
    _FakeLLM.scripts = []
    _FakeLLM.seen = []
    monkeypatch.setattr(
        answer_module, "_verify_claim_support",
        lambda _question, claims: ([True] * len(claims), True, ""),
    )

    def set_scripts(*texts):
        _FakeLLM.scripts = list(texts)

    yield set_scripts
    llm.configure("", "", "")


@pytest.fixture
def db(tmp_path):
    conn = connect(":memory:")
    init_db(conn)
    conn.execute(
        "INSERT INTO sources (name, path, kind, discovered_by, enabled, created_at) "
        "VALUES ('S', ?, 'manual', 'manual', 1, ?)", (str(tmp_path), time.time()),
    )
    (tmp_path / "瓷器.txt").write_text(
        "汝窑天青釉的烧成温度在一千二百度上下，还原气氛决定呈色。" * 6, encoding="utf-8")
    (tmp_path / "盐政.txt").write_text(
        "两淮盐政的稽核制度在乾隆年间经历了三次重大调整与改革。" * 6, encoding="utf-8")
    scan_source(conn, 1, tmp_path)
    # QA contract tests exercise retrieval/validation, not a live Ollama daemon.
    # Build deterministic FTS indexes and let vector search degrade naturally.
    index_pending(conn, limit=10, embed=False)
    yield conn
    conn.close()


def test_not_configured(db):
    llm.configure("", "", "")
    a = ask(db, "汝窑的烧成温度？")
    assert a.status == "not_configured"
    assert a.answer is None


def test_model_probe_succeeds_without_exposing_key(fake_server):
    """检测连接必须是真实补全：成功时带回模型的实际回复与耗时。"""
    llm.configure(fake_server, "probe-secret", "fake-model")
    _FakeLLM.scripts = ["OK"]
    try:
        result = llm.probe(timeout=2)
    finally:
        llm.configure("", "", "")

    assert result["configured"] is True
    assert result["endpoint"] == fake_server
    assert result["model"] == "fake-model"
    assert result["has_key"] is True
    assert result["available"] is True
    assert result["code"] == "ready"
    assert result["reply"] == "OK"
    assert result["latency_ms"] >= 0
    assert "实际回复「OK」" in result["message"]
    assert "probe-secret" not in str(result)


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (401, "auth_failed"), (403, "auth_failed"), (404, "not_found"),
        (408, "timeout"), (429, "rate_limited"), (500, "service_error"),
        (504, "timeout"),
    ],
)
def test_model_probe_maps_http_failure(status_code, code):
    _StatusLLM.status_code = status_code
    srv = HTTPServer(("127.0.0.1", 0), _StatusLLM)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    llm.configure(f"http://127.0.0.1:{srv.server_port}/v1", "secret", "missing")
    try:
        result = llm.probe(timeout=2)
    finally:
        llm.configure("", "", "")
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)

    assert result["available"] is False
    assert result["code"] == code
    assert "secret" not in str(result)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        json.dumps({"unexpected": True}).encode(),
        json.dumps({"choices": [{"message": {"content": 123}}]}).encode(),
        json.dumps({"choices": [{"message": {"content": ""}}]}).encode(),
    ],
)
def test_model_probe_rejects_invalid_response(body):
    _RawLLM.body = body
    srv = HTTPServer(("127.0.0.1", 0), _RawLLM)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    llm.configure(f"http://127.0.0.1:{srv.server_port}/v1", "secret", "model")
    try:
        result = llm.probe(timeout=2)
    finally:
        llm.configure("", "", "")
        srv.shutdown()

    assert result["available"] is False
    assert result["code"] == "invalid_response"
    assert "secret" not in str(result)


@pytest.mark.parametrize(
    "endpoint",
    ["not-a-url", "file:///etc/passwd", "ftp://example.test/v1", "https://u:p@example.test/v1"],
)
def test_model_config_rejects_non_http_or_credentialed_endpoint(endpoint):
    with pytest.raises(llm.LLMError):
        llm.configure(endpoint, "secret", "model")
    assert llm.status()["configured"] is False


def test_answered_with_citations(db, scripted):
    scripted("汝窑的烧成温度在一千二百度上下 [C1]。")
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "answered"
    assert "[C1]" in a.answer
    assert a.citations and a.citations[0]["file_name"] == "瓷器.txt"
    assert a.citations[0]["snippet"]           # 引用带原文片段
    citation = a.citations[0]
    assert citation["span_id"].startswith(f"ch{citation['chunk_id']}:")
    assert citation["end_offset"] > citation["start_offset"]
    assert citation["snippet"] == db.execute(
        "SELECT substr(text, ?, ?) FROM chunks WHERE id = ?",
        (citation["start_offset"] + 1,
         citation["end_offset"] - citation["start_offset"],
         citation["chunk_id"]),
    ).fetchone()[0]
    assert a.validation["attempts"] == 1
    assert a.trace["trace_id"]
    assert [stage["name"] for stage in a.trace["stages"]] == [
        "hierarchy_routing", "lexical_retrieval", "embed_query",
        "deep_retrieval", "decompose", "scope", "rrf",
        "rerank", "diversify", "expand", "compress", "assemble",
    ]


def test_short_cited_numeric_fact_reaches_support_verifier(
    db, scripted, monkeypatch,
):
    seen = []

    def verify(_question, claims):
        seen.extend(claims)
        return [True] * len(claims), True, ""

    monkeypatch.setattr(answer_module, "_verify_claim_support", verify)
    scripted("32 字节 [C1]。")

    answer = ask(db, "汝窑的烧成温度是多少")

    assert answer.status == "answered"
    assert len(seen) == 1
    assert seen[0][1].startswith("32 字节")


def test_fabricated_citation_stripped(db, scripted):
    """虚构的 [C9]（上下文里没有）必须被剔除（§12.4 ①）。"""
    scripted("烧成温度一千二百度 [C1]，另据业内共识 [C9] 无需还原气氛。")
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "answered"
    assert "[C9]" not in a.answer, "虚构引用没被剔除"
    assert "[C1]" in a.answer
    assert a.validation["fabricated_removed"] == 1


def test_grounded_zero_citation_auto_attributed(db, scripted):
    """模型不写 [Cn] 但答案句句有据 → 句级自动归因接住，不降级。"""
    scripted("烧成温度大约一千二百度。呈色由还原气氛决定。")
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "answered"
    assert a.validation["attempts"] == 1
    assert a.validation["auto_cited"] == 2
    assert "[C" in a.answer, "自动归因必须把引用标记注回答案"
    assert a.citations and a.citations[0]["file_name"] == "瓷器.txt"


def test_auto_cite_rejects_wrong_numbers(db, scripted):
    """数字对不上证据 → 自动归因拒绝接受，仍走降级（防幻觉底线）。"""
    scripted("汝窑的烧成温度在一千五百度上下。",
             "烧成温度大概是一千五百度。")
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "fallback"
    assert a.answer is None
    assert "auto_cited" not in a.validation


def test_zero_citation_regenerates(db, scripted):
    """无引用且归因不上 → 判幻觉，重新生成一次（§12.4 ②）。"""
    scripted("这个问题需要结合窑址考古资料综合判断，难以给出结论。",  # 第一次：无引用且无依据
             "烧成温度在一千二百度上下 [C1]。")                       # 重试：带引用
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "answered"
    assert a.validation["attempts"] == 2
    assert len(_FakeLLM.seen) == 2
    # 重试请求里带了纠正指令
    retry_msgs = _FakeLLM.seen[1]["messages"]
    assert any("未逐条引用" in m["content"] for m in retry_msgs)


def test_partial_citation_regenerates(db, scripted):
    scripted(
        "烧成温度在一千二百度上下 [C1]。这一结论还需要行业经验补充。",
        "烧成温度在一千二百度上下 [C1]。",
    )

    answer = ask(db, "汝窑的烧成温度是多少")

    assert answer.status == "answered"
    assert answer.validation["attempts"] == 2
    assert answer.validation["uncited_claims"] == 1
    assert "行业经验" not in answer.answer


def test_semantically_unsupported_citations_retry_then_refuse(
    db, scripted, monkeypatch,
):
    monkeypatch.setattr(
        answer_module, "_verify_claim_support",
        lambda _question, claims: ([False] * len(claims), False, ""),
    )
    monkeypatch.setattr(answer_module, "_rewrite_for_retrieval", lambda _query: None)
    scripted(
        "SMTP 握手流程与 FTP 相同 [C1]。",
        "SMTP 握手流程与 FTP 相同 [C1]。",
        "SMTP 握手流程与 FTP 相同 [C1]。",
    )

    answer = ask(db, "SMTP 协议的握手流程")

    assert answer.status == "refused"
    assert answer.answer == REFUSAL
    assert answer.validation["attempts"] == 3
    assert answer.validation["unsupported_claims"] == 1


def test_fabricated_only_citations_refuse_while_uncited_falls_back(
    db, scripted, monkeypatch,
):
    """只有虚构编号的引用（[C9] 不在上下文）也按引用回答处理：语义校验拒绝后
    拒答。被虚构剔除后当成「无引用」降级成片段列表才是 bug —— §12.4 ③ 的
    降级只服务于真的没写引用的回答。"""
    seen = []

    def verify(_question, claims):
        seen.append(claims)
        return [False] * len(claims), False, ""

    monkeypatch.setattr(answer_module, "_verify_claim_support", verify)
    monkeypatch.setattr(answer_module, "_rewrite_for_retrieval", lambda _query: None)
    scripted(
        "汝窑天青釉的烧成温度在一千二百五十度上下 [C9]。",
        "汝窑天青釉的烧成温度在一千二百五十度上下 [C9]。",
        "汝窑天青釉的烧成温度在一千二百五十度上下 [C9]。",
    )

    answer = ask(db, "汝窑的烧成温度是多少")

    assert answer.status == "refused"
    assert answer.answer == REFUSAL
    assert answer.validation["attempts"] == 3
    assert answer.validation["unsupported_claims"] == 1
    assert seen, "虚构编号的引用也应送入语义校验，而不是被剔除短路"


def test_explicit_scope_verifier_rejects_generic_policy(monkeypatch):
    def unexpected_call(*_args, **_kwargs):
        pytest.fail("scope mismatch should be rejected before the LLM verifier")

    monkeypatch.setattr(llm, "chat", unexpected_call)
    claims = [(
        "按审批权限签批 [C1]。",
        "按审批权限签批。",
        ["文件名：学校财务报销办法.pdf\n学校经费按审批权限审查签批。"],
    )]

    judgments, answerable, note = answer_module._verify_claim_support(
        "请仅根据我的文件库回答：社团经费报销的审批流程",
        claims,
    )

    assert judgments == [True]
    assert answerable is False
    assert note == "scope_mismatch"


def test_incomplete_supported_answer_gets_final_strict_retry(
    db, scripted, monkeypatch,
):
    verdicts = iter((False, False, True))

    def verify(_question, claims):
        return [True] * len(claims), next(verdicts), ""

    monkeypatch.setattr(answer_module, "_verify_claim_support", verify)
    scripted(
        "socket 负责网络通信 [C1]。",
        "socket 负责网络通信 [C1]。",
        "socket 负责网络通信 [C1]。\nssl 负责加密连接 [C1]。",
    )

    answer = ask(db, "项目使用了哪些标准库，各自负责什么")

    assert answer.status == "answered"
    assert answer.validation["attempts"] == 3
    assert "ssl 负责加密连接" in answer.answer
    retry_messages = _FakeLLM.seen[2]["messages"]
    assert any("全部必要事实" in message["content"] for message in retry_messages)


def test_answerable_with_unsupported_claim_gets_final_strict_retry(
    db, scripted, monkeypatch,
):
    calls = 0

    def verify(_question, claims):
        nonlocal calls
        calls += 1
        if calls < 3:
            return [True, False], True, ""
        return [True] * len(claims), True, ""

    monkeypatch.setattr(answer_module, "_verify_claim_support", verify)
    scripted(
        "socket 负责网络通信 [C1]。\nssl 一定使用第三方库 [C1]。",
        "socket 负责网络通信 [C1]。\nssl 一定使用第三方库 [C1]。",
        "socket 负责网络通信 [C1]。\nssl 负责加密连接 [C1]。",
    )

    answer = ask(db, "项目使用了哪些标准库，各自负责什么")

    assert answer.status == "answered"
    assert answer.validation["attempts"] == 3
    assert "ssl 负责加密连接" in answer.answer
    retry_messages = _FakeLLM.seen[2]["messages"]
    assert any("不能由引用直接支持" in message["content"]
               for message in retry_messages)


def test_persistent_zero_citation_falls_back(db, scripted):
    """二次仍无引用且归因不上 → 降级为检索结果列表，**不输出自然语言答案**（§12.4 ③）。"""
    scripted("需要更多窑址考古资料才能判断，无法直接回答。",
             "建议查阅专业文献获取权威结论。")
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "fallback"
    assert a.answer is None, "降级后不该有自然语言答案"
    assert a.retrieved, "降级必须给出检索到的原文"
    assert a.validation.get("fallback") is True


def test_answer_length_follows_model_by_default(db, scripted):
    """默认「自动」：不传 max_tokens，输出上限由所选模型自己决定。"""
    scripted("烧成温度在一千二百度上下 [C1]。")
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "answered"
    assert _FakeLLM.seen[-1]["max_tokens"] == "ABSENT", \
        "auto 档位不该在请求里携带 max_tokens"


def test_answer_length_setting_applies(db, scripted):
    """设置具体档位后，生成请求必须携带对应 max_tokens。"""
    from app.db.database import set_setting

    set_setting(db, "answer_max_tokens", "1000")
    scripted("烧成温度在一千二百度上下 [C1]。")
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "answered"
    assert _FakeLLM.seen[-1]["max_tokens"] == 1000


def test_general_question_routes_past_kb(db, scripted):
    """明确寒暄在检索前本地分流，不把个人文件片段发给模型。"""
    scripted("【通用】你好！我可以帮你检索本地文件、总结资料，也能回答一般问题。")
    a = ask(db, "你好，你能做什么？")
    assert a.status == "answered"
    assert a.mode == "general"
    assert a.citations == []
    assert "你好" in a.answer
    assert "【通用】" not in a.answer, "路由标记不能泄漏到答案里"
    assert a.validation["mode"] == "general"
    assert a.validation["route"] == "local_general"
    assert a.validation["personal_files_sent"] is False
    assert a.used_personal_files is False
    assert a.trace["route"] == "local_general"
    sent = json.dumps(_FakeLLM.seen[-1]["messages"], ensure_ascii=False)
    assert "汝窑" not in sent and "两淮盐政" not in sent


def test_self_contained_translation_skips_retrieval(db, scripted, monkeypatch):
    scripted("The build completed successfully.")
    monkeypatch.setattr(
        answer_module,
        "retrieve_context",
        lambda *_args, **_kwargs: pytest.fail("general transform must not retrieve"),
    )

    result = ask(db, "请翻译成英文：构建已成功完成。")

    assert result.status == "answered"
    assert result.mode == "general"
    assert result.answer == "The build completed successfully."
    assert result.used_personal_files is False


def test_explicit_knowledge_scope_rejects_general_route(db, scripted):
    scripted(
        "【通用】热水通常按当地水价收费。",
        "【通用】可以咨询宿管获取价格。",
    )

    answer = ask(db, "请仅根据我的文件库回答：宿舍热水每立方米收费多少")

    assert answer.status == "refused"
    assert answer.answer == REFUSAL
    assert answer.mode == "knowledge"
    assert answer.validation["attempts"] == 2
    assert answer.validation["invalid_general_route"] is True


def test_general_answer_strips_stray_citations(db, scripted):
    """通用回答里模型误加的 [Cn] 要清掉 —— 通用模式没有可指向的证据。"""
    scripted("【通用】Python 里用 open() 读取文件即可 [C1]。")
    a = ask(db, "python 怎么读文件")
    assert a.mode == "general"
    assert "[C1]" not in a.answer


def test_followup_condensed_with_history(db, scripted):
    """多轮追问：先浓缩成独立问题再检索，生成仍用用户原话。"""
    scripted("汝窑天青釉的烧成温度是多少",                 # 第一次调用：浓缩
             "烧成温度在一千二百度上下 [C1]。")             # 第二次调用：作答
    a = ask(db, "那温度大概是多少？",
            history=[{"q": "汝窑天青釉是什么", "a": "一种宋代青瓷釉色"}])
    assert a.status == "answered"
    assert a.validation["condensed_query"] == "汝窑天青釉的烧成温度是多少"
    assert a.citations and a.citations[0]["file_name"] == "瓷器.txt"
    # 浓缩调用要带上对话历史；主调用的问题保持用户原话
    condense_msgs = _FakeLLM.seen[0]["messages"]
    assert any("对话历史" in m["content"] for m in condense_msgs)
    main_msgs = _FakeLLM.seen[1]["messages"]
    assert any("那温度大概是多少" in m["content"] for m in main_msgs)


def test_no_history_skips_condense(db, scripted):
    """没有历史时不多打浓缩调用 —— 首问零额外开销。"""
    scripted("烧成温度在一千二百度上下 [C1]。")
    a = ask(db, "汝窑的烧成温度是多少", history=[])
    assert a.status == "answered"
    assert "condensed_query" not in a.validation
    assert len(_FakeLLM.seen) == 1


def test_refusal_triggers_retrieval_rewrite(db, scripted):
    """模型拒答后：换检索关键词重试一次，命中即正常作答（有界一轮）。"""
    scripted(REFUSAL,                                     # 第一轮生成：拒答
             "汝窑 天青釉 烧成温度",                        # 检索改写
             "烧成温度在一千二百度上下 [C1]。")             # 第二轮生成：作答
    a = ask(db, "那种宋代名瓷要烧到多热")
    assert a.status == "answered"
    assert a.validation["retrieval_retry"] == "汝窑 天青釉 烧成温度"
    assert a.citations


def test_high_confidence_refusal_gets_same_context_retry(db, scripted, monkeypatch):
    from app.index import confidence

    monkeypatch.setattr(
        confidence, "assess",
        lambda _conn, _query, _top: confidence.Confidence("high", 0.9, []),
    )
    monkeypatch.setattr(answer_module, "_rewrite_for_retrieval", lambda _query: None)
    scripted(
        REFUSAL,
        "烧成温度在一千二百度上下 [C1]。",
    )

    answer = ask(db, "汝窑的烧成温度是多少")

    assert answer.status == "answered"
    assert answer.validation["same_context_retry"] is True


def test_explicit_knowledge_scope_keeps_high_confidence_refusal(
    db, scripted, monkeypatch,
):
    from app.index import confidence

    monkeypatch.setattr(
        confidence, "assess",
        lambda _conn, _query, _top: confidence.Confidence("high", 0.9, []),
    )
    monkeypatch.setattr(
        answer_module, "_rewrite_for_retrieval",
        lambda _query: pytest.fail("explicit-scope refusal must not be rewritten"),
    )
    scripted(REFUSAL)

    answer = ask(
        db,
        "请仅根据我的文件库回答：社团经费报销的审批流程",
    )

    assert answer.status == "refused"
    assert answer.answer == REFUSAL
    assert "same_context_retry" not in answer.validation
    assert answer.validation["knowledge_scope_refusal_preserved"] is True
    assert len(_FakeLLM.seen) == 1


def test_llm_error_degrades_to_snippets(db, scripted, monkeypatch):
    """模型调用失败（限流/断网/中转不兼容）不炸接口 → 降级为片段并说明原因。"""
    def boom(*_args, **_kwargs):
        raise llm.LLMHTTPError(429)

    monkeypatch.setattr(llm, "chat", boom)
    a = ask(db, "汝窑的烧成温度是多少")
    assert a.status == "fallback"
    assert a.answer is None
    assert a.retrieved, "降级必须带出检索到的原文片段"
    assert "模型调用失败" in a.hedge
    assert a.validation["error"]
    assert a.validation["error_type"] == "LLMHTTPError"


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


def test_book_citation_uses_member_copy_for_shared_content(
    db, scripted, tmp_path
):
    """content 共享时，书内问答引用必须指向 book_members 中的具体副本。"""
    member_path = tmp_path / "瓷器书内副本.txt"
    shutil.copy2(tmp_path / "瓷器.txt", member_path)
    scan_source(db, 1, tmp_path)
    member_id = db.execute(
        "SELECT id FROM files WHERE name = '瓷器书内副本.txt'"
    ).fetchone()["id"]
    original_id = db.execute(
        "SELECT id FROM files WHERE name = '瓷器.txt'"
    ).fetchone()["id"]
    assert original_id != member_id
    assert db.execute(
        "SELECT content_id FROM files WHERE id = ?", (original_id,)
    ).fetchone()[0] == db.execute(
        "SELECT content_id FROM files WHERE id = ?", (member_id,)
    ).fetchone()[0]

    db.execute("INSERT INTO books (name, created_at) VALUES ('瓷器副本书', ?)",
               (time.time(),))
    db.execute(
        "INSERT INTO book_members (book_id, file_id, added_at) VALUES (1, ?, ?)",
        (member_id, time.time()),
    )
    db.commit()

    scripted("汝窑烧成温度约一千二百度 [C1]。")
    answer = ask(db, "汝窑的烧成温度是多少", book_id=1)

    assert answer.status == "answered"
    assert answer.citations
    assert {c["file_id"] for c in answer.citations} == {member_id}
    assert {c["file_name"] for c in answer.citations} == {"瓷器书内副本.txt"}


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


def test_validate_handles_three_digit_citation_tags():
    """证据超过 99 条时标签是三位数：[C106] 必须被识别与映射，
    库外的三位数标签必须被剔除（此前两位正则会让两者都原样漏出）。"""
    from app.qa.answer import ContextPiece, validate

    def piece(tag):
        return ContextPiece(tag=tag, chunk_id=1, content_id=1, file_id=1,
                            file_name="f.txt", file_path="/f.txt", page=None,
                            section_path="", text="正文", snippet="正文")

    cleaned, record = validate(
        "第一句 [C1]。第二句 [C106]。第三句 [C999]。",
        [piece("C1"), piece("C106")],
    )
    assert "[C106]" in cleaned
    assert "[C999]" not in cleaned
    assert record["fabricated_removed"] == 1


# ---------------------------------------------------------------- 回答档位（快速 / 深度）


def _verifier_counter(monkeypatch):
    """替换逐条蕴含校验器并统计调用次数。"""
    calls = {"n": 0}

    def counter(_question, claims):
        calls["n"] += 1
        return [True] * len(claims), True, ""

    monkeypatch.setattr(answer_module, "_verify_claim_support", counter)
    return calls


def test_quick_mode_is_concise_with_deterministic_citations(db, scripted, monkeypatch):
    """快速档保留引用和本地支持检查，但不增加第二次模型调用。"""
    scripted("汝窑的烧成温度在一千二百度上下 [C1]。")
    calls = _verifier_counter(monkeypatch)
    a = ask(db, "汝窑的烧成温度是多少", qa_mode="quick")

    assert a.status == "answered"
    assert "[C1]" in a.answer
    assert "烧成温度" in a.answer
    assert a.citations and a.citations[0]["file_name"] == "瓷器.txt"
    assert a.validation["qa_mode"] == "quick"
    assert a.validation["support_check"] == "deterministic_quick"
    assert a.validation["unsupported_claims"] == 0
    assert calls["n"] == 0
    assert len(_FakeLLM.seen) == 1


def test_deep_mode_runs_support_verifier(db, scripted, monkeypatch):
    """深度档保持原默认管线：逐条蕴含校验照常执行。"""
    scripted("汝窑的烧成温度在一千二百度上下 [C1]。")
    calls = _verifier_counter(monkeypatch)
    a = ask(db, "汝窑的烧成温度是多少", qa_mode="deep")

    assert a.status == "answered"
    assert calls["n"] == 1
    assert a.validation["qa_mode"] == "deep"


def test_quick_mode_falls_back_without_retry(db, scripted):
    """快速档空回复不重新生成 —— 单轮失败直接降级为片段列表。"""
    scripted("")
    a = ask(db, "汝窑的烧成温度是多少", qa_mode="quick")

    assert a.status == "fallback"
    assert a.retrieved
    assert len(_FakeLLM.seen) == 1


def test_quick_mode_rejects_unsupported_uncited_answer(db, scripted):
    """快速档仍只有一轮，但无证据自由发挥只能降级为检索片段。"""
    scripted("一段自由发挥、与检索证据毫无重叠的泛泛回答。")
    a = ask(db, "汝窑的烧成温度是多少", qa_mode="quick")

    assert a.status == "fallback"
    assert a.answer is None
    assert a.retrieved
    assert a.validation["support_check"] == "deterministic_quick"
    assert len(_FakeLLM.seen) == 1


def test_deep_mode_retries_uncited_answer(db, scripted):
    """深度档首轮引用缺失会重新生成一次（原默认行为，作对照）。"""
    scripted("一段自由发挥、与检索证据毫无重叠的泛泛回答。",
             "另一段自由发挥、与检索证据毫无重叠的泛泛回答。")
    a = ask(db, "汝窑的烧成温度是多少", qa_mode="deep")

    assert a.status == "fallback"
    assert len(_FakeLLM.seen) == 2


def test_quick_mode_returns_refusal_without_retry(db, scripted):
    """快速档拒答即返回：不做查询改写与二次检索重试。"""
    scripted(REFUSAL)
    a = ask(db, "外星文明的确切起源时间是什么时候确定的？", qa_mode="quick")

    assert a.status == "refused"
    assert a.answer == REFUSAL
    assert len(_FakeLLM.seen) == 1


def test_unknown_mode_falls_back_to_deep(db, scripted, monkeypatch):
    """未知档位值归一到深度档，而不是报错或走快速管线。"""
    scripted("汝窑的烧成温度在一千二百度上下 [C1]。")
    calls = _verifier_counter(monkeypatch)
    a = ask(db, "汝窑的烧成温度是多少", qa_mode="bogus")

    assert a.status == "answered"
    assert a.validation["qa_mode"] == "deep"
    assert calls["n"] == 1


# ---------------------------------------------------------------- 接口格式与推理模型自适应


def test_probe_retries_with_larger_budget_when_content_empty(fake_server):
    """推理模型把 512 预算花在思考上时 content 为空 —— 探测自动放宽预算重试。"""
    llm.configure(fake_server, "sk-test", "fake-model")
    _FakeLLM.scripts = ["", "确认"]
    _FakeLLM.seen = []
    try:
        result = llm.probe(timeout=5)
    finally:
        llm.configure("", "", "")

    assert result["available"] is True
    assert result["reply"] == "确认"
    assert len(_FakeLLM.seen) == 2
    assert _FakeLLM.seen[0]["max_tokens"] == 512
    assert _FakeLLM.seen[1]["max_tokens"] == 2048


def test_ollama_provider_keyless_with_v1_url(fake_server):
    """本地 Ollama 格式：免密钥即可用，地址自动补 /v1/chat/completions。"""
    llm.configure(fake_server, "", "qwen3:8b", provider="ollama")
    _FakeLLM.scripts = ["确认"]
    _FakeLLM.seen = []
    try:
        assert llm.is_configured() is True
        text = llm.chat([{"role": "user", "content": "hi"}],
                        max_tokens=8, timeout=5)
    finally:
        llm.configure("", "", "")

    assert text == "确认"
    assert _FakeLLM.seen[0]["path"].endswith("/v1/chat/completions")
    assert _FakeLLM.seen[0]["auth"] == ""


class _ProtocolLLM(BaseHTTPRequestHandler):
    """按请求路径回对应协议的最小合法响应，并记下请求体。"""

    last: dict = {}
    reply = "确认"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).last = {
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "x_api_key": self.headers.get("x-api-key", ""),
            "version": self.headers.get("anthropic-version", ""),
            "body": body,
        }
        if self.path.endswith("/messages"):
            payload = {"content": [{"type": "text", "text": self.reply}]}
        elif self.path.endswith("/responses"):
            payload = {
                "output_text": self.reply,
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": self.reply}],
                }],
            }
        else:
            payload = {"choices": [{"message": {"role": "assistant", "content": self.reply}}]}
        resp = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass


@pytest.fixture
def protocol_server():
    srv = HTTPServer(("127.0.0.1", 0), _ProtocolLLM)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _ProtocolLLM.last = {}
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def test_unknown_provider_is_rejected():
    with pytest.raises(llm.LLMError, match="未知模型服务类型"):
        llm.configure("http://127.0.0.1:9/v1", "sk", "m", provider="mystery")


def test_cloud_protocols_require_api_key():
    for provider in ("openai", "anthropic", "responses"):
        with pytest.raises(llm.LLMError, match="API 密钥"):
            llm.configure("http://127.0.0.1:9/v1", "", "m", provider=provider)


def test_anthropic_provider_posts_messages(protocol_server):
    llm.configure(protocol_server, "sk-ant", "claude", provider="anthropic")
    try:
        text = llm.chat(
            [{"role": "system", "content": "简洁"},
             {"role": "user", "content": "hi"}],
            max_tokens=8, timeout=5)
    finally:
        llm.configure("", "", "")

    assert text == "确认"
    seen = _ProtocolLLM.last
    assert seen["path"].endswith("/v1/messages")
    assert seen["x_api_key"] == "sk-ant"
    assert seen["version"] == "2023-06-01"
    assert seen["body"]["system"] == "简洁"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]


def test_responses_provider_posts_responses(protocol_server):
    llm.configure(protocol_server, "sk-resp", "gpt", provider="responses")
    try:
        text = llm.chat(
            [{"role": "system", "content": "简洁"},
             {"role": "user", "content": "hi"}],
            max_tokens=16, timeout=5)
    finally:
        llm.configure("", "", "")

    assert text == "确认"
    seen = _ProtocolLLM.last
    assert seen["path"].endswith("/v1/responses")
    assert seen["auth"] == "Bearer sk-resp"
    assert seen["body"]["instructions"] == "简洁"
    assert seen["body"]["input"] == [{"role": "user", "content": "hi"}]
    assert seen["body"]["max_output_tokens"] == 16
    assert "messages" not in seen["body"]


def test_condense_adapts_to_reasoning_models(db, scripted):
    """追问浓缩拿不到正文时放宽预算重试 —— 改写仍生效且只多打一次。"""
    scripted("", "改写后的独立问题", "汝窑的烧成温度在一千二百度上下 [C1]。")
    a = ask(db, "那具体是多少度？",
            history=[{"q": "汝窑的烧成温度", "a": "文档里有记载"}])

    assert a.status == "answered"
    assert a.validation.get("condensed_query") == "改写后的独立问题"
    assert len(_FakeLLM.seen) == 3   # 浓缩(空) → 浓缩重试 → 主回答
