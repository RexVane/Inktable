"""数据库连接与初始化 —— PLAN §9.2。

库文件位置**固定不可配置**：`~/Library/Application Support/Inktable/library.db`。
不放资料库根目录 —— 后者用户可配，可能落在 iCloud / 外置卷 / 网络卷上，
多设备并发写会直接损坏（§9.2）。
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import time
from pathlib import Path

from app.db.schema import DEFAULT_SETTINGS, SCHEMA, SCHEMA_VERSION

APP_DIR = Path.home() / "Library" / "Application Support" / "Inktable"
DB_PATH = APP_DIR / "library.db"
LOCK_PATH = APP_DIR / "inktable.lock"

_lock_fd: int | None = None


class AlreadyRunning(RuntimeError):
    """另一个实例已持有库锁。"""


def acquire_single_instance_lock() -> None:
    """单实例互斥（§9.2）。两个实例同时写 SQLite 必然损坏。"""
    global _lock_fd
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise AlreadyRunning("Inktable 已在运行")
    os.write(fd, str(os.getpid()).encode())
    _lock_fd = fd


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    # INKTABLE_DB 让测试与开发指向独立库，避免污染用户的真实数据
    p = Path(path) if path else Path(os.environ.get("INKTABLE_DB") or DB_PATH)
    if p != Path(":memory:"):
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    conn.commit()


def quick_check(conn: sqlite3.Connection) -> bool:
    """每次启动跑一次（§9.2）。秒级。"""
    try:
        return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        return False


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def check_schema_version(conn: sqlite3.Connection) -> None:
    """版本高于当前程序（用户降级了应用）时拒绝启动，不尝试兼容（§9.2）。"""
    v = int(get_setting(conn, "db_schema_version", "0") or 0)
    if v > SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 {v} 高于当前程序支持的 {SCHEMA_VERSION}，请升级 Inktable"
        )
