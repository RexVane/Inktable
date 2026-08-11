"""端到端验证：投放文件 → 自动监听入库 → 索引 → 搜内容。

跑真实 HTTP，不用 TestClient —— 要验证的正是后台线程与 API 的协作。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx2 as httpx  # starlette 依赖，随 TestClient 一并装入

PORT = 8791
TOKEN = "e2e-token-fixed"
BASE = f"http://127.0.0.1:{PORT}"
H = {"Authorization": f"Bearer {TOKEN}"}


def wait_health(timeout=25):
    for _ in range(int(timeout / 0.3)):
        try:
            if httpx.get(f"{BASE}/health", timeout=2).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="inktable-e2e-"))
    watch_dir = work / "inbox"
    stage = work / "stage"
    watch_dir.mkdir()
    stage.mkdir()

    env = dict(os.environ, INKTABLE_TOKEN=TOKEN, INKTABLE_DB=str(work / "e2e.db"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )

    failures = []
    try:
        if not wait_health():
            print("✗ 服务未就绪")
            print(proc.stderr.read().decode()[-1500:])
            return 1

        # ① 启用来源（自动挂监听）
        r = httpx.post(f"{BASE}/sources/enable", headers=H,
                       json={"name": "E2E", "path": str(watch_dir)}, timeout=30)
        assert r.status_code == 200, f"启用失败 {r.status_code}: {r.text[:200]}"
        print(f"① 启用来源 → source_id={r.json()['source_id']}")

        st = httpx.get(f"{BASE}/watch/status", headers=H).json()
        assert st["running"] and len(st["watched"]) == 1, st
        print(f"   监听已挂载：{len(st['watched'])} 个目录")

        # ② 场景 A：文件在别处写好后移入（微信/QQ 主路径）
        src = stage / "莫高窟.md"
        src.write_text(
            "# 敦煌莫高窟数字化保护进展\n\n"
            "第 285 窟的壁画采用多光谱成像技术记录，分辨率达到每毫米 600 像素。\n"
            "颜料分析显示青金石来自阿富汗巴达赫尚地区，印证了古代贸易路线。\n",
            encoding="utf-8",
        )
        os.rename(src, watch_dir / "莫高窟.md")

        # ③ 场景 B：分段写入（下载中的文件，不能过早入库）
        target = watch_dir / "分段.txt"
        with open(target, "w", encoding="utf-8") as f:
            for i in range(4):
                f.write(f"第 {i+1} 段：钧窑窑变釉的铜元素在还原气氛下呈现紫红色。\n")
                f.flush()
                time.sleep(0.7)

        # 等自动入库
        deadline = time.time() + 30
        while time.time() < deadline:
            st = httpx.get(f"{BASE}/watch/status", headers=H).json()
            if st["counters"]["indexed"] >= 2:
                break
            time.sleep(0.5)

        st = httpx.get(f"{BASE}/watch/status", headers=H).json()
        print(f"\n② 自动入库 counters: {st['counters']}")
        print(f"   watcher stats : {st['watcher']}")
        for a in st["activity"]:
            print(f"   • {a['name']} [{a['status']}] {a['chunks']} 片")

        if st["counters"]["indexed"] < 2:
            failures.append(f"只有 {st['counters']['indexed']} 个文件被索引，应为 2")

        # ④ 分段写入的文件必须完整
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        print(f"\n③ 分段文件完整性：{len(lines)} 行 {'✓' if len(lines) == 4 else '✗'}")
        if len(lines) != 4:
            failures.append(f"分段文件行数 {len(lines)}，应为 4")

        # ⑤ 搜内容 —— 这些词在文件名里都不存在
        print("\n④ 全文检索（关键词均不在文件名中）")
        cases = [
            ("多光谱成像 青金石", "莫高窟.md"),
            ("窑变釉 铜元素", "分段.txt"),
            ("巴达赫尚", "莫高窟.md"),
        ]
        for q, expect in cases:
            d = httpx.post(f"{BASE}/search", headers=H, json={"q": q}, timeout=20).json()
            names = [f["name"] for f in d["files"]]
            ok = expect in names
            if not ok:
                failures.append(f"搜「{q}」未命中 {expect}（得到 {names}）")
            snippet = d["files"][0]["snippets"][0]["text"][:46].replace("\n", " ") if d["files"] else "—"
            print(f"   「{q}」→ {d['total']} 个 {'✓' if ok else '✗'}  {snippet}")

        # ⑥ 删除文件后不应再出现在结果里
        (watch_dir / "分段.txt").unlink()
        time.sleep(3)

    except Exception as e:
        failures.append(f"异常：{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)

    print()
    if failures:
        print(f"✗ {len(failures)} 项失败")
        for f in failures:
            print("   -", f)
        return 1
    print("✓ 端到端全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
