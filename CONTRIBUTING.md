# Contributing to Ordo / 为 Ordo 做贡献

Thanks for looking. Ordo is a local-first desktop app: Electron UI + a Python sidecar. **Do not move, copy, or rename the user’s original files.**

谢谢。Ordo 是本地优先的桌面应用（Electron 界面 + Python sidecar）。**默认不要移动、复制或改名用户的原文件。**

## Setup / 环境

Need Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js 20+, and npm.

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 20+ 和 npm。

```bash
git clone https://github.com/RexVane/Ordo.git
cd Ordo

cd services/api
uv sync
uv run pytest

cd ../../apps/desktop
npm install
npm test
npm start          # runs the API from source; no sidecar binary required
                   # 直接拉 API 源码，不必先打包 sidecar
```

Use a scratch database so you do not touch a real library:

用临时库，避免碰到真实资料库：

```bash
# macOS / Linux
ORDO_DB=/tmp/ordo-dev.db

# Windows PowerShell
$env:ORDO_DB = "$env:TEMP\ordo-dev.db"
```

## What to change where / 改哪里

| Path | English | 中文 |
|---|---|---|
| `apps/desktop/` | Window, renderer, settings, workbench | 窗口、渲染层、设置、工作台 |
| `services/api/app/` | Index, search, Q&A, sources, library | 索引、检索、问答、来源、知识馆 |
| `docs/` | Design, eval, release notes | 设计、评测、发布说明 |

Hard constraints (identity is inode not path, sidecar never deletes originals, FTS/vector writes in one transaction, …) live in [`docs/HANDOFF.md`](docs/HANDOFF.md). Read that before changing storage, ingest, or search.

硬约束（身份是 inode 不是路径、sidecar 绝不删除原文件、FTS/向量必须同一事务写入……）在 [`docs/HANDOFF.md`](docs/HANDOFF.md)。改存储、入库或检索之前先读。

## Pull requests / 提 PR

1. One concern per PR. / 一个 PR 只做一件事。
2. If you touch `services/api/**`, run `uv run pytest` in that directory.  
   动了 sidecar，在该目录跑 `uv run pytest`。
3. If you touch `apps/desktop/**`, run `npm test`. Inline script changes in `renderer/index.html` need `node scripts/csp-hash.js --write`.  
   动了桌面端，跑 `npm test`。改了 `renderer/index.html` 的内联脚本必须重写 CSP 哈希。
4. Do not commit `node_modules/`, `.venv/`, `dist/`, or live `*.db` files.  
   不要提交 `node_modules/`、`.venv/`、`dist/` 或正在用的 `*.db`。
5. Do not add telemetry, extra network from the renderer, or cloud embedding without an explicit design discussion.  
   不要加遥测、不要给渲染层加额外网络、不要在没有设计讨论的情况下上云端嵌入。

## Issues / 议题

Bugs and ideas: GitHub Issues.  
缺陷和想法：GitHub Issues。

Security: see [`SECURITY.md`](SECURITY.md), not a public issue.  
安全问题：见 [`SECURITY.md`](SECURITY.md)，不要开公开 Issue。
