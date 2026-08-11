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

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.health import collect_health

app = FastAPI(title="Inktable API", version="0.1.0")

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
