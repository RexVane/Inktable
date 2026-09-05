<p align="center">
  <a href="./README.md">English</a> | <strong>简体中文</strong>
</p>

<p align="center">
  <img src="docs/github/social-preview.png" width="880" alt="Ordo — 本地优先知识工作台">
</p>

<h1 align="center">Ordo</h1>

<p align="center">
  <a href="https://github.com/RexVane/Ordo/actions/workflows/backend-tests.yml"><img src="https://github.com/RexVane/Ordo/actions/workflows/backend-tests.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg" alt="Windows, macOS, and Linux">
</p>

Ordo 是本地优先的知识管理与严格证据问答产品。当前工程提供统一 Web 工作台和版本化 API，可在单机上完成数据导入、解析、知识块修订、不可变 Release、混合检索、引用问答、Wiki、助手、审计、备份与隔离恢复。

## 运行要求

- Windows、macOS 或 Linux
- Python 3.11 或更高版本，SQLite 需要启用 FTS5
- 首次安装需要访问 PyPI；Node.js 24 仅用于前端测试

## 安装与启动

```powershell
git clone https://github.com/RexVane/Ordo.git
cd Ordo
python -m venv serverpy/.venv
serverpy/.venv/Scripts/python -m pip install -r serverpy/requirements.txt
python ordo.py seed
python ordo.py serve
```

打开 `http://127.0.0.1:8790/`。同一端口提供 Web 和 `/api/v1`，OpenAPI 3.1 契约位于 `http://127.0.0.1:8790/api/v1/openapi.json`。

macOS/Linux 安装依赖时使用 `serverpy/.venv/bin/python`。`python ordo.py` 会自动选择项目虚拟环境。`python ordo.py seed` 会创建示例知识库、执行真实解析并激活知识版本，可重复执行；已有数据的工作区不必运行 seed。

后端统一使用 Python/FastAPI/Uvicorn，前端 224 个 API 操作及 Wiki 兼容地址均已注册，现有页面显示保持不变。详见[后端运行与能力说明](serverpy/README.md)。

## 数据与安全

默认数据目录是 `.ordo-data/`，可通过 `ORDO_DATA_DIR` 指向其他受管位置。目录包含 SQLite 元数据、内容寻址 Blob、标准产物、任务状态、备份、运行密钥和日志。

- 管理 API 使用随机本机会话 HttpOnly Cookie，所有写请求还需要 CSRF 令牌。
- API Key 与数据库密码使用 AES-256-GCM 保存，业务记录和响应只保存 SecretRef 与掩码。
- 上传和归档导入执行格式白名单、MIME、路径穿越、链接、嵌套包和资源预算检查。
- SQLite/PostgreSQL 连接器只允许受控、参数化的只读查询模板。
- 网站助手使用 HMAC、短期令牌、Origin 绑定、nonce 防重放和独立速率限制。
- Release、块修订和 Wiki 修订不可变；审计事件形成可验证的追加哈希链。
- 恢复只写入新的空目录并完成完整性检查，不覆盖当前运行实例。

图片和扫描 PDF 在没有经过验证的 OCR/VLM Provider 时进入“需复核”，不会被伪装成解析成功。知识图谱和网站助手具备稳定后端契约，是否展示或启用由产品功能开关和已验证配置决定。

## 常用命令

```powershell
python ordo.py serve   # 启动 FastAPI 与现有前端
python ordo.py seed    # 创建可选示例知识库
python ordo.py test -q # Python 集成与安全测试
python ordo.py check   # Python 语法与导入检查
npm test              # Python 测试加前端客户端、页面测试
npm run check         # Python 与前端 JavaScript 语法检查
```

## 目录

- `planning/`：冻结决策、专项规范、验收基线和 22 张方向原型。
- `serverpy/ordo/`：Python/FastAPI 后端，包含数据库、任务进程、解析、索引、问答、连接器、图谱、助手与恢复。
- `serverpy/tests/`：Python API、集成、安全、迁移及恢复测试。
- `server/`：保留的旧 Node 实现，供迁移对照；产品启动与默认测试已不再使用。
- `web/`：无依赖的浏览器工作台（原生 HTML/CSS/JS、无构建步骤），所有主数据通过 `/api/v1` 读写。
