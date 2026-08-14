"""Inktable sidecar — FastAPI 本地服务。

安全约束（PLAN §6.2 / §6.3）：
- 绑定 127.0.0.1 + 端口 0（内核分配），端口经 stdout 回传主进程
- 会话令牌与 API 密钥经 stdin 传入，**不走命令行参数**（ps 对本机任意进程可见）
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import secrets
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query
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
from app.discovery.sources import discover_all
from app.health import collect_health
from app.index.confidence import assess as assess_confidence
from app.index.pipeline import (
    activate_index_version,
    count_readable_pending,
    index_pending,
)
from app.retrieval.compress import EvidenceSource, best_span
from app.retrieval.pipeline import run as run_retrieval
from app.watcher.scanner import preview_source, scan_source
from app.watcher.service import WatchService

app = FastAPI(title="Inktable API", version="0.3.0")
log = logging.getLogger("inktable.main")

_db = None
_db_lock = threading.Lock()
_db_init_lock = threading.Lock()
_watch: WatchService | None = None

# 最近检索 trace 的内存环（PLAN §8.1 /runs/{trace_id}）。
# 只存内存不落盘：trace 含 id/score/timing，不含正文与查询原文，
# 但仍按隐私风险登记的口径处理 —— 进程退出即消失。
_TRACE_CACHE: OrderedDict[str, dict] = OrderedDict()
_TRACE_CACHE_CAP = 50
_trace_lock = threading.Lock()


def _remember_trace(trace: dict) -> None:
    trace_id = trace.get("trace_id")
    if not trace_id:
        return
    with _trace_lock:
        _TRACE_CACHE[trace_id] = trace
        _TRACE_CACHE.move_to_end(trace_id)
        while len(_TRACE_CACHE) > _TRACE_CACHE_CAP:
            _TRACE_CACHE.popitem(last=False)


def _empty_database_status() -> dict:
    return {
        "quick_check": {"ok": None, "checked_at": None},
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


def db():
    """单写入线程串行化（PLAN §19 R9）。"""
    global _db, _database_status
    if _db is not None:
        return _db

    # 生产入口会在启动 HTTP 服务前先调用 db()，这里的锁仍然保留：测试、
    # 嵌入式调用或未来改为 lifespan 后，多个首请求也不能各自创建一条连接。
    with _db_init_lock:
        if _db is not None:
            return _db

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
            if os.environ.get("INKTABLE_DB") == ":memory:":
                _database_status["backup"] = {
                    "ok": None, "path": None, "error": "",
                    "checked_at": time.time(), "skipped": True,
                }
            else:
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
        except Exception:
            conn.close()
            raise
        _db = conn
    return _db


def watch_service() -> WatchService:
    global _watch
    if _watch is None:
        _watch = WatchService(db, _db_lock)
    return _watch


@app.on_event("shutdown")
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
SESSION_TOKEN = os.environ.get("INKTABLE_TOKEN") or secrets.token_urlsafe(32)

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
    return {"app": "inktable", "authenticated": True}


@app.post("/sources/discover", dependencies=[Depends(require_token)])
def discover() -> dict:
    """触发来源自动发现，返回候选列表（PLAN §7.4）。

    只探测不启用 —— 启用需要用户逐个确认（§1 约束 4）。
    """
    sources = discover_all()
    return {"sources": [s.to_dict() for s in sources]}


class PreviewRequest(BaseModel):
    path: str


@app.post("/sources/preview", dependencies=[Depends(require_token)])
def preview(req: PreviewRequest) -> dict:
    """启用前预扫描：只计数不入库（PLAN §7.7 ⑤）。

    把「这个目录有 48 万个文件」变成用户可见的决策点，
    而不是点下去之后才发现库被淹了。
    """
    return preview_source(req.path)


class EnableRequest(BaseModel):
    name: str
    path: str
    kind: str = "manual"
    discovered_by: str = "manual"
    volatile: bool = False


@app.post("/sources/enable", dependencies=[Depends(require_token)])
def enable_source(req: EnableRequest) -> dict:
    """用户确认启用一个来源，并立即扫描。"""
    with _db_lock:
        conn = db()
        conn.execute(
            "INSERT INTO sources (name, path, kind, discovered_by, volatile, enabled, "
            "permission_ok, permission_checked_at, created_at) "
            "VALUES (?,?,?,?,?,1,1,?,?) "
            "ON CONFLICT(path) DO UPDATE SET enabled=1, name=excluded.name",
            (req.name, req.path, req.kind, req.discovered_by, int(req.volatile),
             time.time(), time.time()),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM sources WHERE path = ?", (req.path,)).fetchone()
        stats = scan_source(conn, row["id"], req.path)

    # 启用后立即挂上实时监听 —— 之后进来的新文件自动入库
    watch_service().watch(req.path)

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
    watched = set(watch_service().status["watched"])
    rows = conn.execute(
        """SELECT s.id, s.name, s.path, s.kind, s.volatile, s.enabled,
                  s.auto_preserve, s.permission_ok, s.created_at,
                  (SELECT count(*) FROM files f WHERE f.source_id = s.id) AS file_count
           FROM sources s ORDER BY s.enabled DESC, file_count DESC"""
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["volatile"] = bool(r["volatile"])
        d["enabled"] = bool(r["enabled"])
        d["auto_preserve"] = bool(r["auto_preserve"])
        d["watching"] = r["path"] in watched
        d["exists"] = Path(r["path"]).is_dir()
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
    file_ids: list[int]


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
    endpoint: str = ""
    api_key: str = ""
    model: str = ""


@app.post("/settings/llm", dependencies=[Depends(require_token)])
def set_llm(req: LLMConfigRequest) -> dict:
    """配置模型服务。密钥**只进内存**（§6.3）——
    持久化由 Electron 主进程用 safeStorage 加密保存，每次启动重新推送。
    全空 = 清除配置，回到纯本地模式。
    """
    from app.qa import llm

    llm.configure(req.endpoint, req.api_key, req.model)
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


class AskRequest(BaseModel):
    question: str
    book_id: int | None = None
    # 最近几轮问答（[{q, a}]），用于把追问浓缩成可独立检索的问题
    history: list[dict] = []


@app.post("/ask", dependencies=[Depends(require_token)])
def post_ask(req: AskRequest) -> dict:
    """带引用问答（§12.4）。非流式 —— 后置校验会改写答案，流式收不回。"""
    from app.qa.answer import ask
    from app.qa.llm import LLMError

    q = req.question.strip()
    if not q:
        raise HTTPException(status_code=400, detail="问题为空")
    with _db_lock:
        conn = db()
        try:
            a = ask(conn, q, req.book_id, history=req.history[-4:])
        except LLMError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
    trace = a.trace
    _remember_trace(trace)
    return {
        "status": a.status, "answer": a.answer, "citations": a.citations,
        "retrieved": a.retrieved, "hedge": a.hedge, "validation": a.validation,
        "mode": a.mode,
        "trace_id": trace.get("trace_id"), "timings": trace.get("stages", []),
        "degraded": trace.get("degraded", []), "trace": trace,
    }


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
    from app.qa.classify_llm import llm_classify_unclassified
    from app.qa.llm import LLMError

    with _db_lock:
        conn = db()
        try:
            r = llm_classify_unclassified(conn)
            conn.commit()
        except LLMError as e:
            conn.rollback()
            raise HTTPException(status_code=502, detail=str(e)) from e
    return r


# ---------------------------------------------------------------- 文件书（B7）

class BookCreate(BaseModel):
    name: str


class BookMember(BaseModel):
    book_id: int
    file_ids: list[int]


@app.get("/books", dependencies=[Depends(require_token)])
def get_books() -> dict:
    conn = db()
    rows = conn.execute(
        """SELECT b.id, b.name,
                  (SELECT count(*) FROM book_members m WHERE m.book_id = b.id) AS n
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
        n = 0
        for fid in req.file_ids:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO book_members (book_id, file_id, added_at) "
                    "VALUES (?,?,?)", (req.book_id, fid, time.time()),
                )
                n += 1
            except Exception:
                pass
        conn.commit()
    return {"added": n}


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
    name: str
    parent_id: int | None = None


