"""Shared source-path and scan-policy rules."""

from __future__ import annotations

import ntpath
import os
import posixpath
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


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
    raw = os.path.expanduser(os.fspath(path))
    if sys.platform == "win32":
        # Do not use os.path here: tests intentionally evaluate Windows rules
        # on macOS/Linux, where os.path is posixpath and cannot normalize a
        # Windows drive.  The policy must depend on the target platform, not
        # on whichever host happens to run the check.
        drive, tail = ntpath.splitdrive(ntpath.normpath(raw))
        return len(drive) == 2 and drive[1] == ":" and tail == "\\"
    if sys.platform == "darwin":
        normalized = posixpath.normpath(raw)
        if normalized == "/":
            return True
        parts = PurePosixPath(normalized).parts
        return len(parts) == 3 and parts[0] == "/" and parts[1] == "Volumes" and bool(parts[2])
    return False


def canonical_source_path(path: Path | str) -> Path:
    """Normalize a source path without requiring it to exist."""
    raw = os.path.expanduser(os.fspath(path))
    return Path(os.path.abspath(os.path.normpath(raw)))


def resolve_source_policy(path: Path | str) -> ScanPolicy:
    root = canonical_source_path(path)
    return ScanPolicy(root=root, prune_projects=not is_drive_root(root))
