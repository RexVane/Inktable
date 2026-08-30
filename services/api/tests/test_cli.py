"""Installed command-line entry point contracts."""

from __future__ import annotations


def test_cli_delegates_to_real_sidecar(monkeypatch):
    from app import main as sidecar
    import inktable_api

    calls: list[str] = []
    monkeypatch.setattr(sidecar, "main", lambda: calls.append("started"))

    inktable_api.main()

    assert calls == ["started"]
