<p align="right">
  <strong>English</strong> | <a href="./SECURITY.zh-CN.md">简体中文</a>
</p>

# Security Policy

## Supported versions

Security fixes land on `main` (current 0.3.x). There are no long-term support branches yet.

## Reporting a vulnerability

Please **do not** open a public issue.

Use GitHub’s private advisory form:

https://github.com/RexVane/Ordo/security/advisories/new

Include what you ran, the version or commit, and a minimal reproduction if you have one.

Especially useful reports:

- Sidecar bind address / bearer token handling
- `ordodoc://` serving a path that is not a library-registered file
- API keys written to SQLite, logs, or the renderer
- Renderer CSP bypass or unexpected network from the UI

You should hear back. If the report is accepted, we will credit you in the advisory unless you ask otherwise.
