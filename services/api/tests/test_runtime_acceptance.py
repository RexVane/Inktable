"""Runtime acceptance process lifecycle tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import runtime_acceptance


class _FakeProcess:
    pid = 4321

    def __init__(self) -> None:
        self.exited = False
        self.terminated = False
        self.killed = False

    def poll(self):
        return 0 if self.exited else None

    def terminate(self) -> None:
        self.terminated = True
        self.exited = True

    def kill(self) -> None:
        self.killed = True
        self.exited = True

    def wait(self, timeout: float):
        assert timeout > 0
        if not self.exited:
            raise runtime_acceptance.subprocess.TimeoutExpired("sidecar", timeout)
        return 0


def test_windows_close_terminates_the_pyinstaller_process_tree(monkeypatch) -> None:
    process = _FakeProcess()
    sidecar = object.__new__(runtime_acceptance.Sidecar)
    sidecar.process = process
    sidecar.request = lambda *_args, **_kwargs: {}
    calls: list[list[str]] = []

    monkeypatch.setattr(runtime_acceptance.os, "name", "nt")

    def fake_run(command, **_kwargs):
        calls.append(command)
        process.exited = True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime_acceptance.subprocess, "run", fake_run)

    sidecar.close()

    assert calls == [["taskkill", "/PID", "4321", "/T", "/F"]]
    assert process.terminated is False
    assert process.killed is False