class CategoryRename(BaseModel):
    category_id: int
    name: str


class CategoryId(BaseModel):
    category_id: int


class AssignRequest(BaseModel):
    file_ids: list[int]
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
    path: str
    name: str | None = None


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


# 可见性口径：停用来源的文件**保留在库里但不进入浏览视图**（重新启用即恢复）；
# 来源被移除后残留的孤儿文件（source_id 为 NULL，如保全副本）始终可见。
# 磁盘上已消失的文件同样隐藏 —— 除非有保全副本（内容仍可读，这正是
# 保全功能的价值）。文件回到原位时 scanner 会把 state 恢复，自动重新可见。
VISIBLE_FILES_COND = (
    "(f.source_id IS NULL OR s.enabled = 1) "
    "AND (f.state != 'missing' OR COALESCE(f.preserved_path, '') != '')"
)


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
        # 路径里的 % 和 _ 不需要转义。
        prefix = dir_path.rstrip("/") + "/"
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
        roots = conn.execute(
            """SELECT s.id, s.name, s.path,
                      (SELECT count(*) FROM files f
                       WHERE f.source_id = s.id) AS count
               FROM sources s WHERE s.enabled = 1
               ORDER BY count DESC, s.name"""
        ).fetchall()
        return {"roots": [dict(r) for r in roots]}

    prefix = dir_path.rstrip("/") + "/"
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
        if "/" in rest:
            head = rest.split("/", 1)[0]
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
        """SELECT f.id, f.volume_uuid, f.inode, f.content_id, f.path, f.name,
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
           WHERE f.id = ?""",
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
        """SELECT f.id, f.content_id, c.active_index_version AS ver
           FROM files f LEFT JOIN contents c ON c.id = f.content_id
           WHERE f.id = ?""",
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


class SearchRequest(BaseModel):
    q: str
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
    limit: int = 500


