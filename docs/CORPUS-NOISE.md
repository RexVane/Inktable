# 语料噪声与按目录排除（2026-08-18）

用户在真实使用中反馈两条：**"回答的内容好少"** 与 **"有很多没用的 .md
文件在干扰"**。本文记录第二条的定位过程、两条**试过但不成立**的自动识别
方案，以及最终采用的机制。第一条见文末。

## 1. 现象与量化

真实库（迁移后）当时的可见文件构成：

| 扩展名 | 可见数 |
|---|---|
| **.md** | **2,552（41%）** |
| .txt | 1,380 |
| .html | 898 |
| .pdf | 736 |
| .docx | 665 |
| .csv / .htm | 18 / 17 |
| 合计 | 6,266 |

`.md` 集中在少数目录，且文件名高度重复：

| 数量 | 目录 |
|---|---|
| 664 | `B:\openclaw\openclaw_improve\docs` |
| 567 | `B:\TradingAgent\TradingAgents-CN\docs` |
| 418 | `D:\AIApp\Agent\Auto-claude-code-research-in-sleep` |
| 214 | `C:\Users\guica\OneDrive` |

最常见文件名：`readme.md` 348、**`skill.md` 265**、`index.md` 26、
`readme.en.md` 22、`changelog.md` 17、`agents.md` 14。

**根因**：入库白名单按**扩展名**过滤，而 `.md` 同时是用户笔记的格式**和**
每个代码仓库、每个 AI agent 配置的格式。扩展名区分不了这两者。

另外核实过一件事：那 1,746 个云服务器备份目录下的 `.md` 是 `missing`
且无保全副本，按 `VISIBLE_FILES_COND` 本来就不可见，**没有参与干扰**
（第一次统计漏了可见性过滤，数字偏大）。

## 2. 两条试过但不成立的自动识别

### 2.1 打开整盘的代码项目剪枝 —— 会删掉个人证件

`iter_files` 有 `prune_projects`，`policy.resolve_source_policy` 对固定盘根
返回 `False`（`scanner.py` 的注释说明是"整盘收录只按扩展名白名单决定"）。
把它打开看起来正是代码注释里写的设计意图（"整盘来源不收项目文档"）。

实测结果：**6,266 个可见文件里 5,409 个（86%）会消失**，其中包括

```
C:\Users\guica\OneDrive\Desktop\个人简历.docx
OneDrive\Desktop\升学文件\…专利…通知书.pdf
OneDrive\图片\证件\奖状原件.pdf
C:\Users\guica\WPSDrive          ← 753 个文件
```

**进一步定位到一个真 bug**：用户家目录 `C:\Users\guica` 里有一个 `.git`
（dotfiles 仓库或工具留下的），`_looks_like_code_project` 因此对家目录返回
True，于是家目录**以下所有**文件都被判成"在代码项目内"。

已修（`scanner._is_profile_or_drive_root`）：**家目录与盘根永远不算代码
项目** —— 那一级的标记是 dotfiles，不是"这棵树是代码项目"。修完后同样的
剪枝操作变成：

| | 修复前 | 修复后 |
|---|---|---|
| 会被剪掉 | 5,409 / 6,266 | 3,790 / 5,526 |
| .pdf 被剪 | 401（54.5%） | **10（1.4%）** |
| .docx 被剪 | 318（47.8%） | **0** |

但仍**没有**打开剪枝，因为剩下的误伤是真内容：

```
OneDrive\Desktop\大学文件\…\SimOS\实现路线图.md
OneDrive\文档\Agent面试\Agent面试题分类整理.md
OneDrive\文档\Agent面试\Agent面试题分类整理-答案版.md
```

用户把笔记写在带代码标记的目录里，这在个人机器上很常见。

### 2.2 把 `docs/` 当项目文档 —— 会打断 gold 评测

只在"代码项目内部"排除 `docs/` 子树，能多清 1,523 个（其中 1,435 个 `.md`），
个人文件哨兵全部安全。看起来是精准的一刀。

但抽查个人区域时发现它会删掉

```
OneDrive\文档\GitHub\InkHole\docs\墨洞项目计划.md
OneDrive\文档\GitHub\InkHole\docs\跨网络传输方案.md
```

