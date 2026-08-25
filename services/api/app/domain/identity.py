"""文件身份 —— PLAN §8。

索引主键是 `(volume_uuid, inode)` 而不是路径。用户在 Finder 里移动或重命名文件后，
索引自动跟上：只更新 `files.path` 一个字段，不重新解析、不重新嵌入。
若以路径为主键，整理一次 Downloads 目录就要全库重建。

**卷标识的选型**（实测对比，见 docs/M2-NOTES.md）：

    os.stat().st_dev    0.001ms   但内核动态分配，重新挂载后会变
    diskutil VolumeUUID  208ms    写在卷元数据里，重启/换接口都不变

单独用任一个都不行：前者不稳定会导致外置盘重插后全库"消失"触发重索引；
后者每文件调一次的话，239 个文件要 50 秒。

所以 **st_dev 做缓存键，VolumeUUID 做稳定标识** —— 每个卷只调一次 diskutil，
之后走内存缓存。冷启动时缓存重建，st_dev 变了也会重新解析出同一个 UUID。
"""

from __future__ import annotations

import ctypes
import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class _Statfs(ctypes.Structure):
    _fields_ = [
        ("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64), ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32), ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_reserved", ctypes.c_uint32 * 8),
    ]


# libc/statfs 仅 macOS 可用；其它平台走 volume_uuid 里的平台分支
_libc = ctypes.CDLL("libc.dylib") if sys.platform == "darwin" else None

# st_dev → volume_uuid。进程内缓存，避免每个文件都调 diskutil（208ms）
_volume_cache: dict[int, str] = {}

# iCloud/Dropbox 占位文件标志：文件在云端未下载（§7.8）。**仅 macOS**：
# 这是 BSD 的 st_flags 位，Windows 的 os.stat() 根本没有 st_flags 字段。
SF_DATALESS = 0x40000000

# Windows 的云占位标志（OneDrive / WPS 云盘 / Dropbox 都用这套）。
#
# 实测教训：`is_dataless` 原先只看 `st_flags & SF_DATALESS`，而 Windows 上
# `getattr(st, "st_flags", 0)` **恒为 0** —— 占位检测在 Windows 上从来没有
# 生效过。于是云端未下载的文件一路走到读取，`open()` 抛
# `OSError [Errno 22] Invalid argument`，被记成 `hash_failed`。真实库里
# 624 个 WPS 云盘文件（326 个 PDF + 257 个 DOCX）就是这样静默丢掉的：
# 用户以为「索引坏了」，实际是「文件没下载」，两者的处置完全不同。
#
# 三个位都要看：OFFLINE 是传统的 HSM 归档位；RECALL_ON_OPEN 表示打开即召回；
# RECALL_ON_DATA_ACCESS 是现代云同步客户端用的（实测那个 WPS 文件正是
# 0x400020 = RECALL_ON_DATA_ACCESS | ARCHIVE）。
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_WIN_CLOUD_PLACEHOLDER = (
    FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


def is_cloud_placeholder(st) -> bool:
    """文件是否是「云端占位、本地无数据」。

    读它会触发下载（可能是几百 MB，也可能直接失败）。两个平台各有一套标志，
    统一在这里判断，调用方不必关心平台差异。
    """
    if sys.platform == "win32":
        return bool(getattr(st, "st_file_attributes", 0) & _WIN_CLOUD_PLACEHOLDER)
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def _mount_point(path: Path) -> str | None:
    if _libc is None:
        return None
    sfs = _Statfs()
    if _libc.statfs(str(path).encode(), ctypes.byref(sfs)) != 0:
        return None
    return sfs.f_mntonname.decode()


def volume_uuid(path: Path, st_dev: int | None = None) -> str:
    """解析路径所在卷的稳定标识。

    优先返回 diskutil 的 VolumeUUID；拿不到时回落到挂载点路径
    （比 st_dev 稳定：挂载点通常固定，而 st_dev 每次挂载都可能变）。
    """
    if st_dev is None:
        st_dev = os.stat(path).st_dev
    if st_dev in _volume_cache:
        return _volume_cache[st_dev]

    if sys.platform == "win32":
        # Windows 的 st_dev 即卷序列号（存于卷元数据，重启/换盘符不变），
        # 与 mac 的 VolumeUUID 语义一致，直接用作稳定标识。
        uuid = f"winvol:{st_dev}"
        _volume_cache[st_dev] = uuid
        return uuid

    uuid = ""
    mount = _mount_point(path)
    if mount:
        try:
            r = subprocess.run(
                ["diskutil", "info", "-plist", mount],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                info = plistlib.loads(r.stdout)
                uuid = info.get("VolumeUUID") or ""
        except Exception:
            pass
        if not uuid:
            uuid = f"mount:{mount}"   # 网络卷/镜像常常没有 UUID
    else:
        uuid = f"dev:{st_dev}"

    _volume_cache[st_dev] = uuid
    return uuid


@dataclass(frozen=True)
class FileIdentity:
    volume_uuid: str
    inode: int
    size: int
    mtime: float
    is_dataless: bool

    @property
    def key(self) -> tuple[str, int]:
        return (self.volume_uuid, self.inode)


def identify(path: Path | str) -> FileIdentity:
    p = Path(path)
    st = p.stat()
    return FileIdentity(
        volume_uuid=volume_uuid(p, st.st_dev),
        inode=st.st_ino,
        size=st.st_size,
        mtime=st.st_mtime,
        # 云端占位文件：读它会触发从云端下载整个文件（§7.8）。
        # 两个平台的标志不同，判断收在 is_cloud_placeholder 里。
        is_dataless=is_cloud_placeholder(st),
    )


def same_file(identity: FileIdentity, row) -> bool:
    """判断数据库里的记录是否仍指向同一个文件。

    仅比对 (volume_uuid, inode) 不够 —— **inode 会被系统复用**（§19 R21）。
    文件删除后新建的文件可能拿到同一个 inode，只看 inode 会把索引错误关联过去。
    附加 size + mtime 校验：三者全对才认为是同一个文件。
    """
    if row["volume_uuid"] != identity.volume_uuid or row["inode"] != identity.inode:
        return False
    if row["size"] != identity.size:
        return False
    # mtime 允许 1 秒误差：不同文件系统的时间戳精度不同
    if row["mtime"] is not None and abs(row["mtime"] - identity.mtime) > 1.0:
        return False
    return True
