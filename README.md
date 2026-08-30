<p align="center">
  <strong>English</strong> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="docs/github/social-preview.png" width="880" alt="Ordo — local-first personal knowledge base">
</p>

<h1 align="center">Ordo</h1>

<p align="center">
  <a href="https://github.com/RexVane/Ordo/actions/workflows/backend-tests.yml"><img src="https://github.com/RexVane/Ordo/actions/workflows/backend-tests.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey.svg" alt="macOS, Windows, and Linux">
</p>

<p align="center">
  <strong>Local-first personal knowledge base for macOS, Windows, and Linux.</strong><br>
  Turns files already on your disks into a searchable, citable, askable knowledge space.<br>
  <strong>Never moves, copies, or renames originals</strong> — organization happens in the index.
</p>

## Features

- **In place** — Files stay on disk. Identity is volume + inode, not path.
- **Hybrid search** — jieba + trigram + substring + local `bge-m3` vectors, fused with RRF.
- **Cited Q&A** — Fast or deep mode; quotes open the PDF/DOCX original.
- **Knowledge library** — Sidecar-side summaries, tags, topics. Failed items retry only when you ask.
- **Local or cloud models** — Ollama on this machine, or a cloud/proxy API (Chat Completions / Responses / Anthropic Messages).
- **Privacy default** — Renderer CSP has no network. File bytes go through `ordodoc://` after the main process authorizes a library path.

Desktop OSes treat **local disks** as sources (`C:\`, `/`, `/Volumes/…`, `/mnt/…`). Chat apps, browsers, and Downloads are not separate sources.

## Repository

```
apps/desktop/     Electron workbench (no bundler)
services/api/     FastAPI sidecar (SQLite / FTS5 / sqlite-vec)
docs/             design, eval, release notes
```

Current release is **0.3.0**. Windows is in daily use; macOS and Linux share the same disk-source model. Code signing is not done yet — see [Limits](#limits).

## Getting started

There is no signed installer on GitHub Releases yet. Run from source:

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

Packaged build (after freezing the sidecar):

```powershell
cd services\api ; uv run --group dev pyinstaller sidecar.spec --clean --noconfirm
cd ..\..\apps\desktop ; npm run dist -- --win --x64
```

```bash
cd services/api && uv run --group dev pyinstaller sidecar.spec --clean --noconfirm
cd ../../apps/desktop && npm run dist -- --linux --x64
```

## Architecture

```
Electron main  ──stdin: {token}──▶  Python sidecar (FastAPI)
     │          ◀──stdout: {port}        │
     │                                   ├─ library/    sources, identity, watch
  renderer                               ├─ ingestion/  parse, hierarchy, index
  (workbench) ──HTTP + Bearer───────────▶├─ retrieval/  hybrid recall, RRF, Q&A
     │                                   └─ db/         SQLite + FTS5 + sqlite-vec
     └─ ordodoc:// original bytes (main process proxies sidecar /files/{id}/raw)
```

Data directory: `~/Library/Application Support/Ordo/data` (macOS), `%APPDATA%\Ordo\data` (Windows), `~/.local/share/Ordo` (Linux).

**files vs contents:** N files share 1 content. Duplicates are parsed and indexed once.

## Models

Settings → Models, three slots:

1. **Q&A** — Ollama, or cloud/proxy (protocol + key + model). Can import from cc-switch.
2. **Library enrichment** — Ollama or a cloud OpenAI-compatible API (cloud means document text leaves this machine).
3. **Embeddings** — Ollama only; output dimension must be 1024.

Keys use Electron `safeStorage`. The sidecar keeps them in memory — not SQLite, not logs, not echoed back.

## Documentation

- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Release 0.3.0](docs/RELEASE-0.3.0.md)
- [Windows port](docs/WINDOWS-PORT.md) · [Linux](docs/LINUX.md)
- [Plan](docs/PLAN.md) · [Hard constraints](docs/HANDOFF.md)
- [Eval](docs/eval/README.md) — frozen v8: Recall@5 94.3%, MRR@10 88.1%, nDCG@10 91.0%

## Limits

- Not code-signed; first macOS launch needs right-click → Open. Linux ships as AppImage/deb.
- Original-layout viewer is PDF / DOCX; other types use extracted text
- Embedding slot is local Ollama only
- Ingest whitelist: `.txt` `.md` `.pdf` `.docx` `.csv` `.html` `.htm`
- Full-text index caps a document at 10 MB

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs are welcome. Security reports go through [SECURITY.md](SECURITY.md), not public issues.

## License

[MIT](LICENSE) © 2026 RexVane
