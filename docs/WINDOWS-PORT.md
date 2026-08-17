# Windows 移植记录（2026-08-14 ~ 08-15）

macOS 版功能与逻辑**一比一移植**到 Windows：mac 代码路径原样保留，全部
Windows 差异走 `sys.platform` / `process.platform` 分支。本文是唯一权威
记录 —— 环境、代码改动、新增能力、收录规则、并发修复，全部实测验证。

**状态**：可用。全流程验证过：来源发现 → 扫描登记 → 解析索引 →
bge-m3 向量化 → 混合检索 → 带引用问答 → 实时监听自动入库。

---

## 1. 运行环境（本机实测配置）

| 组件 | 版本/位置 | 备注 |
|---|---|---|
| Node / npm | v24.5.0 / 11.6.2 | `apps/desktop` 用 npm（package-lock.json） |
| uv / Python | 0.11.2 / CPython 3.12.13（uv 托管） | `services/api` 用 uv（uv.lock） |
| Ollama | 0.32.11 | **端口改为 18434**，见下 |
| Electron | 43.3.0 | 二进制经 npmmirror 手动安装，见 §6 |

**Ollama 端口**：11434 落在 Windows 保留端口段（实测 11427–11526，
`netsh int ipv4 show excludedportrange`），bind 报 WSAEACCES。解法：
用户环境变量 `OLLAMA_HOST=127.0.0.1:18434` +
`INKTABLE_OLLAMA_URL=http://127.0.0.1:18434`（embedding.py 原生支持该
变量），重启 Ollama 即可，无需改代码。

**开发命令**（Windows）：

```powershell
# 后端
cd services\api
uv sync
uv run pyinstaller sidecar.spec --clean --noconfirm   # 产出 dist\inktable-sidecar.exe

# 桌面端
cd ..\..\apps\desktop
npm install
npm start
```

## 2. 平台移植清单（mac 行为零变化）

| 位置 | Windows 问题 | 修法 |
|---|---|---|
| `sidecar.spec` | 写死 `vec0.dylib` / `target_arch=arm64` | 按平台选 `vec0.dll/.dylib/.so`；arm64 仅 darwin |
| `app/db/database.py` | 无 `fcntl` | 单实例锁 Windows 用 `msvcrt.locking` 字节锁，语义等价 |
| `app/domain/identity.py` | 导入期加载 `libc.dylib` 必崩；卷标识依赖 diskutil | libc 仅 darwin；Windows 用卷序列号（`st_dev`）做稳定卷标识 |
| `app/watcher/stability.py` | 函数内 `import fcntl` 未接 ImportError | 一并捕获，诊断探测视为不持锁 |
| `app/watcher/service.py` | 来源前缀匹配硬编码 `/`，**实时入库整体失效** | `os.sep`（mac 上等价） |
| `app/main.py` 目录过滤/文件树/迁移改写 | 同上，`substr` 前缀永不命中 | `os.sep` |
| `electron/main.js` | PyInstaller onefile 是"引导+子进程"两层，`proc.kill()` 留孤儿占库锁（实测复现） | win32 用同步 `taskkill /T /F` 整树终止；必须等待命令完成再退出 Electron |
| `electron/main.js` | `hiddenInset` 在 Windows 成无边框窗口（无窗口按钮） | win32 用 `titleBarStyle:'hidden'` + 窗口控件叠加（高度 48 对齐 .topbar，配色随主题） |
| 渲染层 | 86px 红绿灯留白、mac 字体栈、`split('/')` | preload 暴露 `platform`，`is-win32` 类微调；字体栈补 Segoe UI/微软雅黑 |

**stdin 令牌链路**：Windows 冻结版下验证过 —— Electron `stdin.write` 的
令牌在 uvicorn 启动前生效（调试期一度怀疑此链路，实为 PowerShell 测试
工具的 .NET `Process.StandardInput` 注入 UTF-8 BOM 所致，Node 管道无此
问题，应用代码无需改动）。

## 3. Windows 来源发现

