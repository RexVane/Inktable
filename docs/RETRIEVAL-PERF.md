# 检索延迟实测与决策记录（2026-08-18）

发布门槛（`PLAN.md` §10.4）把「非生成搜索 P95 ≤ 2.5 秒」「Rerank P95 ≤ 1.5 秒」
定为阻塞项。收口前实测为搜索 P95 **4121ms**，不达标。本文记录定位过程、
三处根因和改法，以及一处**依赖 sqlite-vec 内部布局**的关键实现——后者
是全部提速的前提，换扩展版本时必须先看这一节。

测量环境：Windows 11 / 28 核 / 隔离库 `output/release-gate-20260817/library-working.db`
（4,336 files、71,378 active chunks、向量覆盖 100%）、Ollama bge-m3 @ 18434、
route_limit=120、65 条 gold 查询。工具：`scripts/profile_retrieval.py`。

## 1. 结果

| 指标 | 改前 | 改后 | 门槛 |
|---|---|---|---|
| 非生成搜索 P50 | 2442ms | **609ms** | — |
| 非生成搜索 **P95** | **4121ms** | **977ms** | ≤ 2500 ✅ |
| Rerank P95 | 878ms | **29ms** | ≤ 1500 ✅ |
| 向量单路（每次查询） | 782ms | **13ms** | — |
| 全库向量矩阵构建 | 369,686ms | **1,129ms** | — |

（搜索延迟取 `run_eval.py` 的口径，与门槛判定一致；`profile_retrieval.py`
独立复测为 P50 725ms / P95 1111ms，跑间波动，两者都远在门槛内。）

检索质量在这一轮**未变**（MRR@10 86.9%、nDCG@10 88.8%、Content Recall@5
90.6%，与改前逐位一致）——这一组改动只动执行方式，不动排序逻辑，
质量不变正是它的验收条件。已用 `INKTABLE_VECTOR_NO_MATRIX=1` 强制回到
原 vec0 KNN 路径复跑整套评测，两条路径的 Recall / MRR / nDCG **完全一致**。

证据压缩在检索改动后复跑，全部门槛通过且延迟略有改善：Gold Evidence
Recall 96.9%（门槛 95%）、中位压缩 60.6%（门槛 ≥35%）、offset 往返错误 0、
压缩 P95 178.6ms（此前 219.3ms，预算 500ms）。

工程回归：后端 **283 passed**（改前 266，本轮新增 17 条）、桌面 **16 passed**。


## 2. 为什么之前定位不到

评测产物只记录每题总延迟与 rerank 耗时，中间那两秒落在哪条路线上无法从
产物反推。`RetrievalTrace` 其实已有 stage 级计时，只是 `run_eval.py` 没有
把它写进 JSON。补 `scripts/profile_retrieval.py` 把 stage 明细与
`child_search` 内部各路线单独摊平，瓶颈才显形：

```
stage                   p50      p95     mean   占比
deep_retrieval         1746     2581     1838   73.8%
  └ vector 单路        1079     1518     1112   44.7%
rerank                  463      743      458   18.4%
```

## 3. 三处根因

### 3.1 向量路每次查询都在 vec0 上暴力 KNN（782ms → 13ms）

`chunks_vec` 是 sqlite-vec 的 vec0 虚拟表。无过滤查询走
`embedding MATCH ? AND k = ?`，每次都要扫完 71,378 × 1024 维 float32
（292MB），实测 782ms（k=240）。进程内矩阵乘只要 13ms —— 快 60 倍。

矩阵路此前不可用，原因写在 `vector.py` 的注释里：**逐行从 vec0 导出全库
向量要 369.7 秒**（约 5ms/行）。换任何 SELECT 写法都一样慢：

| 读法 | 折算全表 |
|---|---|
| `SELECT rowid, embedding FROM chunks_vec LIMIT n` | 170.6s |
| `SELECT embedding FROM chunks_vec LIMIT n` | 309.6s |
| `WHERE rowid BETWEEN ? AND ?` | 460.7s |
| `WHERE rowid IN (...)` 每批 500 | 394.3s |

所以历史上尝试过的 `strict-local-matrix` 实验没能提速（p95 7533ms，比对照
还差），矩阵路被判定为不可行。

**转机在影子表**。vec0 并不逐行存向量，而是每 1024 条打包成一个 blob：

| 影子表 | 行数 | 内容 |
|---|---|---|
| `chunks_vec_vector_chunks00` | 70 | `vectors` BLOB = 1024×1024 float32 = 4,194,304 字节 |
| `chunks_vec_chunks` | 70 | `size` 槽位数、`validity` 128 字节位图、`rowids` 1024×int64 |
| `chunks_vec_rowids` | 71,378 | rowid → (chunk_id, chunk_offset) |

