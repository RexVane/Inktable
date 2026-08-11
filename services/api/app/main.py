"""Inktable sidecar — FastAPI 本地服务。

安全约束（PLAN §6.2 / §6.3）：
- 绑定 127.0.0.1 + 端口 0（内核分配），端口经 stdout 回传主进程
- 会话令牌与 API 密钥经 stdin 传入，**不走命令行参数**（ps 对本机任意进程可见）
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from pathlib import Path
import time

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.db.database import connect, init_db, quick_check
from app.discovery.sources import discover_all
from app.health import collect_health
from app.index.pipeline import index_pending, indexable_exts
from app.index.confidence import assess as assess_confidence
from app.index.search import search as fts_search
from app.watcher.scanner import preview_source, scan_source
from app.watcher.service import WatchService

app = FastAPI(title="Inktable API", version="0.1.0")

_db = None
_db_lock = threading.Lock()
_watch: WatchService | None = None


def db():
    """单写入线程串行化（PLAN §19 R9）。"""
    global _db
    if _db is None:
        _db = connect()
        init_db(_db)
        if not quick_check(_db):
            raise RuntimeError("数据库完整性检查失败")
    return _db


def watch_service() -> WatchService:
    global _watch
    if _watch is None:
        _watch = WatchService(db, _db_lock)
    return _watch


@app.on_event("shutdown")
def _shutdown() -> None:
    if _watch is not None:
        _watch.stop()

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
                  s.permission_ok, s.created_at,
                  (SELECT count(*) FROM files f WHERE f.source_id = s.id) AS file_count
           FROM sources s ORDER BY s.enabled DESC, file_count DESC"""
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["volatile"] = bool(r["volatile"])
        d["enabled"] = bool(r["enabled"])
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


def _delete_content(conn, content_id: int) -> None:
    """删除内容及其全部分片，FTS5 两索引与向量表一并清。

    FTS5 是 content='' 的外部内容表、chunks_vec 是独立虚拟表 ——
    都不会自动跟随主表删除。漏删任何一个都会让搜索命中已消失的分片，
    点开报错；向量表漏删还会让语义检索永远"记得"已删除的内容。
    """
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM chunks WHERE content_id = ?", (content_id,)
    )]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", ids)
        conn.execute(f"DELETE FROM chunks_fts_tri WHERE rowid IN ({marks})", ids)
        try:
            conn.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({marks})", ids)
        except Exception:
            pass  # 向量表不存在（扩展未加载）时忽略
        conn.execute("DELETE FROM chunks WHERE content_id = ?", (content_id,))
    conn.execute("DELETE FROM contents WHERE id = ?", (content_id,))


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


