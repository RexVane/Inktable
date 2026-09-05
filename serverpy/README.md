# Python / FastAPI 后端

项目运行入口为根目录 `ordo.py`。后端使用 Python 3.11+、FastAPI、Uvicorn、SQLite FTS5、PyMuPDF、openpyxl、mammoth、httpx、cryptography 和 psycopg；启动不依赖 Node。原 `server/` 作为迁移参考保留，避免覆盖工作区已有修改。

## 启动与检查

在项目根目录运行：

```powershell
python -m venv serverpy/.venv
serverpy/.venv/Scripts/python -m pip install -r serverpy/requirements.txt
python ordo.py serve
```

Linux/macOS 使用 `serverpy/.venv/bin/python` 安装依赖。启动器自动选择已有项目虚拟环境；没有虚拟环境时使用当前 Python。也可在 `serverpy` 下直接运行 `python main.py`。

- 页面：http://127.0.0.1:8790/
- API：http://127.0.0.1:8790/api/v1/
- OpenAPI：http://127.0.0.1:8790/api/v1/openapi.json
- Swagger：http://127.0.0.1:8790/api/docs（先打开产品页面建立本机会话）
- `python ordo.py test -q`：Python 集成、安全、迁移、恢复及流式传输测试。
- `npm test`：上述测试加真实 FastAPI HTTP 下的浏览器客户端测试和现有前端测试。
- `npm start`、`npm run seed`、`npm --prefix web start` 均转到 Python；`npm --prefix web run preview` 仅用于静态页面预览。

默认端口仍为 8790。切换前先停止占用同一端口或同一数据目录的旧实例。当前部署为单 Uvicorn 进程，由它调度有并发上限的独立解析子进程，不要对同一 SQLite 数据目录启动多个服务进程。

## 路由与业务模块

`ordo/api_contract.json` 是可独立运行的后端契约目录，与 `web/api.js` 的 224 个操作逐项测试比对。`routes.py` 将契约注册为真实 FastAPI 路由；OpenAPI 从实际路由生成。另保留 `POST /api/v1/messages/{messageId}/wiki` 兼容地址。

| 模块 | 实现 |
| --- | --- |
| 会话、CSRF、CORS、错误、上传大小限制 | `runtime.py` |
| 知识库、数据集、原件、标准产物、不可变版本 | `knowledge.py` |
| 块修订、拆分、合并、回滚、FTS 与向量投影 | `knowledge_workbench.py`、`vector_index.py` |
| 未归档文件登记、文件夹、移动、解析控制、页面预览、资源监测 | `workbench.py` |
| 问答、引用、Trace、幂等重放 | `query.py` |
| 八阶段输出检查、版本化配置草稿、计算解释和导出 | `trace_workbench.py` |
| OpenAI 兼容接口、Ollama、本地证据抽取、真实增量响应 | `models.py` |
| 数据库只读模板、图谱、网站助手 | `connectors.py`、`graph.py`、`widget.py` |
| 仪表盘、设置、Wiki、助手、审计、加密备份恢复 | `product.py`、`storage.py`、`backup.py` |

管理请求先 `GET /api/v1/session/bootstrap`，保留 Cookie，写请求携带返回的 `csrfToken` 到 `x-ordo-csrf`。普通响应为 `{data}`，列表分页为 `{data: [], meta: {total, limit, offset}}`。产物与导出接口返回原始内容；问答支持 `Accept: text/event-stream`，事件为 `stage`、`token`、`done` 或 `error`。网站访客使用独立 HMAC 初始化和 Bearer Token。

## 数据迁移与恢复

继续使用 `.ordo-data/` 和原有环境变量，`ORDO_DATA_DIR` 可指定独立目录。SQLite migration 5 增加未归档文件、文件夹、Trace 配置草稿和索引投影，保留原表及历史引用。启动会恢复持久化排队任务。

秘密读取兼容旧 Node 的 `IV + tag + ciphertext` 与早期 Python 的 `IV + ciphertext + tag`，新写入使用 Node 兼容格式。备份使用同一 `ORDOENC1`、HKDF-SHA256 与 AES-256-GCM 封装；恢复校验密文、清单、每个文件摘要、SQLite 完整性和文件引用，只写入尚不存在的新目录。

迁移已有数据前应保留整个数据目录或创建加密备份，尤其是 `runtime/master.key`；环境变量 `ORDO_MASTER_KEY` 必须与原数据一致。测试使用独立临时目录，不修改现有 `.ordo-data/`。

## 当前可用能力

所有接口都有 Python 处理器；算法和外部能力以实际安装配置为准：

- 原生文字型 PDF、DOCX、PPTX、XLSX、CSV、Markdown、TXT 由 Python 解析。PDF 提供实际原页 PNG 与文字框；其他格式提供标准文本预览。无可靠文字层的扫描件和图片标记复核，未配置 OCR/VLM 时不声称完成识别；深度 OCR 启动请求返回明确的能力不可用错误。
- 本地向量基线是确定性的哈希向量，重排是词项匹配；它们不是预训练语义模型或交叉编码器。HNSW 接口构建并持久化真实多层图投影，发布版本默认仍走精确余弦检索，全文检索使用 SQLite FTS5/BM25。不存在虚构的召回数、耗时或候选知识块。
- Trace 的已记录阶段不可被草稿覆盖。修改权重、路由、解析或提示词会保存配置版本；重放产生关联的新 Trace，并使用草稿运行。当前重放从头运行完整流水线，返回 `executionMode: full_pipeline` 和请求的阶段标识，不声称复用上游计算缓存。
- 图谱实体/关系管理与数据库只读模板可以使用；自动将图谱和数据库接入问答召回需要另行配置 Provider。未启用的通道明确标识不可用。
- 模型连接支持 OpenAI 兼容 HTTP 接口和 Ollama；传输固定已验证的目标 IP、保留 Host/TLS SNI、禁止重定向并限制响应体和时间。只有显式 `ORDO_ALLOW_LOCAL_MODEL_ENDPOINTS=true` 才允许本机/私网模型地址。
- CPU、内存和队列指标来自实际系统与任务记录；GPU 未配置时返回不可用。吞吐统计明确按文档/分钟计数。

## 常用环境变量

| 变量 | 默认/用途 |
| --- | --- |
| `ORDO_HOST` / `ORDO_PORT` | `127.0.0.1` / `8790` |
| `ORDO_DATA_DIR` | 项目 `.ordo-data/` |
| `ORDO_MAX_FILE_BYTES` | 50 MiB |
| `ORDO_BODY_LIMIT` | 64 MiB |
| `ORDO_PARSER_TIMEOUT_MS` | 120000 |
| `ORDO_ALLOW_LOCAL_MODEL_ENDPOINTS` | `false` |
| `ORDO_ALLOW_LOCAL_DATABASE_HOSTS` | `false` |
| `ORDO_ALLOW_REMOTE`、`ORDO_TLS_TERMINATED`、`ORDO_REMOTE_ADMIN_TOKEN` | 非本机监听需要同时配置 |

远程部署保留 TLS 终止和管理员初始化令牌要求。配置模型或数据库连接的凭据仅通过对应管理 API 提交，由 SecretStore 加密保存。