这是用户**自己**项目的设计文档，而 gold 评测的 X07 题正是
「墨洞项目与 FTP 课程项目的文件传输定位有什么不同」—— 删了直接打断评测。

**克隆的第三方项目与用户自己的项目，在路径上无法区分。** 所以这条不做成
规则（代码里保留了这段结论，见 `should_skip_path` 的注释）。

## 3. 采用的方案

### 3.1 按文件名挡样板（已生效，零误伤）

分两层，因为"是不是噪声"对位置的依赖程度不同：

| 层 | 内容 | 作用范围 |
|---|---|---|
| 一 | `skill.md` / `agents.md` / `claude.md` / `gemini.md` / `cursorrules` 等 **agent 与编辑器机器配置** | 任何位置无条件挡 |
| 二 | `readme` / `changelog` / `contributing` / `codeofconduct` / `notice` / `history` 等**仓库样板** | 只在代码项目内部挡 |

第二层必须结合路径：同一个 `README.md` 在用户资料目录里可能是他自己写的
说明，在 git 仓库里就是样板。本地化与序号变体（`README.en.md`、`README_CN.md`、
`changelog-zh.md`）也算，但**余下部分必须是纯 ASCII 短标记** ——
`readme笔记.md` 带中文后缀，说明是用户自己写的内容，必须保住。

实测清理掉 **845 个**（`filename` 383 + `policy` 462），受保护 0、误伤 0。

清理侧同时补了一个缺口：`ingestion_cleanup` 原来只用占位文件名
`__inktable_policy_probe__.txt` 做**目录级**探测，评估不到与真实文件名相关的
规则，导致 348 个 `README.md` 一直躲过清理。现在按真实路径再判一次。

### 3.2 安装目录剪枝与代码项目剪枝分开门控（已生效，零 PDF/DOCX 风险）

`exclude_dirs.py --list` 把"占地方的目录"排出来之后，噪声的另一半立刻显形：

| 文件数 | 目录 | 性质 |
|---|---|---|
| 751 | `C:\Users\guica\WPSDrive`（.pdf 383 / .docx 312） | **真文档** |
| 690 | `B:\WeChat profiles\xwechat_files\…`（.docx 345 / .pdf 278） | **真文档** |
| 604 | `D:\Android\gradle\caches`（.txt 604） | 构建缓存 |
| 378 + 247 | `D:\git\Git\mingw64` / `usr` | Git 安装目录的文档 |
| 168 | `D:\Android\Sdk\platforms` | Android SDK |

`iter_files` 原来把**代码项目剪枝**和**安装目录剪枝**放在同一个
`prune_projects` 门控下，整盘收录时一起关掉。但两者的歧义程度完全不同：

- 代码项目：用户确实会把真笔记写在带 `.git` 的目录里（实测），不能剪。
- 安装树：没人把个人资料放进 Git 安装目录或 Android SDK；而
  `INSTALL_MARKERS` 认的是 `git.exe` / `adb.exe` / `mvn.cmd` 这类**具体
  可执行文件**，不是通用目录名，误判空间极小。

拆开门控后实测（可见文件 5,526）：

| | 结果 |
|---|---|
| 剪掉 | 1,456（26%） |
| 扩展名 | `.txt` 898、`.html` 507、`.md` 48、`.csv` 3 —— **`.pdf` 与 `.docx` 各 0 个** |
| 个人文件哨兵 | 5/5 安全 |
| WPSDrive / 微信接收目录 | **各 0 个被剪** |

零 PDF/DOCX 被触及，是这一刀可以放心落下的判据。

实现上两处：`iter_files` 的目录遍历里安装树不再受 `prune_projects` 约束
（顺带避免整棵 gradle 缓存被走一遍）；`should_skip_path` 新增独立的
`check_install` 开关，默认开，扫描路径显式传 `False` —— 那里遍历已在目录级
剪过，逐文件再探会对每层祖先重复 stat。清理侧因此也能移除已入库的这批记录。

### 3.3 按目录排除，由用户决定（新增机制）

剩下的噪声（openclaw/TradingAgents 的 docs 等约 1,700 个）只有用户知道
哪些该留。所以提供机制而不是猜测：

