"""Security/concurrency regressions found during the 0.3 hardening review."""
from __future__ import annotations

import contextlib
import importlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app.qa import llm

TOKEN = "hardening-token"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDO_DB", str(tmp_path / "library.db"))
    monkeypatch.setenv("ORDO_TOKEN", TOKEN)
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
        file_id = conn.execute(
            """INSERT INTO files(volume_uuid,inode,path,name,origin_path,source_id,
                                  ext,size,state,is_dataless,mtime,detected_at)
               VALUES('v','i',?,?,?,?,?,1,'registered',0,1,?)""",
            (str(known), known.name, str(known), source_id, ".txt", time.time()),
        ).lastrowid
        conn.commit()

    allowed = client.post(
        "/files/authorize_path", headers=H,
        json={"path": str(known), "action": "open"},
    )
    denied = client.post(
        "/files/authorize_path", headers=H,
        json={"path": str(unknown), "action": "open"},
    )
    traversal = client.post(
        "/files/authorize_path", headers=H,
        json={"path": str(tmp_path / "sub" / ".." / unknown.name), "action": "open"},
    )
    assert allowed.json() == {"authorized": True}
    assert denied.json() == {"authorized": False}
    assert traversal.json() == {"authorized": False}
    assert client.post(
        "/files/authorize_path", headers=H,
        json={"path": str(known), "action": "trash"},
    ).status_code == 422

    targets = client.get(f"/files/{file_id}/trash-targets", headers=H)
    assert targets.status_code == 200
    assert targets.json()["targets"] == [{"kind": "source", "path": str(known)}]


def test_trash_targets_include_existing_preserved_copy_without_duplicates(api, tmp_path):
    client, main_mod = api
    source = tmp_path / "missing.txt"
    preserved = tmp_path / "preserved" / "missing.txt"
    preserved.parent.mkdir()
    preserved.write_text("recoverable", encoding="utf-8")
    with main_mod._db_lock:
        conn = main_mod.db()
        conn.execute(
            """INSERT INTO files(volume_uuid,inode,path,name,origin_path,preserved_path,
                                  ext,size,state,is_dataless,mtime,detected_at)
               VALUES('v','preserved',?,?,?,?,?,1,'missing',0,1,?)""",
            (str(source), source.name, str(source), str(preserved), ".txt", time.time()),
        )
        file_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

    result = client.get(f"/files/{file_id}/trash-targets", headers=H).json()
    assert result["targets"] == [{"kind": "preserved", "path": str(preserved)}]
    raw = client.get(f"/files/{file_id}/raw", headers=H)
    assert raw.status_code == 200
    assert raw.content == b"recoverable"


def test_disabled_source_cannot_be_read_or_mutated_by_known_file_id(api, tmp_path):
    client, main_mod = api
    known = tmp_path / "private.txt"
    known.write_text("private evidence", encoding="utf-8")
    with main_mod._db_lock:
        conn = main_mod.db()
        source_id = conn.execute(
            """INSERT INTO sources(name,path,kind,discovered_by,enabled,created_at)
               VALUES(?,?,?,?,1,?)""",
            ("Private", str(tmp_path), "manual", "test", time.time()),
        ).lastrowid
        file_id = conn.execute(
               """INSERT INTO files(volume_uuid,inode,path,name,origin_path,source_id,
                                  ext,size,state,is_dataless,mtime,detected_at)
               VALUES('v','private',?,?,?,?,?,1,'registered',0,1,?)""",
            (str(known), known.name, str(known), source_id, ".txt", time.time()),
        ).lastrowid
        conn.commit()

    book_id = client.post("/books", headers=H, json={"name": "Private book"}).json()["id"]
    first_add = client.post(
        "/books/add", headers=H,
        json={"book_id": book_id, "file_ids": [file_id]},
    ).json()
    duplicate_add = client.post(
        "/books/add", headers=H,
        json={"book_id": book_id, "file_ids": [file_id, file_id]},
    ).json()
    assert first_add["added"] == 1
    assert duplicate_add["already_present"] == 1

    client.post("/sources/disable", headers=H, json={"source_id": source_id})

    assert client.get(f"/files/{file_id}/detail", headers=H).status_code == 404
    assert client.get(f"/files/{file_id}/content", headers=H).status_code == 404
    assert client.get(f"/files/{file_id}/raw", headers=H).status_code == 404
    assert client.get(f"/files/{file_id}/trash-targets", headers=H).status_code == 404
    assert client.post(
        "/files/authorize_path", headers=H,
        json={"path": str(known), "action": "open"},
    ).json() == {"authorized": False}
    assert client.get("/books", headers=H).json()["books"][0]["n"] == 0

    new_book = client.post(
        "/books", headers=H, json={"name": "Cannot add hidden"}
    ).json()["id"]
    hidden_add = client.post(
        "/books/add", headers=H,
        json={"book_id": new_book, "file_ids": [file_id, 999999]},
    ).json()
    assert hidden_add["added"] == 0
    assert hidden_add["not_found"] == 2

    category_id = client.post(
        "/categories", headers=H, json={"name": "Hidden target"}
    ).json()["id"]
    classified = client.post(
        "/files/classify", headers=H,
        json={"file_ids": [file_id], "category_id": category_id},
    ).json()
    assert classified["assigned"] == 0


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
    monkeypatch.setattr(llm, "open_model_request", lambda *_a, **_k: SlowResponse())
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
