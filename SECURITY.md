# Security Policy / 安全策略

## Supported versions / 支持的版本

Security fixes land on `main` (current 0.3.x). There are no long-term support branches yet.

安全修复合入 `main`（当前 0.3.x）。还没有长期支持分支。

## Reporting a vulnerability / 如何报告漏洞

Please **do not** open a public issue.

请**不要**开公开 Issue。

Use GitHub’s private advisory form / 请使用 GitHub 私密 Advisory：

https://github.com/RexVane/Ordo/security/advisories/new

Include what you ran, the version or commit, and a minimal reproduction if you have one.

请写明运行环境、版本或 commit，以及尽可能小的复现步骤。

Especially useful reports / 特别有用的报告：

- Sidecar bind address / bearer token handling  
  sidecar 绑定地址 / Bearer 令牌处理
- `ordodoc://` serving a path that is not a library-registered file  
  `ordodoc://` 把未在库内登记的路径当文件送出
- API keys written to SQLite, logs, or the renderer  
  API 密钥写进 SQLite、日志或渲染层
- Renderer CSP bypass or unexpected network from the UI  
  渲染层 CSP 被绕过，或界面出现不该有的网络请求

You should hear back. If the report is accepted, we will credit you in the advisory unless you ask otherwise.

我们会回复。若报告被接受，会在 Advisory 里署名，除非你要求匿名。
