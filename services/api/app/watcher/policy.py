"""Shared source-path and scan-policy rules."""

from __future__ import annotations

import ntpath
import os
import posixpath
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


DISK_ROOT_PLATFORMS = frozenset({"win32", "darwin", "linux"})


@dataclass(frozen=True)
class ScanPolicy:
    root: Path
    prune_projects: bool


def uses_disk_root_sources() -> bool:
    """Desktop OSes treat local disks as the only auto-discovered sources."""
    return sys.platform in DISK_ROOT_PLATFORMS


def _is_linux_mount(path: str) -> bool:
    """Best-effort real mount check, isolated for cross-platform policy tests."""
    try:
        return Path(path).is_mount()
    except OSError:
        return False


def is_drive_root(path: Path | str) -> bool:
    """Return whether path is a top-level local disk root.

    Windows: ``C:\\``. macOS: ``/`` or ``/Volumes/Name``.
    Linux: ``/``, ``/mnt/Name``, ``/media/Name``, ``/media/user/Name``,
    ``/run/media/user/Name``, or a depth-1 mount such as ``/data``.
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
    if sys.platform == "linux":
        normalized = posixpath.normpath(raw)
        if normalized == "/":
            return True
        parts = PurePosixPath(normalized).parts
        if not parts or parts[0] != "/":
            return False
        if len(parts) == 3 and parts[1] in {"mnt", "media"} and parts[2]:
            return True
        if len(parts) == 4 and parts[1] == "media" and parts[2] and parts[3]:
            return True
        if len(parts) == 5 and parts[1] == "run" and parts[2] == "media" and parts[4]:
            return True
        system_prefixes = (
            "/proc", "/sys", "/dev", "/run", "/snap", "/tmp", "/boot",
            "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64",
            "/opt", "/var", "/root", "/etc",
        )
        if any(
            normalized == prefix or normalized.startswith(prefix + "/")
            for prefix in system_prefixes
        ):
            return False
        return _is_linux_mount(normalized)
    return False


def canonical_source_path(path: Path | str) -> Path:
    """Normalize a source path without requiring it to exist."""
    raw = os.path.expanduser(os.fspath(path))
    return Path(os.path.abspath(os.path.normpath(raw)))


def resolve_source_policy(path: Path | str) -> ScanPolicy:
    root = canonical_source_path(path)
    return ScanPolicy(root=root, prune_projects=not is_drive_root(root))
