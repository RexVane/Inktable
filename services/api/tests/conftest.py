"""Test-wide safety switches.

Unit tests exercise watcher semantics with watchdog's pure-Python polling backend.
The production default remains macOS FSEvents; real FSEvents coverage lives in the
standalone ``tests/e2e_watch.py`` smoke test.
"""

from __future__ import annotations

import os
from pathlib import Path


def pytest_configure() -> None:
    os.environ.setdefault("INKTABLE_WATCH_BACKEND", "polling")
    # 探测逻辑遇到显式 env 就不再探活。9 号口保证没人听，测试保持封闭。
    # INKTABLE_DB 不在这里 setdefault：部分索引测试依赖各用例自己的 tmp 库，
    # 进程级共享文件在 Windows 上会变成 sqlite「unable to open database file」。
    # CI workflow 已经显式指向 runner.temp。
    os.environ.setdefault("INKTABLE_OLLAMA_URL", "http://127.0.0.1:9")
    # Windows 上若 TEMP 被写成未展开的字面量 `%USERPROFILE%\...`，sqlite 会
    # 在 cwd 下建出同名目录，大事务报 "unable to open database file"。
    real_tmp = Path.home() / "AppData" / "Local" / "Temp"
    for key in ("TEMP", "TMP", "TMPDIR"):
        value = os.environ.get(key, "")
        if "%USERPROFILE%" in value or "%USERNAME%" in value:
            os.environ[key] = str(real_tmp)