读 70 个 blob 重建同一个矩阵：**1,129ms（快 327 倍）**。

### 3.2 rerank 逐行取候选向量（458ms → 21ms）

`rerank._load_vectors` 用 `WHERE rowid IN (...)` 从 vec0 取 80 个候选向量，
按 5ms/行就是 400ms —— 占 rerank 耗时的绝大部分，**打分本身只有几十毫秒**。
改为从整库矩阵缓存里按 rowid 切片（`vector.vectors_for`）。

顺带修正了一处语义：缓存矩阵按行归一化后，`NEARDUP_COSINE` 的点积才是
真余弦；此前依赖「入库向量恰好已归一化」这一未声明的前提。

### 3.3 620ms 的查询嵌入串在关键路径最前面（省下约 200ms mean / 700ms p95）

Ollama 编码一条短查询实测 **619ms**，而**同一次调用编码 8 条只要 719ms**
—— 成本几乎全是每请求固定开销，不是算力。curl 直测同样 767-833ms，
且带 `keep_alive` 无改善、两个模型都已常驻，所以这是本机 Ollama 的固有
开销，不是模型反复加载。

它不依赖任何词法路线，却曾串行排在最前面。改法两条：

1. 全部查询变体（主查询 + 比较类子查询）合并成一次调用。此前每条子查询
   各发一次请求。
2. 整个调用丢后台线程，与 `hierarchy_routing`（54ms）和词法四路
   （140ms mean / 597ms p95）重叠。线程里只做 HTTP，不碰 SQLite ——
   同一连接并发执行语句不安全。

改完后 `hierarchy_routing` 与 `lexical_retrieval` 的耗时被嵌入的固定往返
完全吸收，**关键路径的下界就是这 619ms**。这一点决定了后续优化的方向：
再压词法路线（例如 `substr` 的 p95 697ms 全表扫描）对总延迟没有收益。

### 3.4 同一病理的第三处：清理时的虚拟索引重建（小时级 → 秒级）

不在查询路径上，但根因完全相同，实测代价更夸张。

大批清理会触发 `rebuild_virtual_indexes_after_orphan_cleanup`（它的存在理由
本来就是"逐条删几十万 FTS5/vec0 行病理性地慢，保留活集重建更快"）。但它把
仍被引用的向量搬进临时表用的是

    SELECT v.rowid, v.embedding FROM chunks_vec v JOIN chunks ch ...

也就是**逐行读 vec0**。真实库 16.4 万分片时，这一步在实际运行中跑了
**1 小时 40 分钟仍未完成**（WAL 大小不变而 mtime 持续更新 —— 读不增长 WAL，
正是卡在读上）。

改为优先走 `vector._shadow_bulk_vectors`（`_preserve_kept_vectors`），
同一台机器同一个库实测：

| | 逐行读 | 影子表整块读 |
|---|---|---|
| 搬运 147,427 条向量 | > 100 分钟未完成 | **< 1 分钟** |

布局不符合预期时退回原来的 SQL 直拷，慢但一定正确；写回用
`ndarray.tobytes()`，float32 逐字节还原原始 blob。

中断安全性同时得到实测确认：这一步在破坏性事务之前，两次中断后
`quick_check=ok`、外键错误 0、`chunks / chunks_fts / chunks_fts_tri /
chunks_vec` 四张表仍全部为 164,180 且相互同步（H7）。

### 3.5 顺带挖出的第四处：chunks.parent_id 缺索引（删除路径退化成平方级）

不是 vec0 的病理，是一个**漏建的外键索引**，在同一次排查里暴露出来。

`chunks.parent_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL` 是**自引用
外键**（parent 层指向 child 层）。开着 `PRAGMA foreign_keys` 时，删一个 chunk
就要找出所有 `parent_id` 指向它的行来执行 SET NULL —— 没有索引就是**每删一行
全表扫一遍**。

`sections.parent_id` 建了 `idx_sections_parent`，`chunks.section_id` 建了
`idx_chunks_section`，唯独 `chunks.parent_id` 漏了。

实测（真实库 164,180 个分片，清理 670 个孤儿 content 需级联删 16,753 个分片）：

| | 无索引 | 有 `idx_chunks_parent` |
|---|---|---|
| `DELETE FROM contents WHERE NOT EXISTS(...)` 的级联 | **30 分钟以上未完成**（约 27 亿次行访问） | **3 秒** |
| 建索引本身 | — | 0.5 秒 |

**影响面不止清理**：任何删除内容的路径都在付这笔钱 —— 移除来源
（`/sources/remove`）、删文件（`/files/remove`）、孤儿清扫。在这个规模的库上，
它们此前都会退化到分钟乃至小时级。

