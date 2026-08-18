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

### 3.2 按目录排除，由用户决定（新增机制）

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

改动必须用 65 题真实模型 QA 复验引用门槛（引用支持率 ≥95%、精确引用率、
句级覆盖、正确拒答率），因为放长回答会增加未获支持声明的机会。
