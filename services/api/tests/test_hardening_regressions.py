"""Security/concurrency regressions found during the 0.3 hardening review."""
from __future__ import annotations

import contextlib
import importlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.qa import llm

TOKEN = "hardening-token"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("INKTABLE_DB", str(tmp_path / "library.db"))
    monkeypatch.setenv("INKTABLE_TOKEN", TOKEN)
    from app import main as main_mod

    if main_mod._db is not None:
        main_mod._db.close()
        main_mod._db = None
    if main_mod._watch is not None:
        main_mod._watch.stop()
        main_mod._watch = None
    importlib.reload(main_mod)
    with TestClient(main_mod.app) as client:
        yield client, main_mod


def test_file_shell_authorization_requires_exact_indexed_path(api, tmp_path):
    client, main_mod = api
    known = tmp_path / "known.txt"
    known.write_text("known", encoding="utf-8")
    unknown = tmp_path / "unknown.txt"
    unknown.write_text("unknown", encoding="utf-8")
    with main_mod._db_lock:
        conn = main_mod.db()
        source_id = conn.execute(
            """INSERT INTO sources(name,path,kind,discovered_by,enabled,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("S", str(tmp_path), "manual", "test", 1, time.time()),
        ).lastrowid
        conn.execute(
            """INSERT INTO files(volume_uuid,inode,path,name,origin_path,source_id,
                                  ext,size,state,is_dataless,mtime,detected_at)
               VALUES('v','i',?,?,?,?,?,1,'registered',0,1,?)""",
            (str(known), known.name, str(known), source_id, ".txt", time.time()),
        )
        conn.commit()

    allowed = client.post(
        "/files/authorize_path", headers=H,
        json={"path": str(known), "action": "trash"},
    )
    denied = client.post(
        "/files/authorize_path", headers=H,
        json={"path": str(unknown), "action": "trash"},
    )
    traversal = client.post(
        "/files/authorize_path", headers=H,
        json={"path": str(tmp_path / "sub" / ".." / unknown.name), "action": "open"},
    )
    assert allowed.json() == {"authorized": True}
    assert denied.json() == {"authorized": False}
    assert traversal.json() == {"authorized": False}


def test_invalid_llm_endpoint_is_422_and_never_configured(api):
    client, _main_mod = api
    response = client.post(
        "/settings/llm", headers=H,
        json={"endpoint": "file:///etc/passwd", "api_key": "secret", "model": "x"},
    )
    assert response.status_code == 422
    assert llm.status()["configured"] is False
    assert "secret" not in response.text


def test_file_backed_ask_does_not_hold_global_writer_lock(api, monkeypatch):
    client, main_mod = api
    entered = threading.Event()
    release = threading.Event()

    def slow_ask(_conn, _question, _book_id=None, **_kwargs):
        from app.qa.answer import Answer
        entered.set()
        assert release.wait(3)
        return Answer(status="answered", answer="ok", mode="general")

    monkeypatch.setattr("app.qa.metadata.answer_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr("app.qa.answer.ask", slow_ask)
    result = {}

    def request():
        result["response"] = client.post("/ask", headers=H, json={"question": "hello"})

    thread = threading.Thread(target=request)
    thread.start()
    assert entered.wait(2)
    acquired = main_mod._db_lock.acquire(timeout=0.5)
    try:
        assert acquired, "slow model/retrieval work must not retain the global writer lock"
    finally:
        if acquired:
            main_mod._db_lock.release()
        release.set()
        thread.join(timeout=3)
    assert result["response"].status_code == 200


class _DripBodyHandler(BaseHTTPRequestHandler):
    """Sends headers, then drips body bytes far longer than any test deadline."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "100000")
        self.end_headers()
        try:
            for _ in range(120):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.25)
        except Exception:  # noqa: BLE001 - client hangs up on deadline, expected
            pass

    def log_message(self, *_args):
        pass


