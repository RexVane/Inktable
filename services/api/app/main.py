"""Ordo sidecar — FastAPI 本地服务。

安全约束（PLAN §6.2 / §6.3）：
- 绑定 127.0.0.1 + 端口 0（内核分配），端口经 stdout 回传主进程
- 会话令牌与 API 密钥经 stdin 传入，**不走命令行参数**（ps 对本机任意进程可见）
"""

from __future__ import annotations

import json
import logging
import mimetypes
import multiprocessing
import os
import queue
import secrets
import sys
import threading
import time
import contextlib
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.db.database import (
    BackupError,
    acquire_single_instance_lock,
    connect,
    create_daily_backup,
    init_db,
    integrity_check,
    quick_check,
    release_single_instance_lock,
)
from app.db.visibility import VISIBLE_FILES_COND
from app.discovery.sources import (
    discover_all,
    disk_root_label,
    volume_roots,
)
from app.watcher.policy import is_drive_root, resolve_source_policy, uses_disk_root_sources
from app.health import collect_health
from app.index.confidence import assess as assess_confidence
from app.index.pipeline import (
    activate_index_version,
    count_readable_pending,
    index_pending,
    requeue_retryable_contents,
)
from app.retrieval.compress import EvidenceSource, best_span
from app.retrieval.pipeline import run as run_retrieval
from app.watcher.scanner import (
    ScanStats,
    path_is_within,
    preview_source,
    register_file,
    scan_source,
)
from app.watcher.service import WatchService

def _warm_vector_matrix() -> None:
    """后台预热整库向量矩阵。

    语义检索主路径靠进程内矩阵（每次查询 13ms，vec0 KNN 是 780-980ms），
    但首次重建要读满全库向量、约 1 秒起。放后台线程，别让第一个提问的人
    付这笔钱。自带连接：矩阵缓存是模块级的，任一连接预热完对全进程生效。

    **必须用只读连接**：普通 connect() 会执行 `PRAGMA journal_mode = WAL`，
    那需要短暂排他锁，会和启动时的正常写入抢锁 —— 曾让 TestClient 用例
    间歇性报 "database is locked"。预热只读不写，只读连接不设 journal_mode，
    也就不可能造成这种竞争。

    纯只读，且任何异常都只是没预热到（下一次查询自己建），不能影响启动。
    """
    try:
        from app.db.database import connect_readonly
        from app.index import vector as vec

        conn = connect_readonly()
        try:
            started = time.perf_counter()
            if vec.warmup(conn):
                log.info(
                    "向量矩阵预热完成：%d 条，%.0fms",
                    vec.count(conn), (time.perf_counter() - started) * 1000,
                )
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - 预热失败只是慢一点，不能拖垮启动
        log.debug("向量矩阵预热跳过：%s", e)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    del _app
    threading.Thread(
        target=_warm_vector_matrix, name="ordo-vec-warmup", daemon=True,
    ).start()
    from app.library.worker import start_sidecar_worker, stop_sidecar_worker
    start_sidecar_worker(db, _db_lock)
    try:
        yield
    finally:
        stop_sidecar_worker()
        _shutdown()


app = FastAPI(title="Ordo API", version="0.3.0", lifespan=_lifespan)
log = logging.getLogger("ordo.main")

_db = None
_db_lock = threading.Lock()
_db_init_lock = threading.Lock()
_watch: WatchService | None = None


@contextlib.contextmanager
def _read_lock():
    """Serialize read-only requests **only** when the connection is shared.

    `_db_lock` exists to keep a single writer (PLAN §19 R9). Read-only request
    handlers must not take it: `db()` hands every thread its own connection and
    WAL supports concurrent readers alongside that single writer, so acquiring
    the write lock buys nothing and costs everything.

    Why this matters concretely: `/ask` and `/ask/stream` used to hold
    `_db_lock` across the whole of `ask()` — which includes up to three
    generation attempts plus three entailment-verifier calls. One question
    could therefore hold the global write lock for minutes, blocking every
    write endpoint, real-time ingestion, scans and index jobs behind an
    external service's latency. Retrieval and QA never write (verified: no
    INSERT/UPDATE/DELETE/commit anywhere in app/qa/answer.py,
    app/retrieval/*, app/qa/metadata.py, app/qa/report.py), so they are pure
    readers.

    The `:memory:` exception is required: there, `db()` deliberately returns
    one shared connection for all threads (an in-memory database is private
    per connection), so concurrent statements on it would risk the
    SQLITE_MISUSE interleaving that thread-local connections were introduced
    to eliminate.
    """
    if os.environ.get("ORDO_DB") == ":memory:":
        with _db_lock:
            yield
    else:
        yield

# 最近检索 trace 的内存环（PLAN §8.1 /runs/{trace_id}）。
# 只存内存不落盘：trace 含 id/score/timing，不含正文与查询原文，
# 但仍按隐私风险登记的口径处理 —— 进程退出即消失。
_TRACE_CACHE: OrderedDict[str, dict] = OrderedDict()
_TRACE_CACHE_CAP = 50
_trace_lock = threading.Lock()

# /ask/stream backpressure. The queue is bounded so an abandoned stream cannot
# let the worker accumulate deltas forever; the timeout bounds how long the
# worker waits on a consumer that has gone away before it gives up.
_STREAM_QUEUE_MAX = 256
_STREAM_PUT_TIMEOUT = 5.0

# Request-input ceilings. Every list/string field that reaches SQL or an LLM
# prompt is bounded: unbounded `file_ids` built `?,?,...` placeholder strings
# directly from input length, and an unbounded batch `limit` reached SQLite as
# `LIMIT -N`, which SQLite treats as *no limit at all*.
_MAX_PATH_CHARS = 4096
_MAX_NAME_CHARS = 512
_MAX_QUERY_CHARS = 4096
_MAX_QUESTION_CHARS = 8192
_MAX_ID_LIST = 5000
_MAX_HISTORY_TURNS = 20
_MAX_BATCH_LIMIT = 5000


class _StreamCancelled(Exception):
    """Raised inside the /ask/stream worker once the client has disconnected."""


def _release_thread_connection(conn) -> None:
    """Close a connection opened for a short-lived worker thread.

    `db()` caches per-thread connections in a `threading.local`, which for a
    request-scoped thread would otherwise only be reclaimed when the object is
    garbage collected. The `:memory:` singleton is shared by every thread and
    must never be closed here.
    """
    if conn is None or os.environ.get("ORDO_DB") == ":memory:":
        return
    if conn is _db:
        return
    if getattr(_db_local, "conn", None) is conn:
        _db_local.conn = None
    try:
        conn.close()
    except Exception:  # pragma: no cover - close must never break a response
        log.debug("关闭线程连接失败", exc_info=True)


def _path_within(path: str, root: str) -> bool:
    child = os.path.normcase(os.path.normpath(path))
    base = os.path.normcase(os.path.normpath(root))
    try:
        return os.path.commonpath((child, base)) == base
    except ValueError:
        return False


def _drive_root_for(path: str, roots: list[Path] | None = None) -> Path | None:
    if sys.platform == "win32":
        drive = Path(path).drive
        return Path(drive + os.sep) if drive else None
    if sys.platform == "darwin":
        parts = Path(os.path.abspath(os.path.normpath(path))).parts
        if len(parts) >= 3 and parts[1] == "Volumes":
            return Path("/") / "Volumes" / parts[2]
        if parts and parts[0] == "/":
            return Path("/")
    if sys.platform == "linux":
        normalized = os.path.abspath(os.path.normpath(path))
        candidates = list(roots if roots is not None else volume_roots())
        matches = [root for root in candidates if _path_within(normalized, str(root))]
        if matches:
            return max(matches, key=lambda root: len(Path(root).parts))
    return None


def _legacy_drive_roots(conn) -> list[Path]:
    """Find disks represented by legacy non-manual child sources."""
    if not uses_disk_root_sources():
        return []
    live = volume_roots()
    fixed = {
        os.path.normcase(os.path.normpath(str(root))): root
        for root in live
    }
    rows = conn.execute(
        "SELECT path, discovered_by FROM sources WHERE discovered_by != 'manual'"
    ).fetchall()
    roots: dict[str, Path] = {}
    for row in rows:
        if is_drive_root(row["path"]):
            continue
        drive = _drive_root_for(row["path"], live)
        if drive is None:
            continue
        candidate = os.path.normcase(os.path.normpath(str(drive)))
        if candidate in fixed:
            roots[candidate] = fixed[candidate]
    return list(roots.values())


def _visible_source_rows(conn):
    """Return drive roots plus explicit manual sources, hiding child routes."""
    rows = conn.execute(
        """SELECT s.id, s.name, s.path, s.kind, s.discovered_by, s.volatile,
                  s.enabled, s.auto_preserve, s.permission_ok, s.created_at
           FROM sources s ORDER BY s.enabled DESC, s.id"""
    ).fetchall()
    drive_roots = [row for row in rows if is_drive_root(row["path"])]
    visible = []
    for row in rows:
        if not uses_disk_root_sources() or is_drive_root(row["path"]):
            visible.append(row)
            continue
        if row["discovered_by"] == "manual" and not any(
            root["enabled"] and _path_within(row["path"], root["path"])
            for root in drive_roots
        ):
            visible.append(row)
    return visible


def _normalize_drive_sources(conn) -> None:
    """Fold legacy Windows child sources into their fixed-disk roots."""
    roots = _legacy_drive_roots(conn)
    if roots:
        _ensure_drive_sources(conn, roots)


def _ensure_drive_sources(conn, roots=None) -> dict[str, int]:
    """Create selected disk roots and reassign old child file records."""
    if not uses_disk_root_sources():
        return {}
    roots = list(roots or [])
    if not roots:
        return {}
    existing = conn.execute(
        "SELECT id, path, enabled, discovered_by FROM sources"
    ).fetchall()
    result: dict[str, int] = {}
    for root in roots:
        path = str(root)
        name = disk_root_label(root)
        child_enabled = any(
            _path_within(child["path"], path)
            and child["path"] != path
            and child["enabled"]
            for child in existing
        )
        conn.execute(
            """INSERT INTO sources
               (name, path, kind, discovered_by, volatile, enabled,
                permission_ok, permission_checked_at, created_at)
               VALUES (?,?,?,?,0,?,1,?,?)
               ON CONFLICT(path) DO UPDATE SET name=excluded.name,
                 discovered_by='fixed_drive', enabled=MAX(sources.enabled, excluded.enabled),
                 permission_ok=excluded.permission_ok,
                 permission_checked_at=excluded.permission_checked_at""",
            (name, path, "system", "fixed_drive", int(child_enabled),
             time.time(), time.time()),
        )
        row = conn.execute("SELECT id FROM sources WHERE path = ?", (path,)).fetchone()
        result[path] = row["id"]
        for child in existing:
            if (
                child["id"] != row["id"]
                and _path_within(child["path"], path)
            ):
                conn.execute("UPDATE files SET source_id = ? WHERE source_id = ?",
                             (row["id"], child["id"]))
                conn.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (child["id"],))
    return result


def _remember_trace(trace: dict) -> None:
    trace_id = trace.get("trace_id")
    if not trace_id:
        return
    with _trace_lock:
        _TRACE_CACHE[trace_id] = trace
        _TRACE_CACHE.move_to_end(trace_id)
        while len(_TRACE_CACHE) > _TRACE_CACHE_CAP:
            _TRACE_CACHE.popitem(last=False)


def _record_journal(answer, question: str, book_id: int | None) -> None:
    """把成功的回答沉淀进调查日志。

    只记 status == 'answered'：拒答、降级（degraded_to_retrieval）与通用路由
    都没有「基于库内资料得出的结论」可沉淀，记下来只会让下次找回命中一堆
    查不到。

    日志失败绝不影响问答返回 —— 它是事后沉淀，不在关键路径上。
    """
    if getattr(answer, "status", None) != "answered":
        return
    if getattr(answer, "mode", None) == "general":
        return
    try:
        from app.qa import journal
        content_ids = [c.get("content_id") for c in (answer.citations or [])]
        conn = db()
        with conn:
            journal.record(
                conn, question, answer.answer,
                content_ids=[i for i in content_ids if i],
                book_id=book_id,
                model=str((answer.validation or {}).get("model") or ""),
            )
    except Exception as exc:  # noqa: BLE001 - 沉淀失败不能拖垮问答
        log.warning("调查日志写入失败：%s", exc)


