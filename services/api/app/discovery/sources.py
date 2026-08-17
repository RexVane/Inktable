"""来源自动发现 —— 探测本机的文件入口（PLAN §7）。

三种手段，缺一不可（§7.2）：
  1. 读应用配置   —— 最准，如 Chrome/Edge 的 Preferences JSON
  2. 启发式 glob  —— 应对动态路径，如微信的 wxid 目录
  3. bundle 存在性 —— 应用装了但从没收过文件时静默跳过

**发现结果默认不启用**（§1 约束 4）：自动监听用户没同意的目录是隐私问题。
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()

# 只统计这些扩展名，用于判断一个候选目录是否真的"有文档"
DOC_EXTS = {".txt", ".docx", ".pdf", ".md", ".csv", ".html", ".htm"}


@dataclass
class Source:
    """一个候选来源。"""

    name: str
    path: str
    kind: str  # im / browser / system / manual
    discovered_by: str  # config / heuristic / bundle / default
    volatile: bool = False  # 应用会自行清理？（§2.5）
    enabled: bool = False  # 默认不启用
    file_count: int = 0
    doc_count: int = 0
    recent_mtime: float = 0.0
    note: str = ""
    permission_ok: bool | None = None
    is_drive_root: bool = False

    def to_dict(self) -> dict:
        return {**self.__dict__}


def fixed_drive_roots() -> list[Path]:
    """Return fixed Windows drive roots without scanning their contents."""
    if sys.platform != "win32":
        return []
    import ctypes

    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    roots: list[Path] = []
    for i in range(26):
        if not bitmask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:
            roots.append(Path(root))
    return roots


def discover_fixed_drives() -> list[Source]:
    """Discover fixed disks as the only Windows top-level sources.

    Do not recursively probe here: the real file count is produced by the
    background source scan after the user enables a disk.
    """
    return [
        Source(
            name=f"{root.drive.rstrip(':')} 盘",
            path=str(root),
            kind="system",
            discovered_by="fixed_drive",
            note="启用后递归扫描盘内目录，文件树保留真实路径",
            permission_ok=_check_permission(root),
            is_drive_root=True,
        )
        for root in fixed_drive_roots()
        if root.is_dir()
    ]


def _probe_dir(path: Path, sample_limit: int = 400) -> tuple[int, int, float]:
    """采样统计目录：总文件数、文档数、最近修改时间。

    只采样不遍历全部 —— 微信目录里可能有几十万个缓存文件（§7.7）。
    """
    total = docs = 0
    newest = 0.0
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if fn.startswith("."):
                    continue
                total += 1
                if Path(fn).suffix.lower() in DOC_EXTS:
                    docs += 1
                    try:
                        newest = max(newest, (Path(root) / fn).stat().st_mtime)
                    except OSError:
                        pass
                if total >= sample_limit:
                    return total, docs, newest
    except (PermissionError, OSError):
        pass
    return total, docs, newest


def _probe_direct_dir(path: Path) -> tuple[int, int, float]:
    """Count direct files only, used for custom IM roots above cache trees."""
    total = docs = 0
    newest = 0.0
    try:
        for child in path.iterdir():
            if not child.is_file() or child.name.startswith("."):
                continue
            total += 1
            if child.suffix.lower() in DOC_EXTS:
                docs += 1
                try:
                    newest = max(newest, child.stat().st_mtime)
                except OSError:
                    pass
    except (PermissionError, OSError):
        pass
    return total, docs, newest


def _check_permission(path: Path) -> bool:
    """TCC 权限自检（§6.4）。

    macOS 下访问 ~/Library/Containers 需要授权，**被拒时不抛异常、
    只是读不到内容** —— 这是最坏的一类失败：静默的。
    所以必须实际尝试读取，而不是只看目录是否存在。
    """
    try:
        next(iter(os.scandir(path)), None)
        return True
    except (PermissionError, OSError):
        return False


# ---------------------------------------------------------------- 浏览器

def _chromium_download_dir(prefs: Path) -> str | None:
    try:
        data = json.loads(prefs.read_text(encoding="utf-8", errors="ignore"))
        return data.get("download", {}).get("default_directory")
    except Exception:
        return None


def discover_browsers() -> list[Source]:
    """浏览器下载目录。

    所有浏览器的默认下载位置都是 ~/Downloads —— 那由"下载"系统来源
    统一覆盖（带真实文件统计），这里只上报**用户自定义**的下载目录，
    避免同一个 ~/Downloads 挂着"Chrome 下载"之类的误导名，还把
    系统来源顶掉。
    """
    out: list[Source] = []
    support = HOME / "Library" / "Application Support"
    downloads = HOME / "Downloads"
    seen: set[str] = set()

    def add(label: str, target: Path) -> None:
        if not target.is_dir() or str(target) in seen:
            return
        seen.add(str(target))
        try:
            if target.resolve() == downloads.resolve():
                return  # 默认下载目录归"下载"系统来源管
        except OSError:
            return
        total, docs, newest = _probe_dir(target)
        out.append(
            Source(
                name=f"{label} 下载",
                path=str(target),
                kind="browser",
                discovered_by="config",
                file_count=total,
                doc_count=docs,
                recent_mtime=newest,
            )
        )

    chromium = [
        ("Chrome", support / "Google" / "Chrome"),
        ("Edge", support / "Microsoft Edge"),
        ("Brave", support / "BraveSoftware" / "Brave-Browser"),
        ("Arc", support / "Arc" / "User Data"),
        ("Vivaldi", support / "Vivaldi"),
    ]
    for name, base in chromium:
        if not base.exists():
            continue
        for prefs in base.glob("*/Preferences"):
            profile = prefs.parent.name
            d = _chromium_download_dir(prefs)
            if not d:
                continue  # 未自定义 → 默认 ~/Downloads，跳过
            label = name if profile == "Default" else f"{name}（{profile}）"
            add(label, Path(d).expanduser())

    # Safari：读 defaults；未设置即默认下载目录，同样归"下载"管
    try:
        r = subprocess.run(
            ["defaults", "read", "com.apple.Safari", "DownloadsPath"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            add("Safari", Path(r.stdout.strip()).expanduser())
    except Exception:
        pass

    # Firefox：prefs.js 的 browser.download.dir（folderList=2 时才生效）
    firefox_profiles = support / "Firefox" / "Profiles"
    if firefox_profiles.exists():
        for prefs in firefox_profiles.glob("*/prefs.js"):
            try:
                text = prefs.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if '"browser.download.folderList", 2' not in text:
                continue
            match = re.search(
                r'user_pref\("browser\.download\.dir",\s*"([^"]+)"\)', text)
            if match:
                add("Firefox", Path(match.group(1)).expanduser())

    return out


# ---------------------------------------------------------------- Windows 路径

def _windows_shell_folder(value_name: str) -> Path | None:
    """从注册表 User Shell Folders 取系统目录的**真实指向**。

    OneDrive「重要文件夹备份」会把 桌面/文档 重定向到 OneDrive 下，
    凭 HOME 拼出来的老路径只剩空壳（实测本机：C:\\Users\\<u>\\Desktop
    是 0 文件的空目录，真实桌面在 OneDrive\\Desktop）。注册表里的
    User Shell Folders 是唯一可信来源。
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            raw, _ = winreg.QueryValueEx(key, value_name)
        p = Path(os.path.expandvars(str(raw)))
        return p if p.is_dir() else None
    except OSError:
        return None


