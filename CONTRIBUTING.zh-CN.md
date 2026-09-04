<p align="right">
  <a href="./CONTRIBUTING.md">English</a> | <strong>简体中文</strong>
</p>

# 为 Ordo 做贡献

谢谢。Ordo 是本地优先的桌面应用（Electron 界面 + Python sidecar）。**默认不要移动、复制或改名用户的原文件。**

## 环境

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
npm start          # 直接拉 API 源码，不必先打包 sidecar
```

用临时库，避免碰到真实资料库：

```bash
# macOS / Linux
export ORDO_DB=/tmp/ordo-dev.db

# Windows PowerShell
$env:ORDO_DB = "$env:TEMP\ordo-dev.db"
```

## 改哪里

| 路径 | 内容 |
|---|---|
| `apps/desktop/` | 窗口、渲染层、设置、工作台 |
| `services/api/app/` | 索引、检索、问答、来源、知识馆 |
| `docs/` | 设计、评测、发布说明 |

硬约束（身份是 inode 不是路径、sidecar 绝不删除原文件、FTS/向量必须同一事务写入……）在 [`docs/HANDOFF.md`](docs/HANDOFF.md)。改存储、入库或检索之前先读。

## 提 PR

1. 一个 PR 只做一件事。
2. 动了 sidecar，在该目录跑 `uv run pytest`。
3. 动了桌面端，跑 `npm test`。改了 `renderer/index.html` 的内联脚本必须重写 CSP 哈希：`node scripts/csp-hash.js --write`。
4. 不要提交 `node_modules/`、`.venv/`、`dist/` 或正在用的 `*.db`。
5. 不要加遥测、不要给渲染层加额外网络、不要在没有设计讨论的情况下上云端嵌入。

## 议题

缺陷和想法：GitHub Issues。

安全问题：见 [`SECURITY.zh-CN.md`](SECURITY.zh-CN.md)，不要开公开 Issue。
