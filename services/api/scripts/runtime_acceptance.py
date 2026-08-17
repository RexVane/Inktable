"""Run a real sidecar acceptance flow against the selected database."""

from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.integrations.ccswitch import read_providers


class Sidecar:
    def __init__(self, *, ollama_url: str, executable: Path | None = None):
        self.token = secrets.token_urlsafe(24)
        env = os.environ.copy()
        env["INKTABLE_TOKEN"] = self.token
        env["INKTABLE_OLLAMA_URL"] = ollama_url
        command = [str(executable.resolve())] if executable else [
            sys.executable, "-m", "app.main",
        ]
        self.process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert self.process.stdin and self.process.stdout and self.process.stderr
        self.process.stdin.write(json.dumps({"token": self.token}) + "\n")
        self.process.stdin.flush()
        self.process.stdin.close()
        self._stdout: queue.Queue[str] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=80)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.port = self._wait_for_port()

    def _read_stdout(self) -> None:
        assert self.process.stdout
        for line in self.process.stdout:
            self._stdout.put(line.rstrip())

    def _read_stderr(self) -> None:
        assert self.process.stderr
        for line in self.process.stderr:
            self._stderr.append(line.rstrip())

    def _wait_for_port(self, timeout: float = 90.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"sidecar exited before startup: {list(self._stderr)[-10:]}"
                )
            try:
                line = self._stdout.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("port"):
                return int(payload["port"])
        raise TimeoutError(f"sidecar startup timed out: {list(self._stderr)[-10:]}")

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        timeout: float = 120.0,
    ) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} returned {exc.code}: {detail}") from exc

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.request("POST", "/watch/stop", {}, timeout=30)
        except Exception:
            pass

        if os.name == "nt":
            # PyInstaller onefile uses a bootloader parent plus a Python child.
            # Killing only the Popen handle can orphan the child and leave the
            # database single-instance lock held after acceptance finishes.
            try:
                subprocess.run(
                    [
                        "taskkill", "/PID", str(self.process.pid),
                        "/T", "/F",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr)[-20:]


def emit(phase: str, payload: dict) -> None:
    print(json.dumps({"phase": phase, **payload}, ensure_ascii=False), flush=True)


def choose_provider(
    providers: list[dict], preferred_name: str | None = None,
) -> dict | None:
    usable = [item for item in providers if item.get("model") and item.get("api_key")]
    if preferred_name:
        wanted = preferred_name.casefold()
        usable = [
            item for item in usable
            if str(item.get("name") or "").casefold() == wanted
        ]
    if not usable:
        return None
    usable.sort(
        key=lambda item: (
            bool(item.get("is_current")),
            bool(item.get("openai_native")),
        ),
        reverse=True,
    )
    return usable[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ollama-url", default="http://127.0.0.1:18434")
    parser.add_argument("--sidecar", type=Path,
                        help="test a packaged sidecar instead of the source module")
    parser.add_argument("--backfill-limit", type=int, default=512)
    parser.add_argument("--reconcile-timeout", type=float, default=1200)
    parser.add_argument("--enable-source-name")
    parser.add_argument("--enable-source-path", type=Path)
    parser.add_argument("--qa-smoke", action="store_true")
    parser.add_argument(
        "--provider",
        help="cc-switch provider name to use for QA instead of the current provider",
    )
    parser.add_argument(
        "--ccswitch-db", type=Path,
        help="read providers from a cc-switch database or backup without modifying it",
    )
    parser.add_argument(
        "--model",
        help="override the selected provider model without changing cc-switch",
    )
    parser.add_argument("--skip-backfill", action="store_true")
    parser.add_argument("--skip-reconcile", action="store_true")
    parser.add_argument("--require-cross-encoder", action="store_true")
    args = parser.parse_args()
    if bool(args.enable_source_name) != bool(args.enable_source_path):
        parser.error("--enable-source-name and --enable-source-path must be used together")

    if args.sidecar and not args.sidecar.is_file():
        parser.error(f"sidecar does not exist: {args.sidecar}")
    if args.ccswitch_db and not args.ccswitch_db.is_file():
        parser.error(f"cc-switch database does not exist: {args.ccswitch_db}")
    sidecar = Sidecar(ollama_url=args.ollama_url, executable=args.sidecar)
    try:
        health = sidecar.request("GET", "/health", timeout=120)
        checks = health.get("checks", {})
        emit("health", {
            "status": health.get("status"),
            "sqlite_vec": checks.get("sqlite_vec"),
            "embedding": checks.get("embedding"),
            "reranker": checks.get("reranker"),
        })
        if health.get("status") != "ok":
            raise RuntimeError(f"health check failed: {checks}")

        if not args.skip_backfill:
            embedded = 0
            while True:
                batch = sidecar.request(
                    "POST",
                    "/index/embed_backfill",
                    {"limit": args.backfill_limit},
                    timeout=900,
                )
                embedded += int(batch.get("embedded", 0))
                emit("embed_backfill", {
                    "batch": batch.get("embedded", 0),
                    "embedded": embedded,
                    "remaining": batch.get("remaining"),
                    "available": batch.get("available"),
                    "model": batch.get("model"),
                })
                if not batch.get("available"):
                    raise RuntimeError(f"embedding unavailable: {batch}")
                if int(batch.get("remaining", 0)) == 0:
                    break
                if int(batch.get("embedded", 0)) == 0:
                    raise RuntimeError(f"embedding made no progress: {batch}")

        if args.enable_source_path:
            enabled = sidecar.request(
                "POST",
                "/sources/enable",
                {
                    "name": args.enable_source_name,
                    "path": str(args.enable_source_path.resolve()),
                    "kind": "im",
                    "discovered_by": "config",
                    "volatile": True,
                },
                timeout=300,
            )
            emit("enable_source", {
                "source_id": enabled.get("source_id"),
                "stats": enabled.get("stats"),
            })

        if not args.skip_reconcile:
            initial = sidecar.request("GET", "/watch/status")
            initial_runs = int(initial.get("reconcile", {}).get("runs", 0))
            started = sidecar.request("POST", "/watch/start", {})
            emit("watch_start", {
                "watching": len(started.get("watching", [])),
                "failed": started.get("failed", []),
            })
            deadline = time.monotonic() + args.reconcile_timeout
            status = initial
            while time.monotonic() < deadline:
                status = sidecar.request("GET", "/watch/status")
                runs = int(status.get("reconcile", {}).get("runs", 0))
                if runs > initial_runs:
                    break
                time.sleep(2)
            else:
                raise TimeoutError(f"reconcile did not finish: {status.get('reconcile')}")
            emit("reconcile", status.get("reconcile", {}))

        search = sidecar.request(
            "POST", "/search", {"q": "银行家算法用到哪些数据结构", "limit": 10},
            timeout=180,
        )
        emit("search", {
            "total": search.get("total"),
            "confidence": search.get("confidence"),
            "degraded": search.get("degraded"),
            "top_files": [item.get("name") for item in search.get("files", [])[:3]],
        })
        if int(search.get("total", 0)) == 0:
            raise RuntimeError("search smoke returned no result")
        if args.require_cross_encoder:
            rerank = next(
                (stage for stage in search.get("timings", [])
                 if stage.get("name") == "rerank"),
                {},
            )
            if rerank.get("model_id") != "bge-reranker-base-int8-onnx":
                raise RuntimeError(f"Cross-Encoder not active: {rerank}")
            if rerank.get("degraded") or "rerank" in search.get("degraded", []):
                raise RuntimeError(f"Cross-Encoder degraded: {rerank}")

        if args.qa_smoke:
            integration = (
                read_providers(args.ccswitch_db)
                if args.ccswitch_db else
                sidecar.request("GET", "/integrations/ccswitch")
            )
            provider = choose_provider(
                integration.get("providers", []), args.provider,
            )
            if provider is None:
                raise RuntimeError("no usable cc-switch provider with a model")
            provider = dict(provider)
            if args.model:
                provider["model"] = args.model
            sidecar.request("POST", "/settings/llm", {
                "endpoint": provider["endpoint"],
                "api_key": provider["api_key"],
                "model": provider["model"],
            })
            probe = sidecar.request("POST", "/settings/llm/test", {}, timeout=240)
            emit("llm_probe", {
                "provider": provider.get("name"),
                "model": provider.get("model"),
                "available": probe.get("available"),
                "code": probe.get("code"),
                "latency_ms": probe.get("latency_ms"),
            })
            if not probe.get("available"):
                raise RuntimeError(f"LLM probe failed: {probe.get('code')}")
            answer = sidecar.request(
                "POST", "/ask",
                {"question": "银行家算法用到哪些数据结构？", "history": []},
                timeout=360,
            )
            emit("qa_smoke", {
                "status": answer.get("status"),
                "mode": answer.get("mode"),
                "citations": len(answer.get("citations", [])),
                "degraded": answer.get("degraded"),
                "validation": answer.get("validation"),
                "answer_preview": str(answer.get("answer", ""))[:160],
            })
            if not answer.get("citations"):
                raise RuntimeError("answerable QA smoke returned no citation")

        emit("complete", {"stderr_tail": sidecar.stderr_tail})
        return 0
    finally:
        sidecar.close()


if __name__ == "__main__":
    raise SystemExit(main())