def _windows_documents_roots() -> list[Path]:
    """Windows 的「文档」常被 OneDrive 重定向，两处都可能有 IM 目录。"""
    roots = [HOME / "Documents"]
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        base = os.environ.get(env, "").strip()
        if base:
            roots += [Path(base) / "文档", Path(base) / "Documents"]
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r).lower()
        if key not in seen and r.is_dir():
            seen.add(key)
            out.append(r)
    return out


def _windows_wechat_roots() -> list[Path]:
    """微信数据根：默认在「文档」，用户自定义时写在 config 小 ini 里。

    本机实测（2026-08，微信 4.x/Windows）：
        %APPDATA%/Tencent/xwechat/config/<hash>.ini 的内容就是一行数据根，
        如 ``B:\\WeChat profiles``，其下为 xwechat_files/<wxid>_<hash>/msg/file/。
    3.x 的自定义根在 WeChat/All Users/config/3ebffe94.ini（"MyDocument:" 表示默认）。
    """
    roots = list(_windows_documents_roots())
    appdata = os.environ.get("APPDATA", "").strip()
    if not appdata:
        return roots
    cfg = Path(appdata) / "Tencent" / "xwechat" / "config"
    if cfg.is_dir():
        for ini in cfg.glob("*.ini"):
            try:
                if ini.stat().st_size > 4096:
                    continue  # 数据根配置只有一行路径，大文件是别的东西
                text = ini.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                continue
            if text and Path(text).is_dir():
                roots.append(Path(text))
    legacy = (Path(appdata) / "Tencent" / "WeChat" / "All Users"
              / "config" / "3ebffe94.ini")
    if legacy.is_file():
        try:
            text = legacy.read_text(encoding="utf-8", errors="ignore").strip()
            if text and not text.lower().startswith("mydocument") \
                    and Path(text).is_dir():
                roots.append(Path(text))
        except OSError:
            pass
    return roots


