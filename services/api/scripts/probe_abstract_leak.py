"""摘要 prompt 的示例词泄漏检查。

第一版 prompt 举例说「例如『路径穿越』也写『目录遍历』」，结果模型把这两个
词抄进了一篇**讲检索延迟**的文档摘要里（实测：两词在原文出现 0 次）。摘要
进 FTS，于是查「路径穿越」会命中一篇不相关的文档 —— 索引被静默污染。

这个脚本对若干真实文档生成摘要，并核对摘要里的关键词是否真在原文出现或
确实是原文概念的同义说法。它不是通过/失败判据（同义扩展本来就允许摘要
使用原文没有的词），而是把泄漏摆出来给人看。
"""

from __future__ import annotations

import io
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.index.abstract import generate  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
DOCS = [
    REPO / "docs" / "RETRIEVAL-PERF.md",
    REPO / "docs" / "WINDOWS-PORT.md",
    REPO / "docs" / "CORPUS-NOISE.md",
    REPO / "docs" / "M0-RESULTS.md",
]
# 第一版泄漏过的词。它们不该出现在与安全无关的文档摘要里。
LEAK_CANARIES = ["路径穿越", "目录遍历", "论文", "合同", "日志"]


def main() -> int:
    leaks = 0
    for path in DOCS:
        if not path.exists():
            print("跳过（不存在）:", path.name)
            continue
        body = path.read_text(encoding="utf-8")
        started = time.time()
        try:
            abstract = generate(path.stem, body)
        except Exception as exc:  # noqa: BLE001
            print("FAIL %s: %s" % (path.name, exc))
            leaks += 1
            continue
        print("\n=== %s  (%.1fs, 摘要 %d 字) ===" % (
            path.name, time.time() - started, len(abstract)))
        print(abstract)
        hits = [w for w in LEAK_CANARIES
                if w in abstract and w not in body]
        if hits:
            leaks += 1
            print("!! 泄漏词（摘要里有、原文没有）:", "，".join(hits))
        # 中文词粗切后看有多少不在原文里 —— 只作观察，不判定
        novel = [w for w in set(re.findall(r"[一-鿿]{2,6}", abstract))
                 if w not in body]
        print("   摘要中未在原文出现的中文词片段 %d 个（同义扩展属正常）:"
              % len(novel), "，".join(sorted(novel)[:12]))
    print("\n泄漏文档数:", leaks)
    return 1 if leaks else 0


if __name__ == "__main__":
    raise SystemExit(main())
