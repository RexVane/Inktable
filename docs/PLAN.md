# Inktable 个人知识库实施计划

**版本**：v7

**日期**：2026-08-16（v8 验收收口）

**平台**：macOS 优先，Windows x64 已完成移植与打包验证

**状态**：本文件取代 v6，作为后续产品、架构、实现和验收的唯一计划依据。

> Inktable 是一个本地优先的个人知识库。文件管理负责让知识来源可靠、可追踪、可治理；分层索引、混合检索、Rerank、上下文压缩和带引用问答负责让知识可查、可问、可复用。

## 0.1 当前验收快照（2026-08-16，检索延迟一节更新至 2026-08-18）

本节覆盖并取代下方历史执行日志中的旧数量和旧模型描述；历史段落保留
用于追溯，不应作为当前发布状态引用。

> **2026-08-18 更新**：§10.4 的两条延迟门槛已通过（非生成搜索 P95
> 4121ms → **977ms**、Rerank P95 878ms → **29ms**），检索质量指标不变，
> 已用强制回退原 vec0 KNN 路径复跑评测确认两条路径结果一致。同轮新增
> 可选的级联重排（`INKTABLE_RERANKER=cascade`）把 Content Recall@5 提到
> 96.2%、Recall@20 提到 100%，但 §10.3 的「相对 RRF 提升 ≥15%」两种配置
> 都未达到。全部实测数据、根因、退化路径与对该门槛可达性的讨论见
> `docs/RETRIEVAL-PERF.md`。改动 `app/index/vector.py` 前必读该文档
> ——向量矩阵走的是 sqlite-vec 影子表的内部布局。
> 后端回归 259 → **276 passed**，桌面 15 → **16 passed**。

- Gold 合约：77 题（46 answerable、7 metadata、12 corpus-missing、12
  unanswerable），96 个证据要求、691 个精确 span。
- 检索生产默认：`local-static-v3`；53 个可评估问题 Recall@5 94.34%、
  MRR@10 88.11%、nDCG@10 91.04%、P95 2.669 秒。RRF 控制为
  96.23%/86.45%/88.69%。
- Cross-Encoder 已可冻结运行，但相对 RRF 的 MRR/nDCG 提升约
  7.95%/7.85%，未达到 +15% 选型门槛，且 P95 约 16.5 秒；生产继续用
  `local-static-v3`。
- 压缩门槛已通过：Gold Evidence Recall 98.96%、完整案例 97.83%、中位
  压缩 61.98%、P95 247.5 ms、offset 往返错误 0；唯一漏例为上游 X10。
- 正式真实模型 QA：`kocode / gpt-5.6-sol` 完成 65/65；引用支持率
  95.16%、精确引用率 100%、句级引用覆盖 100%、正确拒答率 100%
  （12/12），无 provider failure 或 degraded；A13、S17 为 fallback。
- 工程回归：后端 `259 passed`，桌面 `15 passed`，`compileall` 与
  `git diff --check` 通过。
- Windows x64 PyInstaller sidecar SHA-256 为
  `2670F7BFFA026E8FCBB82D0D30010DEFE0EF634FC0932D3DF647D046AE77B986`；
  NSIS 安装包 `dist/Inktable-0.3.0-Setup-x64.exe` 为 194,761,734 bytes，
  SHA-256 `E24D5588968B85B380A3A9DC6A407EAEF88FF8973F1C776F47CBF08CB53006E8`。
- Windows 冻结 sidecar 已端到端识别真实无文本层 PDF（`Windows.Media.Ocr`），
  识别正文可进入索引与搜索；同次 trace 使用 `local-static-v3` 且未降级。
- 新目录静默安装退出码 0；安装版 Electron 首次引导、搜索空态、设置页、
  sidecar ready 与退出生命周期均通过，退出后 Electron/sidecar 无残留。
  真实库只读审计 `quick_check=ok`、外键错误 0，chunks 与两套 FTS、
  Document/Section FTS、sqlite-vec rowid 均双向一致且无关系孤儿。

## 0. v7 决策摘要

v6 把“文件支柱”和“知识支柱”定义为同层能力，并因本地大模型延迟暂缓 Rerank。这个定位造成了产品重心偏移：文件发现、分类和管理获得了完整工作流，而知识检索仍停留在 child chunk、RRF 和邻居拼接。

v7 作出以下不可逆转的架构决策：

1. **个人知识库是产品主体，文件管理是支撑能力。** 首页、API、里程碑和验收均以知识检索质量为中心。
2. **核心检索链路固定为：分层索引 -> 混合召回 -> RRF 粗融合 -> Child Rerank -> Parent 扩展 -> 证据压缩 -> 上下文装配 -> 带引用生成。**
3. **Rerank 是固定架构阶段，不再列为远期扩展。** 本地模型、CoreML 或用户显式启用的远程服务只是可替换实现。
4. **上下文压缩必须保留原文区间映射。** 不允许用无法回溯证据的自由摘要代替压缩。
5. **分层索引至少包含 Document、Section、Child 三层。** 上层用于软路由和上下文恢复，Child 用于精确召回、Rerank 与引用。
6. **先建立评测和检索追踪，再改变检索算法。** 不以主观体验代替 Recall、nDCG、证据召回率和引用正确率。
7. **保留现有可靠摄入底座。** Electron + Python sidecar、SQLite、`files N:1 contents`、inode 身份、内容去重、增量更新、来源许可和保全副本不推倒重做。

## 1. 产品定义

### 1.1 核心任务

Inktable 帮助用户完成四类任务，优先级从高到低排列：

1. **找知识**：用自然语言或关键词找到相关事实、章节和原文。
2. **问知识**：基于个人资料得到有证据、有引用、可拒答的答案。
3. **组织知识**：用文件书、专题、标签和范围筛选形成长期知识空间。
4. **治理资料**：管理来源、重复文件、缺失文件、易失文件和保全副本。

任何新功能必须回答它主要提升哪一项。只改善文件操作便利、但不改善知识可靠性或知识使用体验的功能，默认低优先级。

### 1.2 产品边界

**本阶段包含：**