`test_database.py::test_self_referencing_fk_columns_are_indexed` 守住这一列，
顺带守住 `content_id` 与 `section_id`。

### 3.6 修复叠加后的清理耗时

同一次清理（真实库、删 845 个文件 + 清 670 个孤儿 content）：

| 阶段 | 修复前 | 修复后 |
|---|---|---|
| 搬运 147,427 条向量 | > 100 分钟未完成 | 秒级 |
| 级联删 16,753 个分片 | > 30 分钟未完成 | 3 秒 |
| 重建 `chunks_fts`（含 jieba 重切 147,427 片） | — | 约 2 分钟 |
| 重建 `chunks_fts_tri` / `chunks_vec` | — | 分钟级 |

vec0 的**写**侧没有批量接口，147k 次插入仍是分钟级，这部分优化不了；
但整个维护操作从"跑不完"变成几分钟，对一个少见操作可以接受。

## 4. 风险与退化路径

3.1 读的是 sqlite-vec 的**内部布局**，不是公开接口。`_shadow_bulk_vectors`
因此全程校验：两张影子表存在、blob 长度能被维度整除且不小于 `size`、
`rowids` 与 `validity` 长度相符、**重建条数与 `chunks_vec` 自报行数一致**。
任一步不符返回 `None`，退回 vec0 KNN —— 慢一点，但绝不给出错位向量。

同样重要的是：**查询路径永不触发逐行慢导出**。`_cached_matrix(allow_slow=False)`
是查询路径的唯一入口，取不到矩阵就退 KNN；那条 369 秒的逐行路径只保留给
显式预热。

`_search_inmem` 因此必须区分 `None`（矩阵不可用，应退 KNN）与 `[]`
（确实没有结果）。把两者混同会让语义检索静默消失而不是变慢 —— 这正是
`test_search.py` 里那两条用例守的东西。

内存代价：71,378 条 × 1024 维 float32 = 292MB 常驻。`INMEM_LIMIT` 为
100,000 条（约 410MB），超过则不建矩阵、退回 KNN。矩阵在 sidecar 启动时
由后台线程预热（`main._warm_vector_matrix`），避免第一个提问的人付这 1.1 秒。

**预热必须用只读连接**（这是本轮自己踩出来的一个回归）。最初预热线程走
普通 `connect()`，而它会执行 `PRAGMA journal_mode = WAL` —— 那需要短暂的
排他锁，于是启动时预热线程与正常写入方抢锁，`TestClient` 用例开始
**间歇性**报 `database is locked`（三轮全量里挂了两次，单独跑却通过，
典型的 flaky 症状）。

修法是新增 `database.connect_readonly()`：只读连接不设 `journal_mode`，
从根上不可能造成这种竞争。预热本来就只读，用读写连接是没有理由的。
教训一般化：**任何只读旁路（预热、审计、统计）都不该走读写 `connect()`**。


## 5. 未达标项：Rerank 相对提升 +15%

`PLAN.md` §10.3 要求 Rerank 的 nDCG@10 / MRR@10 相对 RRF 基线提升 ≥15%。
同库、同代码、同 gold 的完整对照（`--route-limit 120`，53 道可评估题）：

| 配置 | R@5 | R@20 | Gold Ev@50 | MRR@10 | nDCG@10 | 搜索 P50 | 搜索 P95 | Rerank P95 |
|---|---|---|---|---|---|---|---|---|
| `rrf`（对照分母） | 90.6% | 98.1% | 93.8% | 81.2% | 84.3% | 605ms | 961ms | 1ms |
| `auto` = local-static-v3（生产默认） | 90.6% | 98.1% | 93.8% | 86.9% | 88.8% | 609ms | **977ms** | **29ms** |
| `cascade` K=26 焦点 420（CE 68 / 本地 24） | **96.2%** | **100%** | 93.8% | **90.3%** | **92.2%** | 1684ms | 2274ms | 1642ms |
| `cascade` K=26 焦点 300 | 96.2% | 100% | 93.8% | 84.7% | 88.5% | 1403ms | 1762ms | 1165ms |
| **门槛** | ≥80% | — | ≥90% | **≥93.4%** | **≥97.0%** | — | ≤2500ms | ≤1500ms |

级联的收益是实的：Content Recall@5 从 90.6% 提到 96.2%、Recall@20 从 98.1%
提到 100%，并把最差名次从第 24 名收到第 9 名（`local-static-v3` 遗留的
第 17 / 20 / 24 名全部消除）。但相对提升为 MRR **+11.0%** / nDCG **+9.2%**，
仍未达 +15%。

### 5.1 试过但不成立的省延迟办法