class _SlowHeaderHandler(BaseHTTPRequestHandler):
    """Stalls before sending response headers at all."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        time.sleep(30)
        try:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
        except Exception:  # noqa: BLE001
            pass

    def log_message(self, *_args):
        pass


@contextlib.contextmanager
def _loopback(handler):
    # Threading server with daemon threads: a handler deliberately sleeping past
    # the deadline must not make teardown block (and must not be mistaken for
    # request latency by the timing assertions below).
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _timed_timeout_call(endpoint, call):
    """Run call() against endpoint, returning (code, elapsed) measured in-place."""
    llm.configure(endpoint, "secret", "model")
    started = time.monotonic()
    try:
        with pytest.raises(llm.LLMConnectionError) as exc:
            call()
        # Measure before teardown so server shutdown never inflates the number.
        return exc.value.code, time.monotonic() - started
    finally:
        llm.configure("", "", "")


def test_dripping_socket_cannot_outlive_absolute_deadline():
    """Regression: a real socket dripping bytes must not defeat the deadline.

    Measured before the fix: a 1s deadline took 50s, because ``response.close()``
    from the timer thread waits on the BufferedReader lock held by the blocked
    read instead of interrupting it. The timer now shuts the socket down.
    A cooperative fake response cannot catch this - it must be a real socket.
    """
    with _loopback(_DripBodyHandler) as endpoint:
        code, elapsed = _timed_timeout_call(
            endpoint, lambda: llm.chat([{"role": "user", "content": "x"}], timeout=1.0),
        )
    assert code == "timeout"
    # The server would drip for ~30s; anything near that means no interruption.
    assert elapsed < 10.0, f"absolute deadline not enforced: {elapsed:.2f}s"


def test_stream_dripping_socket_cannot_outlive_absolute_deadline():
    with _loopback(_DripBodyHandler) as endpoint:
        code, elapsed = _timed_timeout_call(
            endpoint,
            lambda: list(llm.chat_stream([{"role": "user", "content": "x"}], timeout=1.0)),
        )
    assert code == "timeout"
    assert elapsed < 10.0, f"stream deadline not enforced: {elapsed:.2f}s"


def test_deadline_covers_connect_and_header_phase():
    """The deadline must also bound DNS/connect/TLS/response-header waiting."""
    with _loopback(_SlowHeaderHandler) as endpoint:
        code, elapsed = _timed_timeout_call(
            endpoint, lambda: llm.chat([{"role": "user", "content": "x"}], timeout=0.5),
        )
    assert code == "timeout"
    assert elapsed < 10.0, f"header-phase deadline not enforced: {elapsed:.2f}s"


def test_nonstream_chat_has_absolute_deadline(monkeypatch):
    # Natural block is long (30s): without the absolute-deadline timer closing
    # the response, chat() would hang the full 30s. The daemon timer fires at
    # the 0.05s deadline and interrupts the read. Assert the interruption
    # happened far below the natural block rather than a tight wall-clock bound
    # (the suite runs this under heavy load, so exact timing is jittery).
    natural_block = 30.0

    class SlowResponse:
        def __init__(self):
            self.closed = threading.Event()
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.close()
        def close(self):
            self.closed.set()
        def read(self, _size=-1):
            self.closed.wait(natural_block)
            raise OSError("closed")

    llm.configure("http://127.0.0.1:9/v1", "secret", "model")
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *_a, **_k: SlowResponse())
    started = time.monotonic()
    try:
        with pytest.raises(llm.LLMConnectionError) as exc:
            llm.chat([{"role": "user", "content": "x"}], timeout=0.05)
    finally:
        llm.configure("", "", "")
    elapsed = time.monotonic() - started
    assert exc.value.code == "timeout"
    # Comfortably below the natural block proves the deadline mechanism engaged,
    # while tolerating scheduler jitter far beyond the 0.05s deadline.
    assert elapsed < natural_block / 3
