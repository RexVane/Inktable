"""Installed command-line entry point contracts."""

from __future__ import annotations


def _route_paths(routes) -> list[str]:
    paths: list[str] = []
    for route in routes:
        if path := getattr(route, "path", None):
            paths.append(path)
        if original := getattr(route, "original_router", None):
            paths.extend(_route_paths(original.routes))
    return paths


def test_cli_delegates_to_real_sidecar(monkeypatch):
    from app import entrypoint
    import ordo_api

    calls: list[str] = []
    monkeypatch.setattr(entrypoint.sidecar, "main", lambda: calls.append("started"))

    ordo_api.main()

    assert calls == ["started"]
    assert "/library/items" in _route_paths(entrypoint.app.routes)
