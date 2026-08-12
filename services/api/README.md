# Inktable Sidecar（FastAPI 后端）

Electron 主进程拉起的本地服务：绑定 `127.0.0.1:0`，令牌经 stdin 传入，
所有业务接口要求 Bearer Token。权威设计见 `../../docs/PLAN.md`。

## 模块地图

```text
app/
├── main.py          FastAPI 入口：sources / files / search / ask / runs …
├── db/              SQLite 连接、schema v2、备份与完整性检查
├── discovery/       微信 4.x / QQ / 浏览器下载目录自动发现
├── watcher/         预扫描、稳定性判据、FSEvents 实时监听
├── domain/          (volume_uuid, inode) 文件身份
├── parsing/         PDF / DOCX / Markdown / TXT → Blocks → Child 分片
├── index/           jieba+trigram FTS、向量索引（进程内矩阵缓存）、
│                    三层层级（Document/Section/Child）、置信度
├── retrieval/       QueryPlan（子查询分解）→ 四路召回 → RRF →
│                    本地重排 v3 → 去冗 → 邻居扩展 → 证据压缩 → 装配
├── qa/              带引用问答：四条后置校验，validation 全留痕
└── organize/        虚拟分类、保全副本
```

## 常用命令

```bash
uv sync                                   # 安装依赖
uv run pytest                             # 全量测试
uv run python tests/run_eval.py           # 72 题检索评测（K6：改检索必跑）
uv run python tests/run_compress_eval.py  # M4 压缩评测
uv run python tests/e2e_watch.py          # 端到端：投放文件→自动入库→搜索
uv run pyinstaller sidecar.spec --clean --noconfirm   # 冻结打包
```

调试用 `INKTABLE_DB=/tmp/dev.db` 指向独立库；`INKTABLE_RERANKER=rrf`
可显式降级重排做对照。

## 改动约束

- 改检索/分片/融合/重排/压缩前后必须跑同一评测集（`docs/eval/` 有冻结基线）。
- 18 条硬约束见 `../../docs/HANDOFF.md`，动任何一条前先读。
