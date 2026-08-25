# Inktable

本地优先的个人知识库（macOS / Windows）。自动发现微信、QQ、浏览器下载
等目录，把散落全机的资料变成可检索、可问答、可回溯证据的知识空间。

**默认不移动、不复制、不改名任何文件** —— 组织全部发生在索引层。

## 当前状态

**0.3.0**（体验重构版，见 `docs/RELEASE-0.3.0.md`；架构见 0.2.0 说明）。
Windows 移植已完成并实测可用（发现/扫描/索引/向量/问答/实时监听全链路），
差异与运维备忘见 `docs/WINDOWS-PORT.md`。

三栏知识工作台：左栏是范围与**文件树**（真实目录逐层展开），中栏在
文件列表（按扩展名分组）与**全文查看器**之间整页切换，右栏常驻知识问答；
搜索与问答共用同一条检索管线。文件管理负责来源发现、解析、去重、
增量更新、保全和治理。

v8 Gold 合约共 77 题（46 answerable、7 metadata、12 corpus-missing、
12 unanswerable），含 96 个证据要求和 691 个精确原文 span。以下冻结指标均来自
Git 基线 `6a7ce93`、同一私有语料快照（29,434 chunks）；可复核原始产物为
`docs/eval/v8-final-local.json`、`v8-final-compress.json` 和 `v8-final-qa.json`，
不能与其他数据库快照或模型直接横比。生产默认 `local-static-v3` 在 53 个可评估
问题上 Recall@5 **94.3%**、MRR@10 **88.1%**、nDCG@10 **91.0%**、P95
2.67 秒。证据压缩的 Gold Evidence Recall 为 **99.0%**、完整案例 **97.8%**、
压缩率中位数 **62.0%**、P95 247.5 ms，offset 往返零错误。ONNX
Cross-Encoder 已完成对照，但没有在质量、召回和延迟的联合发布门槛上优于默认，
因此未切换生产配置；旧版“相对 nDCG +15%”门槛因高基线下数学上失真而已废止。

冻结的真实模型 QA 产物由 `kocode / gpt-5.6-sol` 跑完 65/65 个用例；这里的
“65/65”只表示评测流程完整、无 provider failure/degraded，并不表示每个 gold
证据都被引用。引用支持率 **95.16%** 是由同一模型对自身声明进行的自动判定，
不是独立人工裁决；精确引用格式与句级引用覆盖均为 **100%**，无依据问题正确
拒答率为 **100%**（12/12）。更严格且应优先阅读的端到端指标是 **Gold evidence
citation recall 68.93%（71/103）**；`A13`、`S17` 进入可审计 fallback。

| 能力 | 状态 |
|---|---|
| 来源自动发现（微信 4.x / QQ NT / 飞书 / 钉钉 / 企业微信 / 浏览器） | ✅ |
| 文件登记与内容去重（inode 身份追踪） | ✅ |
| 正文解析（PDF / DOCX / Markdown / TXT） | ✅ |
| 文件树导航 + 目录过滤 + 全文查看器（分页阅读） | ✅ |
| 列表按扩展名分组（服务端排序，组内最新在上） | ✅ |
| 停用来源即从视图隐藏（记录保留，重新启用恢复） | ✅ |
| 默认自动分类（按扩展名，不覆盖手动，可关闭） | ✅ |
| 中文混合检索（jieba / trigram / 子串 / 向量 + RRF） | ✅ |
| 语义嵌入经本机 Ollama bge-m3（1024 维；未装自动降级关键词检索） | ✅ |
| Document / Section / Child 三层索引（影子构建 + 原子切换） | ✅ |
| 本地重排 v3（IDF 覆盖 / 邻近度 / 类型 / 文件名特征） | ✅ |
| 近重复治理 / 比较类子查询分解 / 证据压缩（可回溯） | ✅ |
| 带引用问答（四条后置校验留痕，可审计） | ✅ |
| 意图路由（通用问题直接答，界面标注来源） | ✅ |
| 多轮对话浓缩 + 拒答自动重试 + 句级自动归因 | ✅ |
| 文件删除（库内移除 / 移到废纸篓，绝不直接抹除） | ✅ |
| 语义向量存量补齐（`/index/embed_backfill`，幂等分批） | ✅ |
| 设置三栏：通用（缩放/主题/自动归类）/ 来源 / 模型 | ✅ |
| 数据目录迁移（停库搬迁 + 路径重写 + 自动重启） | ✅ |
| cc-switch 供应商导入 + 真实补全连接检测 | ✅ |
| 实时监听（新文件自动入库，约 3.5 秒） | ✅ |
| 保全副本 / 文件消失处理 / 文件书 | ✅ |
| Windows 移植（真实系统目录/微信 QQ 自定义路径发现、整盘收录、全盘发现、窗口控件叠加） | ✅ |
| ONNX Cross-Encoder 重排 | 🧪 已实装并冻结对照；未通过“nDCG@10 ≥90%、Recall@5 ≥95%、Recall@20 回退 ≤2pp、Rerank P95 ≤1.5s”的联合门槛 |
| 级联重排（本地打分器 + CE 精排融合前 26 位） | 🧪 可选 `INKTABLE_RERANKER=cascade`：冻结快照 Recall@5 96.2% / Recall@20 100%，但 Rerank P95 超 1.5s 门槛约 10% |
| 检索延迟门槛（搜索 P95 ≤2.5s、Rerank P95 ≤1.5s） | ✅ 0.98s / 0.03s，见 `docs/RETRIEVAL-PERF.md` |
| 真实模型 65 题 QA 冻结快照 | ✅ `v8-final-qa.json`：`kocode / gpt-5.6-sol` 流程完成 65/65；同模型自判支持率 95.16%，精确引用 100%，拒答 12/12；Gold evidence citation recall **68.93%** |
| 代码签名（macOS / Windows） | ⬜ |