def _discover_wechat_windows() -> list[Source]:
    """Windows 微信接收目录：4.x 与 3.x 存量都探。

    本机实测（2026-08）：
        4.x  <数据根>/xwechat_files/<wxid>_<hash>/msg/file/<YYYY-MM>/
        3.x  <文档>/WeChat Files/<wxid>/FileStorage/File/
    """
    out: list[Source] = []
    for root in _windows_wechat_roots():
        # Some custom WeChat roots also receive files directly at the root.
        # Only recommend such a root when direct user documents exist; never
        # infer this from recursive cache contents under xwechat_files.
        if re.search(r"wechat|xwechat|微信", root.name, re.I):
            total, docs, newest = _probe_direct_dir(root)
            if docs:
                out.append(Source(
                    name="微信接收（根目录）",
                    path=str(root),
                    kind="im",
                    discovered_by="config",
                    volatile=True,
                    file_count=total,
                    doc_count=docs,
                    recent_mtime=newest,
                    note="微信自定义数据根中的直接文件；建议开启保全副本",
                ))
        # (子目录, wxid 模式, 是否现行收件位)
        for sub, pattern, current in (
            ("xwechat_files", "*/msg/file", True),
            ("WeChat Files", "*/FileStorage/File", False),
        ):
            base = root / sub
            if not base.exists():
                continue
            for hit in glob.glob(str(base / pattern)):
                p = Path(hit)
                if not p.is_dir():
                    continue
                total, docs, newest = _probe_dir(p)
                # 4.x 是现行收件位：还没收过文档也展示，启用后自动收录
                if docs == 0 and not current:
                    continue
                account = next(
                    (s for s in p.parts if s.startswith("wxid_")), "")
                label = f"微信接收（{account[:14]}…）" if account else "微信接收"
                out.append(Source(
                    name=label, path=str(p), kind="im",
                    discovered_by="heuristic", volatile=True,
                    file_count=total, doc_count=docs, recent_mtime=newest,
                    note=("目前还没有收到文档；启用后新收到的文件会自动收录"
                          if docs == 0
                          else "微信可能自动清理此目录，建议开启保全副本"),
                ))
    return out


def _qq_windows_roots() -> list[Path]:
    """QQ 收件位置的候选根。

    「将接收的文件保存到」支持自定义（实测本机设为 ``B:\\QQ profiles``），
    但该配置存在加密库里读不到明文 —— 退而按命名习惯探测：各「文档」
    目录之外，再看每个固定盘的盘根与名字含 qq/tencent 的一级目录。
    """
    roots = list(_windows_documents_roots())
    if sys.platform != "win32":
        return roots
    import ctypes

    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if not bitmask & (1 << i):
            continue
        drive = f"{chr(65 + i)}:\\"
        # DRIVE_FIXED = 3：可移动盘/网络盘拔了就失效，不作候选
        if ctypes.windll.kernel32.GetDriveTypeW(drive) != 3:
            continue
        droot = Path(drive)
        roots.append(droot)
        try:
            for child in droot.iterdir():
                if child.is_dir() and re.search(r"qq|tencent", child.name, re.I):
                    roots.append(child)
        except OSError:
            continue
    return roots


