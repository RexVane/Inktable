"""Shared source-path and scan-policy rules."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScanPolicy:
    root: Path
    prune_projects: bool


def is_drive_root(path: Path | str) -> bool:
    """Return whether path is a Windows fixed-drive-style root."""
    if sys.platform != "win32":
        return False
    raw = os.path.normpath(os.path.expanduser(os.fspath(path)))
    return len(raw) == 3 and raw[1:] == ":\\"


def canonical_source_path(path: Path | str) -> Path:
    """Normalize a source path without requiring it to exist."""
    raw = os.path.expanduser(os.fspath(path))
    return Path(os.path.abspath(os.path.normpath(raw)))


def resolve_source_policy(path: Path | str) -> ScanPolicy:
    root = canonical_source_path(path)
    return ScanPolicy(root=root, prune_projects=not is_drive_root(root))
