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
import time

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.db.database import connect, init_db, quick_check
from app.discovery.sources import discover_all
from app.health import collect_health
from app.watcher.scanner import preview_source, scan_source

app = FastAPI(title="Inktable API", version="0.1.0")

_db = None
_db_lock = threading.Lock()


def db():
    """单写入线程串行化（PLAN §19 R9）。"""
    global _db
    if _db is None:
        _db = connect()
        init_db(_db)
        if not quick_check(_db):
            raise RuntimeError("数据库完整性检查失败")
    return _db

# 令牌优先从 stdin 读取；缺省时自生成（开发态）
SESSION_TOKEN = os.environ.pop("INKTABLE_TOKEN", None) or secrets.token_urlsafe(32)

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
