<p align="center">
  <img src="docs/github/social-preview.png" width="880" alt="Ordo — local-first personal knowledge base">
</p>

<h1 align="center">Ordo</h1>

<p align="center">
  <strong>Local-first personal knowledge base for macOS and Windows.</strong><br>
  Indexes files where they already live — searchable, citable, and askable —<br>
  without moving, copying, or renaming anything on disk.
</p>

<p align="center">
  <strong>本地优先的个人知识库（macOS / Windows）。</strong><br>
  把已经散落在磁盘上的资料变成可检索、可问答、可回溯证据的知识空间。<br>
  <strong>默认不移动、不复制、不改名任何文件</strong> —— 组织全部发生在索引层。
</p>

<p align="center">
  <a href="#status">0.3.0</a> ·
  <a href="docs/RELEASE-0.3.0.md">Release notes</a> ·
  <a href="docs/eval/README.md">Eval</a> ·
  <a href="docs/WINDOWS-PORT.md">Windows</a>
</p>

## What it is

Ordo is a three-pane knowledge workbench on top of your real disks.

- **Left** — local drives as top-level sources, then a file tree of real paths (or the knowledge library)
- **Center** — file list grouped by extension, or the original viewer
- **Right** — knowledge Q&A with clickable citations into the source

Windows and macOS both treat **local disks** as sources (`C:\`, `/`, `/Volumes/…`). Enable a disk, and the tree follows the real directory. You can still add a folder by hand. Chat apps, browsers, and Downloads are not separate sources.

## Features

| | |
|---|---|
| In-place library | Files stay on disk. Identity is volume + inode, not path. |
| Hybrid search | jieba + trigram + substring + local `bge-m3` vectors, fused with RRF |
| Cited answers | Fast or deep mode; quotes jump into PDF/DOCX originals |
| Knowledge library | Sidecar-side AI enrichment (summaries, tags, topics); failed items retry only when you ask |
| Local or cloud models | Ollama on this machine, or a cloud/proxy API (Chat Completions / Responses / Anthropic Messages) |
| Privacy default | Renderer CSP has no network. File bytes go through `ordodoc://` after the main process authorizes a library path. |

## Status

**0.3.0** — workbench rewrite; Windows port is in daily use (discover → scan → index → vectors → Q&A → live watch). See `docs/RELEASE-0.3.0.md` and `docs/WINDOWS-PORT.md`.

Frozen retrieval on the private v8 gold set (`docs/eval/`): Recall@5 **94.3%**, MRR@10 **88.1%**, nDCG@10 **91.0%**. Code signing for macOS/Windows is still open.

## Develop

```bash
# API
cd services/api
uv sync
uv run pytest

# Desktop (runs the API from source; no sidecar build needed)
cd ../../apps/desktop && npm install && npm start
```

```powershell
cd services\api ; uv sync
cd ..\..\apps\desktop ; npm install ; npm start

# Release build (Windows x64 NSIS) — freeze the sidecar first
cd ..\..\services\api ; uv run --group dev pyinstaller sidecar.spec --clean --noconfirm
cd ..\..\apps\desktop ; npm run dist -- --win --x64
```

Point a scratch database with `ORDO_DB=/tmp/dev.db` so you do not touch the real library.

## Architecture

```
Electron main  ──stdin: {token}──▶  Python sidecar (FastAPI)
     │          ◀──stdout: {port}        │
     │                                   ├─ library/    sources, identity, watch, preservation
  renderer                               ├─ ingestion/  parse, hierarchy, incremental index
  (workbench) ──HTTP + Bearer───────────▶├─ retrieval/  hybrid recall, RRF, Q&A
     │                                   └─ db/         SQLite + FTS5 + sqlite-vec
     └─ ordodoc:// original bytes (main process proxies sidecar /files/{id}/raw)
```

Data directory:

- macOS: `~/Library/Application Support/Ordo/data`
- Windows: `%APPDATA%\Ordo\data`
- Linux: `~/.local/share/Ordo`

Migrate it in Settings → General. Do not put the library inside an iCloud/synced documents folder.

**files vs contents:** N files : 1 content. Duplicates share one parse and one index. If any copy still exists, the index stays.

## Models

Three independent slots (Settings → Models):

1. **Q&A** — local Ollama, or cloud/proxy with protocol + key + model. Can import from cc-switch.
2. **Library enrichment** — local Ollama or a cloud OpenAI-compatible API (cloud means document text leaves this machine).
3. **Embeddings** — local Ollama only; output dimension must match the 1024-d vector table.

Keys are stored with Electron `safeStorage`. The sidecar keeps them in memory — not in SQLite, not in logs, not echoed back.

## Docs

- [`docs/PLAN.md`](docs/PLAN.md) — product plan
- [`docs/HANDOFF.md`](docs/HANDOFF.md) — hard constraints before you change behavior
- [`docs/RELEASE-0.3.0.md`](docs/RELEASE-0.3.0.md) — this version
- [`docs/WINDOWS-PORT.md`](docs/WINDOWS-PORT.md) — Windows port notes
- [`docs/RETRIEVAL-PERF.md`](docs/RETRIEVAL-PERF.md) — search latency
- [`docs/eval/README.md`](docs/eval/README.md) — 77-question gold contract and frozen v8 numbers
- [`docs/M0-RESULTS.md`](docs/M0-RESULTS.md) — early measurement notes (Chinese tokenization, FTS5, watchers)

## Limits

- Not code-signed; first macOS launch needs right-click → Open
- Original layout viewer is PDF / DOCX; other types use extracted text
- Embedding slot is local Ollama only
- Ingest whitelist: `.txt` `.md` `.pdf` `.docx` `.csv` `.html` `.htm`
- Full-text index caps a document at 10 MB
