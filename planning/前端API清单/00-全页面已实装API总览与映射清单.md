# 00-全页面已实装 API 总览与页面映射清单

> **文档定位**：Ordo 桌面知识工作台**全量 22 站页面已实装 RESTful API 终极大盘**。  
> **服务底层**：基于 Node.js 24 + Fastify + SQLite3，后端代码统一收敛于 [`server/src/app.js`](file:///d:/AIApp/Ordo/server/src/app.js)。  
> **测试状态**：所有接口均已通过全仓 17 项端到端自动化测试（`npm test` 100% 满分）。  
> **更新时间**：2026-09-04

---

## 一、全量 22 站页面与已实装 API 映射大盘 (共 85 个端点)

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  Ordo 22 站业务页面与已实装 API 架构全景                                                │
├────────────────────────────────────────────────────────────────────────────────────────┤
│  ├── 【模块一：运行总览】                                                              │
│  │   └── 01-首页.png ────────────────► 仪表盘、8大指标、7天请求折线、待办告警队列 (6 APIs)     │
│  ├── 【模块二：知识资产中枢】                                                          │
│  │   ├── 02-知识库-数据登记.png ──────► 外部数据源探测、WebDAV、目录导入、编码检测 (6 APIs)   │
│  │   ├── 03-知识库-数据集.png ────────► 数据集树形目录、文件上传、状态流转与删除 (7 APIs)       │
│  │   ├── 04-知识库-数据解析.png ──────► 异步解析任务队列、暂停/重试、标准产物提取 (7 APIs)     │
│  │   ├── 05-知识库-构建知识索引.png ──► 切块、向量化进度条、HNSW重建、BM25与发布 (17 APIs)    │
│  │   └── 06-知识库-知识库管理.png ──────► 知识库CRUD、向量库连接探活、高级存储与分块配置 (7 APIs) │
│  ├── 【模块三：问答流程诊断中枢】                                                      │
│  │   ├── 07-问答流程-问题解析.png ────► 原始输入、结构化分析、规范问题与重跑 (7 APIs)          │
│  │   ├── 08-问答流程-问题向量化.png ──► 查询向量生成、散点投影、模型对比 (8 APIs)              │
│  │   ├── 09-问答流程-检索路由.png ────► 4通道DAG分发、路由依据多因子评分、通道参数预估 (8 APIs)│
│  │   └── 10~14-问答流程阶段 4~8 ──────► 多路召回、结果融合、重排、提示词与回答生成 (7 APIs)    │
│  ├── 【模块四：AI 应用套件】                                                           │
│  │   ├── 15-AI应用-智能问答.png ──────► 会话流、流式生成、佐证引用卡片、点赞反馈 (8 APIs)     │
│  │   └── 16-AI应用-智能助手.png ──────► 助手CRUD、发布/暂停、Web挂载与Handoff客服 (17 APIs)   │
│  ├── 【模块五：系统配置与存储管理】                                                    │
│  │   ├── 17-设置-通用.png ────────────► 系统通用设置读取/更新、特性开关 (4 APIs)              │
│  │   ├── 18-设置-模型配置.png ────────► 模型提供商、API Key、连通性探测 (4 APIs)             │
│  │   ├── 19-设置-存储配置.png ────────► 本地SQLite、数据快照、备份与还原 (3 APIs)             │
│  │   └── 20-设置-版本信息.png ────────► 系统版本、环境诊断健康度、审计日志 (4 APIs)           │
│  └── 【模块六：全局交互状态与模态框】                                                  │
│      ├── 21-状态-新对话选择知识库.png ──► 知识库快速拉取、挂载切换 (2 APIs)                    │
│      └── 22-状态-全局快捷搜索.png ────► Cmd+K 全局毫秒级全文检索匹配 (1 API)                  │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 二、逐页面实装 API 详细清单

### 1. 【01-首页.png】(Home Dashboard)
* **所属视图**：`HomeView.vue` / `pageHome()`
* **驱动区域**：顶部 8 大指标卡、底部近 7 天请求折线图、告警待办队列、知识库运行状态。
* **已实装 API 列表**：
  1. `GET /api/v1/dashboard`：首页最核心大接口，一次性供给数据库连接数、切块总数、智能助手、今日请求、模型健康度、Wiki、图谱实体与可检索现货切块数。
  2. `GET /api/v1/health`：轻量服务健康探活，供给顶栏在线状态绿点。
  3. `GET /api/v1/knowledge-bases`：获取当前已有知识库列表与各库运行卡片。
  4. `GET /api/v1/connectors`：拉取已接入的外部数据库实例列表。
  5. `GET /api/v1/models`：获取当前配置的 AI 模型列表与 4 类模型就绪状态。
  6. `GET /api/v1/wiki`：拉取专供 RAG 问答检索沉淀的 Wiki 知识条目总数。

---

### 6. 【06-知识库-知识库管理.png】(Knowledge Base Config)
* **所属视图**：`ConfigView.vue` / `pageConfig()`
* **驱动区域**：3 步向导创建流、向量数据库引擎选择（SQLite-VSS / Milvus / Qdrant）、连接测试、HNSW 索引参数。
* **已实装 API 列表**：
  1. `GET /api/v1/knowledge-bases`：获取知识库列表。
  2. `POST /api/v1/knowledge-bases`：创建新知识库。
  3. `GET /api/v1/knowledge-bases/:id`：读取指定知识库详情、名称、空间绑定与语言。
  4. `PATCH /api/v1/knowledge-bases/:id`：修改知识库基本信息与默认配置。
  5. `DELETE /api/v1/knowledge-bases/:id`：删除知识库及其级联资源。
  6. `GET /api/v1/knowledge-bases/:id/impact`：预估删除或修改知识库对下游智能助手的影响范围。
  7. `GET /api/v1/knowledge-bases/:id/index-profiles`：获取向量索引配置模板（Chunk Size、Overlap、维度、距离算法）。
  8. `POST /api/v1/knowledge-bases/:id/index-profiles`：保存新的向量索引配置。
  9. `POST /api/v1/index-profiles/:id/default`：设置当前配置为知识库默认索引策略。

---

### 3. 【03-知识库-数据集.png】(Datasets & Documents)
* **所属视图**：`DatasetsView.vue` / `pageDatasetsTarget()`
* **驱动区域**：左侧数据集列表、中间文件目录树、右侧文档元数据详情与状态。
* **已实装 API 列表**：
  1. `GET /api/v1/knowledge-bases/:id/datasets`：获取知识库绑定的数据集列表。
  2. `POST /api/v1/knowledge-bases/:id/datasets`：创建新数据集。
  3. `GET /api/v1/datasets/:id`：获取指定数据集信息与文件统计。
  4. `PATCH /api/v1/datasets/:id`：更新数据集名称与描述。
  5. `DELETE /api/v1/datasets/:id`：删除数据集。
  6. `GET /api/v1/datasets/:id/documents`：分页获取数据集内部文档列表（支持状态、关键词筛选）。
  7. `GET /api/v1/documents/:id`：查看指定文档的物理路径、解析状态、哈希值与大小。
  8. `DELETE /api/v1/documents/:id`：软删除指定文档。

---

### 2. 【02-知识库-数据登记.png】(Data Sources & Ingest)
* **所属视图**：`RegistryView.vue` / `pageRegistry()`
* **驱动区域**：3 栏自适应登记工作台（本地资料、WebDAV、PostgreSQL）、文件发现表格、字符编码检测。
* **已实装 API 列表**：
  1. `GET /api/v1/datasets/:id/sources`：获取当前数据集接入的外部数据源连接。
  2. `POST /api/v1/datasets/:id/sources`：新增数据源接入（本地目录、WebDAV 凭据、数据库连接）。
  3. `POST /api/v1/datasets/:id/files`：直接上传物理文件并完成鉴权登记。
  4. `POST /api/v1/datasets/:id/archives`：上传 ZIP/TAR 压缩包，自动解压入库。
  5. `POST /api/v1/datasets/:id/directory/preview`：输入本地绝对路径，安全预扫描目录结构与文件清单。
  6. `POST /api/v1/datasets/:id/directory/import`：将预扫描确认的文件批量登记导入至数据集。

---

### 4. 【04-知识库-数据解析.png】(Parsing Pipeline & Artifacts)
* **所属视图**：`ParsingView.vue` / `pageParsing()`
* **驱动区域**：4 阶段解析流水线动画、任务队列调度（运行中/排队中/失败）、16:9 画布彩色识别框、标准产物提取。
* **已实装 API 列表**：
  1. `GET /api/v1/tasks`：获取后台 Worker 解析任务队列列表（支持按 status: pending/running/failed 筛选）。
  2. `GET /api/v1/tasks/:id`：轮询指定解析任务的实时执行百分比、当前页码与耗时。
  3. `POST /api/v1/tasks/:id/cancel`：取消进行中的解析任务。
  4. `POST /api/v1/tasks/:id/pause`：挂起/暂停正在执行的任务。
  5. `POST /api/v1/tasks/:id/resume`：恢复挂起的解析任务。
  6. `POST /api/v1/tasks/:id/retry`：针对失败的解析任务发起一键重试。
  7. `GET /api/v1/tasks/:id/wait`：长轮询挂起等待任务完成。
  8. `GET /api/v1/artifacts/:id/:kind`：按类型下载或预览解析产物（`markdown` 原生清洁文本、`document` 结构化JSON、`manifest` 依赖元数据、`quality` 质量检测报告）。

---

### 5. 【05-知识库-构建知识索引.png】(Indexing & Chunks)
* **所属视图**：`IndexingView.vue` / `pageIndex()`
* **驱动区域**：4 大可点击子工作台（切块治理、向量化调度、HNSW 索引、BM25 全文）、固定 98px 块卡片、4 节点一致性 DAG、动态进度条、混合检索滑块、不可变版本发布。
* **已实装 API 列表**：
  1. `GET /api/v1/datasets/:id/indexing/stats`：四大指标卡（知识块总数、已向量化、待更新、版本）。
  2. `GET /api/v1/datasets/:id/indexing/pipeline`：4 个子阶段执行状态与百分比进度。
  3. `GET /api/v1/datasets/:id/chapters`：获取知识库文档大纲章节，填充筛选下拉框。
  4. `GET /api/v1/datasets/:id/chunks`：分页检索知识块列表（支持正文、ID、文档、Token范围过滤）。
  5. `GET /api/v1/chunks/:id`：读取指定分块完整 Markdown 内容、Token 数、修订历史。
  6. `GET /api/v1/chunks/:id/lineage`：获取一致性 DAG 4 节点血缘状态（数据块 ➔ 向量记录 ➔ 集合 ➔ 索引）。
  7. `POST /api/v1/chunks/:id/revisions`：保存编辑修订并增量重算向量。
  8. `POST /api/v1/chunks/:id/vectorize`：单块立即执行向量化重算。
  9. `POST /api/v1/chunks/:id/toggle-disable`：逻辑禁用/恢复知识块。
  10. `POST /api/v1/chunks/:id/restore`：将知识块回退到指定历史修订版本。
  11. `GET /api/v1/chunks/:id/diff`：比对知识块两次修订之间的文本 Diff 差异。
  12. `POST /api/v1/chunks/:id/split`：将一个知识块拆分为 2 个新知识块。
  13. `POST /api/v1/chunks/merge`：合并多个相邻知识块。
  14. `POST /api/v1/datasets/:id/indexing/vectorize-pending`：批量并发向量化待更新块，**动态推进进度条至 100%**。
  15. `POST /api/v1/datasets/:id/indexing/rebuild-hnsw`：全量重建 4 层 HNSW 图索引，**唤起重建进度条**。
  16. `POST /api/v1/datasets/:id/indexing/optimize-index`：压缩整理索引碎片空间。
  17. `POST /api/v1/datasets/:id/indexing/rebuild-bm25`：重建 BM25 倒排索引词典。
  18. `PUT /api/v1/datasets/:id/indexing/hybrid-weights`：持久化 Dense 语义与 Sparse 词频混合检索权重。
  19. `GET /api/v1/datasets/:id/releases`：获取知识库历史发布轨迹版本列表。
  20. `POST /api/v1/datasets/:id/releases`：冻结发布全新不可变索引版本（v7 ➔ v8）。
  21. `GET /api/v1/releases/:id`：查看指定发布版本的切块快照。
  22. `POST /api/v1/releases/:id/activate`：激活指定版本上线。
  23. `POST /api/v1/releases/:id/rollback`：版本一键秒级回滚。
  24. `POST /api/v1/releases/:id/search`：输入 Query 检验检索效果（返回 Top-K 向量与混合匹配结果）。

---

### 7. 【07~14-问答流程 8 大阶段.png】(QA Flow Pipeline Diagnostics)

#### 7.1 【09-问答流程-检索路由.png】(Retrieval Routing & Channel Dispatch - Stage 3)
* **所属视图**：`QAFlowView.vue` / `pageQA09_Route()`
* **驱动区域**：
  - **顶部**：Trace 链路诊断条（Trace ID、应用名称、知识库、耗时、状态）与 8 阶段步进指示条。
  - **区域 1（输入问题与路由配置）**：原始/规范化提问、数据范围绑定、权限过滤开关、应用画像、降级兜底策略。
  - **区域 2（检索路由 DAG 拓扑图）**：动态渲染 4 通道分支，绿色实线（已启用通道：向量检索、全文检索）与灰色虚线（未启用通道：知识图谱、结构化查询），带方向箭头指示流向。
  - **区域 3（路由依据与判定分析）**：意图匹配置信度与分类、3 大可用索引引擎健康状态、权限约束与命中率、5 条多因子打分路由原因、综合置信度大字展示。
  - **区域 4（通道参数配置与资源预估表）**：各通道状态徽标、TopK 配额、超时熔断时间（ms）、融合加权比重、预估耗时与预估成本。
  - **底部控制栏**：`[ 编辑路由 ]`、`[ 进入多路召回 > ]`、`[ 从此阶段重跑 ]`。
* **已实装 API 列表**：
  1. `GET /api/v1/traces/:id/stages/route`：**检索路由核心大接口**，动态计算并返回提问、路由配置、4 通道 DAG 状态、通道参数表格、多因子打分原因（`routingBasis`）与综合置信度。
  2. `GET /api/v1/traces/:id/pipeline`：8 阶段流水线步骤状态（`completed` / `running`）与各阶段耗时（ms）。
  3. `POST /api/v1/traces/:id/replay`：**从此阶段重放**，携带当前设定的路由通道策略重新执行后续 4~8 阶段，生成新衍生 Trace。
  4. `GET /api/v1/traces/:id`：获取单条 Trace 的完整阶段树（包含 `routingBasis` 与 `stages` 诊断对象）。
  5. `GET /api/v1/traces`：获取 Trace 链路调试历史列表。
  6. `GET /api/v1/traces/:id/compare/:otherId`：比对两次 Trace 运行（如不同路由策略下）的召回与生成指标差异。

#### 7.2 【10-问答流程-多路召回.png】(Multi-Channel Recall - Stage 4)
* **所属视图**：`QAFlowView.vue` / `pageQA10_Recall()`
* **驱动区域**：
  - **顶部**：Trace 链路诊断条与 8 阶段步进指示条。
  - **工具栏控制组**：`TopK: 20` 下拉切换、`仅当前版本` 状态切换、`权限过滤` 开关、`重试失败通道`。
  - **四路并发召回看板**：
    - 通道 1【向量召回】：18 条 / 126 ms，展示 Top-5 排名、候选文档、原始余弦分与 ACL 权限状态。
    - 通道 2【全文召回】：15 条 / 42 ms，展示 Top-5 排名、BM25 词频匹配分与 ACL 权限状态。
    - 通道 3【图谱召回】：8 条 / 88 ms，展示实体关系路径命中分与 ACL 权限状态。
    - 通道 4【结构化查询】：跳过状态及规则命中分析。
  - **底部统计度量区**：去重前候选总数 41 条、重复候选数 9 条、通道失败数 0 个，以及动态计算各通道耗时分布柱状图（向量/全文/图谱）。
  - **底部操作栏**：`[ 查看原文 ]`、`[ 导出候选 ]`、`[ 重试失败通道 ]`、`[ 进入结果融合 > ]`。
* **已实装 API 列表 (共 7 个)**：
  1. `GET /api/v1/traces/:id/stages/recall`：**多路召回核心接口**，聚合返回 4 通道候选切片、去重前 41 条与去重 9 条指标、耗时统计与当前过滤配置。
  2. `POST /api/v1/traces/:id/stages/recall/filter`：**动态筛选重算**，传入新配额或版本范围即时重算各通道候选并联动度量卡。
  3. `POST /api/v1/traces/:id/stages/recall/retry-channel`：**局部重试通道**，对指定或失败通道发起局部重试检测。
  4. `GET /api/v1/traces/:id/stages/recall/chunks/:chunkId`：**正文切片预览**，调取指定切片的完整文本、所属文档、页码与高亮词。
  5. `GET /api/v1/traces/:id/stages/recall/export`：**候选集导出**，将多路召回数据导出为 JSON 或 CSV 调试报表。
  6. `GET /api/v1/traces/:id/pipeline`：8 阶段耗时（问题解析 120ms、向量化 98ms、路由 35ms、召回 346ms 等）与状态。
  7. `GET /api/v1/traces/:id`：单条 Trace 全阶段快照。

#### 7.3 【11-问答流程-结果融合.png】(Multi-Source Fusion & RRF Ranking - Stage 5)
* **所属视图**：`QAFlowView.vue` / `pageQA11_Fuse()`
* **驱动区域**：
  - **顶部**：Trace 链路诊断条与 8 阶段步进指示条。
  - **多路原始召回输入源 (三列网格)**：
    - 向量召回 (15 条)、全文召回 (17 条)、图谱召回 (9 条)；各列以独立卡片呈现 Top-5 候选与彩色排名胶囊。
  - **中枢融合处理流水 (4 步卡片)**：
    - `去重 (32)`、`权限复核 (0)`、`分数归一化`、`RRF 融合`，带精致绿色 SVG 方向连接线与箭头网络。
  - **右翼融合候选集 (Top 20)**：
    - 绿色高亮排名圆圈、融合分数（0.842 ~ 0.523）及多通道彩色来源徽标（向量/全文/图谱）。
  - **核心统计度量卡 (Row 2)**：
    - 原始候选数 `41`、去重后候选数 `32`、权限过滤移除数 `0`、融合候选数 `20`。
  - **操作控制组**：
    - `[ 调整权重 ]`（RRF 常数与通道加权模态窗）、`[ 查看计算 ]`（数学公式 LaTeX 推演模态窗）、`[ 进入重排 > ]`（SPA 路由跳转 `qaflow/rerank`）。
  - **底层分数明细与去重归因表 (Row 3)**：
    - 融合排名、文档标题、三路来源原始分、归一化分、RRF 融合分、去重组 (G1~G5)、去重原因与 ACL 权限状态。
* **已实装 API 列表 (共 10 个)**：
  1. `GET /api/v1/traces/:id/stages/fusion`：**结果融合核心接口**，提供三路召回、4 步流水、4 项度量指标、Top 20 融合结果与底层分数明细。
  2. `PUT /api/v1/traces/:id/stages/fusion/weights`：**调整融合算法与权重**，支持动态调整 RRF 常数与三路加权比重。
  3. `POST /api/v1/traces/:id/stages/fusion/reset-weights`：**一键恢复默认权重**，秒级重置至系统推荐权重配比。
  4. `GET /api/v1/traces/:id/stages/fusion/calculation/:candidateId`：**公式数学推演**，查看单一切片的加权倒数代入推导步骤。
  5. `GET /api/v1/traces/:id/stages/fusion/candidates`：**全量融合候选集分页展开**，突破 Top 6 获取全量 20 条数据。
  6. `GET /api/v1/traces/:id/stages/fusion/chunks/:candidateId`：**候选切片正文预览**，调取融合切片的正文分块与 ACL 判定信息。
  7. `GET /api/v1/traces/:id/stages/fusion/logs`：**算法审计日志**，查看 MD5 指纹去重、ACL 复核与归一化耗时。
  8. `GET /api/v1/traces/:id/stages/fusion/export`：**融合报表导出**，导出融合结果与去重分析表。
  9. `POST /api/v1/traces/:id/stages/fusion/rerun`：**从融合阶段重新重算**，参数修改后向下级联重算重排与生成，生成新衍生 Trace。
  10. `GET /api/v1/traces/:id/pipeline`：8 阶段流水线状态与耗时。

#### 7.4 【12-问答流程-重排.png】(Stage 6: Cross-Encoder Rerank)
* **所属视图**：`RerankView.vue` / `pageQA12_Rerank()`
* **驱动区域**：
  - **顶部 Reranker 模型状态卡**：
    - Reranker 模型名 `bge-reranker-v2-m3`、候选数量 `20` ➔ 保留候选 `8`、耗时 `512ms`、状态 `● 健康`。
  - **区域 1：重排前候选列表 (Top 20)**：
    - 排名、文档切片标识、页码、摘要、位移升降状态徽标（`↑ 5`、`↑ 1`、`↓ 3`、`淘汰`）。
  - **区域 2：重排后保留列表 (保留 8 项)**：
    - 重排后精排序位、文档切片标识、页码、Cross-Encoder 交叉注意力得分（0.912 ~ 0.792）。
  - **区域 3：文档详情深度审视卡片**：
    - 完整正文、相关度变化箭头（0.792 ➔ 0.912）、模型推理语义摘要、Token 消耗统计与权限校验。
  - **区域 4：重排得分对比折线图 (Top 20)**：
    - 重排前 vs 重排后双折线走势图、保留阈值 0.75 虚线基准与 20 个候选打分点。
  - **操作控制组**：
    - `[ 更换模型 ]`（切换 Cross-Encoder 模型）、`[ 调整阈值 ]`（动态配置保留过滤阈值）、`[ 对比结果 ]`（NDCG@10 与 MRR 增益收益评估）、`[ 进入构建提示词 > ]`（跳转 Stage 7）。
* **已实装 API 列表 (共 6 个)**：
  1. `GET /api/v1/traces/:id/stages/rerank`：**重排全景主接口**，获取当前 Trace 模型状态、重排前候选 20 项位移徽标、重排后保留 8 项得分、折线分布图及选中切片详情；累计耗时严格计算至 Stage 6（1.32 s / 1321 ms），后续未走完阶段严禁计入。
  2. `GET /api/v1/traces/:id/stages/rerank/chunks/:chunkId`：**切片深度审视**，拉取指定候选切片的完整正文、前后分数变化、Cross-Encoder 推理判定与 Token 消耗。
  3. `PUT /api/v1/traces/:id/stages/rerank/config`：**重排超参微调**，动态切换重排模型、调整保留过滤阈值（如 0.75 ➔ 0.80）与保留 Top-K 上限。
  4. `POST /api/v1/traces/:id/stages/rerank/compare`：**重排收益与质量对比评估**，计算重排前后 NDCG@10、MRR、Top-5 精度提升率与噪声过滤比率。
  5. `GET /api/v1/traces/:id/stages/rerank/logs`：**执行审计日志**，获取批量推理批次大小、内存/GPU 占用与逐条打分耗时。
  6. `GET /api/v1/traces/:id/pipeline`：8 阶段流水线状态与耗时步进条；支持 `?stage=6` 阶段过滤，严格遵循“**流程未走完时只累加之前及当前步骤耗时**”（Stage 1-6 累计 1.32 s，后续 pending 步骤耗时为 null）。

#### 7.5 【07、08、13、14 问答流程其他阶段】(Pipeline Other Stages)
* **驱动区域**：Stage 1 问题解析、Stage 2 问题向量化、Stage 7 构建提示词、Stage 8 回答生成。
* **已实装 API 列表**：
  1. `GET /api/v1/traces`：全局问答链路列表（空库自动保底标准原型演示 Trace）。
  2. `GET /api/v1/traces/:id`：端到端全阶段性能度量与流水线快照。
  3. `POST /api/v1/traces/:id/replay`：阶段级联重放。
  4. `GET /api/v1/citations/:id`：点击检索引用角标秒级调取正文分块。

---

### 8. 【15-AI应用-智能问答.png】(Smart QA Chat)
* **所属视图**：`ChatView.vue` / `pageChat()`
* **双重使命定位**：生产级业务请求流水线（支持 `[ 🔀 查看问答流程 ]` 携带 TraceID 一键跳转白盒诊断）
* **驱动区域**：会话分组（今天/昨天/更早）、Markdown 气泡对话流、严格证据佐证、引用来源卡片、相关 Wiki、点赞点踩反馈。
* **已实装 API 列表 (共 8 个)**：
  1. `GET /api/v1/conversations`：获取历史问答会话分组列表。
  2. `POST /api/v1/conversations`：新建会话并绑定知识库与数据集发布版本。
  3. `GET /api/v1/conversations/:id`：读取指定会话的历史全量消息、佐证引用与 Trace 标识。
  4. `DELETE /api/v1/conversations/:id`：删除会话记录及其消息快照。
  5. `POST /api/v1/conversations/:id/messages`：发送用户提问，触发完整 8 阶段流水线并流式返回佐证回答与引用。
  6. `POST /api/v1/messages/:id/feedback`：对单条回答进行“点赞/点踩”与改进建议提交。
  7. `POST /api/v1/messages/:id/wiki`：将问答对一键提炼沉淀为企业 Wiki 词条。
  8. `GET /api/v1/citations/:id`：调取单个引用切片的原文快照与 PDF 页码高亮定位。

---

### 9. 【16-AI应用-智能助手.png】(Assistants & Website Widget)
* **所属视图**：`AssistantsView.vue` / `pageAssistants()`
* **驱动区域**：智能助手卡片网格、创建专属助手、外挂企业网站 JS 代码接入、人工客服转接。
* **已实装 API 列表**：
  1. `GET /api/v1/assistants`：获取企业助手列表。
  2. `POST /api/v1/assistants`：创建新的智能助手（配置提示词、温度、绑定知识库）。
  3. `GET /api/v1/assistants/:id`：读取指定助手配置。
  4. `PATCH /api/v1/assistants/:id`：修改助手提示词与知识库绑定。
  5. `POST /api/v1/assistants/:id/publish`：发布上线助手。
  6. `POST /api/v1/assistants/:id/pause`：暂停助手对外服务。
  7. `GET /api/v1/assistants/:id/clients`：获取该助手生成的外挂网站客户端凭据。
  8. `POST /api/v1/assistants/:id/clients`：生成一段嵌入企业官网的 `<script>` 挂载代码。
  9. `POST /api/v1/widget-clients/:id/rotate`：轮换重置外挂防盗刷密钥 Token。
  10. `DELETE /api/v1/widget-clients/:id`：注销网站挂载客户端。
  11. `GET /api/v1/handoffs`：获取外部访客转人工客服申请列表。
  12. `PATCH /api/v1/handoffs/:id`：客服坐席接入对话会话。

---

### 10. 【17~20-系统设置与管理.png】(Settings, Storage, Models & Version)
* **所属视图**：`SettingsView.vue` / `pageGeneral()`, `pageModels()`, `pageStorage()`, `pageVersion()`
* **驱动区域**：界面主题切换、AI 大模型 Key 配置、本地 SQLite 持久化与快照备份、系统健康诊断。
* **已实装 API 列表**：
  1. `GET /api/v1/settings`：拉取全局系统偏好设置。
  2. `PUT /api/v1/settings/:key`：持久化保存主题（浅色/深色/跟随系统）或语言。
  3. `GET /api/v1/feature-flags`：读取实验性特性开关。
  4. `PUT /api/v1/feature-flags/:key`：切换特性开关。
  5. `GET /api/v1/models`：获取大语言模型与 Embedding 模型列表。
  6. `POST /api/v1/models`：接入新的模型服务提供商（Ollama、vLLM、OpenAI Compatible）。
  7. `PATCH /api/v1/models/:id`：修改模型 Base URL 或 API Key。
  8. `DELETE /api/v1/models/:id`：删除模型配置。
  9. `POST /api/v1/models/:id/test`：**模型连通性探测**，发送测试 Ping 校验 API Key 有效性与延迟。
  10. `GET /api/v1/backups`：获取本地数据库快照备份列表。
  11. `POST /api/v1/backups`：一键生成当前知识库与数据库全量物理备份文件。
  12. `POST /api/v1/backups/:id/restore`：从指定备份文件恢复系统数据。
  13. `GET /api/v1/version`：获取 Ordo 系统当前发行版本号（v1.8.0-enterprise）。
  14. `GET /api/v1/diagnostics`：获取数据库健康状态、磁盘物理读写状态与向量引擎健康报告。
  15. `GET /api/v1/audit`：获取系统敏感操作审计日志流水。
  16. `GET /api/v1/audit/verify`：校验审计日志防篡改哈希链。

---

### 11. 【21~22-全局模态框与快捷检索.png】(Modal Dialogs & Fast Search)
* **所属视图**：全局浮层 / `app.js` 模态管理
* **驱动区域**：新对话选择知识库、Cmd+K 全局毫秒级穿梭检索。
* **已实装 API 列表**：
  1. `GET /api/v1/search?q=...`：全局毫秒级模糊检索，跨知识库、文档、助手与设置项秒级穿梭匹配。
  2. `GET /api/v1/openapi.json`：服务端自省生成完整 OpenAPI 3.0 标准元数据。