def _discover_qq_windows() -> list[Source]:
    """Windows QQ 接收目录：QQ NT 与旧版都探。

    本机实测（2026-08，QQ NT）：<文档>/Tencent Files/<QQ号>/nt_qq/nt_data/File/
    旧版则是 <文档>/Tencent Files/<QQ号>/FileRecv/。
    自定义收件根（如 B:\\QQ profiles\\Tencent Files）在首次下载前只是
    空壳 —— 直接推荐根目录本身，启用后靠递归监听收录之后的下载。
    """
    out: list[Source] = []
    for root in _qq_windows_roots():
        candidates: list[tuple[Path, bool]] = []
        # 自定义收件根（如 B:\QQ profiles）：QQ 会把接收的文件**直接放在
        # 根下**（实测 73 个 PDF 就躺在根目录），Tencent Files 子目录只是
        # 基础设施空壳 —— 根下有文档时优先推荐根本身
        if re.search(r"qq|tencent", root.name, re.I):
            _, root_docs, _ = _probe_dir(root)
            if root_docs > 0:
                candidates.append((root, True))
        base = root / "Tencent Files"
        if not candidates and base.exists():
            # NT 收件位是明确的收件位置：存在就展示（同 mac 版逻辑）
            candidates += [(Path(p), True)
                           for p in glob.glob(str(base / "*/nt_qq/nt_data/File"))]
            candidates += [(Path(p), False)
                           for p in glob.glob(str(base / "*/FileRecv"))]
            if not candidates:
                candidates = [(base, True)]
        for p, keep_empty in candidates:
            if not p.is_dir():
                continue
            total, docs, newest = _probe_dir(p)
            if docs == 0 and not keep_empty:
                continue
            out.append(Source(
                name="QQ 接收", path=str(p), kind="im",
                discovered_by="heuristic", volatile=True,
                file_count=total, doc_count=docs, recent_mtime=newest,
                note=("目前还没有收到文档；启用后新收到的文件会自动收录"
                      if docs == 0
                      else "QQ 可能自动清理缓存，建议开启保全副本"),
            ))
    return out


# ---------------------------------------------------------------- 微信

def discover_wechat() -> list[Source]:
    """微信接收文件目录。

    路径随大版本变化，且带 wxid 动态段 —— 硬编码不可行，必须 glob + 筛选。
    本机实测（2026-08，微信 4.x）：

        ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/
            xwechat_files/<wxid_xxx>/msg/file/<YYYY-MM>/

    旧版（3.x）则是 .../Application Support/com.tencent.xinWeChat/<hash>/<hash>/
    Message/MessageTemp/<hash>/File/ —— 两种都试，未来版本靠 §7.5 定期重探测。
    """
    if sys.platform == "win32":
        return _discover_wechat_windows()

    out: list[Source] = []
    container = HOME / "Library" / "Containers" / "com.tencent.xinWeChat" / "Data"

    patterns = [
        # 4.x：一个 wxid 一个账号，msg/file 下按月分目录
        (container / "Documents" / "xwechat_files", "*/msg/file"),
        # 3.x 旧版
        (container / "Library" / "Application Support" / "com.tencent.xinWeChat",
         "*/*/Message/MessageTemp/*/File"),
    ]

    for base, pattern in patterns:
        if not base.exists():
            continue
        for hit in glob.glob(str(base / pattern)):
            p = Path(hit)
            if not p.is_dir():
                continue
            total, docs, newest = _probe_dir(p)
            if docs == 0:
                continue  # 目录在但没收过文件，不制造噪音（§7.2）
            # 从路径里取出账号标识，多账号时便于区分
            account = next((s for s in p.parts if s.startswith("wxid_")), "")
            label = f"微信接收（{account[:14]}…）" if account else "微信接收"
            out.append(
                Source(
                    name=label,
                    path=str(p),
                    kind="im",
                    discovered_by="heuristic",
                    volatile=True,  # 微信会自行清理缓存（§2.5）
                    file_count=total,
                    doc_count=docs,
                    recent_mtime=newest,
                    note="微信可能自动清理此目录，建议开启保全副本",
                )
            )
    return out