def _empty_database_status() -> dict:
    return {        "quick_check": {"ok": None, "checked_at": None},
        "backup": {
            "ok": None, "path": None, "error": "",
            "checked_at": None, "skipped": False,
        },
        "integrity": {
            "ok": None, "results": [], "checked_at": None,
        },
    }


_database_status = _empty_database_status()


def _status_snapshot() -> dict:
    """返回可安全序列化的数据库维护状态副本。"""
    return {name: dict(value) for name, value in _database_status.items()}


_db_local = threading.local()


def _init_primary_connection():
    """首个连接：启动完整性检查 + 每日备份（进程内只跑一次）。"""
    global _database_status
    _database_status = _empty_database_status()
    conn = connect()
    try:
        # Integrity is checked before schema/default writes so a damaged
        # database is never modified merely by launching the application.
        checked_at = time.time()
        check_ok = quick_check(conn)
        _database_status["quick_check"] = {
            "ok": check_ok, "checked_at": checked_at,
        }
        if not check_ok:
            raise RuntimeError("数据库完整性检查失败")
        init_db(conn)

        # 每日首次启动备份失败（例如磁盘满/目录无权限）不应让整个文件库
        # 不可用，但绝不能静默忽略：保留 degraded 状态，供 /db/status、
        # /stats 与设置页明确展示，用户可修复后手动重试。
        #
        # 备份在**后台线程**执行：VACUUM INTO 对 GB 级库是十秒到分钟级
        # IO，放在端口上报之前会把健康启动拖过主进程的启动超时
        # （实测 1.17GB 库启动 15.2s，恰好撞上 15s 超时被连杀三次）。
        # quick_check/init_db 仍然同步 —— 坏库必须 fail-closed。
        if os.environ.get("ORDO_DB") == ":memory:":
            _database_status["backup"] = {
                "ok": None, "path": None, "error": "",
                "checked_at": time.time(), "skipped": True,
            }
        else:
            _start_daily_backup_thread()
    except Exception:
        conn.close()
        raise
    return conn


def _start_daily_backup_thread() -> None:
    """启动后台每日备份。用独立连接，WAL 下与正常读写并行不冲突。"""

    def _worker() -> None:
        try:
            conn = connect()
        except Exception as exc:
            _database_status["backup"] = {
                "ok": False, "path": None, "error": str(exc),
                "checked_at": time.time(), "skipped": False,
            }
            log.error("备份线程无法打开数据库：%s", exc)
            return
        try:
            backup = create_daily_backup(conn)
            _database_status["backup"] = {
                "ok": True, "path": str(backup), "error": "",
                "checked_at": time.time(), "skipped": False,
            }
        except Exception as exc:  # 启动降级，状态必须对外可见
            _database_status["backup"] = {
                "ok": False, "path": None, "error": str(exc),
                "checked_at": time.time(), "skipped": False,
            }
            log.error("每日数据库备份失败，应用以降级状态继续：%s", exc)
        finally:
            conn.close()

    threading.Thread(target=_worker, name="ordo-backup", daemon=True).start()


def db():
    """每线程一条连接；写入仍由 _db_lock 串行（PLAN §19 R9）。

    此前所有线程共享同一条连接：API 线程的**无锁读**与 watcher 线程的
    写入会在语句生命周期上交错，触发 sqlite3 的 SQLITE_MISUSE
    （"bad parameter or other API misuse"，Windows 上实时入库启用后
    稳定复现）。WAL 模式原生支持多连接并发读 + 单写者，改为线程本地
    连接从根上消除共享；写路径依旧全部经 _db_lock 串行，单写者不变。

    ``ORDO_DB=:memory:`` 例外 —— 内存库的每条连接都是独立空库，
    测试环境必须维持单例连接。
    """
    global _db
    if os.environ.get("ORDO_DB") == ":memory:":
        if _db is None:
            with _db_init_lock:
                if _db is None:
                    _db = _init_primary_connection()
        return _db

    conn = getattr(_db_local, "conn", None)
    if conn is not None:
        return conn

    # 首个调用线程负责启动检查与每日备份；其余线程各自开普通连接。
    if _db is None:
        with _db_init_lock:
            if _db is None:
                _db = _init_primary_connection()
                _db_local.conn = _db
                return _db

    conn = connect()
    _db_local.conn = conn
    return conn


def watch_service() -> WatchService:
    global _watch
    if _watch is None:
        _watch = WatchService(db, _db_lock)
    return _watch


def _shutdown() -> None:
    global _db, _watch
    if _watch is not None:
        _watch.stop()
        _watch = None
    if _db is not None:
        _db.close()
        _db = None

# 令牌优先从 stdin 读取；缺省时自生成（开发态）
#
# 用 get 而非 pop：pop 会把环境变量删掉，导致模块重载时读不到
# （测试里 reload 后第二次就拿不到同一个令牌，全部请求 401）。
# 真正的隔离靠"不把令牌写进日志与响应"，而不是靠删环境变量。
SESSION_TOKEN = os.environ.get("ORDO_TOKEN") or os.environ.get("INKTABLE_TOKEN") or secrets.token_urlsafe(32)

_bearer = HTTPBearer(auto_error=False)


