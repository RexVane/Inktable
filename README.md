# Inktable

macOS 本地文件索引与检索。自动发现微信、QQ、浏览器下载等目录，
把散落全机的文档变成可搜内容的统一索引层。

**默认不移动、不复制、不改名任何文件** —— 组织全部发生在索引层。

## 当前状态

V1.0 已可用。`dist/Inktable-0.1.0-arm64.dmg`（295 MB，含 119 MB 本地嵌入模型）

| 能力 | 状态 |
|---|---|
| 来源自动发现（微信 4.x / QQ / Chrome / Edge / Safari） | ✅ |
| 文件登记与内容去重（inode 身份追踪） | ✅ |
| 正文解析（PDF / DOCX / Markdown / TXT） | ✅ |
| 中文全文检索（三路召回 + RRF 融合） | ✅ |
| 实时监听（新文件自动入库，约 3.5 秒） | ✅ |
| 来源管理（启用 / 停用 / 移除 / 手动添加） | ✅ |
| 向量检索（语义匹配，V1.5） | ✅ |
| 置信度标注（库里没有时如实提示） | ✅ |
| 文件消失处理（标 missing 保索引，重现自动复活） | ✅ |
| 保全副本（易失来源防微信清缓存，复制绝不移动） | ✅ |
| 信息层分类（虚拟树 + 规则回流学习，磁盘无目录） | ✅ |
| 增量嵌入（text_hash 内容寻址，改 1 片只编 1 片） | ✅ |
| LLM 问答与引用（B6，需 API 密钥） | ⬜ |

## 开发

```bash
# 后端
cd services/api
uv sync
uv run pytest                      # 61 项测试
uv run python tests/e2e_watch.py   # 端到端：投放文件 → 自动入库 → 搜内容

# 打包
uv run pyinstaller sidecar.spec --clean --noconfirm
cd ../../apps/desktop && npx electron-builder --mac --arm64
```

调试时用 `INKTABLE_DB=/tmp/dev.db` 指向独立库，避免污染真实数据。

## 架构

```
Electron 主进程 ──stdin: {token}──▶ Python sidecar (FastAPI)
      │            ◀──stdout: {port}      │
      │                                   ├─ discovery/  来源探测
   renderer                               ├─ watcher/    监听 + 稳定性判据
  (原生 JS)  ──HTTP + Bearer──────────────▶├─ parsing/    解析 + 分片
                                          ├─ index/      FTS5 三路检索
                                          └─ db/         SQLite
```

数据库固定在 `~/Library/Application Support/Inktable/library.db`。
不放资料库目录 —— 那里可能在 iCloud 里，多设备并发写会损坏。

**files 与 contents 分离**：N 个文件 : 1 份内容。同一份内容存在多处时
只解析一次、只建一次索引；任一副本存活，索引就保留。

## 实测记录

这一路踩到的坑都写在 `docs/M0-RESULTS.md`，其中影响设计决策的几条：

**FTS5 对中文默认零命中** —— `unicode61` 把整段汉字当作一个 token。
解法是三路召回，缺任何一路都会漏结果：

| 路 | 覆盖 | 失效场景 |
|---|---|---|
| jieba 全切分 | 正常成词 | 未登录词切错边界 |
| trigram | 编号、错别字、子串 | 少于 3 字的词 |
| LIKE 子串 | 前两路都失效时兜底 | 无（全表扫描） |

实测 jieba 把「汝窑天青釉」切成 `汝窑天/青釉`，于是查「汝窑」时前两路
同时失效 —— 这不是换分词器能解决的，统计分词必然在未登录词上出错。

**contentless FTS5 表默认不支持 DELETE** —— 必须建表时加
`contentless_delete=1`，否则索引只能进不能出，删来源后搜索仍命中
已消失的分片。这个缺陷只在写删除测试时才暴露。

**macOS FSEvents 对树外移入的文件报 created 而非 moved** ——
微信/QQ「临时目录写完再移入」这条主路径上 `moved_in` 恒为 False，
所以稳定性判据不能依赖事件类型，只能看文件自身可观测状态。

**微信 4.x 路径与网上资料（3.x）完全不同**：
`xwechat_files/<wxid>/msg/file/<YYYY-MM>/`，且一个 wxid 一个账号。
硬编码路径会静默失败，必须 glob + "目录里确有文档"筛选。

## 文档

- `docs/PLAN.md` —— 完整方案（v6）
- `docs/HANDOFF.md` —— 18 条硬约束，改动前必读
- `docs/M0-RESULTS.md` —— 实测结果与决策记录

## 已知限制

- 无应用图标，未代码签名（首次打开需右键→打开）
- 扫描件 PDF 无文本层，不做 OCR（会明确告知"未提取到文本"）
- 单文档全文索引上限 10 MB（机器生成的日志/清单不适合自然语言搜索）
- 源码类（`.py` `.js` `.json` 等）只登记元数据不解析正文 ——
  搜代码该用 ripgrep / IDE，不是文件管理器的职责
