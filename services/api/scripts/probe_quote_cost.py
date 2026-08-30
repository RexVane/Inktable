"""D 的代价测量：加了引文块要求，会不会把可答的题逼成拒答？

第一次端到端试跑时，本地 qwen3:8b 对 A01（gold 认为可答）给出了拒答。这有
两种可能：模型本身能力不够，或者**我新加的引文块要求把 prompt 变难了**。
后者是真代价，必须先量出来再决定 D 是否默认开启 —— 反过来做，就是在没有
基线的情况下动引用可靠性，而这正是 CURRENT_STATUS 第十节记下过的教训。

唯一变量是 `ORDO_QUOTE_CLAUSE`（本脚本内部开关），其余（模型、库、题目、
检索深度）完全相同。

用法：
    ORDO_OLLAMA_URL=http://127.0.0.1:18434 \\
    ORDO_DB=<隔离库副本> \\
      .venv/Scripts/python.exe scripts/probe_quote_cost.py --n 12
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

from app.db.database import connect  # noqa: E402
from app.qa import llm  # noqa: E402
from app.qa import quotes as quotes_mod  # noqa: E402
from tests.evalset import ALL_CASES  # noqa: E402


def run_one(conn, question: str) -> dict:
    from app.qa.answer import ask
    started = time.time()
    try:
        a = ask(conn, question, None)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)[:120], "ms": 0}
    report = (a.validation or {}).get("quotes") or {}
    return {
        "status": a.status,
        "citations": len(a.citations or []),
        "quote_total": report.get("total", 0),
        "quote_verified": report.get("verified", 0),
        "ms": int((time.time() - started) * 1000),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=12)
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    base = os.environ.get("ORDO_OLLAMA_URL", "http://127.0.0.1:11434")
    llm.configure(f"{base}/v1", "ollama-no-key-needed", args.model)
    if not llm.is_configured():
        print("LLM 未配置")
        return 2

    cases = [c for c in ALL_CASES if c.answerable][:args.n]
    conn = connect()
    rows = []

    for arm, clause_on in (("无引文块", False), ("有引文块", True)):
        # 用 monkeypatch 式替换而不是环境变量：prompt_clause 是被 answer.py
        # 在构造 system 时调用的，替换它就等于精确控制这一个变量。
        original = quotes_mod.prompt_clause
        if not clause_on:
            import app.qa.answer as answer_mod
            answer_mod.quote_prompt_clause = lambda: ""
        else:
            import app.qa.answer as answer_mod
            answer_mod.quote_prompt_clause = original
        print("\n=== %s ===" % arm)
        for case in cases:
            r = run_one(conn, case.query)
            r["qid"] = case.qid
            r["arm"] = arm
            rows.append(r)
            print("  %-4s %-10s 引用=%-2s 引文=%s/%s  %sms"
                  % (case.qid, r["status"], r.get("citations", 0),
                     r.get("quote_verified", 0), r.get("quote_total", 0),
                     r["ms"]))

    def agg(arm: str) -> dict:
        sub = [r for r in rows if r["arm"] == arm]
        answered = sum(1 for r in sub if r["status"] == "answered")
        return {
            "n": len(sub),
            "answered": answered,
            "refused": sum(1 for r in sub if r["status"] == "refused"),
            "other": sum(1 for r in sub
                         if r["status"] not in ("answered", "refused")),
            "quote_total": sum(r.get("quote_total", 0) for r in sub),
            "quote_verified": sum(r.get("quote_verified", 0) for r in sub),
            "p50_ms": sorted(r["ms"] for r in sub)[len(sub) // 2] if sub else 0,
        }

    a_off, a_on = agg("无引文块"), agg("有引文块")
    print("\n%-10s %-8s %-8s %-8s %-10s" % ("", "已回答", "拒答", "其他", "P50ms"))
    for label, a in (("无引文块", a_off), ("有引文块", a_on)):
        print("%-10s %-8s %-8s %-8s %-10s"
              % (label, a["answered"], a["refused"], a["other"], a["p50_ms"]))
    print("\n引文核验（有引文块臂）：%d/%d 条通过"
          % (a_on["quote_verified"], a_on["quote_total"]))
    delta = a_on["answered"] - a_off["answered"]
    print("加引文块后已回答数变化：%+d" % delta)
    # 统计功效检查。若基线本身几乎全是拒答，"没观察到下降"只是因为没有
    # 下降的余地 —— 那不是结论，是测不出来。不加这一层，脚本会在一个
    # 1/10 的地板上打印出令人安心的假结论。
    floor = min(a_off["answered"], a_on["answered"])
    if a_off["answered"] <= max(2, a_off["n"] // 4):
        print("→ **此结论无效**：基线只答出 %d/%d 题，样本没有检测变化的功效。"
              % (a_off["answered"], a_off["n"]))
        print("   先换一个能跟住引用协议的模型，再用本脚本量引文块的代价。")
    elif delta < 0:
        print("→ 引文块要求确实压低了回答率，D 不应默认开启强制剔除。")
    else:
        print("→ 未观察到回答率下降（基线 %d/%d，有功效）。"
              % (a_off["answered"], a_off["n"]))
    if a_on["quote_total"] and not a_on["quote_verified"]:
        print("→ 注意：模型产出了 %d 条引文但**一条都没核上**，说明它没有"
              "逐字复制原文。此时开启强制剔除会把引用全删掉。"
              % a_on["quote_total"])
    _ = floor

    if args.json:
        args.json.write_text(json.dumps(
            {"model": args.model, "off": a_off, "on": a_on, "rows": rows},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print("产物：", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
