<p align="right">
  <a href="./SECURITY.md">English</a> | <strong>简体中文</strong>
</p>

# 安全策略

## 支持的版本

安全修复合入 `main`（当前 0.3.x）。还没有长期支持分支。

## 如何报告漏洞

请**不要**开公开 Issue。

请使用 GitHub 私密 Advisory：

https://github.com/RexVane/Ordo/security/advisories/new

请写明运行环境、版本或 commit，以及尽可能小的复现步骤。

特别有用的报告：

- sidecar 绑定地址 / Bearer 令牌处理
- `ordodoc://` 把未在库内登记的路径当文件送出
- API 密钥写进 SQLite、日志或渲染层
- 渲染层 CSP 被绕过，或界面出现不该有的网络请求

我们会回复。若报告被接受，会在 Advisory 里署名，除非你要求匿名。