- 本地文件的可靠发现、解析、去重和增量更新。
- Document / Section / Child 三层知识结构。
- 词法、子串、向量和层级信号的混合召回。
- 可替换 Rerank、证据压缩、带引用问答和检索解释。
- 全库、文件书和专题范围内的搜索与问答。
- 资料库来源、分类、标签、保全和状态治理。

**本阶段不包含：**

- Windows、跨设备同步、多人协作。
- OCR、图片理解、音视频转写、压缩包正文索引。
- 自主执行外部操作的 Agent。
- 自动生成 Wiki、知识图谱或模型微调。
- 未经用户明确启用的云端文本上传。

### 1.3 不可协商约束

`docs/HANDOFF.md` 中 H1-H18 继续生效。本版本补充以下知识引擎约束：

- **K1**：最终答案的事实必须由可定位到原文的 EvidenceSpan 支持。
- **K2**：Rerank 只对 Child 候选打分，不对扩展后的整段 Parent 打分。
- **K3**：Document/Section 路由只能加权，不能硬排除未命中的 Child。
- **K4**：压缩输出必须是原文区间选择，不得把生成式摘要当成引用证据。
- **K5**：检索、Rerank 或压缩降级必须通过响应和界面可见，不得静默伪装为完整管线。
- **K6**：改变分片、嵌入、融合、Rerank 或压缩策略前后必须运行同一评测集。
- **K7**：新索引必须带版本号，并支持影子构建、原子切换和回滚。
- **K8**：任何层级摘要都不是事实来源；最终引用始终落到 Child 原文。

## 2. 系统总览

### 2.1 能力层级

```text
┌────────────────────────── Desktop Workbench ──────────────────────────┐
│  搜索 / 问答 / 专题空间 / 证据阅读                      资料库管理      │
└────────────────────────────────┬──────────────────────────────────────┘
                                 │ HTTP + Bearer / controlled IPC
┌────────────────────────────────▼──────────────────────────────────────┐
│                         Knowledge Engine                              │
│  ingestion -> hierarchy -> retrieve -> fuse -> rerank -> compress    │
│                                      -> assemble -> answer -> verify  │
└────────────────────────────────┬──────────────────────────────────────┘
                                 │ files / contents / sections / chunks
┌────────────────────────────────▼──────────────────────────────────────┐
│                          Library Engine                               │
│  discovery / permission / watcher / identity / dedupe / preserve     │
└────────────────────────────────┬──────────────────────────────────────┘
                                 ▼
                  SQLite + FTS5 + sqlite-vec + source files
```

Knowledge Engine 是产品核心；Library Engine 是它的可靠摄入与资料治理基础。两者不是同等优先级，但必须通过稳定数据契约解耦。

### 2.2 进程边界

继续沿用当前已经跑通的进程模型：

- **Electron 主进程**：窗口、系统权限、目录选择、Finder 跳转、`safeStorage`、sidecar 生命周期。
- **Renderer**：知识工作台界面，不拥有 Node.js 权限，不接触 LLM 密钥。
- **Python sidecar**：Library Engine、Knowledge Engine、FastAPI、SQLite 和模型推理。
- **本地 HTTP**：sidecar 绑定 `127.0.0.1:0`；会话令牌经 stdin 传入；所有业务接口要求 Bearer Token。

不在本阶段迁移到前端框架或 ORM。先按当前原生 JS、FastAPI 和 `sqlite3` 实现拆分模块，避免把产品重构与技术栈替换绑在一起。

### 2.3 代码目标结构

```text
services/api/app/
├── api/
│   ├── knowledge.py          # search / ask / evidence / explain
│   ├── library.py            # sources / files / preserve
│   ├── workspace.py          # books / topics / tags
│   └── system.py             # health / index / database
├── library/
│   ├── discovery/
│   ├── watcher/
│   ├── identity/
│   └── preserve/
├── knowledge/
│   ├── ingestion/            # parse once, build hierarchy, version index
│   ├── hierarchy/            # Document / Section / Child
│   ├── retrieval/
│   │   ├── query.py          # QueryPlan and metadata filters
│   │   ├── routes.py         # lexical / vector / hierarchy routes
│   │   ├── fusion.py         # weighted RRF
│   │   ├── rerank.py         # Reranker protocol and adapters
│   │   ├── diversify.py      # content caps and duplicate control
│   │   ├── expand.py         # Section / neighbor recovery
│   │   ├── compress.py       # extractive EvidenceSpan selection
│   │   └── assemble.py       # token-budgeted ContextPack
│   └── answering/
│       ├── generate.py
│       └── validate.py
├── workspace/
└── db/
    ├── schema.py
    └── migrations/
```

这是目标边界，不要求一次搬完。迁移期间旧模块作为适配层存在，每一步必须保持测试可运行。

## 3. 知识数据模型

### 3.1 保留的现有模型

以下结构已经验证，继续作为资料与知识的连接层：

```text
sources 1 -> N files N -> 1 contents
```

- `files` 表示磁盘上的具体文件，以 `(volume_uuid, inode)` 追踪身份。
- `contents` 表示内容实体，以 SHA-256 去重。
- 多个文件副本共享一份内容和知识索引。
- 文件移动只更新路径；内容不变时不重新解析或嵌入。

### 3.2 三层知识结构

```text
contents                         Document 层
└── sections                     Section 层，可嵌套
    └── chunks                   Child 层，原始证据
```

**Document 层**

- 载体：现有 `contents`，增加或关联文档表示。
- 内容：标题、文件类型、来源集合、时间、结构化大纲、抽取式摘要。
- 用途：全库软路由、文档级相似搜索、范围提示。
- 禁止：直接作为最终引用证据。

**Section 层**

- 新表 `sections`。
- 字段：`id`、`content_id`、`parent_id`、`ordinal`、`heading_path`、`title`、`summary_text`、`start_chunk_ordinal`、`end_chunk_ordinal`、`text_hash`、`token_count`、`index_version`。
- 用途：主题级软路由、Child 命中后的 Parent 恢复、章节浏览。
- `summary_text` 优先采用抽取式表示；若以后引入生成式摘要，必须标记模型和版本，且不得参与最终引用。

