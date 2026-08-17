# Inktable 收录修复最终记录

**完成日期**：2026-08-16（UTC+8）  
**状态**：清理、原子切换、补扫、向量回填和真实运行时验收均已完成。  
**安全边界**：所有清理仅修改 Inktable 数据库，未删除、移动或改写磁盘上的
任何用户原文件。

## 1. 根因与修复结论

旧方案把目录排除改成恒 `False`，试图“所有目录全部下钻，只靠后缀判断”。
整盘来源因此进入 Android SDK、IDE、Git、`node_modules`、聊天缓存和程序内部
文档，最终形成 40,681 个文件、269,697 个 chunk 的污染库。广域 B 盘来源
还覆盖了更具体的微信/QQ 来源，造成来源归属错误。

当前收录边界为：

1. 文件类型分成全文、仅登记、忽略三档。
2. 系统目录、安装树、代码项目、依赖目录、聊天缓存和许可证树在目录层剪枝。
3. 用户显式选择的来源根本身豁免通用目录名规则，避免误伤合法资料目录。
4. 广域来源扫描时剪掉已启用的嵌套来源；历史记录按最长路径重新归属。
5. 64 MB 以上元数据文件不读取全文计算 SHA，避免大视频/压缩包拖垮扫描。
6. 清理器保护用户确认、保全副本、文件书和标签关联；默认只做 dry-run。

外层错误计划
`.zcode/plans/plan-sess_c2914180-04ff-4d6f-abef-78141341ae83.md`
已改成“禁止实施”的废弃说明，不能再作为实现依据。

## 2. 可恢复性与数据库切换

主库：

```text
C:\Users\guica\Library\Application Support\Inktable\library.db
```

清理前完整恢复备份：

```text
D:\AIApp\Inktable\backups\library-pre-cleanup-20260815-235847.db
SHA-256 BD0CD947376B8C6978A6F2168ED39B5ECD259316F901E8C070441143EA8AB5E3
backup_is_restorable=True
```

原污染库原样归档：

```text
C:\Users\guica\Library\Application Support\Inktable\library.noisy-20260816-032955.db
```

逐行删除虚拟表和原库内重建都实测为小时级，最终采用“迁移保留集到新库”的
方式：构建新数据库、重建 FTS/vec/关系表、完整校验，再通过同卷原子替换切换
为主库。切换时的校验结果：

- `PRAGMA quick_check`：`ok`
- `PRAGMA integrity_check`：`ok`
- `PRAGMA foreign_key_check`：0 条错误
- WAL checkpoint：`[0, 0, 0]`
- FTS、vec 与活跃关系表行数逐项一致

在发布验收完成前必须保留上述两个恢复源。恢复时先完全停止 Electron 和
sidecar，验证备份，再用数据库恢复工具替换主库；不能在有写入进程时直接覆盖。

## 3. 最终资料库状态

补扫与向量回填完成后的只读统计：

| 项目 | 数量 |
|---|---:|
| files | 5,753 |
| contents | 5,242 |
| active chunks | 29,434 |
| vectors | 29,434 |
| readable pending | 0 |
| missing vectors | 0 |

内容解析状态：

| 状态 | 数量 |
|---|---:|
| indexed | 2,510 |
| unsupported | 2,644 |
| no_text | 67 |
| parse_failed | 8 |
| too_large | 13 |

所有文件状态均为 `registered`。Android、Microsoft VS Code、系统 Git、
PyCharm、`node_modules`、Tencent Files 缓存和 Thumb 缓存等目标噪声路径
在新库中的计数均为 0。

## 4. 来源与补扫验收

已启用来源的最终文件数：

| 来源 | 文件数 |
|---|---:|
| 文稿 | 46 |
| 微信账户 1 | 1 |
| 微信账户 2 | 1,147 |
| OneDrive 文稿 | 26 |
| 桌面 | 89 |
| QQ | 73 |
| B 盘 | 3,431 |
| D 盘 | 729 |
| 下载 | 0 |
| 图片 | 201 |
| 音乐 | 1 |
| 视频 | 1 |
| 微信接收（根目录） | 8 |

Windows 微信自定义根 `B:\WeChat profiles` 下直接存放的 8 个文档已识别为
独立来源，不再误归 B 盘；递归缓存内容不会触发该根来源。下载、图片、音乐、
视频四个系统来源均已创建并启用。

真实 watcher/reconcile 验收结果：13 个 watcher 成功挂载、0 个失败；首次
reconcile 在 `/watch/start` 后约 2 秒调度，扫描 16,340 个条目，登记或更新
784 个条目，约 3 分钟完成，`last_error` 为空。状态对象现包含 `phase` 和
`current_source`，可定位长扫描正在处理哪个来源。

## 5. 工具与运行方式

正式维护工具：

- `services/api/scripts/cleanup_ingestion_noise.py`：默认 dry-run；`--apply`
  强制要求可恢复备份；大比例清理走新库迁移与原子切换。
- `services/api/scripts/profile_source_scans.py`：逐来源枚举耗时和候选规模。
- `services/api/scripts/runtime_acceptance.py`：启动真实 sidecar，经 Bearer HTTP
  验证健康、回填、watcher/reconcile、搜索和可选真实模型问答。

清理工具绝不操作用户原文件。任何再次清理都必须先运行 dry-run，检查保护项
与删除比例，创建并验证备份后才能 `--apply`。

## 6. 真实链路验收

- bge-m3 从缺失 14,809 个向量开始，29 批回填完成，最终缺失 0；模型标识
  `ollama-bge-m3-d1024`。
- 搜索“银行家算法用到哪些数据结构”返回 10 个结果，置信度 `high`，无降级。
- cc-switch 当前供应商经真实补全探测成功；模型问答返回 `answered / knowledge`，
  16 条引用，验证 1 次完成，无虚构引用、无拒答截断、无降级。

后续检索、问答、打包和发布结果记录在 `docs/eval/`、`docs/PLAN.md` 与
`docs/RELEASE-0.3.0.md`；本文件不再保留“清理进程仍可能运行”的旧警告。