## 开发

```bash
# 后端
cd services/api
uv sync
uv run pytest                      # 全量后端测试
uv run python tests/e2e_watch.py   # 端到端：投放文件 → 自动入库 → 搜内容

# 开发桌面端（直接运行 services/api 源码，不需要先打包 sidecar）
cd ../../apps/desktop && npm install && npm start
```

```powershell
# Windows 开发启动：Electron 直接拉起 services/api/.venv 中的 Python 源码
# Ollama 端口等见 docs/WINDOWS-PORT.md
cd services\api ; uv sync
cd ..\..\apps\desktop ; npm install ; npm start

# 最终发布前才冻结 sidecar 并生成 Windows x64 NSIS 安装包
cd ..\..\services\api ; uv run --group dev pyinstaller sidecar.spec --clean --noconfirm
cd ..\..\apps\desktop ; npm run dist -- --win --x64
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

数据目录默认在 `~/Library/Application Support/Inktable/`（Windows 为
`C:\Users\<用户>\Library\Application Support\Inktable\`），可在
设置 → 通用配置 → 数据位置整体迁移（经 `INKTABLE_DATA_DIR` 传入 sidecar）。
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
- `docs/RETRIEVAL-PERF.md` —— 检索延迟实测与决策（含 sqlite-vec 影子表整块读，
  改动 `app/index/vector.py` 前必读）
- `docs/RELEASE-0.3.0.md` —— 当前版本发布说明（体验重构）
- `docs/RELEASE-0.2.0.md` —— 0.2.0 发布说明（检索与问答架构）
- `docs/WINDOWS-PORT.md` —— Windows 移植记录（平台差异、收录规则、并发修复、运维备忘）
- `docs/eval/README.md` —— 77 题 Gold 合约、v8 评测口径与历史冻结结果

## AI 问答（B6）

设置 → 模型配置 里填 OpenAI 兼容接口的地址 / 模型名 / 密钥即可启用，
也可从本机 cc-switch 一键导入供应商（分组展示、选中高亮）；「检测连接」
是真实补全测试，成功会显示模型实际回复与耗时。右栏输入框回车提问。
密钥经 Electron safeStorage 加密落盘，sidecar 侧只存内存 ——
不进数据库、不进日志、不回显（测试中有专项断言）。

生成后四条硬校验（prompt 是建议，校验才是执行）：
虚构引用剔除 → 零引用重生成一次 → 仍零引用降级为纯检索结果 →
拒答句夹带事实则截断。答案里的 [Cn] 可点击跳到引用，双击引用在 Finder 定位原文。

## 已知限制

- 无应用图标，未代码签名（首次打开需右键→打开）
- 扫描件 PDF 无文本层使用本机系统 OCR（macOS Vision / Windows
  Windows.Media.Ocr）；没有对应系统语言包时会明确告知"未提取到文本"
- 单文档全文索引上限 10 MB（机器生成的日志/清单不适合自然语言搜索）
- 入库白名单：`.txt` `.docx` `.pdf` `.md` `.csv` `.html` `.htm`，
  全部解析正文并参与检索问答；`.doc`、Excel、图片、音视频、压缩包、
  源码、配置、安装包和日志均不入库
- Windows 上浏览器自定义下载目录的自动发现未移植（整盘来源已兜底）