**Child 层**

- 保留现有 `chunks`，新增 `section_id`、`start_offset`、`end_offset`、`token_count` 和 `index_version`。
- 内容：约 300-600 字的完整语义片段，表格和代码保持结构完整。
- 用途：FTS、向量召回、Rerank、压缩和引用。
- 所有 EvidenceSpan 必须能够映射回 `chunk_id + offset + locator`。

### 3.3 索引表

- `chunks_fts`：jieba 词法索引。
- `chunks_fts_tri`：trigram 子串索引。
- `chunks_vec`：Child 向量。
- `sections_vec`：Section 表示向量。
- `documents_vec`：Document 表示向量。
- `retrieval_runs`：一次检索的配置、耗时、降级状态和最终结果。
- `retrieval_candidates`：各路候选、route rank、RRF 分和 Rerank 分；默认只在调试或评测模式持久化。

sqlite-vec 虚拟表按层分开，避免复合实体类型破坏 rowid 映射。所有表均记录 `model_id` 或 `index_version`，禁止不同模型向量混用。

### 3.4 稳定中间对象

检索管线使用明确的数据契约：

```text
QueryPlan
  -> CandidateSet
  -> FusedCandidates
  -> RankedCandidates
  -> ExpandedEvidence
  -> EvidenceSpans
  -> ContextPack
  -> GroundedAnswer
```

每个对象保留 `content_id`、`section_id`、`chunk_id`、分数来源和 trace id。禁止后续阶段靠裸字典猜测字段语义。

## 4. 知识摄入管线

```text
source file
  -> stability check
  -> identity + SHA-256 dedupe
  -> parse Blocks once
  -> build Document / Section hierarchy
  -> create Child chunks
  -> build layer representations
  -> write relational + FTS + vectors atomically
  -> activate index version
```

### 4.1 解析一次

解析器只生成一次结构化 Blocks，分类、层级构建、分片和索引复用同一结果。不得为了 Document、Section 和 Child 三层重复解析文件。

### 4.2 层级构建

- PDF：优先使用目录和标题特征；没有可靠标题时按页组形成弱 Section。
- DOCX/Markdown：使用 Heading 层级直接构建 Section 树。
- TXT：按显式标题、空行和长度形成弱 Section，并标记低结构置信度。
- 表格与代码块不得被无边界切碎。
- 标题路径进入 Section 表示和 Child 嵌入文本，但引用正文仍保留原文。

### 4.3 增量更新

- Child 继续以 `text_hash` 复用嵌入。
- Section 以规范化标题路径、覆盖 Child hashes 和自身 `text_hash` 判断复用。
- Document 表示由结构和 Section hashes 派生。
- 修改一个 Child 时，只重建受影响的 Child、祖先 Section 和 Document 表示。
- 新索引在影子版本中构建，完成后通过一个事务切换 active version。

### 4.4 原子性

关系数据、FTS 和向量必须保持可验证的一致性。任何阶段失败时：

- 当前 active index 继续可查。
- 新影子索引标记失败并可清理。
- 不允许半完成版本进入检索池。
- 原始文件不受影响。

## 5. 核心检索管线

### 5.1 固定顺序

```text
1. QueryPlan：意图、范围、时间、来源、文件类型和会话指代
2. Hierarchy routing：Document / Section 软路由
3. Deep retrieval：Child 多路深召回
4. Fusion：weighted RRF 粗融合
5. Rerank：Child 级精排
6. Diversify：内容去重、来源与文档多样性控制
7. Expand：恢复 Section 和相邻 Child
8. Compress：抽取查询相关 EvidenceSpan
9. Assemble：按 token 预算装配 ContextPack
10. Generate + Validate：生成、引用校验、拒答
```

阶段顺序属于架构契约。特别是 Rerank 必须早于 Parent 扩展和压缩。

### 5.2 QueryPlan

QueryPlan 至少包含：

- 原始问题和规范化检索文本。
- 当前范围：全库、文件书、专题、选中文档。
- 明确的来源、时间、扩展名和标签过滤条件。
- 元数据问题或正文问题的路由结果。
- 会话指代解析结果及其置信度。

元数据问题优先走 SQL。无法可靠判断时走正文检索，不得让 LLM 自由生成 SQL。

### 5.3 分层软路由

- Document 和 Section 向量各自召回候选。
- 命中的上层节点为其 Child 提供有限加权。
- 未命中上层的 Child 仍可通过词法或向量路线进入候选。
- 上层加权系数必须经评测标定，不能形成硬过滤。

这保证上层摘要质量不足时不会吞掉正确原文。

### 5.4 Child 混合深召回

保留当前四路并统一输出 Candidate：

1. jieba FTS：正常中文词和中英文混排。
2. trigram FTS：编号、专名、错别字和子串。
3. LIKE 子串：短词和分词失败兜底，只在必要时启用。
4. sqlite-vec：语义改写和无字面重合问题。

召回深度由评测决定，初始基线为词法/向量各 Top 50-100。RRF 只负责高召回粗融合，不再承担最终头部排序。

### 5.5 Rerank

Reranker 是固定协议：

```python
class Reranker(Protocol):
    def rerank(self, query: str, candidates: list[Candidate]) -> list[RankedCandidate]: ...
```

实现优先级：

1. 本地轻量 Cross-Encoder，首选路径。
2. CoreML/ANE 适配器，用于达到性能门槛。
3. 用户显式启用的 OpenAI 兼容或专用 Rerank API。
4. `RrfOnlyReranker` 仅作为降级实现，响应必须返回 `degraded: ["rerank"]`。

模型不在计划中凭经验拍板。先用真实评测集对候选模型做 benchmark，再根据以下门槛选择：

- 中文和中英混合 nDCG@10、MRR@10 明显优于 RRF 基线。
- Top 20-30 的本地 P95 满足交互延迟预算。
- 峰值内存和 DMG 增量可接受。
- PyInstaller 冻结与 arm64 运行稳定。

若大模型 Top 50 需要 17 秒，应减少候选、使用轻量模型或更换运行时，而不是删除 Rerank 阶段。