@app.get("/files", dependencies=[Depends(require_token)])
def list_files(limit: int = 100, offset: int = 0, q: str | None = None) -> dict:
    conn = db()
    where, params = "", []
    if q:
        where = "WHERE f.name LIKE ?"
        params.append(f"%{q}%")

    total = conn.execute(f"SELECT count(*) c FROM files f {where}", params).fetchone()["c"]
    rows = conn.execute(
        f"SELECT f.id, f.name, f.path, f.ext, f.size, f.state, f.mtime, "
        f"s.name AS source_name, s.volatile "
        f"FROM files f LEFT JOIN sources s ON f.source_id = s.id {where} "
        f"ORDER BY f.mtime DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return {"total": total, "files": [dict(r) for r in rows]}


class SearchRequest(BaseModel):
    q: str
    limit: int = 40


@app.post("/search", dependencies=[Depends(require_token)])
def search_content(req: SearchRequest) -> dict:
    """全文检索 —— 搜内容而非文件名（PLAN §12.3）。

    四路召回后 RRF 融合（§12.3b ④）：jieba / trigram / 子串兜底 / 向量。
    向量路不可用时自动省略，其余三路照常 —— 语义检索是增强而非依赖。

    结果按文件聚合：用户要看的是"哪些文件里有"，
    而不是一堆散落的分片。
    """
    conn = db()
    routes = fts_search(conn, req.q, limit=req.limit * 3)

    # RRF 融合（§12.3b ④）：只用排名，免去 BM25 与余弦分数的量纲归一化
    K = 60
    fused: dict[int, float] = {}
    for hits in routes.values():
        for rank, (chunk_id, _score) in enumerate(hits):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (K + rank + 1)

    # 向量路的最高余弦是唯一有绝对含义的信号，用于置信度判定（§12.3c）
    top_cosine = routes["vector"][0][1] if routes.get("vector") else 0.0
    conf = assess_confidence(conn, req.q, top_cosine)

    if not fused:
        return {"query": req.q, "total": 0, "files": [],
                "confidence": conf.level, "hedge": conf.hedge}

    top = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[: req.limit * 3]
    ids = [cid for cid, _ in top]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT ch.id, ch.content_id, ch.text, ch.section_path, ch.page, ch.ordinal,
                   f.id AS file_id, f.name, f.path, f.ext, f.mtime,
                   s.name AS source_name, s.volatile
            FROM chunks ch
            JOIN files f ON f.content_id = ch.content_id
            LEFT JOIN sources s ON f.source_id = s.id
            WHERE ch.id IN ({marks})""",
        ids,
    ).fetchall()

    # 按文件聚合，每个文件保留最相关的几个片段
    by_file: dict[int, dict] = {}
    for r in rows:
        score = fused.get(r["id"], 0)
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
                "text": r["text"][:220],
                "section_path": r["section_path"],
                "page": r["page"],
                "score": score,
            })

    files = sorted(by_file.values(), key=lambda e: e["score"], reverse=True)[: req.limit]
    for f in files:
        f["snippets"].sort(key=lambda s: s["score"], reverse=True)

    return {
        "query": req.q,
        "total": len(files),
        "files": files,
        # 置信度与提示语：低置信时前端显示一行说明，但**不阻断结果**
        # （§12.3c —— 硬拒答会误伤真实问题，代价比多给结果高）
        "confidence": conf.level,
        "hedge": conf.hedge,
        "routes": {k: len(v) for k, v in routes.items() if v},
    }


class IndexRequest(BaseModel):
    limit: int = 500


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
            r = index_pending(conn, limit=min(req.limit, 200))
            if r["total"] == 0:
                break
            for k in total:
                total[k] += r.get(k, 0)
            if total["total"] >= req.limit:
                break
        return total


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


@app.get("/index/status", dependencies=[Depends(require_token)])
def index_status() -> dict:
    conn = db()
    rows = conn.execute(
        "SELECT parse_state, count(*) c FROM contents GROUP BY parse_state"
    ).fetchall()
    by_state = {r["parse_state"]: r["c"] for r in rows}
    chunks = conn.execute("SELECT count(*) c FROM chunks").fetchone()["c"]

    # pending 只算**真正会被解析的** —— 源码/媒体虽然也是 pending 状态，
    # 但永远不会进解析流水线。把它们算进去会让界面显示一个永降不到 0
    # 的数字，用户以为卡住了。
    exts = indexable_exts()
    marks = ",".join("?" * len(exts))
    real_pending = conn.execute(
        f"""SELECT count(DISTINCT c.id) c
            FROM contents c JOIN files f ON f.content_id = c.id
            WHERE c.parse_state = 'pending'
              AND f.state != 'missing'
              AND lower(f.ext) IN ({marks})""",
        list(exts),
    ).fetchone()["c"]

    return {
        "by_state": by_state,
        "pending": real_pending,
        "pending_raw": by_state.get("pending", 0),
        "indexed": by_state.get("indexed", 0),
        "chunks": chunks,
    }


@app.get("/stats", dependencies=[Depends(require_token)])
def stats() -> dict:
    conn = db()
    files = conn.execute("SELECT count(*) c FROM files").fetchone()["c"]
    contents = conn.execute("SELECT count(*) c FROM contents").fetchone()["c"]
    by_ext = conn.execute(
        "SELECT ext, count(*) c FROM files GROUP BY ext ORDER BY c DESC LIMIT 10"
    ).fetchall()
    by_source = conn.execute(
        "SELECT s.name, count(*) c FROM files f JOIN sources s ON f.source_id = s.id "
        "GROUP BY s.id ORDER BY c DESC"
    ).fetchall()
    return {
        "files": files,
        "contents": contents,
        "deduped": files - contents,
        "by_ext": [dict(r) for r in by_ext],
        "by_source": [dict(r) for r in by_source],
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
    except Exception:
        pass  # 开发态无 stdin 输入，使用自生成令牌


def main() -> None:
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


if __name__ == "__main__":
    main()
