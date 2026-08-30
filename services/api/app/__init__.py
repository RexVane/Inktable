"""Shared package bootstrap for the Ordo sidecar."""

from __future__ import annotations

import os
from collections.abc import MutableMapping


def alias_legacy_environment(
    environment: MutableMapping[str, str] | None = None,
) -> None:
    """Expose legacy ``INKTABLE_*`` settings through their ``ORDO_*`` names.

    This runs at package import time, before any ``app.*`` module reads its
    configuration.  Keeping the bridge here matters for standalone maintenance
    scripts that import a model or retrieval module without importing the
    database module first.  Explicit ``ORDO_*`` values always take precedence.
    """
    target = os.environ if environment is None else environment
    for key, value in list(target.items()):
        if key.startswith("INKTABLE_"):
            target.setdefault("ORDO_" + key.removeprefix("INKTABLE_"), value)


alias_legacy_environment()
