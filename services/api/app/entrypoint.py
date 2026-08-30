"""Stable Ordo sidecar entrypoint.

`app.main` still owns the sidecar lifecycle, database singleton, auth boundary,
watcher and legacy routes.  This module is intentionally thin: it composes
feature routers around that existing core without turning the already-large
`main.py` into a merge-conflict hotspot.

Run source mode with::

    python -m app.entrypoint

PyInstaller also targets this module so packaged and source-mode sidecars use
the same route composition.
"""

from __future__ import annotations

import multiprocessing

from app import main as sidecar
from app.library.api import create_library_router


def install_feature_routers() -> None:
    """Mount feature routers exactly once on the existing FastAPI app."""
    if getattr(sidecar.app.state, "ordo_feature_routers_installed", False):
        return

    sidecar.app.include_router(
        create_library_router(sidecar.db, sidecar._db_lock, sidecar.require_token)
    )
    sidecar.app.state.ordo_feature_routers_installed = True


install_feature_routers()

# Re-export these names for ASGI tooling and packaging smoke tests.
app = sidecar.app


def main() -> None:
    """Start the composed sidecar, including all feature routers."""
    sidecar.main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
