"""Test-wide safety switches.

Unit tests exercise watcher semantics with watchdog's pure-Python polling backend.
The production default remains macOS FSEvents; real FSEvents coverage lives in the
standalone ``tests/e2e_watch.py`` smoke test.
"""

from __future__ import annotations

import os


def pytest_configure() -> None:
    os.environ.setdefault("INKTABLE_WATCH_BACKEND", "polling")
