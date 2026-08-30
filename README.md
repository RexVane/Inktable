<p align="center">
  <img src="docs/github/social-preview.png" width="880" alt="Ordo — local-first personal knowledge base / 本地优先个人知识库">
</p>

<h1 align="center">Ordo</h1>

<p align="center">
  <a href="https://github.com/RexVane/Ordo/actions/workflows/backend-tests.yml"><img src="https://github.com/RexVane/Ordo/actions/workflows/backend-tests.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey.svg" alt="macOS and Windows">
</p>

<p align="center">
  <strong>Local-first personal knowledge base for macOS and Windows.</strong><br>
  Turns files already on your disks into a searchable, citable, askable knowledge space.<br>
  <strong>Never moves, copies, or renames originals</strong> — organization happens in the index.
</p>

<p align="center">
  <strong>本地优先的个人知识库（macOS / Windows）。</strong><br>
  把已经散落在磁盘上的资料变成可检索、可问答、可回溯证据的知识空间。<br>
  <strong>默认不移动、不复制、不改名任何文件</strong> —— 组织全部发生在索引层。
</p>

## Features / 功能

- **In place / 原位索引** — Files stay on disk. Identity is volume + inode, not path.  
  文件留在磁盘上。身份是卷 UUID + inode，不是路径。
- **Hybrid search / 混合检索** — jieba + trigram + substring + local `bge-m3` vectors, fused with RRF.  
  结巴分词 + 三元组 + 子串 + 本机 `bge-m3` 向量，RRF 融合。
- **Cited Q&A / 带引用问答** — Fast or deep mode; quotes open the PDF/DOCX original.  
  快速 / 深度两档；引用可点进 PDF/DOCX 原文。
- **Knowledge library / 知识馆** — Sidecar-side summaries, tags, topics. Failed items retry only when you ask.  
  sidecar 后台做摘要、标签、主题。失败项只有你点「重试」才会再跑。
- **Local or cloud models / 本地或云端模型** — Ollama on this machine, or a cloud/proxy API (Chat Completions / Responses / Anthropic Messages).  
  本机 Ollama，或云端/中转 API（Chat Completions / Responses / Anthropic Messages）。
- **Privacy default / 默认隐私** — Renderer CSP has no network. File bytes go through `ordodoc://` after the main process authorizes a library path.  
  渲染层 CSP 没有网络出口。文件字节走 `ordodoc://`，主进程先核对该路径是否已在库内登记。

