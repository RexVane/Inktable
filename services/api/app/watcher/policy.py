"""Shared source-path and scan-policy rules."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


DISK_ROOT_PLATFORMS = frozenset({"win32", "darwin"})


@dataclass(frozen=True)
class ScanPolicy:
    root: Path
    prune_projects: bool


def uses_disk_root_sources() -> bool:
    """Windows and macOS treat local disks as the only auto-discovered sources."""
    return sys.platform in DISK_ROOT_PLATFORMS


def is_drive_root(path: Path | str) -> bool:
    """Return whether path is a top-level local disk root.

    Windows: ``C:\\``. macOS: ``/`` or ``/Volumes/Name``.
    """
    raw = os.path.normpath(os.path.expanduser(os.fspath(path)))
    if sys.platform == "win32":
        return len(raw) == 3 and raw[1:] == ":\\"
    if sys.platform == "darwin":
        if raw == os.sep:
            return True
        parts = Path(raw).parts
        return len(parts) == 3 and parts[0] == os.sep and parts[1] == "Volumes" and bool(parts[2])
    return False


def canonical_source_path(path: Path | str) -> Path:
    """Normalize a source path without requiring it to exist."""
    raw = os.path.expanduser(os.fspath(path))
    return Path(os.path.abspath(os.path.normpath(raw)))


def resolve_source_policy(path: Path | str) -> ScanPolicy:
    root = canonical_source_path(path)
    return ScanPolicy(root=root, prune_projects=not is_drive_root(root))