macOS 逻辑原样；`discover_wechat/discover_qq` 开头按平台分流。

- **系统目录**：从注册表 `User Shell Folders` 读**真实指向**（OneDrive
  「重要文件夹备份」会把 桌面/文档 重定向；实测 `C:\Users\<u>\Desktop`
  是 0 文件空壳，真实桌面在 `OneDrive\Desktop`）。
- **微信 4.x**：数据根写在 `%APPDATA%\Tencent\xwechat\config\<hash>.ini`
  （内容就是一行路径，实测 `B:\WeChat profiles`），其下
  `xwechat_files/<wxid>_<hash>/msg/file/<YYYY-MM>/`；3.x 存量在
  `<文档>\WeChat Files\<wxid>\FileStorage\File`。
- **QQ NT**：聊天库在 `<文档>\Tencent Files\<QQ号>\nt_qq\`；「接收文件
  保存到」支持自定义且配置在加密库里读不到 —— 按命名习惯探测各固定盘
  上名字含 qq/tencent 的一级目录（实测 `B:\QQ profiles`，**收到的文件
  直接放根目录**，`Tencent Files` 子目录只是空壳基础设施）。
- **全盘发现（deepscan）**：WizTree 思路的免提权版 —— 枚举固定盘文件名
  元数据（不读内容、跳过重解析点防环），聚合"文档密集目录"（直接文档
  数 ≥5，前 40，互不嵌套）供勾选。真 MFT 直读需管理员 + 仅限 NTFS，
  未做；如需秒级可加"管理员运行时自动切换 USN/MFT"。
- **发现弹层**：没有新来源也会打开（「全盘发现」入口在弹层里），新增
  「暂不启用」与点遮罩关闭；v0.3 重构丢失的条目/按钮样式已补齐。

## 4. 收录规则（固定白名单）

Windows 以固定盘为来源根，盘内目录按真实路径逐层展开；是否入库只看
扩展名白名单：

| 档 | 类型 | 行为 |
|---|---|---|
| 全文解析 | txt docx pdf md csv html htm | FTS + 语义向量，可问答可引用 |
| 不入库 | 其余全部格式（含 doc、Excel、图片、音视频、压缩包、代码/配置、安装包、日志、临时残片、隐藏文件） | 连名字都不登记 |

历史库中已登记的代码/配置在补扫时自动翻成 `ignored` 并从视图消失
（`VISIBLE_FILES_COND` 增加 `state != 'ignored'`）。

**目录排除新增**（`EXCLUDED_DIRS`，扫描/监听/发现三处共用）：
Windows 系统与商店目录（Windows / Program Files / AppData /
WindowsApps / WpSystem / DeliveryOptimization / $RECYCLE.BIN 等）、
IM 数据根与缓存（xwechat_files / WeChat Files / Tencent Files /
nt_qq / nt_db / nt_temp / Thumb / ThumbTemp / RWTemp）、游戏与工具链
（steam / SteamLibrary / Windows Kits）、开发数据（testdata /
site-packages）、tmp / temp。
排除只看「相对来源根的祖先目录名」—— 指向排除名内部的来源
（如 QQ 的 `nt_data\File`）不受影响。

**为什么必须排 IM 数据根**：mac 上它们躲在 `~/Library` 容器（天然被
"Library" 挡住），Windows 上直接坐在「文档」下 —— 实测一次吞了
12,430 个表情缓存/缩略图/聊天库文件。

## 5. 并发与性能（整盘首扫暴露的三层问题，均有线程栈证据）

1. **共享连接竞态**：全进程单条 SQLite 连接、读不加锁 —— 实时入库修好
   后写入一活跃，`files_tree` 立刻 `SQLITE_MISUSE`。改为**每线程一条
   连接**（WAL 多读一写），写路径仍走 `_db_lock` 单写者；
   `INKTABLE_DB=:memory:` 维持单例（测试）。
2. **嵌入霸锁**：`/index/run` 在锁内逐批内联嵌入（CPU bge-m3 分钟级/批），
   py-spy 抓到 reconcile 线程等锁 20 分钟零进展。改为：批量索引
   `embed=False`（先 FTS 可搜），向量走 `embed_backfill` 分批补
   （前端 256 片/批已有驱动；reconcile 收尾 128 片/批兜底）。
   watcher 单文件实时路径仍内联嵌入（量小，保新鲜）。
3. **锁不公平（barging）**：`threading.Lock` 无公平性，索引循环
   "释放→立刻再抢"依旧饿死扫描 —— 批间 `sleep(0.1)` 强制让出窗口
   （抓栈确认 reconcile 转为 active 持锁扫描）。
   另：reconcile 从"整轮一把锁"改为逐来源/逐批进出。

**大文件哈希上限**：内容去重原本对所有登记文件读全文算 sha256 ——
整盘收录时几十 GB 视频/压缩包会把首扫拖成天级。新增
`MAX_DEDUP_SIZE = 64MB`：元数据类超限不哈希（content_id 留空，
不参与重复内容归并）；全文类超 `MAX_FULLTEXT_SIZE` 同样只登记。

## 6. 0.3.0 发布验收（2026-08-16）

- PyInstaller sidecar：95,633,956 bytes，SHA-256
  `2670F7BFFA026E8FCBB82D0D30010DEFE0EF634FC0932D3DF647D046AE77B986`。
- NSIS 安装包：`dist/Inktable-0.3.0-Setup-x64.exe`，194,761,734 bytes，
  SHA-256 `E24D5588968B85B380A3A9DC6A407EAEF88FF8973F1C776F47CBF08CB53006E8`。
- 新目录静默安装退出码 0，安装目录 sidecar 与构建产物逐字节一致。
- 冻结 sidecar 使用独立数据库端到端收录真实无文本层 PDF，
  `Windows.Media.Ocr` 识别正文后成功进入索引与搜索；同次检索 trace 为
  `local-static-v3`、`degraded=false`。
- 使用独立 `INKTABLE_DATA_DIR` 和 `--user-data-dir` 启动安装版 Electron：
  首次来源引导、搜索空态、设置页、暗色主题和 sidecar ready 均通过；
  关闭后 Electron、PyInstaller 引导进程和 Python 子进程全部退出。
- 真实库只读审计通过：`quick_check=ok`、外键错误 0，chunks、两套 FTS、
  Document/Section FTS 与 sqlite-vec rowid 一致，无文件/内容/section 孤儿。

## 7. 已知限制与运维备忘

- **Electron 安装**：npm postinstall 在本机网络下会静默卡死；
  `node_modules\electron\dist` 缺 `electron.exe` 时，从
  `https://npmmirror.com/mirrors/electron/<版本>/electron-v<版本>-win32-x64.zip`
  下载解压到 `dist\`，并写 `path.txt` 内容 `electron.exe`。
- **OCR**：Windows 使用系统 `Windows.Media.Ocr`（经内置 PowerShell
  WinRT 桥接），不捆绑 Tesseract 或模型文件；需要系统安装至少一个 OCR
  语言包（中文/英文均可）。没有语言包时 `available=false`，扫描件保持
  原有的“未提取到文本”降级提示。macOS 继续使用 Vision。
- **浏览器/办公应用发现**未移植 Windows 分支（Chrome/Edge 自定义下载
  目录等）；覆盖面已由整盘来源兜住。
- 托盘图标用的 `NSTouchBarSearchTemplate` 是 mac API，Windows 无托盘
  （try/catch 兜住，不影响功能）；全局快捷键实测注册为 Ctrl+Shift+K。
- 数据目录在 `C:\Users\<u>\Library\Application Support\Inktable`
  （沿用 mac 风格路径拼接，功能正常；可在设置里迁移）。
- 回传 macOS：所有改动平台隔离，另有两处**顺带修复对 mac 同样生效**
  —— 每线程连接（同款竞态在 mac 潜伏）、设置→文件来源列表缺失的
  `.srow` 样式（mac 上同样是坏的）。
