"""数据库连接与初始化 —— PLAN §9.2。

数据目录默认在 `~/Library/Application Support/Inktable/`，
可经 `INKTABLE_DATA_DIR` 环境变量整体迁移（由桌面端主进程在
"设置 → 通用配置 → 数据位置" 完成搬迁后传入）。库文件位置不放
资料库根目录 —— 后者可能落在 iCloud / 外置卷 / 网络卷上，
多设备并发写会直接损坏（§9.2）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

from app.db.schema import (
    DEFAULT_SETTINGS,
    LIBRARY_BOOTSTRAP_SQL,
    SCHEMA,
    SCHEMA_VERSION,
)

# 单实例锁的平台原语：Unix 用 flock，Windows 用 msvcrt 字节区锁
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def _resolve_app_dir() -> Path:
    override = os.environ.get("INKTABLE_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "Inktable"


APP_DIR = _resolve_app_dir()
DB_PATH = APP_DIR / "library.db"
LOCK_PATH = APP_DIR / "inktable.lock"
BACKUP_DIR = APP_DIR / "backups"
BACKUP_KEEP = 7

_lock_fd: int | None = None
_backup_lock = threading.Lock()


class AlreadyRunning(RuntimeError):
    """另一个实例已持有库锁。"""


class BackupError(RuntimeError):
    """数据库备份无法创建或无法通过恢复前校验。"""


def _instance_lock_path() -> Path:
    """Return the lock corresponding to the database selected for this process.

    Production always uses ``LOCK_PATH``.  Tests and development may point the
    sidecar at an isolated database through ``INKTABLE_DB``; those databases
    must not contend for the production lock or for each other's locks.
    """
    override = os.environ.get("INKTABLE_DB")
    if override and override != ":memory:":
        db_path = Path(override)
        return db_path.with_name(f"{db_path.name}.lock")
    return LOCK_PATH


def acquire_single_instance_lock() -> None:
    """单实例互斥（§9.2）。两个实例同时写 SQLite 必然损坏。"""
    global _lock_fd
    if _lock_fd is not None:
        return

    lock_path = _instance_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if sys.platform == "win32":
            # Windows 没有 flock：对首字节做非阻塞独占锁，语义等价 ——
            # 锁随进程退出自动释放，第二个实例立即失败（PermissionError）。
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        os.close(fd)
        raise AlreadyRunning("Inktable 已在运行")
    except OSError:
        os.close(fd)
        raise
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode())
    _lock_fd = fd


def release_single_instance_lock() -> None:
    """Release the process-wide database lock, mainly for orderly shutdown."""
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        if sys.platform == "win32":
            os.lseek(_lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(_lock_fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(_lock_fd)
        _lock_fd = None


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
    _load_vec_extension(conn)
    return conn


def connect_readonly(path: Path | str | None = None) -> sqlite3.Connection:
    """只读连接。用于绝不写库的旁路（预热、审计、统计）。

    为什么必须有这个：普通 connect() 会执行 `PRAGMA journal_mode = WAL`，
    那需要短暂的排他锁。只读旁路若走 connect()，就会和正常写入方抢锁 ——
    启动时的向量矩阵预热线程曾因此让 TestClient 用例间歇性报
    "database is locked"（app/main.py 的 _warm_vector_matrix）。
    只读连接不设 journal_mode，也就不可能造成这种竞争。
    """
    p = Path(path) if path else Path(os.environ.get("INKTABLE_DB") or DB_PATH)
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    _load_vec_extension(conn)
    return conn


def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    """加载 sqlite-vec 扩展。

    **必须对每个新连接都做一次** —— SQLite 扩展是连接级的，不随数据库
    文件持久化。漏掉会静默失效：`chunks_vec` 表看起来是空的（count 返回 0），
    向量检索路直接跳过，整个语义检索悄悄退化成纯关键词，
    而日志里什么都不会报。所以放在 connect() 里统一处理，
    不指望每个调用方自己记得。
    """
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        # 扩展不可用不是致命错误：纯关键词检索仍然可用（§16.1a 降级链）
        return False


def init_db(conn: sqlite3.Connection) -> None:
    # Reject application downgrades before executing even additive schema SQL.
    # A brand-new/legacy database has no settings table yet and is allowed.
    check_schema_version(conn)
    conn.executescript(SCHEMA)
    conn.execute("BEGIN")
    try:
        _migrate_schema(conn)
        # Library rows are derived from the migrated files/contents graph. Run
        # this only after legacy file columns exist, and keep it in the same
        # transaction so a failed upgrade cannot leave a half-backfilled layer.
        conn.execute(LIBRARY_BOOTSTRAP_SQL)
        _init_vec_table(conn)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (k, v),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply additive migrations and backfill a searchable v1 active version."""
    _repair_legacy_chunk_fts(conn)
    # These columns are part of the Library visibility/read contract. CREATE
    # TABLE IF NOT EXISTS cannot add them to a v1 files table, so they must
    # exist before the post-migration Library bootstrap runs.
    _add_column(conn, "files", "preserved_path TEXT")
    _add_column(conn, "files", "ext TEXT")
    _add_column(
        conn, "contents", "active_index_version INTEGER NOT NULL DEFAULT 1",
    )
    _add_column(
        conn, "chunks", "section_id INTEGER REFERENCES sections(id) ON DELETE SET NULL",
    )
    _add_column(conn, "chunks", "start_offset INTEGER")
    _add_column(conn, "chunks", "end_offset INTEGER")
    _add_column(conn, "chunks", "index_version INTEGER NOT NULL DEFAULT 1")
    # Document 层的检索用主题摘要（可为 NULL）。summary_text 是 full_text[:1000]
    # 的确定性截断，永远可用；abstract 由本机 Ollama 生成，缺失时文档路的
    # 索引文本与改动前逐字一致 —— 因此这一列是纯增量，不需要开关。
    _add_column(conn, "document_representations", "abstract TEXT")
    _add_column(conn, "document_representations", "abstract_model TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunks_version "
        "ON chunks(content_id, index_version, ordinal)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_section ON chunks(section_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_path ON files(path)")

    # Existing v1 libraries had one implicit active generation. Backfill a
    # coarse Document/Section representation without reparsing user files;
    # the next normal reindex replaces it with the structured hierarchy.
    legacy = conn.execute(
        """SELECT c.id, c.active_index_version,
                  COALESCE((SELECT name FROM files f WHERE f.content_id = c.id
                            ORDER BY f.id LIMIT 1), '') AS name
           FROM contents c
           WHERE EXISTS (SELECT 1 FROM chunks ch WHERE ch.content_id = c.id)
             AND NOT EXISTS (
                 SELECT 1 FROM index_versions iv WHERE iv.content_id = c.id
             )"""
    ).fetchall()
    from app.index.search import segment_for_index

    for content in legacy:
        version = int(content["active_index_version"] or 1)
        rows = conn.execute(
            "SELECT id, ordinal, text, section_path FROM chunks "
            "WHERE content_id = ? ORDER BY ordinal, id",
            (content["id"],),
        ).fetchall()
        full_text = "\n\n".join(row["text"] for row in rows)
        title = content["name"]
        summary = full_text[:1000]
        digest = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
        rep_id = conn.execute(
            """INSERT INTO document_representations
               (content_id, index_version, title, summary_text, full_text,
                text_hash, token_count, structure_confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            (content["id"], version, title, summary, full_text, digest,
             len(full_text)),
        ).lastrowid
        conn.execute(
            "INSERT INTO documents_fts(rowid, text) VALUES (?, ?)",
            (rep_id, segment_for_index(f"{title}\n{summary}")),
        )
        heading = next(
            (row["section_path"] for row in rows if row["section_path"]),
            "正文",
        )
        section_id = conn.execute(
            """INSERT INTO sections
               (content_id, index_version, ordinal, heading_path, title,
                summary_text, start_chunk_ordinal, end_chunk_ordinal,
                start_offset, end_offset, text_hash, token_count,
                structure_confidence)
               VALUES (?, ?, 0, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0)""",
            (content["id"], version, heading, heading.split(" › ")[-1],
             summary, rows[0]["ordinal"], rows[-1]["ordinal"], len(full_text),
             digest, len(full_text)),
        ).lastrowid
        conn.execute(
            "INSERT INTO sections_fts(rowid, text) VALUES (?, ?)",
            (section_id, segment_for_index(f"{heading}\n{summary}")),
        )
        cursor = 0
        for row in rows:
            start = full_text.find(row["text"], cursor)
            if start < 0:
                start = cursor
            end = start + len(row["text"])
            conn.execute(
                """UPDATE chunks SET section_id = ?, start_offset = ?,
                   end_offset = ?, index_version = ? WHERE id = ?""",
                (section_id, start, end, version, row["id"]),
            )
            cursor = end
        conn.execute(
            """INSERT INTO index_versions
               (content_id, version, status, document_hash, section_count,
                chunk_count, created_at, activated_at)
               VALUES (?, ?, 'active', ?, 1, ?, ?, ?)""",
            (content["id"], version, digest, len(rows), time.time(), time.time()),
        )

    conn.execute(
        """INSERT INTO settings(key, value) VALUES ('db_schema_version', ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (str(SCHEMA_VERSION),),
    )


def _repair_legacy_chunk_fts(conn: sqlite3.Connection) -> None:
    """Rebuild v1 contentless FTS tables so indexed rows can be deleted."""
    definitions = {
        "chunks_fts": """CREATE VIRTUAL TABLE chunks_fts USING fts5(
            text, content='', contentless_delete=1, tokenize='unicode61'
        )""",
        "chunks_fts_tri": """CREATE VIRTUAL TABLE chunks_fts_tri USING fts5(
            text, content='', contentless_delete=1, tokenize='trigram'
        )""",
    }
    legacy = []
    for table in definitions:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        compact_sql = "".join((row["sql"] or "").split()) if row else ""
        if row and "contentless_delete=1" not in compact_sql:
            legacy.append(table)
    if not legacy:
        return

    from app.index.search import segment_for_index

    chunks = conn.execute(
        "SELECT id, text, section_path FROM chunks ORDER BY id"
    ).fetchall()
    for table in legacy:
        conn.execute(f"DROP TABLE {table}")
        conn.execute(definitions[table])
        if table == "chunks_fts":
            rows = (
                (
                    row["id"],
                    segment_for_index(
                        f'{row["section_path"]}\n{row["text"]}'
                        if row["section_path"] else row["text"]
                    ),
                )
                for row in chunks
            )
        else:
            rows = (
                (
                    row["id"],
                    f'{row["section_path"]}\n{row["text"]}'
                    if row["section_path"] else row["text"],
                )
                for row in chunks
            )
        conn.executemany(
            f"INSERT INTO {table}(rowid, text) VALUES (?, ?)", rows
        )


def _init_vec_table(conn: sqlite3.Connection) -> None:
    """建向量表。放在这里而不是 SCHEMA 里，因为它依赖扩展是否加载成功。

    扩展不可用时静默跳过 —— 纯关键词检索仍然可用（§16.1a 降级链）。

    **维度迁移**：嵌入模型换代（如 256 维静态嵌入 → 1024 维 bge-m3）时
    vec0 表维度对不上，旧向量整体作废：重建表并清空 embedding_model_id，
    由回填路径（embed_backfill）分批重嵌。不迁移旧向量 —— 不同模型的
    向量空间不可比，混用比没有更糟。
    """
    import re as _re

    from app.index.embedding import DIM

    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'chunks_vec'"
        ).fetchone()
        if row and row[0]:
            m = _re.search(r"float\[(\d+)\]", row[0])
            if m and int(m.group(1)) != DIM:
                conn.execute("DROP TABLE chunks_vec")
                conn.execute("UPDATE chunks SET embedding_model_id = NULL")
                conn.execute("UPDATE contents SET embedding_model_id = NULL")
                import logging
                logging.getLogger("inktable.db").warning(
                    "向量表维度 %s → %d：已重建，全部分片待回填重嵌",
                    m.group(1), DIM)
                try:
                    from app.index import vector as vec
                    vec.invalidate_cache()
                except Exception:
                    pass
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec "
            f"USING vec0(embedding float[{DIM}])"
        )
    except sqlite3.Error:
        pass


