"""全盘候选发现 —— 用户显式触发的深度探测（参考 WizTree 的思路）。

WizTree 的秒级速度来自直读 NTFS 的 MFT，但那需要管理员权限且仅限
NTFS 卷。这里取其思想的免提权版本：只枚举**文件名元数据**（绝不打开
文件内容），配合大力剪枝 —— 系统目录、缓存、排除名单、重解析点/符号
链接一律跳过 —— 在 SSD 上通常十几秒内完成全部固定盘。

产出「文档密集目录」候选：按目录直接包含的文档数聚合，取分值最高的
若干个，且互不嵌套（已选目录的子孙不再重复出现）。与常规发现一样，
结果**默认不启用**（§1 约束 4）：勾选启用之前，不会索引任何内容。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from app.discovery.sources import DOC_EXTS, HOME, Source
from app.watcher.scanner import (
    LINUX_BOOT_SKIP_DIRS,
    MAC_BOOT_SKIP_DIRS,
    should_skip_dir,
)

# 系统与程序目录：不可能是用户资料库，无条件跳过（按目录名小写匹配，
# 任意层级生效 —— AppData 藏在用户目录下面，也要挡）
_SYSTEM_SKIP = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery", "perflogs",
    "appdata", "onedrivetemp", "windows.old", "msocache",
    "intel", "amd", "nvidia", "drivers",
    # macOS/Linux 对应物（deep_scan 也能在 mac 上跑）
    "applications", "system", "private", "cores",
}

MIN_DOCS = 5          # 少于这个文档数的目录不值得推荐
MAX_CANDIDATES = 40   # 最多推荐条数
TIME_BUDGET = 45.0    # 单盘扫描时间预算（秒），超时带着已有结果返回
_REPARSE = 0x400      # FILE_ATTRIBUTE_REPARSE_POINT


def _fixed_drives() -> list[Path]:
    """固定磁盘的根。可移动盘/网络盘不扫 —— 拔了就失效，别推荐。"""
    from app.discovery.sources import volume_roots

    roots = volume_roots()
    if roots:
        return roots
    if sys.platform != "win32":
        return [HOME]
    import ctypes

    drives: list[Path] = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if not bitmask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        # DRIVE_FIXED = 3
        if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:
            drives.append(Path(root))
    return drives


def _skip_name(name: str) -> bool:
    return should_skip_dir(name) or name.lower() in _SYSTEM_SKIP


def _is_nested_mount(root: Path, candidate: Path) -> bool:
    if sys.platform == "win32" or candidate == root:
        return False
    try:
        return candidate.is_mount()
    except OSError:
        return False


def _scan_drive(root: Path, counts: dict[str, int], deadline: float) -> bool:
    """迭代枚举一个盘，统计每个目录**直接包含**的文档数。

    返回 False 表示时间预算耗尽（结果仍然可用，只是不完整）。
    """
    root = Path(root)
    stack = [str(root)]
    visited = 0
    while stack:
        visited += 1
        if visited % 512 == 0 and time.time() > deadline:
            return False
        current = stack.pop()
        current_path = Path(current)
        boot_skip_names = (
            MAC_BOOT_SKIP_DIRS if sys.platform == "darwin"
            else LINUX_BOOT_SKIP_DIRS if sys.platform == "linux"
            else set()
        )
        boot_root = str(root) == "/" and current_path == root
        try:
            with os.scandir(current) as it:
                docs = 0
                for entry in it:
                    name = entry.name
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            candidate = Path(entry.path)
                            if (
                                _skip_name(name)
                                or (boot_root and name.casefold() in boot_skip_names)
                                or _is_nested_mount(root, candidate)
                            ):
                                continue
                            # 重解析点（junction/符号链接/挂载点）不下钻：
                            # 会造成环路或把同一棵树扫两遍
                            st = entry.stat(follow_symlinks=False)
                            if getattr(st, "st_file_attributes", 0) & _REPARSE:
                                continue
                            stack.append(entry.path)
                        elif not name.startswith("."):
                            if os.path.splitext(name)[1].lower() in DOC_EXTS:
                                docs += 1
                    except OSError:
                        continue
                if docs:
                    counts[current] = docs
        except (PermissionError, OSError):
            continue
    return True


def deep_scan(time_budget: float = TIME_BUDGET) -> list[Source]:
    """全盘发现文档密集目录，返回候选来源列表（全部未启用）。"""
    counts: dict[str, int] = {}
    complete = True
    for drive in _fixed_drives():
        deadline = time.time() + time_budget
        if not _scan_drive(drive, counts, deadline):
            complete = False

    picked: list[tuple[str, int]] = []
    for path, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n < MIN_DOCS or len(picked) >= MAX_CANDIDATES:
            break
        sep_path = path.rstrip("\\/") + os.sep
        if any(sep_path.startswith(q.rstrip("\\/") + os.sep) for q, _ in picked):
            continue  # 已选目录的子孙不重复推荐
        picked.append((path, n))

    out: list[Source] = []
    for path, n in picked:
        p = Path(path)
        out.append(Source(
            name=p.name or str(p),
            path=path,
            kind="manual",
            discovered_by="deepscan",
            file_count=n,
            doc_count=n,
            note="全盘发现" + ("" if complete else "（超时，结果不完整）"),
        ))
    return out
