"""打包后 sidecar 的 headless 冒烟。

「打包成功」与「装上能用」是两件事：`build-sidecar.js` 的注释就写着,
少了 `services/api/dist` 也能打出一个永远起不来的包。所以发布物必须实测 ——
用隔离数据目录起打包出来的 exe,读它自报的端口与令牌,打 /health。

隔离是必需的:sidecar 启动就 `acquire_single_instance_lock()`,不隔离会与
用户正在跑的应用抢锁而直接失败,看起来像「打包坏了」。

还核对**冻结进去的检索配置**。8/26 那次重出安装包排在门控级联四条改动之前,
于是产物里没有门控、文档却按门控的实测数字写,而「起得来 + /health 全绿」
对这种错配一个字都不会说。所以这里把 /health 的 retrieval_config 与发布配置
（docs/RETRIEVAL-PERF.md §5.9）逐个比,不一致就红。
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

# 发布配置。改这里之前先重跑 65 题（services/api/tests/test_rerank.py 也钉着）。
EXPECTED_RETRIEVAL = {
    "mode": "auto",
    "lex_gate": 0.45,
    "vec_share": 0.25,
    "pairs": 20,
    "max_tokens": 192,
}


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
    """逐项判定 /health。

    必须先下钻到 `checks`：collect_health() 返回的是
    `{status, python, sqlite, frozen, checks: {...}}`，检查项在里面嵌一层。
    早先这里直接遍历顶层，顶层没有任何带 `ok` 的项，于是 `bad` 恒为空 ——
    脚本**不可能因为检查失败而变红**，还会把整个 checks 截到 120 字符打出来。
    这类冒烟比没有冒烟更坏：它发绿灯。
    """
    checks = health.get("checks")
    if not isinstance(checks, dict):
        print("!! /health 里没有 checks 字段，拿到的是: %s"
              % json.dumps(health, ensure_ascii=False)[:300])
        return 1
    print("  -- %-18s python=%s sqlite=%s frozen=%s status=%s"
          % ("runtime", health.get("python"), health.get("sqlite"),
             health.get("frozen"), health.get("status")))
    bad = []
    for key, value in sorted(checks.items()):
        if not isinstance(value, dict) or "ok" not in value:
            print("  !! %-18s 形状不对: %s"
                  % (key, json.dumps(value, ensure_ascii=False)[:120]))
            bad.append(key)
            continue
        extra = json.dumps({k: v for k, v in value.items() if k != "ok"},
                           ensure_ascii=False)
        # 失败项不截断 —— 截掉的正是需要看的那段
        print("  %s%-18s %s" % ("ok " if value["ok"] else "!! ", key,
                                extra if not value["ok"] else extra[:150]))
        if not value["ok"]:
            bad.append(key)
    if health.get("frozen") is not True:
        print("!! frozen=%r —— 这不是打包产物，冒烟测的是源码"
              % health.get("frozen"))
        bad.append("not_frozen")
    bad += check_retrieval_config(checks.get("retrieval_config"))
    print("\n失败项: %s" % (bad or "无"))
    return 1 if bad else 0


def check_retrieval_config(actual: dict | None) -> list[str]:
    """冻结进去的检索配置必须是发布配置。

    `retrieval_config` 整块缺失是最要紧的一种失败：说明冻结的源码早于
    /health 加这一块，也就早于门控级联 —— 正是 8/26 那次错配的形状。
    """
    print()
    if not isinstance(actual, dict):
        print("!! 检索配置：/health 里没有 retrieval_config —— 冻结的源码早于"
              "门控级联，产物与文档的实测数字不对应")
        return ["retrieval_config_missing"]
    problems = []
    for key, want in EXPECTED_RETRIEVAL.items():
        got = actual.get(key)
        same = abs(got - want) < 1e-6 if isinstance(want, float) and isinstance(
            got, (int, float)) else got == want
        print("  %s检索配置 %-12s 期望 %-8s 实际 %s"
              % ("ok " if same else "!! ", key, want, got))
        if not same:
            problems.append("retrieval_config.%s" % key)
    print("  -- 检索配置 effective_mode 实际 %s（临时数据目录里没装 CE 资产，"
          "所以是 local，这是对的）" % actual.get("effective_mode"))
    return problems


if __name__ == "__main__":
    raise SystemExit(main())
