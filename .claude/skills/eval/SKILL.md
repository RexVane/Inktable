---
name: eval
description: 跑 Inktable 的 72 题检索评测（或 M4 压缩评测），把结果与 docs/eval/ 里的冻结基线逐指标对比。改动检索、分片、融合、重排、压缩任一环节后使用。
---

改动检索 / 分片 / 融合 / 重排 / 压缩后，必须跑同一评测集与基线对比（`services/api/README.md`「改动约束」）。

## 铁律：不得反向调优

**K6 / H12：gold 标注只在资料变化或标注确实错误时才改，绝不能为了让某个检索实现分数变好而调。**

如果分数没达到预期，去改检索实现，不要改 `services/api/tests/evalset.py` 或 `docs/eval/` 里的标注。若你判断某条标注确实错了，**停下来向用户说明是哪条、为什么错**，等确认后再改。

## 步骤

### 1. 先确认库里有数据

评测跑的是**用户真实资料库**（默认 `~/Library/Application Support/Inktable/`），不是 fixture。

```bash
ls ~/Library/Application\ Support/Inktable/ 2>/dev/null || echo "数据目录不存在"
```

目录不存在或 `chunks` 表为空时，脚本会打印「库里没有分片，先启用来源并跑 /index/run」并 `return 1`。这**不是代码问题**——需要用户先启动应用、启用来源、完成索引。遇到这种情况就报告给用户并停下，不要试图绕过或改用 fixture。

### 2. 记录改动前的基线

如果你正要改检索实现，**先在改动前跑一遍**存下数字，否则事后无从对比。已有基线在 `docs/eval/`（`docs/eval/README.md` 说明每个文件对应哪一轮）。

### 3. 跑评测

在 `services/api/` 目录下：

```bash
cd services/api
uv run python tests/run_eval.py --verbose --label <这轮的标签> --json ../../docs/eval/<文件名>.json
```

压缩评测（只支持 `--json`）：

```bash
uv run python tests/run_compress_eval.py --json ../../docs/eval/<文件名>.json
```

想验证重排是否真的有收益，用 `INKTABLE_RERANKER=rrf` 跑一遍降级对照（仓库里已有 `v7-m3-rrf-baseline.json` / `v7-m3b-rrf-control.json` 这类对照结果）。

### 4. 读懂退出码

**退出码 1 不一定是脚本失败。** 也可能是 Recall@5 低于 80% 发布门槛——那是已记录的产品缺口，不是回归（见 `docs/eval/README.md`）。先看输出里到底哪个指标没过，再判断。

### 5. 对比并汇报

读回新产出的 JSON 与对应基线 JSON，逐指标对比（Recall@5、严格通过率等），然后汇报：

- 哪些指标涨了 / 跌了，具体数字
- **任何指标下降都要明确说出来**，不要只报涨的
- 如果结果不可跨机复现（资料库变了），说明这一点

不要把 `docs/eval/` 里的既有基线文件覆盖掉——那些是冻结产物。新结果写新文件名。`baseline-fts5.json` 和 `with-vector.json` 是不可变的 pre-v7 30 题基线，绝不要动。