```bash
cd services/api
.venv/Scripts/python.exe scripts/exclude_dirs.py --list          # 看哪些目录最占地方
.venv/Scripts/python.exe scripts/exclude_dirs.py --add "B:/openclaw"
.venv/Scripts/python.exe scripts/exclude_dirs.py --remove "B:/openclaw"
```

对应 API：`GET /sources/exclusions`、`POST /sources/exclude`、
`POST /sources/unexclude`。桌面端 UI 尚未接入。

性质上的保证：

- **只作用于索引层**。不移动、不改名、不删除磁盘上的任何文件（§1 约束 1）。
- **记录保留**。被排除的文件置为 `ignored`，`VISIBLE_FILES_COND` 立即
  把它们从浏览、检索、问答里去掉；取消排除后恢复为 `registered`。
- **边界按路径判定**，不用字符串前缀 —— `B:\foo2` 不是 `B:\foo` 的子目录，
  而 `LIKE 'B:\foo%'` 会把它算进去。
- **嵌套安全**。取消外层排除时，仍被内层排除覆盖的文件不会被放出来。
- 扫描侧复用 `iter_files` 已有的 `prune_roots`，不另铺管道。

## 4. "回答的内容好少"

同一轮反馈的另一条。根因在提示词，不在预算：上下文给到 64,000 字符 /
120 个分片，`answer_max_tokens` 是 `auto`（不传上限），但提示词写着

> 只回答问题直接要求的事实，通常用 **1 至 6 个简短条目**……不要添加背景
> 知识、泛化建议、推理过程、总结复述……标题、表格和无引用的过渡句都不要输出

这是为把 `citation_support_rate ≥95%` 做高而优化的写法 —— 每行一个可验证
事实、句句带引用最安全，代价就是读起来单薄。**提示词在按指标优化，
而不是按体感。**

已改为：去掉人为的条数上限（"覆盖资料中直接回答问题的**全部**事实，
通常 3 至 12 行，证据充分时可以更多"），并要求每行写成完整的一句话、
把相伴的数值与限定条件写进同一句。**一条安全约束都没放松**：仍然不加背景
知识、不加推理、每行一个可验证事实并带引用、无依据时只输出拒答句。

### 4.1 复验状态：尚未验证（provider 全部不可用）

放长回答会增加未获支持声明的机会，所以必须用 65 题真实模型 QA 复验引用门槛。
2026-08-18 尝试复验时，cc-switch 里 7 个配好的 provider 全部不可用：

| provider / model | 状态 |
|---|---|
| myself / gpt-5.6-terra、modelshare | HTTP 503 |
| kocode、Z30、ModelShare、佬友炸弹车（gpt-5.6-sol） | HTTP 403 |
| anyrouter | 超时 |

**因此 `docs/eval` 与 CURRENT_STATUS 第九节记录的引用支持率 96.99% 等数字，
是用旧提示词测出来的**；在复验通过之前不应把它当作当前代码的门槛结果。

风险性质要分清：引用的**强制执行在代码里，不在提示词里**（`HANDOFF.md` H8
「prompt 是建议，校验是执行」）。四条后置硬校验一行未动，且已用
`build_messages` 直接断言新提示词仍禁止背景知识与推理、仍要求每行带引用、
仍保留拒答规则。所以放长回答的风险是**指标可能下滑**，不是安全性下滑。

provider 恢复后：

```bash
cd services/api
INKTABLE_DB=../../output/release-gate-20260817/library-working.db INKTABLE_OLLAMA_URL=http://127.0.0.1:18434 .venv/Scripts/python.exe scripts/run_qa_eval.py   --provider myself --model gpt-5.6-luna --reranker auto   --ollama-url http://127.0.0.1:18434 --case-delay 2 --judge-retries 3   --json ../../output/release-gate-20260817/qa-luna-65-longer.json
```

判据：引用支持率 ≥95%、精确引用率 100%、句级引用覆盖 100%、正确拒答 12/12、
provider 故障 0。任一项跌破就把提示词改回原来的「1 至 6 个简短条目」——
不为"回答长一点"牺牲引用可靠性。