def quick_check(conn: sqlite3.Connection) -> bool:
    """每次启动跑一次（§9.2）。秒级。"""
    try:
        return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        return False


def integrity_check(conn: sqlite3.Connection) -> list[str]:
    """执行完整 ``PRAGMA integrity_check``，返回 SQLite 的逐条诊断。

    与 ``quick_check`` 不同，这会遍历索引，适合每周或用户手动触发。
    调用方以结果严格等于 ``["ok"]`` 判断通过，损坏细节可直接展示或记日志。
    """
    try:
        return [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
    except sqlite3.DatabaseError as exc:
        return [f"database error: {exc}"]


def integrity_check_ok(conn: sqlite3.Connection) -> bool:
    return integrity_check(conn) == ["ok"]


def _main_database_path(conn: sqlite3.Connection) -> Path:
    """取得连接当前主库文件；内存库没有可持久化备份。"""
    for row in conn.execute("PRAGMA database_list"):
        # sqlite3.Row 与 tuple 都支持数字下标；第 1/2 列为 name/path。
        if row[1] == "main" and row[2]:
            return Path(row[2]).resolve()
    raise BackupError("内存数据库无法创建磁盘备份")


def _open_backup_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _load_vec_extension(conn)
    return conn


def backup_is_restorable(path: Path | str) -> bool:
    """验证备份可打开、核心 schema 存在且完整性检查通过。

    全程只读，不会把备份切到 WAL，也不会修改主库。该检查用于备份落盘前
    的临时文件以及用户选择恢复前的最后一道门。
    """
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return False
    try:
        conn = _open_backup_readonly(p)
        try:
            required = {"settings", "sources", "files", "contents", "chunks"}
            present = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not required.issubset(present):
                return False
            check_schema_version(conn)
            return integrity_check_ok(conn)
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError, RuntimeError):
        return False