# ---------------------------------------------------------------- QQ

def discover_qq() -> list[Source]:
    """QQ 接收文件目录，新旧版本都探。

    本机实测（2026-08，QQ NT）：
        ~/Library/Containers/com.tencent.qq/Data/Library/
            Application Support/QQ/nt_qq_<hash>/nt_data/File/
    旧版则在 Documents/Tencent Files/<QQ号>/FileRecv。
    """
    if sys.platform == "win32":
        return _discover_qq_windows()

    out: list[Source] = []
    container = HOME / "Library" / "Containers" / "com.tencent.qq" / "Data"

    patterns: list[tuple[Path, str, str]] = [
        # QQ NT（新版）：账号一个 nt_qq_<hash> 目录，File 下是接收的文件
        (container / "Library" / "Application Support" / "QQ",
         "nt_qq_*/nt_data/File", "QQ 接收"),
        # 旧版容器内 Documents
        (container / "Documents", "*/FileRecv", "QQ 接收"),
        # 更旧的非容器路径
        (HOME / "Documents" / "Tencent Files", "*/FileRecv", "QQ 接收"),
    ]
    direct_bases = [
        (container / "Downloads", "QQ 下载"),
        (HOME / "Documents" / "Tencent Files", "QQ 接收"),
    ]

    candidates: list[tuple[Path, str, bool]] = []
    for i, (base, pattern, label) in enumerate(patterns):
        if not base.exists():
            continue
        # NT 接收目录（第一条模式）是明确的收件位置：存在就展示，
        # 即使还没收到过文档 —— 用户可先启用，之后收到的文件自动收录。
        keep_empty = i == 0
        candidates += [(Path(p), label, keep_empty)
                       for p in glob.glob(str(base / pattern))]
    candidates += [(p, label, False) for p, label in direct_bases if p.is_dir()]

    for p, label, keep_empty in candidates:
        if not p.is_dir():
            continue
        total, docs, newest = _probe_dir(p)
        if docs == 0 and not keep_empty:
            continue
        out.append(
            Source(
                name=label,
                path=str(p),
                kind="im",
                discovered_by="heuristic",
                volatile=True,
                file_count=total,
                doc_count=docs,
                recent_mtime=newest,
                note=("目前还没有收到文档；启用后新收到的文件会自动收录"
                      if docs == 0
                      else "QQ 可能自动清理缓存，建议开启保全副本"),
            )
        )
    return out


# ---------------------------------------------------------------- 办公协作应用

# (显示名, 候选 (基目录, glob) 列表, 是否易失)。目录不存在时静默跳过（§7.2），
# 装了应用但没收过文件也不会制造噪音（docs == 0 过滤）。
_WORK_APPS: list[tuple[str, list[tuple[Path, str]], bool]] = [
    ("飞书", [
        # 容器安装（App Store / 新版）
        (HOME / "Library" / "Containers" / "com.bytedance.macos.feishu" / "Data"
         / "Library" / "Application Support" / "LarkShell", "sdk_storage/*/downloads"),
        # 直装
        (HOME / "Library" / "Application Support" / "LarkShell", "sdk_storage/*/downloads"),
        (HOME / "Downloads" / "Lark", "*"),
        (HOME / "Downloads" / "飞书下载", "*"),
    ], True),
    ("Lark", [
        (HOME / "Library" / "Containers" / "com.larksuite.larkApp" / "Data"
         / "Library" / "Application Support" / "LarkShell", "sdk_storage/*/downloads"),
    ], True),
    ("钉钉", [
        (HOME / "Library" / "Application Support" / "DingTalkMac", "*/Data/Files"),
        (HOME / "Library" / "Containers" / "com.alibaba.DingTalkMac" / "Data"
         / "Library" / "Application Support" / "DingTalkMac", "*/Data/Files"),
        (HOME / "Documents" / "DingTalk", "*"),
    ], True),
    ("企业微信", [
        (HOME / "Library" / "Containers" / "com.tencent.WeWorkMac" / "Data"
         / "Library" / "Application Support" / "WXWork", "Data/*/Cache/File"),
        (HOME / "Library" / "Application Support" / "WXWork", "Data/*/Cache/File"),
    ], True),
]


