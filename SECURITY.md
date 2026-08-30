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

## 说明（中文）

安全问题请走上面的私密 Advisory，不要发公开 Issue。Ordo 是本地优先应用，侧车鉴权、原文协议、密钥存储相关的报告特别有用。
