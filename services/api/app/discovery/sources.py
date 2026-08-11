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
import plistlib
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()

# 只统计这些扩展名，用于判断一个候选目录是否真的"有文档"
DOC_EXTS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".txt", ".md", ".csv", ".zip", ".rar", ".7z", ".pages", ".numbers", ".key",
}


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

    def to_dict(self) -> dict:
        return {**self.__dict__}


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
    out: list[Source] = []
    support = HOME / "Library" / "Application Support"

    chromium = [
        ("Chrome", support / "Google" / "Chrome"),
        ("Edge", support / "Microsoft Edge"),
        ("Brave", support / "BraveSoftware" / "Brave-Browser"),
    ]
    seen: set[str] = set()

    for name, base in chromium:
        if not base.exists():
            continue
        for prefs in base.glob("*/Preferences"):
            profile = prefs.parent.name
            d = _chromium_download_dir(prefs)
            target = Path(d) if d else HOME / "Downloads"
            if not target.exists() or str(target) in seen:
                continue
            seen.add(str(target))
            label = name if profile == "Default" else f"{name}（{profile}）"
            out.append(
                Source(
                    name=f"{label} 下载",
                    path=str(target),
                    kind="browser",
                    discovered_by="config",
                )
            )

    # Safari：读 defaults，未设置则用默认下载目录
    try:
        r = subprocess.run(
            ["defaults", "read", "com.apple.Safari", "DownloadsPath"],
            capture_output=True, text=True, timeout=5,
        )
        p = Path(r.stdout.strip()).expanduser() if r.returncode == 0 and r.stdout.strip() else HOME / "Downloads"
    except Exception:
        p = HOME / "Downloads"
    if p.exists() and str(p) not in seen:
        seen.add(str(p))
        out.append(Source(name="Safari 下载", path=str(p), kind="browser", discovered_by="config"))

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
    out: list[Source] = []
    bases = [
        HOME / "Library" / "Containers" / "com.tencent.qq" / "Data" / "Documents",
        HOME / "Library" / "Containers" / "com.tencent.qq" / "Data" / "Library"
        / "Application Support" / "com.tencent.qq" / "Documents",
        HOME / "Documents" / "Tencent Files",
    ]
    for base in bases:
        if not base.exists():
            continue
        candidates = [base] + [Path(p) for p in glob.glob(str(base / "*" / "FileRecv"))]
        for p in candidates:
            if not p.is_dir():
                continue
            total, docs, newest = _probe_dir(p)
            if docs == 0:
                continue
            out.append(
                Source(
                    name="QQ 接收",
                    path=str(p),
                    kind="im",
                    discovered_by="heuristic",
                    volatile=True,
                    file_count=total,
                    doc_count=docs,
                    recent_mtime=newest,
                )
            )
    return out


# ---------------------------------------------------------------- 系统目录

def discover_system() -> list[Source]:
    out = []
    for name, rel, note in [
        ("下载", "Downloads", ""),
        ("桌面", "Desktop", ""),
        ("文稿", "Documents", "文件多时建议在设置里收窄范围"),
    ]:
        p = HOME / rel
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
    sources: list[Source] = []
    for fn in (discover_browsers, discover_wechat, discover_qq, discover_system):
        try:
            sources.extend(fn())
        except Exception:
            continue  # 单个探测器失败不影响其余（§7.2：探测失败静默跳过）

    # 去重：同一路径只保留一条
    seen: set[str] = set()
    unique = []
    for s in sources:
        if s.path in seen:
            continue
        seen.add(s.path)
        s.permission_ok = _check_permission(Path(s.path))
        unique.append(s)

    # 有文档的排前面，其次按最近活动
    unique.sort(key=lambda s: (s.doc_count > 0, s.recent_mtime), reverse=True)
    return unique