class IndexVersionRequest(BaseModel):
    content_id: int = Field(ge=1)
    version: int = Field(ge=1)


@app.post("/index/run", dependencies=[Depends(require_token)])
def run_index(req: IndexRequest) -> dict:
    """解析并索引待处理内容（PLAN §12.2）。

    循环直到没有 pending —— 单次调用就把队列清空，避免前端需要
    反复轮询调用。每批之间 commit，中断也不丢已完成的部分。
    """
    with _db_lock:
        conn = db()
        total = {"indexed": 0, "chunks": 0, "no_text": 0, "failed": 0,
                 "unsupported": 0, "total": 0}
        while True:
            pending_before = count_readable_pending(conn)
            r = index_pending(conn, limit=min(req.limit, 200))
            if r["total"] == 0:
                break
            for k in total:
                total[k] += r.get(k, 0)
            # ``unsupported`` 可以被 index_pending 反复选中（例如扩展名在
            # 白名单内但解析器拒绝了文件）。没有这个护栏，单次请求会把
            # 同一个文档重复尝试直到耗尽 req.limit，看起来像 sidecar 卡死。
            pending_after = count_readable_pending(conn)
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
    from app.parsing import ocr_mac

    conn = db()
    return {
        "enabled": get_setting(conn, "ocr_enabled", "1") == "1",
        "available": ocr_mac.is_available(),
    }


@app.post("/settings/ocr", dependencies=[Depends(require_token)])
def set_ocr_setting(req: OcrSettingRequest) -> dict:
    from app.db.database import set_setting
    from app.parsing import ocr_mac

    with _db_lock:
        conn = db()
        set_setting(conn, "ocr_enabled", "1" if req.enabled else "0")
    return {"enabled": req.enabled, "available": ocr_mac.is_available()}


@app.post("/index/retry_scanned", dependencies=[Depends(require_token)])
def retry_scanned() -> dict:
    """把无文本（疑似扫描件）的 PDF 重新排队解析 —— 开启 OCR 后补课用。"""
    with _db_lock:
        conn = db()
        rows = conn.execute(
            """SELECT DISTINCT c.id FROM contents c
               JOIN files f ON f.content_id = c.id
               WHERE c.parse_state = 'no_text'
                 AND lower(COALESCE(f.ext, '')) = '.pdf'"""
        ).fetchall()
        if rows:
            marks = ",".join("?" * len(rows))
            conn.execute(
                f"UPDATE contents SET parse_state = 'pending' WHERE id IN ({marks})",
                [r["id"] for r in rows],
            )
            conn.commit()
    return {"requeued": len(rows)}


@app.get("/reports/weekly", dependencies=[Depends(require_token)])
def get_weekly_report(force: bool = False) -> dict:
    """本周知识库摘要报告（幂等：同周复用，force=true 重新生成）。"""
    from app.qa.report import weekly_report

    with _db_lock:
        conn = db()
        return weekly_report(conn, force=force)


@app.get("/integrations/ccswitch", dependencies=[Depends(require_token)])
def ccswitch_providers() -> dict:
    """读取 cc-switch 的供应商配置（只读），供模型设置一键导入。"""
    from app.integrations.ccswitch import read_providers

    return read_providers()


class RebasePreservedRequest(BaseModel):
    old_prefix: str
    new_prefix: str


@app.post("/system/rebase_preserved", dependencies=[Depends(require_token)])
def rebase_preserved(req: RebasePreservedRequest) -> dict:
    """数据目录迁移后，把库里记录的保全副本绝对路径改到新前缀。"""
    old = req.old_prefix.rstrip("/") + "/"
    new = req.new_prefix.rstrip("/") + "/"
    if not req.old_prefix or old == new:
        return {"preserved_updated": 0, "paths_updated": 0}
    with _db_lock:
        conn = db()
        preserved = conn.execute(
            "UPDATE files SET preserved_path = ? || substr(preserved_path, ?) "
            "WHERE substr(preserved_path, 1, ?) = ?",
            (new, len(old) + 1, len(old), old),
        ).rowcount
        paths = conn.execute(
            "UPDATE files SET path = ? || substr(path, ?) "
            "WHERE substr(path, 1, ?) = ?",
            (new, len(old) + 1, len(old), old),
        ).rowcount
        conn.commit()
    return {"preserved_updated": preserved, "paths_updated": paths}


class EmbedBackfillRequest(BaseModel):
    limit: int = 500


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
    """监听状态 + 最近自动入库的文件，供界面「实时动态」展示。"""
    return watch_service().status


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
    """主进程通过 stdin 传入 {"token": "...", "api_key": "..."}。"""
    global SESSION_TOKEN
    try:
        line = sys.stdin.readline()
        if not line:
            return
        payload = json.loads(line)
        if tok := payload.get("token"):
            SESSION_TOKEN = tok
        if cfg := payload.get("llm"):
            from app.qa import llm as _llm

            _llm.configure(cfg.get("endpoint", ""), cfg.get("api_key", ""),
                           cfg.get("model", ""))
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
