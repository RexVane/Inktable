<p align="center">
  <a href="./README.md">English</a> | <strong>简体中文</strong>
</p>

<p align="center">
  <img src="docs/github/social-preview.png" width="880" alt="Ordo — 本地优先个人知识库">
</p>

<h1 align="center">Ordo</h1>

<p align="center">
  <a href="https://github.com/RexVane/Ordo/actions/workflows/backend-tests.yml"><img src="https://github.com/RexVane/Ordo/actions/workflows/backend-tests.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey.svg" alt="macOS、Windows、Linux">
</p>

<p align="center">
  <strong>本地优先的个人知识库（macOS / Windows / Linux）。</strong><br>
  把已经散落在磁盘上的资料变成可检索、可问答、可回溯证据的知识空间。<br>
  <strong>默认不移动、不复制、不改名任何文件</strong> —— 组织全部发生在索引层。
</p>

## 功能

- **原位索引** — 文件留在磁盘上。身份是卷 UUID + inode，不是路径。
- **混合检索** — 结巴分词 + 三元组 + 子串 + 本机 `bge-m3` 向量，RRF 融合。
- **带引用问答** — 快速 / 深度两档；引用可点进 PDF/DOCX 原文。
- **知识馆** — sidecar 后台做摘要、标签、主题。失败项只有你点「重试」才会再跑。
- **本地或云端模型** — 本机 Ollama，或云端/中转 API（Chat Completions / Responses / Anthropic Messages）。
- **默认隐私** — 渲染层 CSP 没有网络出口。文件字节走 `ordodoc://`，主进程先核对该路径是否已在库内登记。

桌面系统都以**本地磁盘**为顶层来源（`C:\`、`/`、`/Volumes/…`、`/mnt/…`）。微信、浏览器、下载文件夹不再单独列为来源。

## 仓库结构

```
apps/desktop/     Electron 工作台（无打包器）
services/api/     FastAPI 侧车（SQLite / FTS5 / sqlite-vec）
docs/             设计、评测、发布说明
```

当前版本 **0.3.0**。Windows 已日常使用；macOS 与 Linux 同一套磁盘来源模型。尚未代码签名 —— 见 [限制](#限制)。

## 开始使用

GitHub Releases 上还没有签名安装包。请从源码运行：

```bash
git clone https://github.com/RexVane/Ordo.git
cd Ordo/services/api && uv sync
cd ../../apps/desktop && npm install && npm start
```

```powershell
git clone https://github.com/RexVane/Ordo.git
cd Ordo\services\api ; uv sync
cd ..\..\apps\desktop ; npm install ; npm start
```

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 20+。用 `ORDO_DB` 指向临时库，避免碰到真实资料库。可选：本机 [Ollama](https://ollama.com) 安装 `bge-m3` 做语义检索（不装仍可用关键词检索）。

打包安装包（先冻结 sidecar）：

```powershell
cd services\api ; uv run --group dev pyinstaller sidecar.spec --clean --noconfirm
cd ..\..\apps\desktop ; npm run dist -- --win --x64
```

```bash
cd services/api && uv run --group dev pyinstaller sidecar.spec --clean --noconfirm
cd ../../apps/desktop && npm run dist -- --linux --x64
```

## 架构

```
Electron 主进程 ──stdin: {token}──▶  Python sidecar (FastAPI)
     │          ◀──stdout: {port}        │
     │                                   ├─ library/    来源、身份、监听
  渲染层                                 ├─ ingestion/  解析、层级、索引
  （工作台）  ──HTTP + Bearer───────────▶├─ retrieval/  混合召回、RRF、问答
     │                                   └─ db/         SQLite + FTS5 + sqlite-vec
     └─ ordodoc:// 原文字节（主进程代理 sidecar /files/{id}/raw）
```

数据目录：`~/Library/Application Support/Ordo/data`（macOS）、`%APPDATA%\Ordo\data`（Windows）、`~/.local/share/Ordo`（Linux）。

**文件与内容：** N 个文件可以对应 1 份内容。重复文件只解析、只建一次索引。

## 模型

设置 → 模型，三个槽位：

1. **知识问答** — 本机 Ollama，或云端/中转（协议 + 密钥 + 模型）。可从 cc-switch 导入。
2. **知识馆整理** — 本机 Ollama，或云端 OpenAI 兼容接口（选云端即表示文档正文会离开本机）。
3. **向量** — 仅本机 Ollama；输出维度必须是 1024。

密钥走 Electron `safeStorage`。sidecar 只放内存 —— 不进 SQLite、不进日志、不回显。

## 文档

- [贡献指南](CONTRIBUTING.zh-CN.md)
- [安全](SECURITY.zh-CN.md)
- [行为准则](CODE_OF_CONDUCT.zh-CN.md)
- [0.3.0 发布说明](docs/RELEASE-0.3.0.md)
- [Windows 移植](docs/WINDOWS-PORT.md) · [Linux](docs/LINUX.md)
- [方案](docs/PLAN.md) · [硬约束](docs/HANDOFF.md)
- [评测](docs/eval/README.md) — 冻结 v8：Recall@5 94.3%、MRR@10 88.1%、nDCG@10 91.0%

## 限制

- 未代码签名；macOS 首次打开需右键 → 打开。Linux 提供 AppImage / deb
- 原版式查看仅 PDF / DOCX；其余格式走提取文本
- 向量槽位仅支持本机 Ollama
- 入库白名单：`.txt` `.md` `.pdf` `.docx` `.csv` `.html` `.htm`
- 单文档全文索引上限 10 MB

## 贡献

见 [CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)。欢迎 Issue 和 PR。安全问题走 [SECURITY.zh-CN.md](SECURITY.zh-CN.md)，不要开公开 Issue。

## 许可证

[MIT](LICENSE) © 2026 RexVane