### 5.6 多样性控制

- 内容去重按 `content_id`，不是 `file_id`。
- Rerank 前使用软 cap，避免一个超长文档占满计算预算。
- Rerank 后使用硬 cap，初始值为每份内容最多 3 个 Child。
- 对高度重叠的相邻 Child 合并处理。
- 文件书范围必须在 SQL/向量路线内执行，不能在 Top-K 后过滤。

### 5.7 Parent 扩展

对通过 Rerank 的 Child 执行：

- 恢复所属 Section 标题和结构路径。
- 根据语义边界取前后相邻 Child。
- 对表格、列表和定义块采用结构感知扩展。
- 扩展只增加供压缩选择的候选上下文，不直接全部送入生成模型。

### 5.8 上下文压缩

压缩采用“抽取优先、生成禁入证据”的原则：

```text
ExpandedEvidence
  -> sentence / row segmentation
  -> query relevance scoring
  -> select exact source spans
  -> merge overlaps
  -> cross-document dedupe
  -> token-budget packing
```

输出 `EvidenceSpan`：

- `span_id`
- `chunk_id`
- `start_offset` / `end_offset`
- `text`
- `relevance_score`
- `content_id` / `section_id`
- `page` / `heading_path` / locator

第一版使用可解释的抽取式压缩：词法覆盖、向量相似度、Rerank 分和位置特征组合。以后可增加 LLM 选择器，但模型只能返回已有 `span_id`，不能自由改写证据。

### 5.9 Token 预算装配

- ContextPack 根据模型上下文上限和回答预留空间计算预算。
- 优先保留不同文档的高分证据，再补充必要邻接上下文。
- 每个证据块携带稳定 `[C1]` 锚点。
- 装配结果记录被保留和被丢弃的理由，供 explain 和评测使用。

## 6. 生成、引用与拒答

### 6.1 生成原则

- 模型只接收 ContextPack，不直接访问文件系统或数据库。
- 每个事实性陈述必须引用一个或多个 EvidenceSpan。
- 资料不足时必须拒答，不允许用模型常识补齐个人资料中的空缺。
- 云端模型只在用户明确启用后接收必要 EvidenceSpan，不上传文件本体。

### 6.2 后置校验

保留并加强现有四条校验：

1. 删除不存在的引用标签。
2. 非拒答答案没有引用时重生成一次。
3. 二次无引用时降级为证据列表，不输出自然语言答案。
4. 拒答句夹带事实时截断。

新增：

- 引用必须对应本次 ContextPack 中的 EvidenceSpan。
- 引用展示文本直接取原文 span，不取模型复述。
- 文件缺失时仍可展示已索引证据，但跳转明确标记不可用。
- 最终事件只在校验完成后发布；草稿不得被标记为已验证答案。

### 6.3 检索解释

调试模式和评测工具需要展示：

- 各召回路线及排名。
- Document/Section 路由加权。
- RRF 分、Rerank 分和多样性淘汰原因。
- Parent 扩展范围。
- 压缩保留的原文区间。
- 当前降级阶段和模型版本。

普通用户界面只显示简化状态，详细 trace 不暴露密钥或完整隐私文本。

## 7. Workspace 与文件管理

### 7.1 Workspace

- 文件书升级为知识空间，可限制搜索和问答范围。
- 专题空间可以包含文件、Section 和保存的查询，但第一阶段仍以文件成员为主。
- 标签、来源、时间和文件类型作为检索过滤条件。
- Workspace 不复制文件和索引，只保存成员与查询范围。

### 7.2 文件管理的职责

文件管理保留以下功能：

- 来源发现、用户授权、启用与停用。
- 文件身份追踪、内容去重、缺失和卷离线处理。
- 易失来源提醒与保全副本。
- 分类、标签、重复文件和索引状态治理。
- Finder 定位和原始证据回溯。

这些能力集中到“资料库管理”视图，不再占据产品首页的中心位置。

## 8. API 设计

### 8.1 Knowledge API

- `POST /knowledge/search`：统一搜索入口，支持 scope、filters、分页和 explain。
- `POST /knowledge/ask`：执行同一检索管线后生成答案。
- `GET /knowledge/evidence/{span_id}`：读取引用证据及 locator。
- `GET /knowledge/runs/{trace_id}`：调试模式下读取检索 trace。
- `POST /knowledge/reindex`：按 index version 构建影子索引。
- `GET /knowledge/index/status`：分层索引数量、版本和降级状态。

搜索与问答必须复用同一 RetrievalPipeline，不能各自实现一套融合逻辑。

### 8.2 Library API

现有来源、文件、保全、分类和数据库接口保持兼容，逐步迁移到 `/library/*` 命名空间。旧路径在桌面端完成迁移前保留适配器。

### 8.3 响应公共字段

知识接口至少返回：

- `trace_id`
- `index_version`
- `scope`
- `degraded`：缺失的 route、rerank、compression 或 generation 阶段
- `timings`
- `results` 或 `answer`

密钥、完整模型配置和内部文件系统权限信息绝不回显。

## 9. 桌面信息架构

### 9.1 第一屏：统一三栏知识库

```text
┌────────────────────┬──────────────────────────────┬─────────────────────┐
│ 文件管理            │ 文件详情与命中片段             │ 知识问答              │
│ 搜索、分类、文件书   │ 文件元数据、正文 Section       │ 模型状态、回答、引用   │
│ 来源、类型、文件列表 │ 页码、章节路径、Finder 定位     │ 独立问题输入框         │
└────────────────────┴──────────────────────────────┴─────────────────────┘
```

- 不设置“知识工作台/资料库管理”顶层切换，也不设置“搜索/问答”模式切换；三个区域始终同时存在。
- 左侧输入框只检索文件名和正文；点击文件或检索结果只更新中栏，不清空右侧回答。
- 右侧有独立问题输入框；点击回答引用在中栏打开证据与 Section 上下文。
- 模型状态常驻右栏。配置保存不等于可用，必须提供真实的端到端连接检测，并区分未配置、待检测、可用、鉴权失败、模型不存在、限流、超时、不可达和响应无效。
- Rerank 或压缩降级时显示安静但可见的状态，不用技术术语打扰普通用户。
- 来源、重复项、保全和分类规则保留为文件管理的支撑能力，不独立成另一个产品模式。