def _prune_backups(directory: Path, stem: str, keep: int) -> None:
    if keep < 1:
        raise ValueError("至少保留 1 份备份")
    backups = sorted(directory.glob(f"{stem}-????-??-??.db"), reverse=True)
    for stale in backups[keep:]:
        stale.unlink(missing_ok=True)


def create_daily_backup(
    conn: sqlite3.Connection,
    *,
    backup_dir: Path | str | None = None,
    today: date | None = None,
    keep: int = BACKUP_KEEP,
) -> Path:
    """用 ``VACUUM INTO`` 创建当天首份一致性快照，并保留最近 ``keep`` 份。

    已有且可恢复的当日备份直接复用，不会重复写盘。先写同目录临时文件，
    完整性验证通过后再原子改名；失败不会留下一个冒充成功的 ``.db``。
    """
    # The startup worker and a manual API request use independent SQLite
    # connections. Serialize the check/create/replace sequence so they cannot
    # both race to replace the same daily destination on Windows.
    with _backup_lock:
        return _create_daily_backup_locked(
            conn, backup_dir=backup_dir, today=today, keep=keep,
        )


def _create_daily_backup_locked(
    conn: sqlite3.Connection,
    *,
    backup_dir: Path | str | None = None,
    today: date | None = None,
    keep: int = BACKUP_KEEP,
) -> Path:
    if conn.in_transaction:
        raise BackupError("存在未提交事务，不能创建一致性备份")
    if keep < 1:
        raise ValueError("至少保留 1 份备份")

    source = _main_database_path(conn)
    directory = Path(backup_dir) if backup_dir else source.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass

    stamp = (today or date.today()).isoformat()
    destination = directory / f"{source.stem}-{stamp}.db"
    if destination.exists():
        if not backup_is_restorable(destination):
            raise BackupError(f"当天备份已存在但不可恢复：{destination}")
        _prune_backups(directory, source.stem, keep)
        return destination

    fd, temp_name = tempfile.mkstemp(
        dir=directory, prefix=f".{destination.name}.", suffix=".partial"
    )
    os.close(fd)
    temp_path = Path(temp_name)
    # VACUUM INTO 要求目标不存在；mkstemp 只用于取得不冲突的安全文件名。
    temp_path.unlink()
    try:
        conn.execute("VACUUM INTO ?", (str(temp_path),))
        if not backup_is_restorable(temp_path):
            raise BackupError("新备份未通过完整性/可恢复性检查")
        temp_path.chmod(0o600)
        os.replace(temp_path, destination)
        _prune_backups(directory, source.stem, keep)
        return destination
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def restore_backup_to(backup: Path | str, destination: Path | str) -> Path:
    """把已验证备份复制到一个**新的**数据库文件供人工恢复。

    目标存在时一律拒绝，尤其不会自动覆盖正在使用的 ``library.db``。
    上层确认后可自行停库并完成最终切换；本函数只负责生成可验证候选文件。
    """
    source, target = Path(backup), Path(destination)
    if not backup_is_restorable(source):
        raise BackupError(f"备份不可恢复：{source}")
    if target.exists():
        raise FileExistsError(f"恢复目标已存在，拒绝覆盖：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".partial"
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copyfile(source, temp_path)
        temp_path.chmod(0o600)
        if not backup_is_restorable(temp_path):
            raise BackupError("恢复候选文件复制后校验失败")
        os.replace(temp_path, target)
        return target
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


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
    has_settings = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
    ).fetchone()
    if has_settings is None:
        return

    raw = get_setting(conn, "db_schema_version", "0") or "0"
    try:
        v = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"数据库版本号无效：{raw!r}") from exc
    if v > SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 {v} 高于当前程序支持的 {SCHEMA_VERSION}，请升级 Inktable"
        )
