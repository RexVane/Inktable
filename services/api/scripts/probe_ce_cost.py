"""Cross-Encoder 的**纯打分**成本，与 rerank 总耗时拆开量。

上一轮我用「Rerank P95 2140ms ÷ 26 对 ≈ 107ms/对」推断「CE 太贵，必须换更小的
模型」。这个推断有个漏洞：Rerank 那段耗时里还有候选正文加载、向量取用、本地
打分器、冗余降权。若纯打分只占一小半，那结论就是错的 —— 该优化的不是模型。

所以直接量 `runtime.score(query, documents)`：真实库里取真实分片正文，按实装
的焦点窗口截取，扫不同 batch / 线程 / max_tokens。只读。
"""

from __future__ import annotations

import io
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB = os.environ.get("ORDO_DB") or os.path.join(
    os.path.expanduser("~"), "Library", "Application Support",
    "Ordo", "library.db")

QUERY = "怎样防止用户用相对路径跑出自己的文件目录"
PAIRS = int(os.environ.get("PROBE_PAIRS", "26"))


def real_documents(n: int) -> list[str]:
    conn = sqlite3.connect("file:{}?mode=ro".format(DB.replace("\\", "/")),
                           uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT ch.text, ch.section_path FROM chunks ch
           JOIN contents c ON c.id = ch.content_id
           WHERE ch.index_version = c.active_index_version
             AND length(ch.text) > 400
           LIMIT ?""", (n,)).fetchall()
    conn.close()
    from app.retrieval.rerank import CASCADE_FOCUS_CHARS, _focus_window
    return [
        "\n".join(p for p in (
            r["section_path"] or "",
            _focus_window(r["text"], ["路径", "目录", "相对"], CASCADE_FOCUS_CHARS),
        ) if p)
        for r in rows
    ]


def bench(label: str, docs: list[str], repeats: int = 5) -> float:
    from app.retrieval import cross_encoder as ce

    runtime = ce.get_runtime()
    runtime.score(QUERY, docs[:2])          # 预热，别把首次图优化算进去
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        runtime.score(QUERY, docs)
        times.append((time.perf_counter() - t0) * 1000)
    best, med = min(times), statistics.median(times)
    print("%-46s 中位 %7.1fms  最好 %7.1fms  → %5.1fms/对"
          % (label, med, best, med / max(1, len(docs))))
    return med


def _fresh_runtime() -> None:
    """丢掉上一轮的 ONNX 会话再建新的。

    少了这一步，每换一组环境变量就多留一个活着的 session，每个都带
    intra_op 条线程 —— 扫到网格后半段时线程数已经严重超订，测出来的会是
    「越往后越慢」，而那是探针自己造成的，不是被测配置的性质。
    """
    import gc

    from app.retrieval import cross_encoder as ce

    ce._runtime = None
    gc.collect()


def main() -> int:
    docs = real_documents(PAIRS)
    if len(docs) < PAIRS:
        print("真实分片不足（拿到 %d 条），继续但样本偏小" % len(docs))
    lens = sorted(len(d) for d in docs)
    print("%d 条候选，字符数 min=%d 中位=%d max=%d\n"
          % (len(docs), lens[0], lens[len(lens) // 2], lens[-1]))

    grid = [
        ("默认（threads=14, batch=8, max_tokens=384）", {}),
        ("batch=26 一次喂完", {"ORDO_RERANK_BATCH": "26"}),
        ("batch=4", {"ORDO_RERANK_BATCH": "4"}),
        ("threads=28", {"ORDO_RERANK_THREADS": "28"}),
        ("threads=7", {"ORDO_RERANK_THREADS": "7"}),
        ("max_tokens=256", {"ORDO_RERANK_MAX_TOKENS": "256"}),
        ("max_tokens=192", {"ORDO_RERANK_MAX_TOKENS": "192"}),
        ("max_tokens=192 + batch=26",
         {"ORDO_RERANK_MAX_TOKENS": "192", "ORDO_RERANK_BATCH": "26"}),
    ]
    results = {}
    for label, env in grid:
        for key in ("ORDO_RERANK_BATCH", "ORDO_RERANK_THREADS",
                    "ORDO_RERANK_MAX_TOKENS"):
            os.environ.pop(key, None)
        os.environ.update(env)
        # 运行时是模块级缓存的，改环境变量后必须重建才生效
        import importlib

        from app.retrieval import cross_encoder as ce
        importlib.reload(ce)
        _fresh_runtime()
        results[label] = bench(label, docs)

    base = results["默认（threads=14, batch=8, max_tokens=384）"]
    print("\n以默认为 1.00 倍：")
    for label, ms in sorted(results.items(), key=lambda kv: kv[1]):
        print("  %-46s %.2fx" % (label, ms / base))
    print("\n注意：这只是**纯打分**。rerank 总耗时还含候选加载、向量取用、"
          "本地打分与冗余降权 —— 两者的差额就是「换模型」之外的可优化空间。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
