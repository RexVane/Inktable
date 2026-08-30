"""Shared source-path and scan-policy rules."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


DISK_ROOT_PLATFORMS = frozenset({"win32", "darwin", "linux"})


@dataclass(frozen=True)
class ScanPolicy:
    root: Path
    prune_projects: bool


def uses_disk_root_sources() -> bool:
    """Desktop OSes treat local disks as the only auto-discovered sources."""
    return sys.platform in DISK_ROOT_PLATFORMS


def is_drive_root(path: Path | str) -> bool:
    """Return whether path is a top-level local disk root.

    Windows: ``C:\\``. macOS: ``/`` or ``/Volumes/Name``.
    Linux: ``/``, ``/mnt/Name``, ``/media/Name``, ``/media/user/Name``,
    ``/run/media/user/Name``, or a depth-1 mount such as ``/data``.
    """
    raw = os.path.normpath(os.path.expanduser(os.fspath(path)))
    if sys.platform == "win32":
        return len(raw) == 3 and raw[1:] == ":\\"
    if sys.platform == "darwin":
        if raw == os.sep:
            return True
        parts = Path(raw).parts
        return len(parts) == 3 and parts[0] == os.sep and parts[1] == "Volumes" and bool(parts[2])
    if sys.platform == "linux":
        if raw in {"/", os.sep}:
            return True
        parts = Path(raw).parts
        if not parts or parts[0] not in {"/", os.sep}:
            return False
        if len(parts) == 3 and parts[1] in {"mnt", "media"} and parts[2]:
            return True
        if len(parts) == 4 and parts[1] == "media" and parts[2] and parts[3]:
            return True
        if len(parts) == 5 and parts[1] == "run" and parts[2] == "media" and parts[4]:
            return True
        if len(parts) == 2 and parts[1] not in {
            "proc", "sys", "dev", "run", "snap", "tmp", "boot", "usr", "bin",
            "sbin", "lib", "lib64", "opt", "var", "root", "etc", "srv",
        }:
            try:
                return Path(raw).is_mount()
            except OSError:
                return False
        return False
    return False


def canonical_source_path(path: Path | str) -> Path:
    """Normalize a source path without requiring it to exist."""
    raw = os.path.expanduser(os.fspath(path))
    return Path(os.path.abspath(os.path.normpath(raw)))


def resolve_source_policy(path: Path | str) -> ScanPolicy:
    root = canonical_source_path(path)
    return ScanPolicy(root=root, prune_projects=not is_drive_root(root))