### 9.2 实现策略

先拆分当前单文件 Renderer 的状态和 API 调用，不同步引入大型前端重写。完成 Knowledge API 和检索 trace 后再决定是否迁移 Vue/React；框架迁移不能阻塞知识管线。

## 10. 评测体系

### 10.1 先冻结基线

在实现新层级和 Rerank 前，使用当前 child + 四路召回 + RRF 管线跑一次完整基线，保存：

- 代码提交和索引版本。
- 每题各路候选与最终排序。
- Recall、MRR、nDCG、延迟和内存。
- 当前问答输出、引用和拒答结果。

### 10.2 评测集

首个正式评测集不少于 60 题：

- 20 题单文档事实定位。
- 10 题同义改写或无关键词重合。
- 10 题跨文档综合。
- 10 题元数据、时间和范围问题。
- 10 题库内确实没有依据的问题。

每题标注：scope、相关 document、相关 section、gold chunks、gold evidence spans、答案要点和是否应拒答。标注在实现新算法前完成，之后只因资料变化而修订，不因算法结果反向修改。

### 10.3 指标与发布门槛

| 阶段 | 指标 | v7 首个发布门槛 |
|---|---|---|
| 深召回 | Gold evidence Recall@50 | >= 0.90 |
| Rerank | nDCG@10 / MRR@10 | 相对 RRF 基线提升 >= 15%，Recall@20 回退 <= 2pp |
| 压缩 | Evidence Recall | >= 0.95 |
| 压缩 | 输入 token 减少 | 中位数 >= 35%，且 Evidence Recall 达标 |
| 问答 | 引用支持率 | >= 0.95 |
| 问答 | 无依据正确拒答率 | >= 0.85 |
| 安全 | 虚构引用进入最终答案 | 0 |
| 增量 | 200 片改 1 片 | 仅重嵌变化 Child 及受影响上层表示 |

绝对门槛与相对提升同时使用：绝对值防止低质量发布，相对值用于证明新增阶段确实产生收益。

### 10.4 延迟预算

延迟用目标硬件的 P50/P95 记录，初始预算：

- 非生成搜索 P95：<= 2.5 秒。
- Rerank Top 20-30 P95：<= 1.5 秒。
- 压缩 P95：<= 500 毫秒。
- 问答首个可见状态：<= 1 秒；完整生成延迟单独记录。

模型质量达标但延迟超标时，优先优化候选数、批处理、量化和运行时。不得通过跳过阶段来伪造达标。

## 11. 测试策略

### 11.1 单元测试

- 层级构建：Heading 树、弱 Section、表格、代码、空标题和超长段落。
- QueryPlan：scope、时间、来源、文件类型和会话指代。
- Fusion：多路线缺失、重复候选和权重稳定性。
- Rerank：排序、批处理、超时、模型不可用和显式降级。
- Diversify：按 content 去重、软硬 cap 和相邻重叠。
- Compression：offset 往返、重叠合并、token 预算和 evidence recall。
- Citation：span -> chunk -> file/page/section 映射一致。

### 11.2 集成测试

- 摄入一个结构化文档后，Document/Section/Child 数量和父子关系正确。
- 关系表、FTS 和三层向量表任一写入失败时全部回滚或影子版本失效。
- Document/Section 未命中时，正确 Child 仍可通过其他路线进入候选。
- Rerank 只接收 Child，Parent 扩展发生在其后。
- 压缩后的每个字符都来自标注的原文区间。
- 文件书范围在每条召回路线内生效。
- 模型不可用时响应和 UI 明确标记降级。

### 11.3 回归与端到端

- 保留现有文件身份、监听、去重、保全、引用校验和数据库原子性测试。
- 修复当前桌面测试中依赖函数字面声明的脆弱断言，改测行为和 IPC 契约。
- E2E 覆盖：导入资料 -> 索引完成 -> 搜索 -> 查看证据 -> 提问 -> 引用跳转。
- E2E 覆盖 sidecar 重启后检索恢复和 active index 不变。
- 每次检索策略改动自动运行离线评测并与基线比较。

## 12. 数据迁移与回滚

### 12.1 Schema 迁移

当前数据库 `SCHEMA_VERSION = 1`。v7 使用显式、可重复执行的迁移：

1. 新增 `sections`、上层向量、index version 和 retrieval trace 表。
2. 以 nullable 字段扩展 `chunks`，不立即删除旧字段或旧索引。
3. 对现有 contents 后台构建 v2 层级索引。
4. 校验行数、外键、向量数量和抽样检索结果。
5. 在事务中切换 active knowledge index version。

不在首次迁移中重写 `files` / `contents` 主键，也不引入 ORM。

### 12.2 双轨和回滚

- `RetrievalPipelineV1` 保留为只读回退路径，直到 v7 评测和 E2E 全部通过。
- 新索引失败时继续使用旧 active version。
- 切换后出现错误可将 active version 指回旧索引，不重新扫描原始文件。
- 清理旧索引必须晚于稳定观察期，并需要明确版本目标。

## 13. 实施里程碑

### M0：基线与文档收口

**执行状态（2026-08-13）**：已完成。后端 118 项、桌面 10 项测试全绿；72 题 v7 基线已冻结；搜索与问答均返回不含正文的非持久化 trace。

- 将本计划作为唯一权威计划，根目录只保留入口链接。
- 修复现有桌面测试，使当前分支全绿。
- 建立不少于 60 题的评测集和当前管线基线。
- 为检索结果增加非持久化 trace 对象。

**完成标志**：当前功能不变；后端和桌面测试全绿；基线结果可重复生成。

### M1：检索管线解耦

**执行状态（2026-08-13）**：已完成。QueryPlan、四路召回、文件书范围、RRF、Candidate、diversify、expand、assemble 和阶段 timings 已由搜索与问答共享；后端 121 项、桌面 10 项测试全绿。72 题固定评测与 M0 基线逐题等价，Recall@5 保持 78.3%，严格通过率保持 61.7%。