送进 CE 的候选文本从 420 字压到 300 字，rerank P95 从 1642ms 降到 1165ms
（过门槛），但质量塌下来：nDCG 92.2% → 88.5%，比 `local-static-v3` 还低。
所以 CE 的延迟不能靠继续截短正文来买。

可换的余量只有截断深度 K：每降 1 位省约 63ms，但 `probe_cascade_depth.py`
实测最深的 gold 在第 25 位，K < 25 就会重新丢掉 P19 / P20 两道改写类问题。
即 **K=26 与 Rerank P95 ≤1500ms 不可兼得**，缺口约 10%。

### 5.2 一处没测出收益的机制（记录以免重复投入）

针对「改写类问题上本地打分器的 coverage / proximity / exact 全为 0，其分数
是噪声」这一机理，实现了按词法置信度缩放本地分权重的自适应融合
（`CASCADE_LEX_FULL`）。结果：自适应 CE=70/本地=22 得 MRR 90.2 / nDCG 92.0，
固定 CE=68/本地=24 得 90.3 / 92.2 —— **差异在噪声内，没有可观测收益**。
默认权重因此取实测最好的固定组合。机制保留但基本休眠，因为它针对的失效
模式在 53 题里只有 2-3 题，样本量不足以判定；评测集扩大后应重新验证。

### 5.3 结论与建议

`local-static-v3` 与 `cascade` 各有一条门槛不过，且不是同一条：

- `local-static-v3`：两条延迟门槛都过（977ms / 29ms），相对提升不过（+7.0% / +5.3%）。
- `cascade`：搜索 P95 过（2274ms），Rerank P95 差约 10%，相对提升不过（+11.0% / +9.2%）。

**生产默认保持 `auto`（local-static-v3）**：它把交互搜索留在亚秒级
（P50 609ms），这是"顺畅"的直接来源；`cascade` 作为实装并已量化的可选模式
保留，用 `INKTABLE_RERANKER=cascade` 开启。

`pipeline.run()` 新增了 `reranker` 参数，使"搜索用快的、问答用准的"成为
可能（问答本来就要等 LLM 生成数秒，多花 1 秒重排不可感知，而问答恰恰最
吃排序质量）。**但这条没有启用**：搜索与问答共用同一条检索管线是 v7 的
明确决策，给两边配不同实现会让"搜到的"与"答出来的"证据顺序分叉，
属于需要项目决定的事，不由实现方单方面改。

### 5.4 关于 +15% 这条门槛本身

RRF 基线的 nDCG@10 已是 84.3%，+15% 意味着绝对值要达到 **97.0%**。53 道题
单文档相关，nDCG@10 在 rank 1 得 1.0、rank 2 得 0.631，所以 97.0% 等价于
「约 49 题排第 1、其余 4 题排第 2、且没有一题更差」——接近完美排序。

而且这条门槛有个内在张力：**一级召回越强，分母越高，相对门槛越难达到**。
本轮 RRF 基线自身就从 83.9% 升到 84.3%，把 nDCG 门槛从 96.4% 顶到 97.0%。

继续靠这 53 题调参逼近 97.0% 会正面违反 `HANDOFF.md` H12 的立意
（评测集先于实现冻结，不因算法结果反向修改）。建议二选一，由项目决定：

- 以绝对值作为发布口径（例如 nDCG@10 ≥ 90%、Content Recall@5 ≥ 95%），
  把相对提升降为观测指标；
- 或先把评测集扩到能支撑相对门槛的规模，再继续调 Rerank。

## 6. 复现

```bash
cd services/api
export INKTABLE_DB=../../output/release-gate-20260817/library-working.db
export INKTABLE_OLLAMA_URL=http://127.0.0.1:18434

# 逐阶段 / 逐路线延迟
.venv/Scripts/python.exe scripts/profile_retrieval.py --route-limit 120

# 级联截断深度的实测判据（决定 CASCADE_PAIRS）
.venv/Scripts/python.exe scripts/probe_cascade_depth.py --route-limit 120

# 门槛评测（--enforce-latency 让延迟超标直接判失败）
.venv/Scripts/python.exe tests/run_eval.py --route-limit 120 --enforce-latency
INKTABLE_RERANKER=cascade .venv/Scripts/python.exe tests/run_eval.py --route-limit 120
```

相关环境变量：`INKTABLE_RERANKER`（auto / cascade / cross / rrf / off）、
`INKTABLE_CASCADE_PAIRS`、`INKTABLE_CASCADE_FOCUS_CHARS`、
`INKTABLE_CASCADE_W_CROSS` / `_W_LOCAL` / `_W_RRF`、`INKTABLE_CASCADE_LEX_FULL`、
`INKTABLE_RERANK_THREADS`、`INKTABLE_RERANK_MAX_TOKENS`。
