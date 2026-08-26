from __future__ import annotations

from app import entrypoint
from app import main as sidecar


def test_entrypoint_reuses_existing_sidecar_app_and_mounts_library_routes_once() -> None:
    assert entrypoint.app is sidecar.app

    paths = [route.path for route in entrypoint.app.routes]
    assert "/library/items" in paths
    assert "/library/items/{item_id}" in paths
    assert "/library/stats" in paths
    assert "/library/enrichment/status" in paths
    assert "/library/sync" in paths
    assert "/library/enrich" in paths
    assert "/library/relations/rebuild" in paths

    before = len(entrypoint.app.routes)
    entrypoint.install_feature_routers()
    assert len(entrypoint.app.routes) == before