### 4.2 部分证据：同模型 8 题对比（不构成门槛结论）

`佬友炸弹车 / gpt-5.6-sol` 短暂可用时跑了一轮，第 12 题被 403 打断（4 次
provider 故障触发 `--max-consecutive-provider-failures`）。这个模型**正是最初
门槛用的 gpt-5.6-sol**，所以拿它与 `docs/eval/v8-final-qa.json`（同模型、旧
提示词）在重叠的 8 道题上比，能得到一份控制了模型变量的对比：

| 指标（中位） | 旧提示词 | 新提示词 | 变化 |
|---|---|---|---|
| `answer_chars` | 46 | **172** | **+278%** |
| `claims` | 2 | 4 | +100% |
| `statements` | 2 | 4 | +100% |
| `supported_claims` | 2 | 4 | +100% |

仅看这 8 道真正作答的题：

| | |
|---|---|
| 引用支持 | **26/26 = 100%** |
| 精确引用 | 14/14 |
| 句级引用覆盖 | 26/26 |
| 虚构引用被剔除 | 0 |
| 过程中捕获的无依据声明 | 1（触发重生成后修正） |
| 重生成生效 | 2 题 |

即：回答长了约三倍，而**每一条声明都仍有依据**；`supported_claims` 与
`claims` 在 8 行里逐行相等。四条后置硬校验也确实在工作（2 题走了"零/不足
引用 → 重生成一次"，1 条无依据声明被捕获）。

产物汇总里的 `citation_support_rate = 86.67%` 是被那 4 个 fallback 拉低的 ——
它们是 provider 故障（连不上 / 403），claims 计入分母而 supported 为 0，
不是无依据声明。

**样本量 8 / 65，不构成门槛结论**：这轮完全没跑到 12 道 unanswerable 题，
所以正确拒答率无从判断，而那恰恰是放长回答最该担心的一项（更长的输出更容易
在该拒答时夹带事实）。门槛仍以 §4.1 的完整复验为准。


---

## 5. 2026-08-26：真因是 B:\devcache（97.1%），不是 .md 样板

用户报「快 10 万个文件」「检索效果和知识库很差」。先量再改，盘点结果推翻了
从界面截图得到的第一印象（我原以为主因是哈希命名的 .html 缓存 —— 那类实际
只有 64 个）。

`scripts/audit_corpus_noise.py` / `scripts/audit_devcache_share.py`（只读）：

| | 数量 | 占已登记 |
|---|---:|---:|
| 已登记文件 | 110,211 | 100% |
| `.html` | 103,958 | 94.3% |
| **`B:\devcache` 下** | **107,026** | **97.1%** |
| ├ rustup toolchains（stable + nightly 两套 `share/doc/rust/html`） | 92,090 | 83.6% |
| ├ gradle wrapper / caches | 11,905 | 10.8% |
| ├ gomodcache / cargo registry / uv-cache | ~2,700 | 2.4% |
| 哈希命名（>=16 位十六进制主名） | 64 | 0.06% |

`B:\devcache` 是开发工具缓存根目录，下面**只有**工具链产物：rustup 的两套
文档树（内容相同，这正是 17,807 个 content 有多副本的来源）、gradle/go/cargo/uv
的下载缓存。BM25 的分母里 97% 是这些东西 —— 真资料的排名被稀释，这就是
「检索效果差」的机制。

**处置**：`scripts/exclude_dirs.py --add "B:/devcache"`。只作用于索引层
（`state='ignored'`），磁盘一个文件都没动，取消排除后重新扫描即恢复；扫描端
把排除根并进 `prune_roots`，下次扫描不会再登记。

排除前先用 SQLite backup API 做了一致快照（穿过 WAL）：
`backups/library-pre-devcache-exclude-20260826.db`（1,805 MB，`quick_check=ok`，
含 114,186 条 files 记录）。

**核对结果**（`scripts/verify_exclusion.py` / `verify_no_collateral.py`）：

- 可见文件 110,211 → **3,185**；devcache 残留 **0**
- 剩余构成正是真资料：`.md` 1,491、`.txt` 604、`.pdf` 406、`.docx` 404
- 真资料目录被连带排除的文件数 **0**（Documents 898、微信 697、QQ 58、
  OneDrive 171 全部保留）
