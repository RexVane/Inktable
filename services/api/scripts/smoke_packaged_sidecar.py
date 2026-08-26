"""打包后 sidecar 的 headless 冒烟。

「打包成功」与「装上能用」是两件事：`build-sidecar.js` 的注释就写着,
少了 `services/api/dist` 也能打出一个永远起不来的包。所以发布物必须实测 ——
用隔离数据目录起打包出来的 exe,读它自报的端口与令牌,打 /health。

隔离是必需的:sidecar 启动就 `acquire_single_instance_lock()`,不隔离会与
用户正在跑的应用抢锁而直接失败,看起来像「打包坏了」。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

EXE = sys.argv[1] if len(sys.argv) > 1 else (
    "D:/AIApp/Inktable/Inktable/dist/win-unpacked/resources/sidecar/"
    "inktable-sidecar.exe")


def main() -> int:
    if not os.path.exists(EXE):
        print("找不到打包出的 sidecar: %s" % EXE)
        return 1
    data_dir = tempfile.mkdtemp(prefix="inktable-smoke-")
    env = dict(os.environ, INKTABLE_DATA_DIR=data_dir)
    env.pop("INKTABLE_DB", None)
    proc = subprocess.Popen(
        [EXE], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
    )
    try:
        line = proc.stdout.readline().strip() if proc.stdout else ""
        if not line:
            print("sidecar 没有回报端口，stderr:")
            print((proc.stderr.read() if proc.stderr else "")[:2000])
            return 1
        info = json.loads(line)
        print("sidecar 启动: port=%s" % info["port"])

        req = urllib.request.Request(
            "http://127.0.0.1:%d/health" % info["port"],
            headers={"Authorization": "Bearer %s" % info["token"]},
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=120) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        print("/health %.1fs" % (time.time() - t0))
        return report(health)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def report(health: dict) -> int:
    bad = []
    for key, value in sorted(health.items()):
        if isinstance(value, dict) and "ok" in value:
            mark = "ok " if value["ok"] else "!! "
            extra = {k: v for k, v in value.items() if k != "ok"}
            print("  %s%-18s %s" % (mark, key, json.dumps(extra, ensure_ascii=False)[:120]))
            if not value["ok"]:
                bad.append(key)
        else:
            print("  -- %-18s %s" % (key, json.dumps(value, ensure_ascii=False)[:120]))
    print("\n失败项: %s" % (bad or "无"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
