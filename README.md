# Inktable

macOS 本地优先的个人知识库。自动发现微信、QQ、浏览器下载等目录，
把散落全机的资料变成可检索、可问答、可回溯证据的知识空间。

**默认不移动、不复制、不改名任何文件** —— 组织全部发生在索引层。

## 当前状态

**0.2.0 发布候选**（v7 知识库优先架构，见 `docs/RELEASE-0.2.0.md`）。

产品主体是统一三栏知识工作台：文件管理、文件详情与证据、知识问答同屏常驻，
搜索与问答共用同一条检索管线；文件管理负责来源发现、解析、去重、增量更新、
保全和治理。

72 题冻结评测（同日 RRF 对照）：Recall@5 **95.0%**，严格通过率 **90.0%**，
nDCG@10 84.0%，检索 p50 46ms；证据压缩保留率 95.0%、压缩率中位数 61.1%、
offset 往返零错误。

| 能力 | 状态 |
|---|---|
| 来源自动发现（微信 4.x / QQ / Chrome / Edge / Safari） | ✅ |
| 文件登记与内容去重（inode 身份追踪） | ✅ |
| 正文解析（PDF / DOCX / Markdown / TXT） | ✅ |
| 中文混合检索（jieba / trigram / 子串 / 向量 + RRF） | ✅ |
| Document / Section / Child 三层索引（影子构建 + 原子切换） | ✅ |
| 本地重排 v3（IDF 覆盖 / 邻近度 / 类型 / 文件名特征） | ✅ |
| 比较类问题子查询分解（"A 和 B 分别…"） | ✅ |
| 近重复治理（text_hash 去重 + 向量近重复软降权） | ✅ |
| 证据压缩（EvidenceSpan 原文区间，逐字节可回溯） | ✅ |
| 带引用问答（四条后置校验留痕，可审计） | ✅ |
| 检索 trace 与调试回读（`GET /runs/{trace_id}`） | ✅ |
| 实时监听（新文件自动入库，约 3.5 秒） | ✅ |
| 保全副本 / 文件消失处理 / 信息层分类 / 文件书 | ✅ |
| 增量嵌入（text_hash 内容寻址，改 1 片只编 1 片） | ✅ |
| Cross-Encoder 重排（nDCG 相对提升 15% 门槛） | ⬜ 下一版本 |
| 代码签名（需 Apple 开发者账号） | ⬜ |

## 开发

```bash
# 后端
cd services/api
uv sync
uv run pytest                      # 全量后端测试
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
      │                                   ├─ library/    来源、身份、监听、保全
   renderer                               ├─ ingestion/  解析、层级、增量索引
  (知识工作台) ──HTTP + Bearer────────────▶├─ retrieval/  混合召回、RRF、问答
                                          └─ db/         SQLite + FTS5 + sqlite-vec
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

- `docs/PLAN.md` —— 完整方案（v7，知识库优先），各里程碑执行状态在文内
- `docs/HANDOFF.md` —— 18 条硬约束，改动前必读
- `docs/M0-RESULTS.md` —— 实测结果与决策记录
- `docs/RELEASE-0.2.0.md` —— 当前版本发布说明
- `docs/eval/README.md` —— 72 题评测体系与各里程碑冻结结果

## AI 问答（B6）

设置 → AI 问答 里填 OpenAI 兼容接口的地址 / 模型名 / 密钥即可启用；
搜索框输入问题后按 **⌘↵** 提问。密钥经 Electron safeStorage 加密落盘，
sidecar 侧只存内存 —— 不进数据库、不进日志、不回显（72 项测试中有专项断言）。

生成后四条硬校验（prompt 是建议，校验才是执行）：
虚构引用剔除 → 零引用重生成一次 → 仍零引用降级为纯检索结果 →
拒答句夹带事实则截断。答案里的 [Cn] 可点击跳到引用，双击引用在 Finder 定位原文。

## 已知限制

- 无应用图标，未代码签名（首次打开需右键→打开）
- 扫描件 PDF 无文本层，不做 OCR（会明确告知"未提取到文本"）
- 单文档全文索引上限 10 MB（机器生成的日志/清单不适合自然语言搜索）
- 源码类（`.py` `.js` `.json` 等）只登记元数据不解析正文 ——
  搜代码该用 ripgrep / IDE，不是文件管理器的职责