- **gold 证据 content 29/29 仍可见** —— 这条最关键：若 gold 资料被排掉，
  评测会凭空变好（正确答案不在库里，"没找到"反而不算错），指标就失去意义
- 可见活跃分片 244,611 → **46,075**

## 6. 附带发现：Windows 云占位检测从未生效，静默丢掉 624 个真文档

核对 WPSDrive 时发现候选列表 753 个、可见只有 129 个。差额不是排除造成的
（连带排除为 0），而是 **624 个记录处于 `state='failed'`，`error_code='hash_failed'`**，
其中 326 个 PDF、257 个 DOCX —— 正是用户最在意的那类资料。

根因：`app/domain/identity.py` 的 `is_dataless` 只看 `st_flags & SF_DATALESS`，
而那是 **macOS/BSD** 的位。Windows 的 `os.stat()` 没有 `st_flags` 字段，
`getattr(st, "st_flags", 0)` **恒为 0** —— 占位检测在 Windows 上从来没有生效。
于是云端未下载的文件一路走到读取，`open()` 抛
`OSError [Errno 22] Invalid argument`，被记成「哈希失败」。

用户看到的是「索引坏了」，实际是「文件没下载」，两者处置完全不同：前者要修
代码，后者只需在 WPS 里把文件下载到本地。

实测那个文件的属性是 `0x400020` =
`FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_ARCHIVE`。修复把判断
收进 `is_cloud_placeholder(st)`，Windows 侧检查三个位（`OFFLINE` /
`RECALL_ON_OPEN` / `RECALL_ON_DATA_ACCESS`），macOS 侧保持 `SF_DATALESS`。

修复后 `identify()` 对该文件返回 `is_dataless=True`，扫描会走
`state='cloud_placeholder'` 分支而不再尝试读取。既有的 624 条会在下次扫描被
重试 —— `register_file` 的「未变化」短路显式排除了 `HASH_FAILED`，有测试守着
这一点（少了它，那 624 条会因 size/mtime 没变而被永远跳过）。


## 7. 2026-08-26（同日续）：剪掉已排除内容的索引行

排除只把 `files.state` 置成 `ignored`，索引行原样留在库里。审计
（`scripts/audit_index_residue.py`，只读）：

| 表 | 总行数 | 属于已排除内容 | 占比 |
|---|---:|---:|---:|
| chunks / chunks_fts / chunks_fts_tri | 244,688 | 198,613 | 81.2% |
| chunks_vec | 181,989 | 135,914 | 74.7% |

`cleanup_ingestion_noise.py` 处理不了这批：它的判据是「content 没有任何 files
行」（孤儿），而被排除的文件 files 行仍在，所以干跑恒为 `removals=0`。

### 7.1 两个假设，都被实测否掉

先前把这件事归成「省 1.8GB 磁盘」，归错了。检索代码里确实有两处会被残留影响：

1. `bm25()` 的 IDF 与长度归一化用的是**整张 FTS 表**的统计量。可见性过滤写在
   `WHERE` 里，但分数是先算出来的 —— 81% 的噪声还在分母里。
2. 向量路是「先 KNN、后过滤」（`app/index/search.py::_vector_search`），超取
   上限 3 倍。残留占 74.7% 时 top-3k 大部分会被过滤掉，**有效深度**下降。

由此预测「剪掉之后 nDCG 明显上升、paraphrase 类题目受益最大」。**预测错了。**
65 题 A/B（同一份快照，剪枝前后各跑两遍，结果可复现）里只有 **3 题**动了名次：

    F28  rank 2 → 1     （nDCG/MRR 的涨幅全部来自这一题）
    A09  rank 15 → 16
    P19  rank 20 → 22   （Recall@20 的 −1.9pp 就是这一题掉出 top-20）

paraphrase 仍是 8/11，向量路照样返回满深度 20。也就是说：**剪枝是质量中性的**，
上面两条机制真实存在但量级远小于预期。把它当作质量手段是我的误判。

### 7.2 判据必须是「每份副本都被排除」，不是「没有可见副本」

