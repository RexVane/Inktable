"""评测结论的不确定度:53 题上的点估计能撑多硬。

我在文档里写过「真正决定结论的只有 4 题」,但那是定性说法。这里给定量口径:

- **自助法置信区间**:把 53 道题按题重采样 10,000 次,看指标的 95% 区间。
  n=53 时一道题就值 1.9pp,区间会比点估计难看得多 —— 这正是要说清的。
- **留一法**:逐题剔除后重算,报最小/最大值。看单题的杠杆有多大。
- **翻转距离**:还差几道题翻面就会掉出门槛。这个数最好读。

为什么不是「再出 20 道题然后重跑」:出题必须只看文档原文、不看任何检索结果
(HANDOFF H12,`scripts/sample_corpus_for_gold.py` 的模块说明也写着)。而我这一轮
从头到尾在研究哪几道题为什么失败、gold 在融合序与向量序里各排第几 —— 由我出题
就是照着实现的失效模式出题,那是评测集最典型的失效方式。所以扩集必须换一个
没看过这些的人或模型来做,我只能把现有证据的不确定度量清楚。

只读,不碰数据库,只吃 run_eval.py 的 JSON。
"""

from __future__ import annotations

import argparse
import io
import json
import random
import statistics
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

GATES = {"recall_at_5": 95.0, "ndcg_10": 90.0}


def load_cases(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [c for c in data["results"] if "doc_hit" in c]


def metrics(cases: list[dict]) -> dict[str, float]:
    n = max(1, len(cases))
    return {
        "recall_at_5": 100.0 * sum(1 for c in cases if c["doc_hit"]) / n,
        "ndcg_10": 100.0 * sum(c["ndcg_10"] or 0.0 for c in cases) / n,
        "mrr_10": 100.0 * sum(c["mrr_10"] or 0.0 for c in cases) / n,
    }


def bootstrap(cases: list[dict], key: str, rounds: int, rng: random.Random):
    n = len(cases)
    draws = []
    for _ in range(rounds):
        sample = [cases[rng.randrange(n)] for _ in range(n)]
        draws.append(metrics(sample)[key])
    draws.sort()
    lo = draws[int(0.025 * rounds)]
    hi = draws[min(rounds - 1, int(0.975 * rounds))]
    return lo, hi, draws


def leave_one_out(cases: list[dict], key: str):
    vals = []
    for i in range(len(cases)):
        rest = cases[:i] + cases[i + 1:]
        vals.append((metrics(rest)[key], cases[i]["qid"]))
    return min(vals), max(vals)


def flips_to_fail(cases: list[dict], key: str, gate: float) -> str:
    """还差几道题翻面就掉出门槛。

    Recall@5 是二值的,直接算差几道;nDCG 是连续量,换算成「几道满分题变零分」。
    """
    n = len(cases)
    now = metrics(cases)[key]
    if now < gate:
        return "已在门槛之下"
    if key == "recall_at_5":
        hits = sum(1 for c in cases if c["doc_hit"])
        need = 0
        while hits - need >= 0 and 100.0 * (hits - need) / n >= gate:
            need += 1
        return "%d 道题翻面（当前命中 %d/%d）" % (need, hits, n)
    margin = (now - gate) / 100.0 * n
    return "约 %.2f 个「满分题变零分」的量（余量 %.2fpp）" % (margin, now - gate)


def report(label: str, cases: list[dict], rounds: int, seed: int) -> None:
    rng = random.Random(seed)
    base = metrics(cases)
    print("== %s ==  n=%d" % (label, len(cases)))
    for key in ("recall_at_5", "ndcg_10", "mrr_10"):
        lo, hi, draws = bootstrap(cases, key, rounds, rng)
        (lmin, qmin), (lmax, qmax) = leave_one_out(cases, key)
        gate = GATES.get(key)
        line = "  %-12s 点估计 %5.1f%%  自助 95%% 区间 [%5.1f, %5.1f]" % (
            key, base[key], lo, hi)
        if gate:
            below = 100.0 * sum(1 for d in draws if d < gate) / len(draws)
            line += "  门槛 %.0f%%：重采样里 %5.1f%% 的次数不达标" % (gate, below)
        print(line)
        print("       留一法 [%5.1f (去掉 %s), %5.1f (去掉 %s)]"
              % (lmin, qmin, lmax, qmax))
        if gate:
            print("       翻转距离：%s" % flips_to_fail(cases, key, gate))
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("json", type=Path, nargs="+",
                        help="run_eval.py 写出的结果 JSON,可给多个做对照")
    parser.add_argument("--rounds", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260826)
    args = parser.parse_args()

    for path in args.json:
        cases = load_cases(path)
        label = json.loads(path.read_text(encoding="utf-8")).get(
            "label", path.stem)
        report(label, cases, args.rounds, args.seed)

    print("怎么读这些数字:")
    print("  · 区间宽是 n=53 的直接后果 —— 一道题就值 1.9pp,不是实现不稳。")
    print("  · 「重采样里 X% 不达标」是对『换一批同分布的题还能不过门槛吗』的")
    print("    回答。X 明显大于 0 就说明点估计过门槛**不等于**结论稳。")
    print("  · 留一法的极值告诉你单题杠杆;翻转距离最好读:差几道题就掉下去。")
    print("  · 要真正收窄区间只有扩集,而出题必须换一个没看过实现失效模式的人")
    print("    或模型来做(HANDOFF H12,见 scripts/sample_corpus_for_gold.py)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