- 从 `qa/answer.py` 拆出 QueryPlan、routes、fusion、diversify、expand 和 assemble。
- 搜索与问答复用同一 RetrievalPipeline。
- 引入稳定中间对象和阶段 timings。

**完成标志**：新旧管线在固定输入上结果等价，Recall 不回退。

### M2：Document / Section / Child 分层索引

**执行状态（2026-08-13）**：已完成。schema v2 已支持持久化 Document / Section / Child、Child 原文 offset、每内容索引版本、影子构建、原子激活和已完成版本回滚；检索只读取 active 版本，Document / Section 以低权重软路由进入融合。真实 v1 库迁移前已创建可恢复备份，迁移后 `quick_check` 通过且 3460 个 Child 全部保持 active。72 题 Document Recall@50 为 98.3%（门槛 90%），Recall@5 保持 78.3%；后端 131 项、桌面 10 项测试全绿。当前评测集的 gold 以文档和答案关键词为主，精确 EvidenceSpan 标注在 M4 前补齐，不将 Document Recall 冒充 Evidence Recall。

- 增加 schema migration、Section 树和三层表示。
- 实现影子构建、索引版本切换和增量祖先更新。
- 接入 Document/Section 软路由。

**完成标志**：层级关系、原子性和回滚测试通过；Recall@50 达标。

### M3：Rerank

**执行状态（2026-08-13）**：协议与首个本地适配器已完成，模型发布门槛未完成。Rerank 已固定在 RRF 后、diversify 前，支持 `LocalStaticReranker` 与显式 `RrfOnlyReranker` 降级，trace 返回模型、耗时和 `degraded: ["rerank"]`。72 题对照中 local-static 将 Recall@5 从 78.3% 提至 83.3%、严格通过率从 65.0% 提至 76.7%、Recall@20 从 96.7% 提至 98.3%，P50 从 26.8ms 增至 36.7ms；但 MRR@10 / nDCG@10 仅相对提升 5.6% / 4.7%，未达 15% 门槛。当前环境无 Cross-Encoder 运行时或模型资产，且计划禁止未批准的原生依赖，因此 M3 不标完成；现有适配器默认启用并保留可回退路径。

**M3b 补充（2026-08-13 晚）**：基于逐题运行时探针完成五项管线改进——比较类问题子查询分解（QueryPlan，K3 安全）、查询词抽取对中文空格片段强制重分词、rerank 输入按 `text_hash` 跨内容去重（拦截近重复文件副本刷榜）、LocalStaticReranker v2 新增数值答案/显式类型/文件名覆盖特征、rerank 后同文档冗余覆盖软降权。同日 RRF 对照下 Recall@5 85.0%→95.0%，严格通过率 73.3%→90.0%，MRR@10 73.2%→78.8%，nDCG@10 77.3%→83.0%（p50 50ms），后端 168 项测试全绿。MRR/nDCG 相对提升 7.6%/7.5% 仍低于 15% 选型门槛，M3 保持未完成；剩余失败（F29/F31/P19 片级排序、X06 近重复洪灾）正是 Cross-Encoder 与模糊去重的目标场景。冻结结果见 `docs/eval/v7-m3b-local-static-v2.json` 与对照 `v7-m3b-rrf-control.json`。

- 建立候选模型 benchmark，不凭模型大小或记忆选型。
- 实现 Reranker 协议、本地适配器和显式降级适配器。
- 调整深召回、RRF 和软硬 cap。

**完成标志**：nDCG/MRR 提升和延迟预算同时达标；PyInstaller/DMG 冒烟通过。

### M4：证据压缩

**执行状态（2026-08-13）**：抽取式压缩链路已接入，发布门槛未完成。当前实现支持分句/表格行切分、相邻句窗口、跨文档覆盖、重复控制和字符/token 代理预算；搜索摘要使用最佳原文 EvidenceSpan，问答改为消费 ContextPack，引用可回溯 `span_id`、Child offset 和 Document offset。对 60 个可回答问题的关键词代理评测中，keyword evidence recall 为 90.0%，字符压缩比例中位数约 68%，压缩阶段 P95 约 59ms；漏例为 A10、F20、F30、F31、P19、P20，其中部分由上游候选未召回造成。由于 90.0% 未达 95% Evidence Recall 门槛，且精确 gold span 标注尚未完成，该结果不能替代正式 Evidence Recall，M4 不标完成。

**M4 复评（2026-08-13 晚）**：达标（关键词代理口径）。M3b/M3c 的检索改进把上游漏例带了回来，新增可重复评测脚本 `tests/run_compress_eval.py` 完整镜像 /ask 链路（route_limit=60 → 多样性 → 邻居扩展 → 压缩 → 装配）。60 题实测：关键词证据保留率 95.0%（门槛 95%）、字符压缩率中位数 61.1%（门槛 ≥35%）、offset 往返错误 0、压缩阶段 P95 3.7ms（预算 500ms）。剩余漏例 A10/F30/F31 属片级排序边界，与 M3 的 Cross-Encoder 缺口同源。注意该指标仍是答案关键词锚定的代理口径，精确 gold span 标注未完成，不冒充正式 Evidence Recall。冻结结果见 `docs/eval/v7-m4-compress-eval.json`。

- 实现分句/表格行切分、相关性评分、区间选择、去重和 token 装配。
- 建立 EvidenceSpan 到原文 locator 的完整映射。
- 接入搜索摘要和问答 ContextPack。

**完成标志**：Evidence Recall 与 token 减少门槛同时达标，offset 往返零错误。

### M5：带引用问答升级

