# Inktable Sidecar（FastAPI 后端）

Local FastAPI sidecar spawned by the Electron main process: binds
`127.0.0.1:0`, receives the session token on stdin, and requires Bearer
auth on every business route. Design authority: `../../docs/PLAN.md`.

Electron 主进程拉起的本地服务：绑定 `127.0.0.1:0`，令牌经 stdin 传入，
所有业务接口要求 Bearer Token。权威设计见 `../../docs/PLAN.md`。

## 模块地图

```text
app/
├── main.py          FastAPI 入口：sources / files（tree·content·group）/
│                    search / ask / runs / classify / integrations / system …
├── db/              SQLite 连接、schema v2、备份与完整性检查；
│                    数据目录可经 INKTABLE_DATA_DIR 迁移
├── discovery/       本地磁盘为顶层来源（Windows 固定盘 / macOS 卷）；
│                    启用后按真实路径展开，可再手动添加目录
├── watcher/         预扫描、稳定性判据、FSEvents 实时监听
├── domain/          (volume_uuid, inode) 文件身份
├── parsing/         PDF / DOCX / Markdown / TXT → Blocks → Child 分片
├── index/           jieba+trigram FTS、向量索引（进程内矩阵缓存）、
│                    嵌入经本机 Ollama bge-m3（1024 维，未装则降级纯 FTS）、
│                    三层层级（Document/Section/Child）、置信度、
│                    存量向量补齐（embed_backfill）
├── retrieval/       QueryPlan（子查询分解）→ 四路召回 → RRF →
│                    本地重排 v3 → 去冗 → 邻居扩展 → 证据压缩 → 装配
├── qa/              带引用问答（四条后置校验留痕）、
│                    真实补全连接检测（probe 返回实际回复与耗时）
├── organize/        虚拟分类（含按扩展名自动归类）、保全副本
└── integrations/    cc-switch 供应商导入（只读 ~/.cc-switch/cc-switch.db）
```

关键口径：`VISIBLE_FILES_COND` —— 停用来源的文件保留记录与索引，
但从列表 / 统计 / 搜索 / 分类计数中隐藏；重新启用即恢复。

## 常用命令

```bash
uv sync                                   # 安装依赖
uv run --group dev python -m pytest       # 全量测试
uv run python tests/run_eval.py           # 72 题检索评测（K6：改检索必跑）
uv run python tests/run_compress_eval.py  # M4 压缩评测
uv run python tests/e2e_watch.py          # 端到端：投放文件→自动入库→搜索
uv run --group dev pyinstaller sidecar.spec --noconfirm   # 冻结打包
```

调试环境变量：

- `INKTABLE_DB=/tmp/dev.db` —— 指向独立库，避免污染真实数据
- `INKTABLE_DATA_DIR=…` —— 整体迁移数据目录（库/备份/保全副本）
- `INKTABLE_RERANKER=rrf` —— 显式降级重排做对照
- `INKTABLE_OLLAMA_URL=…` —— Ollama 地址；不设则探测本机 11434，其次 18434

语义检索依赖本机 Ollama + bge-m3（`ollama pull bge-m3`）。
未检测到时全链路自动降级纯关键词检索；模型维度变更会在启动时
自动重建向量表并触发全量回填（`db/database._init_vec_table`）。

## 改动约束

- 改检索/分片/融合/重排/压缩前后必须跑同一评测集（`docs/eval/` 有冻结基线）。
- 18 条硬约束见 `../../docs/HANDOFF.md`，动任何一条前先读。
- 桌面端开发态直接加载本目录源码：先执行 `uv sync`，再在
  `apps/desktop` 执行 `npm start`；不需要先冻结 sidecar。
- 最终发布前才执行 `uv run --group dev pyinstaller sidecar.spec`，
  electron-builder 发布包只使用 `Resources/sidecar` 中的冻结产物。
