# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 结构

monorepo，两个独立模块，各有自己的依赖与 README：

- `services/api/` —— Python sidecar（FastAPI + SQLite/FTS5/sqlite-vec），uv 管理。模块地图见 `services/api/README.md`
- `apps/desktop/` —— Electron 应用，原生 JS/HTML/CSS，**无框架、无构建步骤**
- `docs/` —— 权威设计文档（`PLAN.md` / `HANDOFF.md`），根目录的 `PLAN.md` / `HANDOFF.md` 只是指向它的 stub

## 命令

后端命令**必须在 `services/api/` 目录下执行**。`app/` 不是已安装包（`pyproject.toml` 声明的 src-layout `src/inktable_api/` 是脚手架残留），测试靠 pytest rootdir 解析 `from app.xxx import`，从仓库根跑会 ImportError。

```bash
cd services/api
uv sync                                   # 首次或依赖变更后
uv run pytest                             # 全量测试
uv run pytest tests/test_search.py -k test_name   # 单测
uv run python tests/run_eval.py           # 72 题检索评测，改检索必跑
uv run python tests/e2e_watch.py          # 端到端：投放文件→自动入库→搜索
```

日常改后端用 uvicorn 直起验证，不必打包：

```bash
cd services/api
INKTABLE_TOKEN=dev INKTABLE_DB=/tmp/dev.db uv run uvicorn app.main:app --port 8790
```

**但改动要在桌面端生效必须重新打包**：`apps/desktop/electron/main.js:89` 把开发态 sidecar 路径写死为 `services/api/dist/inktable-sidecar`（PyInstaller 产物），产物不存在时 `npm start` 直接报错。

```bash
cd services/api && uv run --group dev pyinstaller sidecar.spec --clean --noconfirm
cd apps/desktop && npm start
```

桌面测试：`cd apps/desktop && npm test`（Node 内置 `node:test`，无 jest/vitest）。

## 硬约束：改之前先读文档

`docs/HANDOFF.md` §2 是 18 条硬约束（H1–H18，每条附验证方法），`docs/PLAN.md` §1.3 是 8 条不可协商约束（K1–K8）。**这里不复述条文**——仓库刻意不维护副本以防漂移。

`HANDOFF.md` 原话点出了危险所在：这些约束的共同特点是**违反它们会让代码更短更简单**，所以会有强烈的本能去改掉。最常踩的几条：

- **H1** 索引主键是 `(volume_uuid, inode)`，不是路径
- **H2** 默认不移动 / 不重命名 / 不删除任何原文件（sidecar 只清库；动磁盘只由 Electron 的 `shell.trashItem` 做）
- **H7** `chunks` / `chunks_fts` / `chunks_vec` 三表写入必须同一事务
- **H13** `chunks` 挂在 `contents` 上，**不是** `files`
- **H16** 增量 diff 以 `text_hash` 为主键，不是 `section_path`

`HANDOFF.md` §7 定了提问约定——以下四种情况**停下来问，不要自行决定**：想改任一硬约束、发现方案自相矛盾、认为某个阻塞验收项做不到、想引入方案未提及的第三方依赖（尤其带原生扩展的）。

## 评测

`docs/eval/*.json` 是冻结基线，规范用法见 `docs/eval/README.md`：

```bash
uv run python tests/run_eval.py --verbose --label <标签> --json ../../docs/eval/<file>.json
```

- **K6 / H12：gold 标注只在资料变化或标注确实错误时才改，不得为了让某个检索实现分数变好而调。**
- 评测跑的是用户真实资料库（默认 `~/Library/Application Support/Inktable/`），不是 fixture。`chunks` 表为空时脚本打印提示并 `return 1`——先启用来源并跑 `/index/run` 才有数字。结果不可跨机复现。
- **退出码 1 不一定是脚本失败**：也可能是 Recall@5 低于 80% 发布门槛，属已记录的产品缺口（见 `docs/eval/README.md`）。

## 代码风格