def discover_work_apps() -> list[Source]:
    """飞书 / 钉钉 / 企业微信等办公应用的接收文件目录。"""
    out: list[Source] = []
    for name, candidates, volatile in _WORK_APPS:
        hits: list[Path] = []
        for base, pattern in candidates:
            if not base.exists():
                continue
            if pattern == "*":
                hits.append(base)
            else:
                hits += [Path(p) for p in glob.glob(str(base / pattern))]
        for p in hits:
            if not p.is_dir():
                continue
            total, docs, newest = _probe_dir(p)
            if docs == 0:
                continue
            out.append(
                Source(
                    name=f"{name} 接收",
                    path=str(p),
                    kind="im",
                    discovered_by="heuristic",
                    volatile=volatile,
                    file_count=total,
                    doc_count=docs,
                    recent_mtime=newest,
                    note=f"{name} 可能自动清理缓存，建议开启保全副本" if volatile else "",
                )
            )
    return out


# ---------------------------------------------------------------- 系统目录

def discover_system() -> list[Source]:
    out = []
    downloads = HOME / "Downloads"
    desktop = HOME / "Desktop"
    documents = HOME / "Documents"
    pictures = HOME / "Pictures"
    music = HOME / "Music"
    videos = HOME / "Videos"
    if sys.platform == "win32":
        # 桌面/文档常被 OneDrive 重定向，必须按注册表的真实指向探测
        downloads = _windows_shell_folder(
            "{374DE290-123F-4565-9164-39C4925E467B}") or downloads
        desktop = _windows_shell_folder("Desktop") or desktop
        documents = _windows_shell_folder("Personal") or documents
        pictures = _windows_shell_folder("My Pictures") or pictures
        music = _windows_shell_folder("My Music") or music
        videos = _windows_shell_folder("My Video") or videos
    entries: list[tuple[str, Path, str]] = [
        ("下载", downloads, ""),
        ("桌面", desktop, ""),
        ("文稿", documents, "文件多时建议在设置里收窄范围"),
        ("图片", pictures, "图片只登记名称和位置，不做正文问答"),
        ("音乐", music, "音频只登记名称和位置，不做正文问答"),
        ("视频", videos, "视频只登记名称和位置，不做正文问答"),
    ]
    if sys.platform == "win32":
        # 其余 OneDrive 文档候选（与上面重复的会在 discover_all 里按路径去重）
        entries += [
            ("文稿（OneDrive）", r, "文件多时建议在设置里收窄范围")
            for r in _windows_documents_roots()
            if "onedrive" in str(r).lower()
        ]
    for name, p, note in entries:
        if not p.exists():
            continue
        total, docs, newest = _probe_dir(p)
        out.append(
            Source(
                name=name, path=str(p), kind="system", discovered_by="default",
                file_count=total, doc_count=docs, recent_mtime=newest, note=note,
            )
        )
    return out


# ---------------------------------------------------------------- 汇总

def discover_all() -> list[Source]:
    # Windows 的统一来源模型：固定盘是唯一顶层来源，盘内目录由
    # /files/tree 按真实文件路径逐层展开，不再把应用缓存目录列成来源。
    if sys.platform == "win32":
        return discover_fixed_drives()

    sources: list[Source] = []
    for fn in (discover_browsers, discover_wechat, discover_qq,
               discover_work_apps, discover_system):
        try:
            sources.extend(fn())
        except Exception:
            continue  # 单个探测器失败不影响其余（§7.2：探测失败静默跳过）

    # 去重：同一路径只保留一条
    seen: set[str] = set()
    unique = []
    for s in sources:
        key = os.path.normcase(os.path.normpath(s.path))
        if key in seen:
            continue
        seen.add(key)
        s.permission_ok = _check_permission(Path(s.path))
        unique.append(s)

    # 有文档的排前面，其次按最近活动
    unique.sort(key=lambda s: (s.doc_count > 0, s.recent_mtime), reverse=True)
    return unique
