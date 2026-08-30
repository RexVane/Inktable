<p align="right">
  <strong>English</strong> | <a href="./CONTRIBUTING.zh-CN.md">简体中文</a>
</p>

# Contributing to Ordo

Thanks for looking. Ordo is a local-first desktop app: Electron UI + a Python sidecar. **Do not move, copy, or rename the user’s original files.**

## Setup

Need Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js 20+, and npm.

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
```

Use a scratch database so you do not touch a real library:

```bash
# macOS / Linux
ORDO_DB=/tmp/ordo-dev.db

# Windows PowerShell
$env:ORDO_DB = "$env:TEMP\ordo-dev.db"
```

## What to change where

| Path | What |
|---|---|
| `apps/desktop/` | Window, renderer, settings, workbench |
| `services/api/app/` | Index, search, Q&A, sources, library |
| `docs/` | Design, eval, release notes |

Hard constraints (identity is inode not path, sidecar never deletes originals, FTS/vector writes in one transaction, …) live in [`docs/HANDOFF.md`](docs/HANDOFF.md). Read that before changing storage, ingest, or search.

## Pull requests

1. One concern per PR.
2. If you touch `services/api/**`, run `uv run pytest` in that directory.
3. If you touch `apps/desktop/**`, run `npm test`. Inline script changes in `renderer/index.html` need `node scripts/csp-hash.js --write`.
4. Do not commit `node_modules/`, `.venv/`, `dist/`, or live `*.db` files.
5. Do not add telemetry, extra network from the renderer, or cloud embedding without an explicit design discussion.

## Issues

Bugs and ideas: GitHub Issues.

Security: see [`SECURITY.md`](SECURITY.md), not a public issue.