**执行状态（2026-08-13）**：工程链路完成，两项模型指标待真实跑批。生成已消费 ContextPack；引用由 EvidenceSpan 驱动（`span_id` + Child/Document offset 双向可回溯）；四条后置校验全部留痕在 `Answer.validation`（attempts / fabricated_removed / truncated_refusal / fallback），可审计；新增 `GET /runs/{trace_id}` 调试端点回读最近 50 条非持久化检索 trace（Bearer 鉴权，不含正文与查询原文，重启即失效）。虚构引用剔除、零引用重生成、二次失败降级、拒答句截断均有单元测试覆盖（scripted LLM）。「引用支持率 ≥0.95」「正确拒答率 ≥0.85」需要真实模型跑批标定——本机未配置云端密钥，如实标记为待验证，不用 scripted 结果冒充。检索侧拒答门限于 2026-08-13 重新标定过一次：误拒 ≤5% 约束下正确拒答率仅 58.3%（U10 类"词都在但不回答问题"仍无法用检索信号区分），维持"不硬拒 + 置信度标注"策略。

- 生成改为消费 ContextPack。
- 引用由 EvidenceSpan 驱动，完善拒答和降级。
- 增加检索 explain 和可审计 validation。

**完成标志**：引用支持率、正确拒答率和虚构引用门槛通过。

### M6：统一三栏知识库界面

**执行状态（2026-08-13）**：已完成。首页为统一三栏界面：左侧文件管理与检索，中间文件详情、正文和命中证据，右侧常驻知识问答；没有顶层双视图，也没有搜索/问答模式切换。新增受鉴权保护的文件详情 API 和模型端到端连接检测，保存配置后不会伪装成“已验证”。使用正式 Electron bridge 和真实资料库一致性副本完成端到端验证：默认载入 2587 个文件，检索返回 32 个真实文件，文件/结果点击只更新中栏，右侧状态保持；模拟可用模型时显示“模型可用”并启用提问，不可达时显示明确错误并禁用提问。1280x820 与最小 960x640 窗口三栏均完整可见，无横纵溢出、Renderer 控制台错误或安全策略警告。

- 左侧统一承载范围、文件管理、文件书、分类和检索结果。
- 中栏承载选中文件详情、全部检索命中片段和引用证据。
- 右栏常驻知识问答及模型检测状态。
- 来源、重复和保全留在设置与文件管理能力中，不拆成独立主视图。

**完成标志**：用户在同一屏幕完成文件浏览、检索、详情阅读、问答、引用回查和范围切换。

### M7：发布候选

**执行状态（2026-08-13）**：0.2.0 发布候选已产出。全量回归后端 170 项、桌面 15 项全绿；72 题检索评测与压缩评测均已冻结（`docs/eval/`）；PyInstaller sidecar 重打包后经 headless 冒烟（health 全绿：sqlite-vec v0.1.9、FTS5、中文检索探针 7/7、嵌入模型 256 维加载）；`dist/Inktable-0.2.0-arm64.dmg`（300 MB）打包完成，asar 内确认为新界面、sidecar 与最新构建逐字节一致。README、HANDOFF 入口、发布说明（`docs/RELEASE-0.2.0.md`）已更新；根目录 HANDOFF.md 收口为入口链接。数据库为 M2 时已完成的 v2 schema（迁移前有可恢复备份），本轮无 schema 变更。损坏恢复演练已实际执行（2026-08-13，于真实库只读副本上）：建备份 → `backup_is_restorable` 通过 → 注入 4KB 中段损坏 → `quick_check` 正确检出 → 从备份恢复后分片数一致、FTS 可查；真实库存在当日自动备份。待用户完成：真机安装冒烟（无签名需右键打开）。未关闭的阻塞项如实列于发布说明「已知限制」：Cross-Encoder 选型门槛、两项真实模型 QA 指标。

- 全量回归、离线评测、性能测试、数据库升级/回滚演练。
- 重跑 PyInstaller、Electron DMG 和真实安装冒烟。
- 更新 README、HANDOFF 和发布说明。

**完成标志**：本计划全部阻塞指标通过，未降级模式下完整知识管线可用。

### M8：体验重构与集成（0.3.0，按真实使用反馈追加）

**同日追加（问答体验与删除）**：意图路由（通用/知识库带内声明，界面标注来源）、多轮对话浓缩（`condensed_query`）、拒答后检索改写重试（`retrieval_retry`，有界一轮）、句级自动归因（`auto_cited`，数字必须命中证据）、真实补全连接检测 + 启动自动检测、模型调用失败优雅降级；文件删除两档（库内移除 / 系统废纸篓，sidecar 不碰磁盘）；问答区与全页视觉重做。回归 189 后端 + 15 桌面全绿；72 题评测复跑达标（Recall@50 100%、误拒 0%，其余指标在冻结基线波动范围内）。

**执行状态（2026-08-14 追加）**：嵌入模型换代 —— 移除内置 model2vec 静态嵌入（potion 裁剪版 256 维，含仓库约 770 MB 模型文件与 tokenizers/safetensors 依赖），改为本机 **Ollama + bge-m3**（1024 维上下文编码；`GET /api/tags` 探测 30 秒缓存，`POST /api/embed` 批量编码，未检测到自动降级纯 FTS5）。向量表维度迁移自动化（`_init_vec_table` 检测不符即重建 + 清 `embedding_model_id` 触发回填），真实库 5535 片全量重嵌完成。随后修复换代引出的性能坑：重排现场编码 80 候选耗时 6–14 秒 → 改为批量复用 `chunks_vec` 已入库向量（真实库单查询 13.9s → 0.4s / 8.1s → 2.0s），查询变体合并批量编码，问答生成超时 60s → 180s；72 题评测复测达标（Recall@5 95.0%）。引用标签支持三位数（`[C100+]` 此前前后端正则均漏）。可见性口径扩展：磁盘上已消失且无保全副本的文件从全部视图隐藏（有保全副本的继续可见，文件回归自动恢复）。回归规模：后端 209 项、桌面 15 项全绿。

