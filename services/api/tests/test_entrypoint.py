from __future__ import annotations

import importlib

from app import entrypoint
from app import main as sidecar


def _route_paths(routes) -> list[str]:
    """Support FastAPI's lazy router wrapper as well as older flat routes."""
    paths: list[str] = []
    for route in routes:
        path = getattr(route, "path", None)
        if path is not None:
            paths.append(path)
        original = getattr(route, "original_router", None)
        if original is not None:
            paths.extend(_route_paths(original.routes))
    return paths


def test_entrypoint_reuses_existing_sidecar_app_and_mounts_library_routes_once() -> None:
    # Several API modules deliberately reload app.main with an isolated test
    # database. Reload this thin composer too so the assertion is independent
    # of collection order and reflects the currently active sidecar module.
    current = importlib.reload(entrypoint)
    assert current.app is sidecar.app

    paths = _route_paths(current.app.routes)
    assert "/library/items" in paths
    assert "/library/items/{item_id}" in paths
    assert "/library/stats" in paths
    assert "/library/enrichment/status" in paths
    assert "/library/enrichment/drain" in paths
    assert "/library/enrichment/drain/cancel" in paths
    assert "/library/relations/status" in paths
    assert "/library/sync" in paths
    assert "/library/enrich" in paths
    assert "/library/relations/rebuild" in paths

    before = len(current.app.routes)
    current.install_feature_routers()
    assert len(current.app.routes) == before