- **注释与 docstring 全部中文**，模块头部 docstring 说明「为什么这么做 / 踩过什么坑」并引用文档章节号（如 `PLAN §6.2`、`H2`、`K6`）。这是全仓贯彻的强约定，改代码时保持。
- Python：`from __future__ import annotations`、类型注解齐全、`X | None`；手写 SQL + `sqlite3.Row`，**禁止 ORM**（PLAN §1.3）。共享条件常量如 `VISIBLE_FILES_COND`（`app/main.py:812`）必须复用而非重写。
- JS：2 空格缩进、单引号、分号、CommonJS。
- **本仓库没有 formatter，不要引入 black / ruff format，也不要顺手重排现有代码。**`ruff check` 只用于查实错（`[tool.ruff]` 见 `services/api/pyproject.toml`）；607 行超过 88 列是既有状态，不是待修项。

## 桌面测试是源码字面断言

`apps/desktop/tests/*.test.js` 用 `fs.readFileSync` 读 `renderer/index.html` 和 `electron/main.js`，再对源码做 regex 匹配。**改引号、换变量名、抽函数都会让测试红灯，即使行为完全没变。**

改 renderer/main.js 时**必须同步修正这些正则**，不要放宽或删除断言。（PLAN §11.3 已把「改成行为/IPC 契约测试」列为待办，但尚未做，在那之前维持现状。）

## 静默失败的坑

这些都不抛异常，只是结果变少或变空：

- **FTS5 `unicode61` 对中文双字词零命中** → 必须 jieba 全切分（`cut_for_search` 建索引、精确模式查询）+ trigram + LIKE 三路，缺一路会漏
- **FTS5 把 `-` 当 NOT 运算符** → 用户输入进 MATCH 前统一包双引号转义，不能靠调用方自觉
- contentless FTS5 默认不支持 DELETE → 建表必须 `contentless_delete=1`
- **建表 SQL 只有一份**，在 `app/db/schema.py`。曾有 schema.py 与 search.py 各存一份的坑，已合并，不要再复制
- macOS FSEvents 对树外移入的文件报 `created` 而非 `moved`（`moved_in` 恒为 False）→ 稳定性判据不能依赖事件类型
- 微信 4.x 路径是 `xwechat_files/<wxid>/msg/file/<YYYY-MM>/`，与网上 3.x 资料完全不同 → 必须 glob + 内容筛选，硬编码会静默失败
- 超过约 3.2 万分片时语义检索曾静默消失（全量 active id 作 SQL 绑定参数超 SQLite 32766 上限，异常被降级路径吞掉）→ 先检索、后小集合核对
- macOS TCC 拒绝访问 `~/Library/Containers` 时不报错，只是读不到内容 → 用 `os.scandir` 实测而非看目录是否存在

更多背景见 `docs/M0-RESULTS.md`。

## 环境变量（全部可选）

| 变量 | 作用 |
| --- | --- |
| `INKTABLE_DB` | 指向独立库，调试必用；`:memory:` 有特殊分支 |
| `INKTABLE_DATA_DIR` | 整体迁移数据目录（库 / 备份 / 保全副本），由 Electron 主进程传入 |
| `INKTABLE_TOKEN` | 会话令牌；不传则随机生成。**生产路径经 stdin 传入，绝不走命令行参数**（ps 可见） |
| `INKTABLE_RERANKER=rrf` | 显式降级重排做对照 |
| `INKTABLE_WATCH_BACKEND=polling` | 轮询替代 FSEvents；`tests/conftest.py` 自动 setdefault 为此 |

**LLM 密钥没有环境变量注入口**：用户在「设置 → 模型配置」填写，经 Electron `safeStorage` 加密落 `llm.enc`，sidecar 侧只存内存，不进库 / 日志 / 回显（有专项断言）。

语义检索需本机 Ollama + `ollama pull bge-m3`（1024 维）；未装时自动降级纯关键词检索，功能不塌但指标不同。

## 打包注意

`sidecar.spec` 手动处理两个 PyInstaller 不会自动收集的原生依赖：`sqlite_vec/vec0.dylib` 与 jieba 词典。**`upx=False` 是必须的**（UPX 会破坏 dylib 签名）。任一遗漏的表现是：开发态完全正常，冻结后 `/health` 报 degraded。

数据目录不能放在资料库目录（可能在 iCloud，多设备并发写会损坏 SQLite）。

## Git

- 直接在 `main` 上工作，不要自建分支。
- **不要自动 commit / push**，由用户执行。
- 提交信息风格：中文，「主题：要点 —— 量化结果」，带里程碑编号。例：`向量路性能与静默故障修复：矩阵进程内缓存（写路径失效）+ 活跃过滤改为后置小集合核对 —— 30k 分片 p95 717ms→78ms`