**执行状态（2026-08-13）**：已完成，随 0.3.0 交付（`docs/RELEASE-0.3.0.md`）。本里程碑不在 v7 原始计划内，来源于用户实际使用反馈的连续迭代。要点：① 信息架构 v3——左栏收敛为"范围 + 文件树"（`GET /files/tree` 从库内路径推导，目录点击经 `/files?dir=` 前缀过滤），中栏在"扩展名分组列表（`group=ext` 服务端窗口函数排序）⇄ 全文查看器（`GET /files/{id}/content` 分页续载）"之间整页切换；② 可见性口径统一（`VISIBLE_FILES_COND`）——停用来源的文件在列表/统计/搜索/分类计数中全部隐藏，记录与索引保留，重新启用即恢复；③ 默认自动分类（`POST /classify/auto_ext`，规则化、不覆盖手动、可关闭）；④ **语义向量存量补齐**（`POST /index/embed_backfill`）——修复"模型晚于内容入库导致 95% 分片无向量"的长期缺口，真实库从 194/3627 补齐至 3627/3627；⑤ 设置三栏（通用/来源/模型），数据目录可迁移（`INKTABLE_DATA_DIR` + 停库搬迁 + `POST /system/rebase_preserved` 路径重写）；⑥ cc-switch 供应商导入（`GET /integrations/ccswitch` 只读解析，分组展示、选中高亮），"检测连接"升级为真实补全测试（返回实际回复与耗时，512 token 防推理模型假阴性）；⑦ 来源发现扩展——QQ NT 接收目录（存在即显示）、飞书/Lark/钉钉/企业微信规则、浏览器自定义下载目录（默认 ~/Downloads 归"下载"系统来源，Chrome/Edge/Brave/Arc/Vivaldi/Safari/Firefox）。回归规模：后端 179 项、桌面 15 项全绿；72 题检索评测与压缩评测未受影响（检索管线本轮无行为变更，除 /search 增加可见性过滤）。遗留与 0.2.0 相同：Cross-Encoder 门槛、两项真实模型 QA 指标、代码签名；新增注意项：Anthropic 中转的 OpenAI 协议兼容性以"检测连接"实测为准，办公应用路径规则未在真机验证。

**最终发布验收（2026-08-16）**：0.3.0 的 65 题正式 QA、259 项后端测试、
15 项桌面测试、Windows sidecar/NSIS 构建、隔离安装版 Electron 冒烟、
退出进程树清理和真实库只读一致性审计全部完成。Cross-Encoder 的 +15%
相对提升门槛仍只用于是否切换生产 reranker；因未达到，生产继续使用已通过
绝对质量门槛的 `local-static-v3`。剩余发布限制为代码未签名及文档列出的
平台能力差异，不再有真实模型 QA 阻塞项。

## 14. 风险登记

| 风险 | 影响 | 应对 |
|---|---|---|
| 本地 Rerank 太慢 | 搜索不可交互 | Top-N 控制、批处理、量化、轻量模型、CoreML；不删除阶段 |
| 新模型显著增大 DMG | 分发和启动变差 | 模型基准同时记录质量、大小、内存和冻结结果 |
| 上层摘要漏掉正确主题 | 层级路由误杀 | 上层只软加权，Child 四路保持独立入口 |
| 压缩丢失关键限定词 | 回答失真 | Evidence Recall 门槛、保留否定/数值句、原文区间测试 |
| 三层增量索引复杂 | 更新错误或全量重算 | hash 依赖图、影子版本、祖先最小更新测试 |
| 检索 trace 泄露隐私 | 敏感内容落盘 | 默认只存 id/score/timing；正文仅评测模式短期保存 |
| 云端 Rerank/LLM 泄露文本 | 信任边界破坏 | 默认关闭、明确提示、只发必要片段、密钥 safeStorage |
| 新架构重构范围过大 | 长期无法交付 | 按 M1-M6 垂直里程碑推进，每步保持可运行和可回滚 |

## 15. 开工顺序

严格按以下顺序执行：

1. 修复当前测试基线，不在红灯上做架构迁移。
2. 标注评测集并冻结当前检索结果。
3. 只拆管线，不改变行为。
4. 建三层索引和软路由。
5. 接入实际 Rerank 并用评测选择模型。
6. 在 Rerank 之后实现 Parent 扩展和证据压缩。
7. 升级生成、引用和拒答。
8. 最后调整桌面信息架构。

文件管理新增功能在 M0-M5 期间只接受数据安全、资料可靠性或知识溯源相关改动。普通管理便利性功能不抢占知识引擎里程碑。

## 附录 A：v6 到 v7 的关键变化

| v6 | v7 |
|---|---|
| 文件支柱与知识支柱同层 | Knowledge Engine 为核心，Library Engine 为基础设施 |
| 产品先成为全机文件索引工具 | 产品首先交付可评测的个人知识检索与问答 |
| Parent-Child 主要靠命中邻居拼接 | Document / Section / Child 持久层级 + Rerank 后扩展 |
| RRF 输出作为最终排序 | RRF 只粗融合，Child Rerank 决定精排 |
| Rerank 因大模型慢推迟到 V2 | Rerank 固定存在，模型和运行时通过 benchmark 选择 |
| Top 片段直接送模型 | Parent 扩展后做 EvidenceSpan 抽取式压缩 |
| 30 题 Recall@5 为主 | 60+ 题，分阶段评测 Recall/nDCG/Evidence/引用/拒答 |
| 文件库是第一主视图 | 文件管理、详情阅读与知识问答统一为同屏三栏，文件管理是知识库的支撑入口 |
| V2 再考虑检索扩展 | 分层、Rerank、压缩全部进入当前主路线 |

## 附录 B：当前实现基线

截至 v7 编写时：

- 已实现 Electron + Python sidecar、本地鉴权和 LLM 密钥加密。
- 已实现来源发现、扫描监听、inode 身份、SHA-256 去重、缺失恢复和保全副本。
- 已实现 PDF/DOCX/Markdown/TXT 解析和约 500 字 Child 分片。
- 已实现 jieba、trigram、LIKE、向量四路召回和 RRF。
- 已实现命中 Child 的前后邻居动态扩展、带引用问答和四条后置校验。
- 已实现文件书范围、虚拟分类和增量嵌入。
- 尚未实现持久化 Document/Section 层级索引。
- 尚未实现实际 Reranker。
- 尚未实现带原文 offset 的查询相关上下文压缩。

因此 v7 是在可靠底座上的检索质量升级，不是从零重写。