Windows and macOS both treat **local disks** as sources (`C:\`, `/`, `/Volumes/…`). Chat apps, browsers, and Downloads are not separate sources.

Windows 与 macOS 都以**本地磁盘**为顶层来源（`C:\`、`/`、`/Volumes/…`）。微信、浏览器、下载文件夹不再单独列为来源。

## Repository / 仓库结构

```
apps/desktop/     Electron workbench (no bundler)     Electron 工作台（无打包器）
services/api/     FastAPI sidecar (SQLite / FTS5)     FastAPI 侧车（SQLite / FTS5 / sqlite-vec）
docs/             design, eval, release notes         设计、评测、发布说明
```

Current release is **0.3.0**. Windows is in daily use. Code signing is not done yet — see [Limits](#limits--限制).

当前版本 **0.3.0**。Windows 已日常使用。尚未代码签名 —— 见 [限制](#limits--限制)。

## Getting started / 开始使用

There is no signed installer on GitHub Releases yet. Run from source:

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

Needs Python 3.12, [uv](https://docs.astral.sh/uv/), and Node.js 20+. Point a scratch DB with `ORDO_DB` so you do not touch a real library. Optional: local [Ollama](https://ollama.com) with `bge-m3` for semantic search (keyword search still works without it).

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 20+。用 `ORDO_DB` 指向临时库，避免碰到真实资料库。可选：本机 [Ollama](https://ollama.com) 安装 `bge-m3` 做语义检索（不装仍可用关键词检索）。

Packaged Windows build (after freezing the sidecar) / 打包 Windows 安装包（先冻结 sidecar）：

```powershell
cd services\api ; uv run --group dev pyinstaller sidecar.spec --clean --noconfirm
cd ..\..\apps\desktop ; npm run dist -- --win --x64
```

## Architecture / 架构

```
Electron main  ──stdin: {token}──▶  Python sidecar (FastAPI)
     │          ◀──stdout: {port}        │
     │                                   ├─ library/    sources, identity, watch
  renderer                               ├─ ingestion/  parse, hierarchy, index
  (workbench) ──HTTP + Bearer───────────▶├─ retrieval/  hybrid recall, RRF, Q&A
     │                                   └─ db/         SQLite + FTS5 + sqlite-vec
     └─ ordodoc:// original bytes (main process proxies sidecar /files/{id}/raw)
```

Data directory / 数据目录: `~/Library/Application Support/Ordo/data` (macOS), `%APPDATA%\Ordo\data` (Windows), `~/.local/share/Ordo` (Linux).

**files vs contents / 文件与内容：** N files share 1 content. Duplicates are parsed and indexed once.  
N 个文件可以对应 1 份内容。重复文件只解析、只建一次索引。

## Models / 模型

Settings → Models, three slots / 设置 → 模型，三个槽位：

1. **Q&A / 知识问答** — Ollama, or cloud/proxy (protocol + key + model). Can import from cc-switch.  
   本机 Ollama，或云端/中转（协议 + 密钥 + 模型）。可从 cc-switch 导入。
2. **Library enrichment / 知识馆整理** — Ollama or a cloud OpenAI-compatible API (cloud means document text leaves this machine).  
   本机 Ollama，或云端 OpenAI 兼容接口（选云端即表示文档正文会离开本机）。
3. **Embeddings / 向量** — Ollama only; output dimension must be 1024.  
   仅本机 Ollama；输出维度必须是 1024。

Keys use Electron `safeStorage`. The sidecar keeps them in memory — not SQLite, not logs, not echoed back.

密钥走 Electron `safeStorage`。sidecar 只放内存 —— 不进 SQLite、不进日志、不回显。

## Documentation / 文档

- [Contributing / 贡献指南](CONTRIBUTING.md)
- [Security / 安全](SECURITY.md)
- [Code of conduct / 行为准则](CODE_OF_CONDUCT.md)
- [Release 0.3.0 / 发布说明](docs/RELEASE-0.3.0.md)
- [Windows port / Windows 移植](docs/WINDOWS-PORT.md)
- [Plan / 方案](docs/PLAN.md) · [Hard constraints / 硬约束](docs/HANDOFF.md)
- [Eval / 评测](docs/eval/README.md) — frozen v8 / 冻结指标: Recall@5 94.3%, MRR@10 88.1%, nDCG@10 91.0%

## Limits / 限制

- Not code-signed; first macOS launch needs right-click → Open  
  未代码签名；macOS 首次打开需右键 → 打开
- Original-layout viewer is PDF / DOCX; other types use extracted text  
  原版式查看仅 PDF / DOCX；其余格式走提取文本
- Embedding slot is local Ollama only  
  向量槽位仅支持本机 Ollama
- Ingest whitelist: `.txt` `.md` `.pdf` `.docx` `.csv` `.html` `.htm`  
  入库白名单：`.txt` `.md` `.pdf` `.docx` `.csv` `.html` `.htm`
- Full-text index caps a document at 10 MB  
  单文档全文索引上限 10 MB

## Contributing / 贡献

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs are welcome. Security reports go through [SECURITY.md](SECURITY.md), not public issues.

见 [CONTRIBUTING.md](CONTRIBUTING.md)。欢迎 Issue 和 PR。安全问题走 [SECURITY.md](SECURITY.md)，不要开公开 Issue。

## License / 许可证

[MIT](LICENSE) © 2026 RexVane