def require_token(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if cred is None or not secrets.compare_digest(cred.credentials, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")


@app.get("/health")
def health() -> dict:
    """真实健康检查（PLAN §15 A0）。

    不是返回 200 就算通过 —— 必须实际 dlopen sqlite-vec、建 FTS5 虚拟表、
    跑一次 KNN 查询。冻结环境（PyInstaller）下最可能翻车的正是这些，
    只返回 200 的冒烟测试暴露不出来。
    """
    return collect_health()


@app.get("/whoami", dependencies=[Depends(require_token)])
def whoami() -> dict:
    return {"app": "ordo", "authenticated": True}


@app.post("/sources/discover", dependencies=[Depends(require_token)])
def discover() -> dict:
    """触发来源自动发现，返回候选列表（PLAN §7.4）。

    只探测不启用 —— 启用需要用户逐个确认（§1 约束 4）。
    """
    sources = discover_all()
    return {"sources": [s.to_dict() for s in sources]}


@app.post("/sources/discover_deep", dependencies=[Depends(require_token)])
def discover_deep() -> dict:
    """Compatibility endpoint: Windows returns fixed disks, other platforms deep-scan."""
    if uses_disk_root_sources():
        return {"sources": [s.to_dict() for s in discover_all()]}
    from app.discovery.deepscan import deep_scan

    return {"sources": [s.to_dict() for s in deep_scan()]}


class PreviewRequest(BaseModel):
    path: str = Field(min_length=1, max_length=_MAX_PATH_CHARS)


@app.post("/sources/preview", dependencies=[Depends(require_token)])
def preview(req: PreviewRequest) -> dict:
    """启用前预扫描，使用与实际来源相同的路径策略。"""
    policy = resolve_source_policy(req.path)
    prune_roots: tuple[Path, ...] = ()
    conn = db()
    source = conn.execute(
        "SELECT id FROM sources WHERE path = ?", (str(policy.root),)
    ).fetchone()
    if source:
        rows = conn.execute(
            "SELECT path FROM sources WHERE enabled = 1 AND id != ?", (source["id"],)
        ).fetchall()
        prune_roots = tuple(
            Path(row["path"]) for row in rows
            if path_is_within(row["path"], str(policy.root), strict=True)
        )
    return preview_source(
        policy.root,
        prune_projects=policy.prune_projects,
        prune_roots=prune_roots,
    )


class EnableRequest(BaseModel):
    name: str = Field(min_length=1, max_length=_MAX_NAME_CHARS)
    path: str = Field(min_length=1, max_length=_MAX_PATH_CHARS)
    kind: Literal["im", "browser", "system", "manual"] = "manual"
    discovered_by: str = Field(default="manual", min_length=1, max_length=128)
    volatile: bool = False


@app.post("/sources/enable", dependencies=[Depends(require_token)])
def enable_source(req: EnableRequest) -> dict:
    """Enable a source, mount its watcher, then scan it in the background."""
    policy = resolve_source_policy(req.path)
    path = str(policy.root)
    with _db_lock:
        conn = db()
        drive_root = policy.root if is_drive_root(policy.root) else None
        if drive_root is not None:
            req = req.model_copy(update={
                "name": req.name or disk_root_label(drive_root),
                "kind": "system",
                "discovered_by": "fixed_drive",
            })
        conn.execute(
            "INSERT INTO sources (name, path, kind, discovered_by, volatile, enabled, "
            "permission_ok, permission_checked_at, created_at) "
            "VALUES (?,?,?,?,?,1,1,?,?) "
            "ON CONFLICT(path) DO UPDATE SET enabled=1, name=excluded.name",
            (req.name, path, req.kind, req.discovered_by, int(req.volatile),
             time.time(), time.time()),
        )
        if drive_root is not None:
            _ensure_drive_sources(conn, [drive_root])
        conn.commit()
        row = conn.execute("SELECT id, path FROM sources WHERE path = ?", (path,)).fetchone()

    watcher = watch_service()
    watcher.sync_watched_paths()
    watcher.watch(row["path"])
    if is_drive_root(row["path"]):
        job_id = watcher.queue_scan(
            row["id"], row["path"], prune_projects=policy.prune_projects,
        )
        return {
            "source_id": row["id"],
            "job_id": job_id,
            "stats": {"scanned": 0, "registered": 0, "unchanged": 0,
                       "duplicates": 0, "skipped_ext": 0, "errors": 0},
        }

    # 保留手动目录和非 Windows 的同步兼容语义。
    with _db_lock:
        conn = db()
        stats = scan_source(conn, row["id"], row["path"])
        conn.commit()
    return {
        "source_id": row["id"],
        "stats": {
            "scanned": stats.scanned,
            "registered": stats.registered,
            "unchanged": stats.unchanged,
            "duplicates": stats.duplicates,
            "skipped_ext": stats.skipped_ext,
            "errors": stats.errors,
        },
    }


@app.get("/sources", dependencies=[Depends(require_token)])
def list_sources() -> dict:
    """已配置的来源及其实时状态（PLAN §7）。

    每条附带：是否正在被监听、目录是否还存在、已收录多少文件。
    "目录还在不在"必须实时 stat —— 外置盘拔了、微信换了路径，
    库里的记录不会自己失效，只有查了才知道（§8.3 重定位的前提）。
    """
    conn = db()
    with _db_lock:
        _normalize_drive_sources(conn)
        conn.commit()
    watch_service().sync_watched_paths()
    watched = set(watch_service().status["watched"])
    rows = _visible_source_rows(conn)
    jobs = {job["source_id"]: job for job in watch_service().scan_jobs()
            if job["state"] in {"queued", "scanning", "indexing"}}

    out = []
    for r in rows:
        if sys.platform == "win32" and is_drive_root(r["path"]):
            prefix = os.path.normcase(os.path.normpath(r["path"]))
            file_count = conn.execute(
                "SELECT count(*) c FROM files WHERE lower(path) LIKE lower(?) || '%'",
                (prefix,),
            ).fetchone()["c"]
        else:
            # macOS ``/`` is a prefix of every path; count by source_id so
            # ``/Volumes/Other`` files are not attributed to the boot disk.
            file_count = conn.execute(
                "SELECT count(*) c FROM files WHERE source_id = ?", (r["id"],)
            ).fetchone()["c"]
        d = dict(r)
        d["file_count"] = file_count
        d["volatile"] = bool(r["volatile"])
        d["enabled"] = bool(r["enabled"])
        d["auto_preserve"] = bool(r["auto_preserve"])
        d["is_drive_root"] = is_drive_root(r["path"])
        d["watching"] = r["path"] in watched
        d["exists"] = Path(r["path"]).is_dir()
        d["scan"] = jobs.get(r["id"])
        out.append(d)
    return {"sources": out}


class SourceIdRequest(BaseModel):
    source_id: int


@app.post("/sources/disable", dependencies=[Depends(require_token)])
def disable_source(req: SourceIdRequest) -> dict:
    """停用来源：摘掉监听，但**保留已收录的文件与索引**。

    停用不等于删除 —— 用户可能只是暂时不想让某个目录继续进新文件，
    已经索引好的内容不该跟着消失。想彻底清掉走 /sources/remove。
    """
    with _db_lock:
        conn = db()
        row = conn.execute(
            "SELECT path FROM sources WHERE id = ?", (req.source_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="来源不存在")
        conn.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (req.source_id,))
        conn.commit()

    watch_service().unwatch(row["path"])
    return {"disabled": True, "path": row["path"]}


class ExcludeRequest(BaseModel):
    path: str = Field(min_length=1, max_length=_MAX_PATH_CHARS)


@app.get("/sources/exclusions", dependencies=[Depends(require_token)])
def list_source_exclusions(candidates: bool = True) -> dict:
    """列出已排除的目录，并给出按可见文件数排名的候选目录。

    候选只统计不判断：哪个目录是噪声只有用户知道 —— 入库白名单按扩展名
    过滤，而 .md 同时是用户笔记和代码仓库样板的格式，实测占可见文件 41%，
    自动识别的两条路（整盘剪代码项目、按 docs/ 目录名排除）都会误删真资料。
    """
    from app.domain.exclusions import list_exclusions, noisy_directory_candidates

    with _db_lock:
        conn = db()
        payload: dict = {"excluded": list_exclusions(conn)}
        if candidates:
            payload["candidates"] = noisy_directory_candidates(conn)
        return payload


@app.post("/sources/exclude", dependencies=[Depends(require_token)])
def exclude_source_path(req: ExcludeRequest) -> dict:
    """排除一个目录子树：其下文件不再进入浏览、检索与问答。

    **不移动、不改名、不删除磁盘上的任何文件**（§1 约束 1）；文件记录
    保留并置为 ignored，取消排除后重新扫描即可恢复。
    """
    from app.domain.exclusions import add_exclusion

    target = req.path.strip()
    if not target:
        raise HTTPException(status_code=400, detail="path 不能为空")
    with _db_lock:
        conn = db()
        result = add_exclusion(conn, target)
        conn.commit()
    return result


@app.post("/sources/unexclude", dependencies=[Depends(require_token)])
def unexclude_source_path(req: ExcludeRequest) -> dict:
    """取消排除。被隐藏的记录恢复为 registered，等下次扫描重新索引。"""
    from app.domain.exclusions import remove_exclusion

    target = req.path.strip()
    if not target:
        raise HTTPException(status_code=400, detail="path 不能为空")
    with _db_lock:
        conn = db()
        result = remove_exclusion(conn, target)
        conn.commit()
    return result


@app.post("/sources/remove", dependencies=[Depends(require_token)])
def remove_source(req: SourceIdRequest) -> dict:
    """移除来源，连带清掉它的文件记录与索引。

    **绝不触碰磁盘上的原文件**（§1 约束 1）—— 只删库里的记录。
    孤立的 content（没有任何 file 指向）一并清理，否则 chunks 会永久滞留。
    """
    with _db_lock:
        conn = db()
        row = conn.execute(
            "SELECT path, name FROM sources WHERE id = ?", (req.source_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="来源不存在")

        n_files = conn.execute(
            "SELECT count(*) c FROM files WHERE source_id = ?", (req.source_id,)
        ).fetchone()["c"]

        conn.execute("DELETE FROM files WHERE source_id = ?", (req.source_id,))
        # 清理不再被引用的内容及其分片（FTS5 靠触发器或显式删除同步）
        orphans = conn.execute(
            """SELECT id FROM contents
               WHERE id NOT IN (SELECT content_id FROM files WHERE content_id IS NOT NULL)"""
        ).fetchall()
        for o in orphans:
            _delete_content(conn, o["id"])
        conn.execute("DELETE FROM sources WHERE id = ?", (req.source_id,))
        conn.commit()

    watch_service().unwatch(row["path"])
    return {"removed": True, "name": row["name"],
            "files_removed": n_files, "contents_removed": len(orphans)}


class FilesRemoveRequest(BaseModel):
    file_ids: list[int] = Field(min_length=1, max_length=_MAX_ID_LIST)


class FilePathAuthorizationRequest(BaseModel):
    path: str = Field(min_length=1, max_length=_MAX_PATH_CHARS)
    action: Literal["reveal", "open"]


@app.post("/files/authorize_path", dependencies=[Depends(require_token)])
def authorize_file_path(req: FilePathAuthorizationRequest) -> dict:
    """Authorize privileged Electron shell operations for an indexed path.

    The renderer is an untrusted boundary: arbitrary local-file/LLM content is
    rendered there. A missed escape must not turn into “trash any disk path”.
    Only exact paths already known to the library are eligible. Destructive
    operations never accept a renderer-provided path; trash resolves targets
    from a stable file ID in the privileged main-process flow below.
    """
    normalized = os.path.normcase(os.path.abspath(os.path.expanduser(req.path)))
    conn = db()
    # Renderer receives paths directly from these columns, so exact lookup is
    # the common path and uses idx_files_path. Normalize again before deciding
    # to close `..`/separator/case tricks.
    rows = conn.execute(
        """SELECT f.path, f.preserved_path
           FROM files f LEFT JOIN sources s ON s.id = f.source_id
           WHERE (f.path = ? OR f.preserved_path = ?)
             AND """ + VISIBLE_FILES_COND,
        (req.path, req.path),
    ).fetchall()
    authorized = False
    for row in rows:
        source = (
            os.path.normcase(os.path.abspath(os.path.expanduser(row["path"])))
            if row["path"] else None
        )
        preserved = (
            os.path.normcase(os.path.abspath(os.path.expanduser(row["preserved_path"])))
            if row["preserved_path"] else None
        )
        if normalized == source or normalized == preserved:
            authorized = True
            break
    return {"authorized": authorized}


@app.get("/files/{file_id}/trash-targets", dependencies=[Depends(require_token)])
def file_trash_targets(file_id: int) -> dict:
    """Resolve trash targets from a stable ID for the trusted Electron main process.

    This route is deliberately absent from the renderer proxy allowlist. Paths
    come only from the database and are returned immediately before the native
    confirmation, closing the remove-then-authorize race.
    """
    conn = db()
    row = conn.execute(
        """SELECT f.id, f.name, f.path, f.preserved_path
           FROM files f LEFT JOIN sources s ON s.id = f.source_id
           WHERE f.id = ? AND """ + VISIBLE_FILES_COND,
        (file_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    targets: list[dict] = []
    seen: set[str] = set()
    for kind, candidate in (("source", row["path"]),
                            ("preserved", row["preserved_path"])):
        if not candidate:
            continue
        normalized = os.path.normcase(os.path.abspath(os.path.expanduser(candidate)))
        if normalized in seen or not Path(candidate).is_file():
            continue
        seen.add(normalized)
        targets.append({"kind": kind, "path": str(candidate)})
    return {"file_id": row["id"], "name": row["name"], "targets": targets}


@app.post("/files/remove", dependencies=[Depends(require_token)])
def remove_files(req: FilesRemoveRequest) -> dict:
    """从库中移除文件记录与索引。**绝不触碰磁盘上的原文件**（H2）。

    "移到废纸篓"由桌面端主进程用系统 API 执行 —— sidecar 永远不做
    磁盘删除，本接口只负责库内清理，并把涉及的磁盘路径返回给调用方。
    孤儿 content（不再被任何 file 引用）连同 FTS / 向量一并清理；
    file_tags / book_members / file_history 靠外键级联删除。
    """
    if not req.file_ids:
        return {"removed": 0, "contents_removed": 0,
                "paths": [], "preserved_paths": []}
    with _db_lock:
        conn = db()
        marks = ",".join("?" * len(req.file_ids))
        rows = conn.execute(
            f"SELECT id, path, state, preserved_path FROM files WHERE id IN ({marks})",
            req.file_ids,
        ).fetchall()
        if not rows:
            return {"removed": 0, "contents_removed": 0,
                    "paths": [], "preserved_paths": []}
        ids = [r["id"] for r in rows]
        id_marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM files WHERE id IN ({id_marks})", ids)
        orphans = conn.execute(
            """SELECT id FROM contents
               WHERE id NOT IN (SELECT content_id FROM files WHERE content_id IS NOT NULL)"""
        ).fetchall()
        for orphan in orphans:
            _delete_content(conn, orphan["id"])
        conn.commit()
    return {
        "removed": len(ids),
        "contents_removed": len(orphans),
        # 原件已消失的不再返回路径，避免调用方对着不存在的文件报错
        "paths": [r["path"] for r in rows if r["path"] and r["state"] != "missing"],
        "preserved_paths": [r["preserved_path"] for r in rows if r["preserved_path"]],
    }


def _delete_content(conn, content_id: int) -> None:
    """删除内容及其全部分片，FTS5 两索引与向量表一并清。

    FTS5 是 content='' 的外部内容表、chunks_vec 是独立虚拟表 ——
    都不会自动跟随主表删除。漏删任何一个都会让搜索命中已消失的分片，
    点开报错；向量表漏删还会让语义检索永远"记得"已删除的内容。
    """
    from app.index.pipeline import _delete_content_indexes

    _delete_content_indexes(conn, content_id)
    conn.execute("DELETE FROM contents WHERE id = ?", (content_id,))


# ---------------------------------------------------------------- 问答（B6）与模型配置

class LLMConfigRequest(BaseModel):
    endpoint: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=8192)
    model: str = Field(default="", max_length=512)
    # openai / responses / anthropic / ollama
    provider: str = Field(default="openai", max_length=16)


@app.post("/settings/llm", dependencies=[Depends(require_token)])
def set_llm(req: LLMConfigRequest) -> dict:
    """配置模型服务。密钥**只进内存**（§6.3）——
    持久化由 Electron 主进程用 safeStorage 加密保存，每次启动重新推送。
    全空 = 清除配置，回到纯本地模式。
    """
    from app.qa import llm

    try:
        llm.configure(req.endpoint, req.api_key, req.model, req.provider)
    except llm.LLMError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return llm.status()


@app.get("/settings/llm", dependencies=[Depends(require_token)])
def get_llm() -> dict:
    from app.qa import llm

    return llm.status()


@app.post("/settings/llm/test", dependencies=[Depends(require_token)])
def test_llm() -> dict:
    """User-triggered end-to-end model connectivity check."""
    from app.qa import llm

    return llm.probe()


# ------------------------------------------------- 模型槽位（整理 / 向量）

class ModelSlotRequest(BaseModel):
    slot: str = Field(min_length=1, max_length=16)
    provider: str = Field(default="ollama", max_length=16)
    endpoint: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=8192)
    model: str = Field(default="", max_length=512)
    clear: bool = False


@app.get("/settings/models", dependencies=[Depends(require_token)])
def get_model_slots() -> dict:
    from app.config import models as model_slots
    from app.qa import llm

    return {"slots": {"qa": llm.status(), **model_slots.all_status()}}


@app.post("/settings/models", dependencies=[Depends(require_token)])
def set_model_slot(req: ModelSlotRequest) -> dict:
    """写入/清除一个模型槽位；密钥只进内存，持久化在 Electron 侧。"""
    from app.config import models as model_slots
    from app.qa import llm

    try:
        if req.slot == "qa":
            # qa 槽位沿用 app.qa.llm 的全局配置（/settings/llm 等价路径）
            if req.clear:
                llm.configure("", "", "")
            else:
                llm.configure(req.endpoint, req.api_key, req.model,
                              req.provider)
            return llm.status()
        if req.slot not in model_slots.SLOTS:
            raise model_slots.SlotConfigError(
                f"未知槽位：{req.slot}（可用：qa/{'/'.join(model_slots.SLOTS)}）")
        if req.clear:
            model_slots.clear(req.slot)
        else:
            model_slots.configure(req.slot, req.provider, req.endpoint,
                                  req.api_key, req.model)
        if req.slot == "embedding":
            # 换模型/换地址后丢弃旧客户端实例与探测缓存
            from app.index import embedding
            embedding.unload()
        return model_slots.status(req.slot)
    except (model_slots.SlotConfigError, llm.LLMError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class ModelSlotTestRequest(BaseModel):
    slot: str = Field(min_length=1, max_length=16)


@app.post("/settings/models/test", dependencies=[Depends(require_token)])
def test_model_slot(req: ModelSlotTestRequest) -> dict:
    """按槽位做真实探测：qa/整理发最小补全，向量试编码并报维度。"""
    from app.config import llm_client, models as model_slots
    from app.qa import llm

    if req.slot == "qa":
        return llm.probe()
    if req.slot not in model_slots.SLOTS:
        raise HTTPException(status_code=422, detail=f"未知槽位：{req.slot}")
    if req.slot == "embedding":
        from app.index import embedding
        embedding.unload()
        started = time.time()
        try:
            emb = embedding.Embedder()
            vec = emb.encode_one("维度探测")
        except embedding.EmbeddingUnavailable as exc:
            return {"ok": False, "message": str(exc), "latency_ms": 0}
        dim = int(vec.shape[0])
        matched = dim == embedding.DIM
        return {
            "ok": matched, "model": emb.tag, "dim": dim,
            "expected_dim": embedding.DIM,
            "message": ("维度匹配，语义检索可用" if matched else
                        f"该模型输出 {dim} 维，与当前向量表 {embedding.DIM} 维"
                        "不符，不能启用（换模型会作废全部向量）"),
            "latency_ms": int((time.time() - started) * 1000),
        }
    cfg = model_slots.effective(req.slot)
    if cfg is None:
        raise HTTPException(status_code=422, detail="该槽位尚未配置")
    return llm_client.probe_chat(cfg)


class ModelListRequest(BaseModel):
    slot: str = Field(min_length=1, max_length=16)
    provider: str = Field(default="", max_length=16)
    endpoint: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=8192)


@app.post("/settings/models/list", dependencies=[Depends(require_token)])
def list_slot_models(req: ModelListRequest) -> dict:
    """拉取可选模型名。表单值优先，缺省回退已保存配置（密钥不出服务端）。"""
    from app.config import llm_client, models as model_slots

    saved: dict | None = None
    if req.slot == "qa":
        from app.qa import llm
        endpoint, api_key, _model, saved_provider = llm.credentials_for_proxy()
        saved = {"provider": saved_provider, "endpoint": endpoint, "api_key": api_key}
    elif req.slot in model_slots.SLOTS:
        saved = model_slots.get(req.slot)
    else:
        raise HTTPException(status_code=422, detail=f"未知槽位：{req.slot}")

    from app.config.endpoints import (
        EndpointPolicyError,
        credential_scope,
        normalize_model_endpoint,
    )
    provider = (req.provider or (saved or {}).get("provider") or "ollama").strip()
    endpoint = (req.endpoint or (saved or {}).get("endpoint") or "").strip().rstrip("/")
    if not endpoint and provider == "ollama":
        # 本地 Ollama 没填地址就探测本机地址（11434 / 18434），不要写死官方口
        endpoint = model_slots.discover_ollama_url()
    if not endpoint:
        raise HTTPException(status_code=422, detail="接口地址不能为空")
    try:
        endpoint = normalize_model_endpoint(endpoint)
    except EndpointPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    api_key = req.api_key
    if not api_key and saved and saved.get("api_key"):
        try:
            same_scope = credential_scope(provider, endpoint) == credential_scope(
                str(saved.get("provider") or ""), str(saved.get("endpoint") or ""))
        except EndpointPolicyError:
            same_scope = False
        if same_scope:
            api_key = str(saved["api_key"])
    if provider in {"openai", "anthropic", "responses"} and not api_key:
        raise HTTPException(
            status_code=422, detail="拉取模型列表需要 API 密钥（先填写或保存密钥）")
    try:
        names = llm_client.list_models(
            provider=provider, endpoint=endpoint, api_key=api_key)
    except llm_client.LLMClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"models": names}


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=_MAX_QUESTION_CHARS)
    book_id: int | None = None
    # 最近几轮问答（[{q, a}]），用于把追问浓缩成可独立检索的问题
    history: list[dict] = Field(default_factory=list, max_length=_MAX_HISTORY_TURNS)
    # 回答档位：quick = 单轮轻量管线（小证据预算、无重试、无蕴含校验）；
    # deep = 全量管线（默认，原行为）
    mode: str = Field(default="deep", max_length=8)


def _validate_ask_mode(req: AskRequest) -> None:
    if req.mode not in ("quick", "deep"):
        raise HTTPException(status_code=422, detail="mode 必须是 quick 或 deep")


@app.post("/ask", dependencies=[Depends(require_token)])
def post_ask(req: AskRequest) -> dict:
    """带引用问答（§12.4）。非流式 —— 后置校验会改写答案，流式收不回。"""
    from app.qa.answer import ask
    from app.qa.llm import LLMError

    q = req.question.strip()
    if not q:
        raise HTTPException(status_code=400, detail="问题为空")
    _validate_ask_mode(req)
    with _read_lock():
        conn = db()
        from app.qa.metadata import answer_metadata
        metadata = answer_metadata(conn, q)
        if metadata is not None:
            return {
                "status": "answered", "answer": metadata.answer,
                "citations": [], "retrieved": [], "hedge": "",
                "validation": {"route": metadata.query_kind},
                "mode": "metadata", "answer_source": "metadata",
                "used_personal_files": False, "trace_id": None,
                "timings": [], "degraded": [],
                "trace": {"route": metadata.query_kind, "stages": [], "degraded": []},
            }
        try:
            a = ask(conn, q, req.book_id, history=req.history[-4:],
                    qa_mode=req.mode)
        except LLMError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
    trace = a.trace
    _remember_trace(trace)
    _record_journal(a, q, req.book_id)
    return {
        "status": a.status, "answer": a.answer, "citations": a.citations,
        "retrieved": a.retrieved, "hedge": a.hedge, "validation": a.validation,
        "mode": a.mode,
        "answer_source": "general" if a.mode == "general" else "library",
        "used_personal_files": bool(getattr(a, "used_personal_files", False)),
        "trace_id": trace.get("trace_id"), "timings": trace.get("stages", []),
        "degraded": trace.get("degraded", []), "trace": trace,
    }


@app.post("/ask/stream", dependencies=[Depends(require_token)])
def post_ask_stream(req: AskRequest):
    """SSE draft/regenerate/finalize protocol; citations are emitted last."""
    q = req.question.strip()
    if not q:
        raise HTTPException(status_code=400, detail="问题为空")
    _validate_ask_mode(req)

    def sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def generate():
        # Bounded queue: an abandoned stream must not let the producer grow the
        # queue without limit. Cancellation is cooperative — the worker only
        # notices when it next emits, which is per draft delta.
        events: queue.Queue[tuple[str, dict]] = queue.Queue(maxsize=_STREAM_QUEUE_MAX)
        cancelled = threading.Event()

        def _offer(item: tuple[str, dict]) -> bool:
            """Put with a bounded wait; report whether the consumer took it."""
            try:
                events.put(item, timeout=_STREAM_PUT_TIMEOUT)
                return True
            except queue.Full:
                cancelled.set()
                return False

        def emit(name: str, payload: dict) -> None:
            if cancelled.is_set():
                raise _StreamCancelled()
            if not _offer((name, payload)):
                raise _StreamCancelled()

        def worker() -> None:
            conn = None
            try:
                with _read_lock():
                    conn = db()
                    from app.qa.metadata import answer_metadata
                    metadata = answer_metadata(conn, q)
                    if metadata is not None:
                        result = {
                            "status": "answered", "answer": metadata.answer,
                            "citations": [], "retrieved": [], "hedge": "",
                            "validation": {"route": metadata.query_kind},
                            "mode": "metadata", "answer_source": "metadata",
                            "used_personal_files": False, "trace_id": None,
                            "timings": [], "degraded": [],
                            "trace": {"route": metadata.query_kind, "stages": [], "degraded": []},
                        }
                    else:
                        from app.qa.answer import ask
                        answer = ask(
                            conn, q, req.book_id, history=req.history[-4:],
                            event_callback=emit, qa_mode=req.mode,
                        )
                        trace = answer.trace
                        _remember_trace(trace)
                        _record_journal(answer, q, req.book_id)
                        result = {
                            "status": answer.status, "answer": answer.answer,
                            "citations": answer.citations,
                            "retrieved": answer.retrieved, "hedge": answer.hedge,
                            "validation": answer.validation, "mode": answer.mode,
                            "answer_source": (
                                "general" if answer.mode == "general" else "library"
                            ),
                            "used_personal_files": bool(
                                getattr(answer, "used_personal_files", False)
                            ),
                            "trace_id": trace.get("trace_id"),
                            "timings": trace.get("stages", []),
                            "degraded": trace.get("degraded", []), "trace": trace,
                        }
                if not cancelled.is_set():
                    _offer(("__result__", result))
            except _StreamCancelled:
                log.debug("ask/stream 客户端已断开，生成中止")
            except Exception as exc:
                if not cancelled.is_set():
                    _offer(("chat.error", {"message": str(exc)}))
            finally:
                _offer(("__done__", {}))
                _release_thread_connection(conn)

        thread = threading.Thread(
            target=worker, name="ordo-ask-stream", daemon=True,
        )
        thread.start()
        result = None
        try:
            yield sse("chat.searching", {"question": q})
            while True:
                name, payload = events.get()
                if name == "__done__":
                    break
                if name == "__result__":
                    result = payload
                    continue
                yield sse(name, payload)
            if result is not None:
                citations = result.pop("citations", [])
                yield sse("chat.finalize", result)
                yield sse("chat.citations", {"citations": citations})
        finally:
            # Reached on client disconnect (GeneratorExit) as well as normal
            # completion. Signal the worker and drain so a producer blocked on
            # a full queue can observe the cancellation and unwind.
            cancelled.set()
            while True:
                try:
                    events.get_nowait()
                except queue.Empty:
                    break

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/runs/{trace_id}", dependencies=[Depends(require_token)])
def get_run(trace_id: str) -> dict:
    """调试：读取最近一次检索/问答的非持久化 trace（PLAN §8.1）。

    只保留最近 50 条且不落盘 —— sidecar 重启后即失效，这是有意的：
    trace 服务于"刚才这次检索为什么这样排"，不是审计日志。
    """
    with _trace_lock:
        trace = _TRACE_CACHE.get(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace 不存在或已过期")
    return trace


@app.post("/classify/llm", dependencies=[Depends(require_token)])
def post_llm_classify() -> dict:
    """让模型归类未分类文件（B1）。手动触发 —— 每次点击即一次云端授权。"""
    from app.qa.classify_llm import apply_llm_classification, plan_llm_classification
    from app.qa.llm import LLMError

    # 读取候选与模型调用在锁外完成；只有短写入进锁（见 _read_lock 的说明）。
    try:
        with _read_lock():
            plan = plan_llm_classification(db())
    except LLMError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    with _db_lock:
        conn = db()
        try:
            r = apply_llm_classification(conn, plan)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return r


# ---------------------------------------------------------------- 文件书（B7）

class BookCreate(BaseModel):
    name: str = Field(min_length=1, max_length=_MAX_NAME_CHARS)


class BookMember(BaseModel):
    book_id: int = Field(ge=1)
    file_ids: list[int] = Field(min_length=1, max_length=_MAX_ID_LIST)


@app.get("/books", dependencies=[Depends(require_token)])
def get_books() -> dict:
    conn = db()
    rows = conn.execute(
        f"""SELECT b.id, b.name,
                   (SELECT count(*)
                    FROM book_members m
                    JOIN files f ON f.id = m.file_id
                    LEFT JOIN sources s ON s.id = f.source_id
                    WHERE m.book_id = b.id AND {VISIBLE_FILES_COND}) AS n
            FROM books b ORDER BY b.name"""
    ).fetchall()
    return {"books": [dict(r) for r in rows]}


@app.post("/books", dependencies=[Depends(require_token)])
def post_book(req: BookCreate) -> dict:
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="书名不能为空")
    with _db_lock:
        conn = db()
        try:
            cur = conn.execute(
                "INSERT INTO books (name, created_at) VALUES (?, ?)",
                (name, time.time()),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail="已有同名文件书") from e
    return {"id": cur.lastrowid}


@app.post("/books/add", dependencies=[Depends(require_token)])
def book_add(req: BookMember) -> dict:
    with _db_lock:
        conn = db()
        if conn.execute(
            "SELECT 1 FROM books WHERE id=?", (req.book_id,)
        ).fetchone() is None:
            raise HTTPException(status_code=404, detail="文件书不存在")
        requested = list(dict.fromkeys(req.file_ids))
        marks = ",".join("?" * len(requested))
        visible_rows = conn.execute(
            f"""SELECT f.id FROM files f
                LEFT JOIN sources s ON s.id = f.source_id
                WHERE f.id IN ({marks}) AND {VISIBLE_FILES_COND}""",
            requested,
        ).fetchall()
        visible_ids = {int(row["id"]) for row in visible_rows}
        existing_ids = {
            int(row["file_id"])
            for row in conn.execute(
                f"""SELECT file_id FROM book_members
                    WHERE book_id=? AND file_id IN ({marks})""",
                [req.book_id, *requested],
            ).fetchall()
            if int(row["file_id"]) in visible_ids
        }
        to_add = [fid for fid in requested if fid in visible_ids - existing_ids]
        added = 0
        now = time.time()
        try:
            for fid in to_add:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO book_members
                       (book_id, file_id, added_at) VALUES (?, ?, ?)""",
                    (req.book_id, fid, now),
                )
                added += max(0, cursor.rowcount)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    not_found = [fid for fid in requested if fid not in visible_ids]
    return {
        "added": added,
        "already_present": len(existing_ids),
        "not_found": len(not_found),
        "already_present_ids": sorted(existing_ids),
        "not_found_ids": not_found,
    }


@app.post("/books/remove_member", dependencies=[Depends(require_token)])
def book_remove_member(req: BookMember) -> dict:
    with _db_lock:
        conn = db()
        marks = ",".join("?" * len(req.file_ids))
        conn.execute(
            f"DELETE FROM book_members WHERE book_id = ? AND file_id IN ({marks})",
            [req.book_id, *req.file_ids],
        )
        conn.commit()
    return {"ok": True}


class BookId(BaseModel):
    book_id: int


@app.post("/books/delete", dependencies=[Depends(require_token)])
def book_delete(req: BookId) -> dict:
    """删书只删集合关系，**文件与索引一概不动** —— 书是虚拟的。"""
    with _db_lock:
        conn = db()
        if conn.execute("DELETE FROM books WHERE id = ?", (req.book_id,)).rowcount == 0:
            raise HTTPException(status_code=404, detail="文件书不存在")
        conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 分类（信息层）

class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=_MAX_NAME_CHARS)
    parent_id: int | None = None


class CategoryRename(BaseModel):
    category_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=_MAX_NAME_CHARS)


class CategoryId(BaseModel):
    category_id: int


class AssignRequest(BaseModel):
    file_ids: list[int] = Field(min_length=1, max_length=_MAX_ID_LIST)
    category_id: int | None = None
    # 顺手建规则：同来源同扩展名以后自动归到此分类（§11.4 回流学习）
    learn_rule: bool = False


@app.get("/categories", dependencies=[Depends(require_token)])
def get_categories() -> dict:
    from app.organize.classify import category_tree

    conn = db()
    unclassified = conn.execute(
        "SELECT count(*) c FROM files f LEFT JOIN sources s ON f.source_id = s.id "
        f"WHERE f.category_id IS NULL AND {VISIBLE_FILES_COND}"
    ).fetchone()["c"]
    return {"tree": category_tree(conn), "unclassified": unclassified}


@app.post("/categories", dependencies=[Depends(require_token)])
def post_category(req: CategoryCreate) -> dict:
    from app.organize.classify import CategoryError, create_category

    with _db_lock:
        conn = db()
        try:
            cid = create_category(conn, req.name, req.parent_id)
            conn.commit()
        except CategoryError as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(e)) from e
    return {"id": cid}


@app.post("/categories/rename", dependencies=[Depends(require_token)])
def post_category_rename(req: CategoryRename) -> dict:
    from app.organize.classify import CategoryError, rename_category

    with _db_lock:
        conn = db()
        try:
            rename_category(conn, req.category_id, req.name)
            conn.commit()
        except CategoryError as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@app.post("/categories/delete", dependencies=[Depends(require_token)])
def post_category_delete(req: CategoryId) -> dict:
    from app.organize.classify import CategoryError, delete_category

    with _db_lock:
        conn = db()
        try:
            delete_category(conn, req.category_id)
            conn.commit()
        except CategoryError as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True}


@app.post("/files/classify", dependencies=[Depends(require_token)])
def post_classify(req: AssignRequest) -> dict:
    """批量归类。learn_rule=True 时顺手生成规则并回溯存量（§11.4）。

    规则从**第一个文件**的来源+扩展名归纳 —— 用户勾了"以后都这样"，
    指的就是"这一类"。
    """
    from app.organize.classify import (
        CategoryError,
        assign_category,
        backfill_rule,
        create_rule,
    )

    with _db_lock:
        conn = db()
        try:
            n = assign_category(conn, req.file_ids, req.category_id, by="user")
            learned = backfilled = 0
            if req.learn_rule and req.category_id is not None and req.file_ids:
                f = conn.execute(
                    "SELECT ext, source_id FROM files WHERE id = ?",
                    (req.file_ids[0],),
                ).fetchone()
                if f and f["ext"]:
                    rid = create_rule(
                        conn, req.category_id, match_ext=f["ext"],
                        match_source_id=f["source_id"],
                        learned_from=req.file_ids[0],
                    )
                    learned = 1
                    backfilled = backfill_rule(conn, rid)
            conn.commit()
        except CategoryError as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(e)) from e
    return {"assigned": n, "rule_created": learned, "backfilled": backfilled}


@app.post("/classify/auto_ext", dependencies=[Depends(require_token)])
def post_auto_classify_by_ext() -> dict:
    """默认自动分类：按扩展名归类未分类文件。"""
    from app.organize.classify import CategoryError, auto_classify_by_ext

    with _db_lock:
        conn = db()
        try:
            result = auto_classify_by_ext(conn)
            conn.commit()
        except CategoryError as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(e)) from e
    return result


class FileIdRequest(BaseModel):
    file_id: int


@app.post("/files/preserve", dependencies=[Depends(require_token)])
def preserve_one(req: FileIdRequest) -> dict:
    """保全单个文件（§2.5）：复制到应用空间，原文件不动。"""
    from app.organize.preserve import PreserveError, preserve_file

    with _db_lock:
        conn = db()
        try:
            result = preserve_file(conn, req.file_id)
            conn.commit()
            return result
        except PreserveError as e:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/sources/preserve_all", dependencies=[Depends(require_token)])
def preserve_all(req: SourceIdRequest) -> dict:
    """保全来源下全部文件。微信随时可能清缓存 —— 一键兜底。"""
    from app.organize.preserve import preserve_source

    with _db_lock:
        conn = db()
        result = preserve_source(conn, req.source_id)
    return result


class AutoPreserveRequest(BaseModel):
    source_id: int
    enabled: bool


@app.post("/sources/auto_preserve", dependencies=[Depends(require_token)])
def set_auto_preserve(req: AutoPreserveRequest) -> dict:
    """开关易失来源的自动保全：新文件入库即复制（§2.5，默认关）。"""
    with _db_lock:
        conn = db()
        cur = conn.execute(
            "UPDATE sources SET auto_preserve = ? WHERE id = ?",
            (int(req.enabled), req.source_id),
        )
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="来源不存在")
    return {"source_id": req.source_id, "auto_preserve": req.enabled}


class AddSourceRequest(BaseModel):
    path: str = Field(min_length=1, max_length=_MAX_PATH_CHARS)
    name: str | None = Field(default=None, max_length=_MAX_NAME_CHARS)


@app.post("/sources/add", dependencies=[Depends(require_token)])
def add_source(req: AddSourceRequest) -> dict:
    """手动添加目录（PLAN §7.6）。

    自动发现覆盖不到的场景：NAS 挂载点、外置盘、自定义工作目录。
    """
    p = Path(req.path).expanduser()
    if not p.is_dir():
        raise HTTPException(status_code=400, detail="目录不存在")
    try:
        next(iter(os.scandir(p)), None)
    except (PermissionError, OSError) as e:
        raise HTTPException(status_code=403, detail=f"无法读取该目录：{e}") from e

    return enable_source(EnableRequest(
        name=req.name or p.name, path=str(p),
        kind="manual", discovered_by="manual", volatile=False,
    ))


# 可见性口径由 app.db.visibility 统一提供，浏览、检索和问答共用。

@app.get("/files", dependencies=[Depends(require_token)])
def list_files(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1_000_000),
    q: str | None = None,
    category_id: int | None = None,
    unclassified: bool = False,
    book_id: int | None = None,
    source: str | None = None,
    ext: str | None = None,
    duplicate: bool = False,
    dir_path: str | None = Query(None, alias="dir"),
    group: str | None = None,
    since_days: int | None = Query(None, ge=1, le=365),
) -> dict:
    conn = db()
    conds, params = [VISIBLE_FILES_COND], []
    if since_days is not None:
        # 时间线视图："最近一周收到的文件"
        conds.append("f.mtime >= ?")
        params.append(time.time() - since_days * 86400)
    if q:
        conds.append("f.name LIKE ?")
        params.append(f"%{q}%")
    if category_id is not None:
        # 含子分类：分类树很浅（个人库），递归 CTE 一次取全
        conds.append(
            """f.category_id IN (
                 WITH RECURSIVE sub(id) AS (
                   SELECT ? UNION ALL
                   SELECT c.id FROM categories c JOIN sub ON c.parent_id = sub.id
                 ) SELECT id FROM sub)"""
        )
        params.append(category_id)
    if unclassified:
        conds.append("f.category_id IS NULL")
    if book_id is not None:
        conds.append("f.id IN (SELECT file_id FROM book_members WHERE book_id = ?)")
        params.append(book_id)
    if source is not None:
        # renderer 侧来源导航使用展示名；这里必须服务端过滤，否则分页后再
        # 前端过滤会漏掉后续页中属于该来源的文件。
        conds.append("s.name = ?")
        params.append(source)
    if ext is not None:
        # Query 中 ``ext=`` 是有意义的：筛选无扩展名文件。数据库历史数据
        # 可能用 NULL 或空串表示，统一视作空扩展名。
        conds.append("lower(COALESCE(f.ext, '')) = lower(?)")
        params.append(ext)
    if duplicate:
        # duplicate 是共享同一 content 的视图状态；返回该重复组的全部文件，
        # 方便用户看到原件与各副本，而不是只返回任意一个“后来的”路径。
        # 重复对端也必须可见，否则停用来源后会出现"孤儿重复项"。
        conds.append(
            """(f.content_id IS NOT NULL AND EXISTS (
                   SELECT 1 FROM files other
                   LEFT JOIN sources os ON os.id = other.source_id
                   WHERE other.content_id = f.content_id AND other.id != f.id
                     AND (other.source_id IS NULL OR os.enabled = 1)
               ))"""
        )
    if dir_path:
        # 目录过滤（文件树点击）：前缀匹配用 substr 而不是 LIKE，
        # 路径里的 % 和 _ 不需要转义。分隔符必须用 os.sep —— 库里存的
        # 是平台原生路径，Windows 上硬编码 "/" 会让过滤永远落空。
        prefix = dir_path.rstrip("\\/") + os.sep
        conds.append("substr(f.path, 1, ?) = ?")
        params.extend([len(prefix), prefix])
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    from_sql = "FROM files f LEFT JOIN sources s ON f.source_id = s.id"

    total = conn.execute(
        f"SELECT count(*) c {from_sql} {where}", params
    ).fetchone()["c"]
    # group=ext：同扩展名聚在一起（文件多的类型靠前），组内最新在上。
    # 排序在服务端做，分页跨页时分组才不会断。
    select_extra = ""
    order_by = "ORDER BY f.mtime DESC, f.id DESC"
    if group == "ext":
        select_extra = (
            ", count(*) OVER (PARTITION BY lower(COALESCE(f.ext, ''))) AS ext_group_size"
        )
        order_by = ("ORDER BY ext_group_size DESC, lower(COALESCE(f.ext, '')), "
                    "f.mtime DESC, f.id DESC")
    rows = conn.execute(
        f"SELECT f.id, f.name, f.path, f.ext, f.size, f.state, f.mtime, f.preserved_path, "
        f"f.source_id, s.name AS source_name, s.volatile{select_extra} "
        f"{from_sql} {where} "
        f"{order_by} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return {"total": total, "files": [dict(r) for r in rows]}


@app.get("/files/tree", dependencies=[Depends(require_token)])
def files_tree(
    dir_path: str | None = Query(None, alias="dir"),
    file_limit: int = Query(120, ge=1, le=500),
) -> dict:
    """左栏文件树。不传 dir 返回树根（已启用来源）；传 dir 返回下一层。

    树完全从库内已登记的文件路径推导，**不扫磁盘** —— 所以只包含
    已收录的文件，停用来源的子树自然消失。
    """
    conn = db()
    if not dir_path:
        rows = _visible_source_rows(conn)
        roots = []
        for s in rows:
            if not s["enabled"]:
                continue
            if sys.platform == "win32" and is_drive_root(s["path"]):
                prefix = os.path.normcase(os.path.normpath(s["path"]))
                count = conn.execute(
                    """SELECT count(*) c FROM files f
                       LEFT JOIN sources s ON s.id = f.source_id
                       WHERE lower(f.path) LIKE lower(?) || '%' AND """
                    + VISIBLE_FILES_COND,
                    (prefix,),
                ).fetchone()["c"]
            else:
                count = conn.execute(
                    """SELECT count(*) c FROM files f
                       LEFT JOIN sources s ON s.id = f.source_id
                       WHERE f.source_id = ? AND """ + VISIBLE_FILES_COND,
                    (s["id"],),
                ).fetchone()["c"]
            roots.append({"id": s["id"], "name": s["name"],
                          "path": s["path"], "count": count})
        roots.sort(key=lambda item: (-item["count"], item["name"]))
        return {"roots": roots}

    prefix = dir_path.rstrip("\\/") + os.sep
    rows = conn.execute(
        f"""SELECT f.id, f.name, f.path, f.ext, f.state, f.preserved_path
            FROM files f LEFT JOIN sources s ON s.id = f.source_id
            WHERE {VISIBLE_FILES_COND} AND substr(f.path, 1, ?) = ?
            ORDER BY f.name, f.id""",
        (len(prefix), prefix),
    ).fetchall()
    dirs: dict[str, int] = {}
    files_out: list[dict] = []
    for r in rows:
        rest = r["path"][len(prefix):]
        if os.sep in rest:
            head = rest.split(os.sep, 1)[0]
            dirs[head] = dirs.get(head, 0) + 1
        elif len(files_out) <= file_limit:
            files_out.append(dict(r))
    truncated = len(files_out) > file_limit
    if truncated:
        files_out = files_out[:file_limit]
    return {
        "dir": dir_path,
        "dirs": [{"name": name, "path": prefix + name, "count": count}
                 for name, count in sorted(dirs.items())],
        "files": files_out,
        "truncated": truncated,
    }


FILE_DETAIL_SECTION_LIMIT = 24
FILE_DETAIL_TEXT_LIMIT = 4000


def _decode_locator(value: str | None) -> dict:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


@app.get("/files/{file_id}/detail", dependencies=[Depends(require_token)])
def file_detail(file_id: int) -> dict:
    """Return bounded detail for the selected file and its active index only."""
    conn = db()
    row = conn.execute(
        f"""SELECT f.id, f.volume_uuid, f.inode, f.content_id, f.path, f.name,
                  f.origin_path, f.preserved_path, f.source_id, f.ext, f.mime,
                  f.size, f.state, f.error_code, f.retry_count, f.category_id,
                  f.confidence, f.confirmed_by_user, f.is_dataless, f.mtime,
                  f.detected_at, f.indexed_at, f.missing_since,
                  s.name AS source_name, s.path AS source_path,
                  s.kind AS source_kind, s.discovered_by AS source_discovered_by,
                  s.volatile AS source_volatile, s.enabled AS source_enabled,
                  cat.parent_id AS category_parent_id,
                  cat.name AS category_name,
                  c.sha256 AS content_sha256, c.size AS content_size,
                  c.parse_state, c.chunk_count, c.embedding_model_id,
                  c.indexed_at AS content_indexed_at, c.active_index_version
           FROM files f
           LEFT JOIN sources s ON s.id = f.source_id
           LEFT JOIN categories cat ON cat.id = f.category_id
           LEFT JOIN contents c ON c.id = f.content_id
           WHERE f.id = ? AND {VISIBLE_FILES_COND}""",
        (file_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")

    tags = [dict(item) for item in conn.execute(
        """SELECT t.id, t.name, t.color
           FROM tags t JOIN file_tags ft ON ft.tag_id = t.id
           WHERE ft.file_id = ? ORDER BY t.name, t.id""",
        (file_id,),
    )]
    books = [dict(item) for item in conn.execute(
        """SELECT b.id, b.name
           FROM books b JOIN book_members bm ON bm.book_id = b.id
           WHERE bm.file_id = ? ORDER BY b.name, b.id""",
        (file_id,),
    )]

    source = None
    if row["source_id"] is not None:
        source = {
            "id": row["source_id"], "name": row["source_name"],
            "path": row["source_path"], "kind": row["source_kind"],
            "discovered_by": row["source_discovered_by"],
            "volatile": bool(row["source_volatile"]),
            "enabled": bool(row["source_enabled"]),
        }
    category = None
    if row["category_id"] is not None:
        category = {
            "id": row["category_id"], "parent_id": row["category_parent_id"],
            "name": row["category_name"],
        }
    content = None
    if row["content_id"] is not None:
        content = {
            "id": row["content_id"], "sha256": row["content_sha256"],
            "size": row["content_size"], "parse_state": row["parse_state"],
            "chunk_count": row["chunk_count"],
            "embedding_model_id": row["embedding_model_id"],
            "indexed_at": row["content_indexed_at"],
            "active_index_version": row["active_index_version"],
        }

    file = {
        "id": row["id"], "content_id": row["content_id"],
        "volume_uuid": row["volume_uuid"], "inode": row["inode"],
        "name": row["name"], "path": row["path"],
        "origin_path": row["origin_path"],
        "preserved_path": row["preserved_path"],
        "ext": row["ext"], "mime": row["mime"], "size": row["size"],
        "state": row["state"], "error_code": row["error_code"],
        "retry_count": row["retry_count"], "confidence": row["confidence"],
        "confirmed_by_user": bool(row["confirmed_by_user"]),
        "is_dataless": bool(row["is_dataless"]), "mtime": row["mtime"],
        "detected_at": row["detected_at"], "indexed_at": row["indexed_at"],
        "missing_since": row["missing_since"], "source": source,
        "category": category, "tags": tags, "books": books,
        "content": content,
    }

    document = None
    sections: list[dict] = []
    truncated = False
    if row["content_id"] is not None:
        active_version = row["active_index_version"]
        document_row = conn.execute(
            """SELECT title, summary_text, token_count, structure_confidence,
                      index_version
               FROM document_representations
               WHERE content_id = ? AND index_version = ?""",
            (row["content_id"], active_version),
        ).fetchone()
        if document_row is not None:
            document = dict(document_row)

        passage_rows = conn.execute(
            """SELECT ch.id, ch.section_id, sec.title AS section_title,
                      ch.section_path, ch.page, ch.page_end, ch.ordinal,
                      substr(ch.text, 1, ?) AS text, length(ch.text) AS text_length,
                      ch.start_offset, ch.end_offset, ch.bbox
               FROM chunks ch
               LEFT JOIN sections sec
                 ON sec.id = ch.section_id AND sec.index_version = ch.index_version
               WHERE ch.content_id = ? AND ch.index_version = ?
                 AND ch.layer = 'child'
               ORDER BY ch.ordinal, ch.id LIMIT ?""",
            (FILE_DETAIL_TEXT_LIMIT + 1, row["content_id"], active_version,
             FILE_DETAIL_SECTION_LIMIT + 1),
        ).fetchall()
        truncated = len(passage_rows) > FILE_DETAIL_SECTION_LIMIT
        for passage in passage_rows[:FILE_DETAIL_SECTION_LIMIT]:
            text = passage["text"] or ""
            text_truncated = passage["text_length"] > FILE_DETAIL_TEXT_LIMIT
            truncated = truncated or text_truncated
            sections.append({
                "id": passage["id"], "section_id": passage["section_id"],
                "title": passage["section_title"],
                "section_path": passage["section_path"],
                "page": passage["page"], "page_end": passage["page_end"],
                "ordinal": passage["ordinal"],
                "text": text[:FILE_DETAIL_TEXT_LIMIT],
                "text_truncated": text_truncated,
                "start_offset": passage["start_offset"],
                "end_offset": passage["end_offset"],
                "locator": _decode_locator(passage["bbox"]),
            })

    return {
        "file": file, "document": document, "sections": sections,
        "truncated": truncated,
    }


@app.get("/files/{file_id}/content", dependencies=[Depends(require_token)])
def file_content(
    file_id: int,
    offset: int = Query(0, ge=0, le=1_000_000),
    limit: int = Query(40, ge=1, le=200),
) -> dict:
    """文件查看器：按顺序分页返回全部子层分片，不截断正文。

    详情接口（/detail）只给前 24 段做速览；查看器走这里，
    前端滚到底自动取下一页，直到读完整份文件。
    """
    conn = db()
    row = conn.execute(
        f"""SELECT f.id, f.content_id, c.active_index_version AS ver
            FROM files f
            LEFT JOIN sources s ON s.id = f.source_id
            LEFT JOIN contents c ON c.id = f.content_id
            WHERE f.id = ? AND {VISIBLE_FILES_COND}""",
        (file_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if row["content_id"] is None:
        return {"file_id": file_id, "total": 0, "sections": [],
                "offset": offset, "has_more": False}

    total = conn.execute(
        "SELECT count(*) c FROM chunks "
        "WHERE content_id = ? AND index_version = ? AND layer = 'child'",
        (row["content_id"], row["ver"]),
    ).fetchone()["c"]
    rows = conn.execute(
        """SELECT ch.id, ch.section_path, ch.page, ch.page_end, ch.ordinal, ch.text
           FROM chunks ch
           WHERE ch.content_id = ? AND ch.index_version = ? AND ch.layer = 'child'
           ORDER BY ch.ordinal, ch.id LIMIT ? OFFSET ?""",
        (row["content_id"], row["ver"], limit, offset),
    ).fetchall()
    return {
        "file_id": file_id,
        "total": total,
        "sections": [dict(r) for r in rows],
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


@app.get("/files/{file_id}/raw", dependencies=[Depends(require_token)])
def file_raw(file_id: int):
    """原始文件字节 —— 供桌面端自定义协议（ordodoc://）流式转发给原文查看器。

    授权模型：路径只来自库内登记（file_id → path），**不接受任何用户提供的
    路径**，没有目录遍历面；未登记或文件已不在磁盘上时 404。
    Range 请求由 FileResponse 处理（大 PDF 懒加载不整读内存）。
    """
    conn = db()
    row = conn.execute(
        f"""SELECT f.path, f.preserved_path
            FROM files f LEFT JOIN sources s ON s.id = f.source_id
            WHERE f.id = ? AND {VISIBLE_FILES_COND}""",
        (file_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    source_path = Path(str(row["path"]))
    preserved = Path(str(row["preserved_path"])) if row["preserved_path"] else None
    path = source_path if source_path.is_file() else preserved
    if path is None:
        raise HTTPException(status_code=404, detail="文件已不在磁盘上")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件已不在磁盘上")
    media_type, _ = mimetypes.guess_type(str(path))
    return FileResponse(
        path, media_type=media_type or "application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


class SearchRequest(BaseModel):
    q: str = Field(max_length=_MAX_QUERY_CHARS)
    limit: int = Field(default=40, ge=1, le=100)
    book_id: int | None = None


@app.post("/search", dependencies=[Depends(require_token)])
def search_content(req: SearchRequest) -> dict:
    """全文检索 —— 搜内容而非文件名（PLAN §12.3）。

    四路召回后 RRF 融合（§12.3b ④）：jieba / trigram / 子串兜底 / 向量。
    向量路不可用时自动省略，其余三路照常 —— 语义检索是增强而非依赖。

    结果按文件聚合：用户要看的是"哪些文件里有"，
    而不是一堆散落的分片。
    """
    conn = db()
    route_limit = 200 if req.book_id is not None else req.limit * 3
    retrieval = run_retrieval(
        conn, req.q, route_limit=route_limit,
        candidate_limit=req.limit * 3, book_id=req.book_id,
    )
    routes = retrieval.routes
    fused = {candidate.chunk_id: candidate.final_score
             for candidate in retrieval.candidates}

    # 向量路的最高余弦是唯一有绝对含义的信号，用于置信度判定（§12.3c）
    top_cosine = routes["vector"][0][1] if routes.get("vector") else 0.0
    conf = assess_confidence(conn, req.q, top_cosine)

    if not fused:
        trace = retrieval.trace.to_dict()
        _remember_trace(trace)
        return {"query": req.q, "total": 0, "files": [],
                "confidence": conf.level, "hedge": conf.hedge,
                "trace_id": trace["trace_id"], "timings": trace["stages"],
                "degraded": trace["degraded"], "trace": trace}

    top = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[: req.limit * 3]
    ids = [cid for cid, _ in top]
    marks = ",".join("?" * len(ids))
    book_file_filter = ""
    row_params: list[object] = list(ids)
    if req.book_id is not None:
        # content 可能被书内、书外多个 file 共享。这里必须限定具体 file_id，
        # 不能只限定 content_id，否则同一 chunk 会再次展开成书外副本。
        book_file_filter = (
            " AND f.id IN (SELECT file_id FROM book_members WHERE book_id = ?)"
        )
        row_params.append(req.book_id)
    rows = conn.execute(
        f"""SELECT ch.id, ch.content_id, ch.section_id, ch.text,
                   ch.section_path, ch.page, ch.ordinal, ch.start_offset,
                   ch.end_offset,
                   f.id AS file_id, f.name, f.path, f.ext, f.mtime,
                   s.name AS source_name, s.volatile
            FROM chunks ch
            JOIN files f ON f.content_id = ch.content_id
            LEFT JOIN sources s ON f.source_id = s.id
            WHERE ch.id IN ({marks}){book_file_filter}
              AND {VISIBLE_FILES_COND}""",
        row_params,
    ).fetchall()

    # 按文件聚合，每个文件保留最相关的几个片段
    by_file: dict[int, dict] = {}
    for r in rows:
        score = fused.get(r["id"], 0)
        span = best_span(req.q, EvidenceSource(
            chunk_id=r["id"], content_id=r["content_id"],
            section_id=r["section_id"], file_id=r["file_id"],
            file_name=r["name"], file_path=r["path"], page=r["page"],
            section_path=r["section_path"] or "", ordinal=r["ordinal"],
            text=r["text"], document_start_offset=r["start_offset"],
            document_end_offset=r["end_offset"], candidate_score=score,
        ))
        entry = by_file.setdefault(
            r["file_id"],
            {
                "file_id": r["file_id"], "name": r["name"], "path": r["path"],
                "ext": r["ext"], "mtime": r["mtime"],
                "source_name": r["source_name"], "volatile": bool(r["volatile"]),
                "score": 0.0, "snippets": [],
            },
        )
        entry["score"] = max(entry["score"], score)
        if len(entry["snippets"]) < 3:
            entry["snippets"].append({
                "chunk_id": r["id"],
                "span_id": span.span_id,
                "text": span.text,
                "section_path": r["section_path"],
                "page": r["page"],
                "start_offset": span.start_offset,
                "end_offset": span.end_offset,
                "document_start_offset": span.document_start_offset,
                "document_end_offset": span.document_end_offset,
                "score": score,
            })

    files = sorted(by_file.values(), key=lambda e: e["score"], reverse=True)[: req.limit]
    for f in files:
        f["snippets"].sort(key=lambda s: s["score"], reverse=True)

    trace = retrieval.trace.to_dict()
    _remember_trace(trace)
    return {
        "query": req.q,
        "total": len(files),
        "files": files,
        # 置信度与提示语：低置信时前端显示一行说明，但**不阻断结果**
        # （§12.3c —— 硬拒答会误伤真实问题，代价比多给结果高）
        "confidence": conf.level,
        "hedge": conf.hedge,
        "routes": {k: len(v) for k, v in routes.items() if v},
        "trace_id": trace["trace_id"], "timings": trace["stages"],
        "degraded": trace["degraded"], "trace": trace,
    }


class IndexRequest(BaseModel):
    # Bounded: a negative value reached SQLite as `LIMIT -N`, which SQLite
    # treats as unlimited, so one request would index the whole pending queue
    # in a single batch while holding the write lock.
    limit: int = Field(default=500, ge=1, le=_MAX_BATCH_LIMIT)


class IndexVersionRequest(BaseModel):
    content_id: int = Field(ge=1)
    version: int = Field(ge=1)


@app.post("/index/run", dependencies=[Depends(require_token)])
def run_index(req: IndexRequest) -> dict:
    """解析并索引待处理内容（PLAN §12.2）。

    循环直到没有 pending —— 单次调用就把队列清空，避免前端需要
    反复轮询调用。每批之间 commit，中断也不丢已完成的部分。
    """
    total = {"indexed": 0, "chunks": 0, "no_text": 0, "failed": 0,
             "unsupported": 0, "total": 0}
    # 分批进出锁：嵌入是分钟级慢操作，整个循环霸占写锁会饿死补扫与
    # 实时入库（实测整盘首扫时 reconcile 线程等锁 20 分钟毫无进展）。
    # 每批之间释放锁，扫描/监听线程得以插队。
    while True:
        with _db_lock:
            conn = db()
            pending_before = count_readable_pending(conn)
            # 批量路径不做内联嵌入：先让文件可见可搜（FTS），
            # 向量由前端驱动的 embed_backfill 分批补齐
            r = index_pending(conn, limit=min(req.limit, 24), embed=False)
            # ``unsupported`` 可以被 index_pending 反复选中（例如扩展名在
            # 白名单内但解析器拒绝了文件）。没有这个护栏，单次请求会把
            # 同一个文档重复尝试直到耗尽 req.limit，看起来像 sidecar 卡死。
            pending_after = count_readable_pending(conn)
        # threading.Lock 没有公平性：紧循环"释放→立刻再抢"会让等锁的
        # 补扫/入库线程永远抢不到（实测扫描线程被饿 20 分钟）。批间让出
        # 一个保证窗口，等待者必然获得锁。
        time.sleep(0.1)
        if r["total"] == 0:
            break
        for k in total:
            total[k] += r.get(k, 0)
        if pending_after >= pending_before:
            break
        if total["total"] >= req.limit:
            break
    return total


class QaSettingRequest(BaseModel):
    answer_max_tokens: str  # "auto" 或数字字符串


@app.get("/settings/qa", dependencies=[Depends(require_token)])
def get_qa_setting() -> dict:
    from app.db.database import get_setting

    conn = db()
    return {"answer_max_tokens": get_setting(conn, "answer_max_tokens", "auto")}


@app.post("/settings/qa", dependencies=[Depends(require_token)])
def set_qa_setting(req: QaSettingRequest) -> dict:
    from app.db.database import set_setting

    value = req.answer_max_tokens.strip()
    if value != "auto":
        try:
            n = int(value)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="回答长度必须是 auto 或数字") from e
        if not (256 <= n <= 1_000_000):
            raise HTTPException(status_code=400, detail="回答长度超出合理范围")
        value = str(n)
    with _db_lock:
        conn = db()
        set_setting(conn, "answer_max_tokens", value)
    return {"answer_max_tokens": value}


class OcrSettingRequest(BaseModel):
    enabled: bool


@app.get("/settings/ocr", dependencies=[Depends(require_token)])
def get_ocr_setting() -> dict:
    from app.db.database import get_setting
    from app.parsing import ocr

    conn = db()
    return {
        "enabled": get_setting(conn, "ocr_enabled", "1") == "1",
        "available": ocr.is_available(),
        "engine": ocr.engine_id(),
        "engine_label": ocr.engine_label(),
    }


@app.post("/settings/ocr", dependencies=[Depends(require_token)])
def set_ocr_setting(req: OcrSettingRequest) -> dict:
    from app.db.database import set_setting
    from app.parsing import ocr

    with _db_lock:
        conn = db()
        set_setting(conn, "ocr_enabled", "1" if req.enabled else "0")
    return {
        "enabled": req.enabled,
        "available": ocr.is_available(),
        "engine": ocr.engine_id(),
        "engine_label": ocr.engine_label(),
    }


@app.post("/index/retry_scanned", dependencies=[Depends(require_token)])
def retry_scanned() -> dict:
    """Requeue recoverable parse/index failures and scanned PDFs.

    Kept under the historical route name for desktop compatibility. The retry
    now covers ``parse_failed``, ``index_failed``, ``missing_file`` and files
    that were formerly too large but are now below the parser limit, in
    addition to no-text PDFs after OCR is enabled.
    """
    with _db_lock:
        conn = db()
        result = requeue_retryable_contents(conn, include_no_text_pdf=True)
        conn.commit()

    # Hash failures live on `files`, not `contents`. Re-run registration in
    # bounded single-file lock sections: this both retries the filesystem read
    # and prevents hashing hundreds of files while monopolizing the writer.
    failed_rows = db().execute(
        """SELECT id, path, source_id FROM files
           WHERE state = 'failed' AND error_code = 'hash_failed'
           ORDER BY id LIMIT ?""",
        (_MAX_BATCH_LIMIT,),
    ).fetchall()
    hash_recovered = 0
    hash_failed = 0
    for row in failed_rows:
        with _db_lock:
            conn = db()
            stats = ScanStats()
            recovered_id = register_file(
                conn, Path(row["path"]), row["source_id"], stats,
            )
            conn.commit()
        if recovered_id is not None and not stats.errors:
            hash_recovered += 1
        else:
            hash_failed += 1
    return {
        **result,
        "hash_retried": len(failed_rows),
        "hash_recovered": hash_recovered,
        "hash_failed": hash_failed,
    }


class JournalDeleteRequest(BaseModel):
    journal_id: int | None = None
    clear: bool = False


@app.get("/journal", dependencies=[Depends(require_token)])
def get_journal(q: str = "", limit: int = 20) -> dict:
    """找回过往调查。

    这是**导航**接口：返回的 content_ids 指向真实资料，让用户跳回原文。
    答案文本是模型上次的输出，界面必须标明这一点，不能让它看起来像原文。
    """
    from app.qa import journal
    with _read_lock():
        conn = db()
        items = journal.search(conn, q, limit=limit)
        total = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    return {"items": items, "total": int(total), "query": q}


@app.get("/journal/related", dependencies=[Depends(require_token)])
def get_journal_related(question: str, limit: int = 3) -> dict:
    """新问题的「你之前问过」提示（保守判据，见 journal.related）。"""
    from app.qa import journal
    with _read_lock():
        items = journal.related(db(), question, limit=limit)
    return {"items": items}


@app.post("/journal/remove", dependencies=[Depends(require_token)])
def post_journal_remove(req: JournalDeleteRequest) -> dict:
    """删一条或清空。日志是用户自己的记录，必须能删。"""
    from app.qa import journal
    conn = db()
    with conn:
        if req.clear:
            return {"removed": journal.clear(conn)}
        if req.journal_id is None:
            raise HTTPException(status_code=400, detail="缺少 journal_id")
        return {"removed": 1 if journal.delete(conn, req.journal_id) else 0}


@app.get("/reports/weekly", dependencies=[Depends(require_token)])
def get_weekly_report(force: bool = False) -> dict:
    """本周知识库摘要报告（幂等：同周复用，force=true 重新生成）。"""
    from app.qa.report import weekly_report

    with _read_lock():
        conn = db()
        return weekly_report(conn, force=force)


@app.get("/integrations/ccswitch", dependencies=[Depends(require_token)])
def ccswitch_providers() -> dict:
    """读取 cc-switch 的供应商配置（只读），供模型设置一键导入。"""
    from app.integrations.ccswitch import read_providers

    return read_providers()


class RebasePreservedRequest(BaseModel):
    old_prefix: str = Field(min_length=1, max_length=_MAX_PATH_CHARS)
    new_prefix: str = Field(min_length=1, max_length=_MAX_PATH_CHARS)


@app.post("/system/rebase_preserved", dependencies=[Depends(require_token)])
def rebase_preserved(req: RebasePreservedRequest) -> dict:
    """数据目录迁移后，把库里记录的保全副本绝对路径改到新前缀。"""
    old = req.old_prefix.rstrip("\\/")
    new = req.new_prefix.rstrip("\\/")
    if not old or old == new:
        return {"preserved_updated": 0, "paths_updated": 0}

    def rebased(value: str | None) -> str | None:
        if not value:
            return None
        compare_value = os.path.normcase(value)
        compare_old = os.path.normcase(old)
        if compare_value != compare_old:
            if not compare_value.startswith(compare_old):
                return None
            boundary = value[len(old):len(old) + 1]
            if boundary not in {"/", "\\"}:
                return None
        suffix = value[len(old):].lstrip("\\/")
        if not suffix:
            return new
        # 迁移路径可能来自另一平台；沿用请求中新前缀的分隔符风格。
        separator = "\\" if "\\" in new and "/" not in new else "/"
        return new + separator + suffix

    with _db_lock:
        conn = db()
        rows = conn.execute("SELECT id, path, preserved_path FROM files").fetchall()
        preserved_updates = [
            (updated, row["id"]) for row in rows
            if (updated := rebased(row["preserved_path"])) is not None
            and updated != row["preserved_path"]
        ]
        path_updates = [
            (updated, row["id"]) for row in rows
            if (updated := rebased(row["path"])) is not None
            and updated != row["path"]
        ]
        conn.executemany(
            "UPDATE files SET preserved_path = ? WHERE id = ?", preserved_updates,
        )
        conn.executemany("UPDATE files SET path = ? WHERE id = ?", path_updates)
        conn.commit()
    return {
        "preserved_updated": len(preserved_updates),
        "paths_updated": len(path_updates),
    }


class EmbedBackfillRequest(BaseModel):
    limit: int = Field(default=500, ge=1, le=_MAX_BATCH_LIMIT)


@app.post("/index/embed_backfill", dependencies=[Depends(require_token)])
def post_embed_backfill(req: EmbedBackfillRequest) -> dict:
    """给存量分片补语义向量（模型晚于内容入库时）。

    幂等分批：反复调用直到 remaining=0。模型或向量表不可用时
    返回 available=false，调用方静默跳过即可。
    """
    from app.index.pipeline import embed_backfill

    with _db_lock:
        conn = db()
        try:
            result = embed_backfill(conn, limit=max(1, min(req.limit, 2000)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return result


@app.get("/index/versions", dependencies=[Depends(require_token)])
def index_versions(content_id: int = Query(ge=1)) -> dict:
    conn = db()
    content = conn.execute(
        "SELECT active_index_version FROM contents WHERE id = ?", (content_id,),
    ).fetchone()
    if content is None:
        raise HTTPException(status_code=404, detail="内容不存在")
    rows = conn.execute(
        """SELECT version, status, document_hash, section_count, chunk_count,
                  error, created_at, activated_at
           FROM index_versions WHERE content_id = ? ORDER BY version DESC""",
        (content_id,),
    ).fetchall()
    return {
        "content_id": content_id,
        "active_version": content["active_index_version"],
        "versions": [dict(row) for row in rows],
    }


@app.post("/index/activate", dependencies=[Depends(require_token)])
def activate_version(req: IndexVersionRequest) -> dict:
    with _db_lock:
        conn = db()
        try:
            result = activate_index_version(conn, req.content_id, req.version)
            conn.commit()
        except ValueError as exc:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            conn.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


@app.post("/watch/start", dependencies=[Depends(require_token)])
def watch_start() -> dict:
    """挂上所有已启用来源的实时监听（PLAN §11.1）。

    幂等 —— 重复调用不会重复挂载。前端启动时无条件调一次即可。
    """
    return watch_service().start()


@app.post("/watch/stop", dependencies=[Depends(require_token)])
def watch_stop() -> dict:
    watch_service().stop()
    return {"stopped": True}


@app.get("/watch/status", dependencies=[Depends(require_token)])
def watch_status() -> dict:
    """监听状态、后台来源扫描进度与最近自动入库文件。"""
    status = watch_service().status
    status["source_scans"] = watch_service().scan_jobs()
    return status


@app.get("/db/status", dependencies=[Depends(require_token)])
def database_status() -> dict:
    """数据库启动检查与最近一次维护动作的可见状态。"""
    db()  # 首次请求也必须先完成 quick_check / 每日备份
    return _status_snapshot()


@app.post("/db/integrity_check", dependencies=[Depends(require_token)])
def database_integrity_check() -> dict:
    """手动执行完整 ``PRAGMA integrity_check``（PLAN §9.2）。"""
    with _db_lock:
        results = integrity_check(db())
        ok = results == ["ok"]
        _database_status["integrity"] = {
            "ok": ok, "results": results, "checked_at": time.time(),
        }
    if not ok:
        log.error("数据库完整性检查未通过：%s", "; ".join(results[:10]))
    return {"ok": ok, "results": results}


@app.post("/db/backup", dependencies=[Depends(require_token)])
def database_backup() -> dict:
    """手动确保当天的一致性快照存在；不会覆盖或自动恢复主库。"""
    with _db_lock:
        try:
            path = create_daily_backup(db())
        except (BackupError, OSError) as exc:
            _database_status["backup"] = {
                "ok": False, "path": None, "error": str(exc),
                "checked_at": time.time(), "skipped": False,
            }
            log.error("手动数据库备份失败：%s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        _database_status["backup"] = {
            "ok": True, "path": str(path), "error": "",
            "checked_at": time.time(), "skipped": False,
        }
    return {"ok": True, "path": str(path)}


@app.get("/index/status", dependencies=[Depends(require_token)])
def index_status() -> dict:
    conn = db()
    rows = conn.execute(
        "SELECT parse_state, count(*) c FROM contents GROUP BY parse_state"
    ).fetchall()
    by_state = {r["parse_state"]: r["c"] for r in rows}
    chunks = conn.execute(
        """SELECT count(*) c FROM chunks ch JOIN contents c ON c.id = ch.content_id
           WHERE ch.index_version = c.active_index_version"""
    ).fetchone()["c"]
    sections = conn.execute(
        """SELECT count(*) c FROM sections s JOIN contents c ON c.id = s.content_id
           WHERE s.index_version = c.active_index_version"""
    ).fetchone()["c"]

    # pending 只算**真正会被解析的** —— 源码/媒体虽然也是 pending 状态，
    # 但永远不会进解析流水线。把它们算进去会让界面显示一个永降不到 0
    # 的数字，用户以为卡住了。
    real_pending = count_readable_pending(conn)

    return {
        "by_state": by_state,
        "pending": real_pending,
        "pending_raw": by_state.get("pending", 0),
        "indexed": by_state.get("indexed", 0),
        "chunks": chunks,
        "sections": sections,
        "index_schema_version": 2,
    }


@app.get("/stats", dependencies=[Depends(require_token)])
def stats() -> dict:
    """浏览视图的统计口径 = 可见文件（见 VISIBLE_FILES_COND）：
    停用来源的文件保留在库里，但不再计入左栏与列表。"""
    conn = db()
    visible_from = (
        "FROM files f LEFT JOIN sources s ON f.source_id = s.id "
        f"WHERE {VISIBLE_FILES_COND}"
    )
    files = conn.execute(f"SELECT count(*) c {visible_from}").fetchone()["c"]
    contents = conn.execute("SELECT count(*) c FROM contents").fetchone()["c"]
    dup = conn.execute(
        f"SELECT count(*) c, count(DISTINCT f.content_id) d {visible_from} "
        "AND f.content_id IS NOT NULL"
    ).fetchone()
    by_ext = conn.execute(
        f"SELECT f.ext, count(*) c {visible_from} "
        "GROUP BY f.ext ORDER BY c DESC LIMIT 10"
    ).fetchall()
    by_source = conn.execute(
        "SELECT s.name, count(*) c FROM files f JOIN sources s ON f.source_id = s.id "
        "WHERE s.enabled = 1 GROUP BY s.id ORDER BY c DESC"
    ).fetchall()
    now = time.time()
    recent = conn.execute(
        f"SELECT sum(CASE WHEN f.mtime >= ? THEN 1 ELSE 0 END) week, "
        f"sum(CASE WHEN f.mtime >= ? THEN 1 ELSE 0 END) month {visible_from}",
        (now - 7 * 86400, now - 30 * 86400),
    ).fetchone()
    return {
        "files": files,
        "contents": contents,
        "deduped": dup["c"] - dup["d"],
        "recent7": recent["week"] or 0,
        "recent30": recent["month"] or 0,
        "by_ext": [dict(r) for r in by_ext],
        "by_source": [dict(r) for r in by_source],
        "database": _status_snapshot(),
    }


def _read_stdin_secrets() -> None:
    """主进程通过 stdin 传入 {"token": "...", "llm": {...}}。"""
    global SESSION_TOKEN
    try:
        line = sys.stdin.readline()
        if not line:
            return
        payload = json.loads(line)
        if tok := payload.get("token"):
            SESSION_TOKEN = tok
        if cfg := payload.get("llm"):
            from app.config import models as _slots
            from app.qa import llm as _llm

            if "qa" in cfg or "endpoint" in cfg:
                # v2：{"qa": {...}, "library": {...}}；旧版平铺 {endpoint,...} 视为 qa
                qa = cfg.get("qa") if isinstance(cfg.get("qa"), dict) else (
                    cfg if "endpoint" in cfg else None)
                if qa:
                    _llm.configure(str(qa.get("endpoint") or ""),
                                   str(qa.get("api_key") or ""),
                                   str(qa.get("model") or ""),
                                   str(qa.get("provider") or "openai"))
            for slot in _slots.SLOTS:
                slot_cfg = cfg.get(slot)
                if not isinstance(slot_cfg, dict):
                    continue
                try:
                    _slots.configure(slot, str(slot_cfg.get("provider") or "ollama"),
                                     str(slot_cfg.get("endpoint") or ""),
                                     str(slot_cfg.get("api_key") or ""),
                                     str(slot_cfg.get("model") or ""))
                except _slots.SlotConfigError as exc:
                    logging.getLogger("ordo.main").warning(
                        "启动推送的 %s 槽位配置不合法，已忽略：%s", slot, exc)
    except Exception:
        pass  # 开发态无 stdin 输入，使用自生成令牌


def main() -> None:
    acquire_single_instance_lock()
    try:
        # Open and validate the database before reporting a listening port.  A
        # corrupt or newer database must fail closed instead of producing a UI
        # that appears ready and errors only on its first request.
        db()

        if not sys.stdin.isatty():
            threading.Thread(target=_read_stdin_secrets, daemon=True).start()

        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        server = uvicorn.Server(config)

        # 端口由内核分配，启动后经 stdout 回传主进程
        original_startup = server.startup

        async def startup_with_port_report(sockets=None):
            await original_startup(sockets=sockets)
            for srv in server.servers:
                for sock in srv.sockets:
                    port = sock.getsockname()[1]
                    print(json.dumps({"port": port, "token": SESSION_TOKEN}), flush=True)
                    return

        server.startup = startup_with_port_report  # type: ignore[method-assign]
        server.run()
    finally:
        release_single_instance_lock()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