第一版判据写的是「没有可见副本」。它多剪掉 56,735 个分片 —— 属于 **3,246 篇
真文档**，它们不可见的原因是 `state='missing'`（外置盘拔了、云盘文件没下载），
不是被排除。

这是**静默永久失踪**：剪掉索引行没有对应的唤醒路径（只有「取消排除」会
requeue），盘插回来之后内容仍是 `parse_state='excluded'`、零分片，而且不报错。

判据收窄为「该 content 的**每一份**副本都是 `ignored`」。排除是用户的显式决定，
`missing` 与「源被禁用」都是过渡态，只对显式决定动手。两个判据的实测差别不只
是安全性 —— 宽判据的 nDCG 是 **87.3%（比不剪还低 0.3pp）**，窄判据是 88.3%：
把真文档从 BM25 统计量里拿掉反而伤了排序。

### 7.3 实测（真实库快照，65 题，route_limit=120，degraded=0）

| 指标 | 剪枝前 | 剪枝后 | 门槛 |
|---|---:|---:|---:|
| nDCG@10 | 87.6% | **88.3%** | ≥90%（缺口 2.4→1.7pp）|
| MRR@10 | 85.0% | 86.0% | — |
| Recall@5 | 92.5% | 92.5% | ≥95%（缺口仍 2.5pp）|
| Recall@20 | 100.0% | **98.1%** | 回退 ≤2pp（用掉 1.9pp）|
| Recall@50 | 100% | 100% | — |
| Gold Ev@50 | 94.8% | 94.8% | ≥90% |
| 搜索 P95 | 1,846 / 2,609ms | **1,561 / 1,509ms** | ≤2,500ms |
| rerank P95 | 137 / 154ms | 108 / 105ms | ≤1,500ms |
| 65 题全量 | 102s | 63s | — |
| 库体积 | 1,805MB | **941MB** | — |

延迟给的是两次独立运行的两个值：应用在后台扫描时同一份语料的 P95 能从
1,846ms 漂到 2,609ms（一度越过 2,500ms 门槛）。剪枝后两次分别是 1,561 /
1,509ms，波动明显收窄 —— 这是本次改动**唯一有实际分量**的收益，加上体积减半。

**代价要记清**：Recall@20 掉了 1.9pp（P19 从 20 名掉到 22 名），把「回退 ≤2pp」
的额度几乎用光。该题在 @50 仍能召回。

### 7.4 落地与回滚

`scripts/prune_excluded_index.py`，默认干跑；动真实库强制 `--backup`（会跑
`backup_is_restorable`：可打开 + schema 齐全 + `integrity_check`），并要求先退出
应用（`acquire_single_instance_lock`，避免并发写）。

保留 `files` 与 `contents` 行，只删索引行（chunks / 三个 FTS / chunks_vec /
document_representations / sections / index_versions），并把
`contents.parse_state` 记成 `excluded`。**不能记成 `pending`**：
`index_pending()` 的队列查询不看 `state='ignored'`，一旦记成 pending，10.7 万个
被排除的文件会立刻回到索引队列，排除的收益当场清零。`remove_exclusion()` 里新增
`_requeue_pruned_contents()`，取消排除时把 `excluded` 改回 `pending`，下次扫描
重新解析与嵌入（代价是重算，不是丢失）。

每批一个 savepoint。核心不变式：**可见分片集合在删除前后逐 id 相同** ——
断言写在 `prune()` 里，不匹配就退出并提示用回滚。比「gold 还在」更强：少一个
可见分片就等于把正确答案从库里拿掉，评测会因此「变好」而失去意义。

实测：删 141,878 个分片，可见 46,075 个逐 id 不变，`quick_check=ok`，
库 1,805→941MB。真实库已执行，备份在
`backups/library-pre-prune-20260826.db`（1,805MB，已验证可恢复）。
回滚就是用它覆盖 `library.db`。

顺带修掉一个静默回退：`_populate_empty_virtual_indexes()` 重建 `documents_fts`
时直接拼 `title + summary_text`，会把已回填的 `abstract` 抹掉 —— 「跑一次清理」
等于「撤销一次回填」，而两者在日志里毫无关联。改为走 `document_index_text()`。
