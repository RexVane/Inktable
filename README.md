<p align="center">
  <strong>English</strong> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="docs/github/social-preview.png" width="880" alt="Ordo — local-first knowledge workbench">
</p>

<h1 align="center">Ordo</h1>

<p align="center">
  <a href="https://github.com/RexVane/Ordo/actions/workflows/backend-tests.yml"><img src="https://github.com/RexVane/Ordo/actions/workflows/backend-tests.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg" alt="Windows, macOS, and Linux">
</p>

Ordo is a local-first knowledge management and strict evidence-grounded Q&A product. The current codebase provides a unified web workbench and a versioned API that handle data import, parsing, knowledge-block revision, immutable releases, hybrid retrieval, cited Q&A, wiki, assistants, audit, backup, and isolated recovery on a single machine.

## Requirements

- Windows, macOS, or Linux
- Node.js 24 or later (uses the built-in `node:sqlite` module)
- The first install needs access to the npm registry

## Install and run

```powershell
git clone https://github.com/RexVane/Ordo.git
cd Ordo
npm --prefix server install
npm run seed
npm start
```

Open `http://127.0.0.1:8790/`. The same port serves both the web workbench and `/api/v1`; the OpenAPI 3.1 contract is available at `http://127.0.0.1:8790/api/v1/openapi.json`.

`npm run seed` imports [`server/fixtures/ordo-sample-knowledge.md`](./server/fixtures/ordo-sample-knowledge.md), runs the real parsing pipeline, and builds the active release. The command is repeatable — content-hash deduplication prevents duplicate documents and blobs.

## Data and security

The default data directory is `.ordo-data/`, which you can relocate to another managed location via `ORDO_DATA_DIR`. It contains SQLite metadata, content-addressed blobs, standard artifacts, task state, backups, runtime secrets, and logs.

- The admin API uses random local-only session HttpOnly cookies; every write request additionally requires a CSRF token.
- API keys and database passwords are stored with AES-256-GCM; business records and responses only keep SecretRefs and masked values.
- Uploads and archive imports enforce format whitelists, MIME checks, path-traversal, symlink, nested-archive, and resource-budget checks.
- The SQLite/PostgreSQL connectors only allow controlled, parameterized read-only query templates.
- The website assistant uses HMAC, short-lived tokens, Origin binding, nonce replay protection, and an independent rate limit.
- Releases, block revisions, and wiki revisions are immutable; audit events form a verifiable append-only hash chain.
- Restore only writes into a new empty directory and completes integrity checks; it never overwrites the running instance.

Images and scanned PDFs enter a "needs review" state when no verified OCR/VLM provider is configured; they are never disguised as successful parses. The knowledge graph and website assistant have stable backend contracts; whether they are shown or enabled is decided by product feature flags and verified configuration.

## Common commands

```powershell
npm start          # Start the unified product service
npm run seed       # Idempotently import the sample knowledge document
npm test           # Backend integration & security tests plus frontend contract tests
npm run check      # JavaScript syntax check
npm --prefix server audit --registry=https://registry.npmjs.org
```

## Layout

- `planning/` — frozen decisions, feature specifications, acceptance baselines, and 22 directional page prototypes.
- `server/src/` — database, storage, tasks, parsing, indexing, Q&A, connectors, graph, assistants, and the HTTP API.
- `server/tests/` — real API end-to-end, security boundary, recovery, and public assistant tests.
- `web/` — the React 18 + React Router + Vite product workbench; all primary data is read and written through `/api/v1`.
