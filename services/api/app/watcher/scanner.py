"""文件扫描与登记 —— PLAN §11.2、§7.7。

职责：把一个来源目录里的文件登记进 files/contents 表。

三条硬约束：
  · **只读，不碰任何文件**（§1 约束 1）
  · iCloud 占位文件不读取内容（§7.8）—— 读它会触发从云端下载
  · 排除规则先行，避免 node_modules 之类把库淹掉（§7.7）
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import time
from dataclasses import dataclass
from pathlib import Path

from app.domain.identity import FileIdentity, identify

# 不下钻的目录（§7.7 ①）。开发者的 ~/Documents 里一个 node_modules 就是几万个文件
EXCLUDED_DIRS = {
    "node_modules", ".git", ".svn", ".hg", "Pods", "vendor", "target",
    "DerivedData", "build", "dist", "out", ".next", ".nuxt", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".tox",
    "Library", ".Trash", ".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100",
    ".cache", ".npm", ".gradle", ".m2",
}

# 目录形态的"文件"，整体当作一个文件而不下钻（§7.7 ②）
PACKAGE_EXTS = {
    ".app", ".photoslibrary", ".xcodeproj", ".xcworkspace", ".bundle",
    ".framework", ".rtfd", ".pages", ".numbers", ".key", ".sparsebundle",
}

# 解析正文 + 建全文索引
#
# 只放**用户会用自然语言去搜内容**的格式。源码/配置不在此列 —— 见下方 CODE_EXTS。
FULLTEXT_EXTS = {
    ".pdf", ".docx", ".md", ".markdown", ".txt", ".rtf",
}

# 源码与配置：**只登记元数据，不解析正文**
#
# 实测（本机 ~/Documents）：源码类占入库文件的 59%，把它们降级后
# 需要解析正文的文件减少 78%，而用户并无实际损失 ——
# 文件仍可按文件名搜到，而搜代码内容本就该用 ripgrep / IDE 全局搜索。
#
# 注意 .md **不在此列**：它既是文档格式又常驻代码库（README、设计文档），
# 按扩展名归入文档类是正确的 —— 用路径判断"是否在代码目录里"会误杀
# ~/Documents/code/*/PLAN.md 这类真文档。
CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".cc", ".h", ".hpp",
    ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".m", ".mm",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat",
    ".sql", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".html", ".htm", ".css", ".scss", ".less", ".vue", ".svelte",
    ".lock", ".gradle", ".cmake", ".mk",
}

# 只登记元数据，不解析正文
METADATA_EXTS = {
    ".xlsx", ".xls", ".pptx", ".ppt", ".doc", ".csv",
    ".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp", ".svg", ".bmp", ".tiff",
    ".mp4", ".mov", ".avi", ".mkv", ".mp3", ".m4a", ".wav", ".flac",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".dmg", ".pkg", ".img", ".iso",
    ".pages", ".numbers", ".key", ".dwg", ".psd", ".ai", ".sketch", ".fig",
    ".epub", ".mobi", ".apk", ".ipa", ".log",
} | CODE_EXTS

MAX_FULLTEXT_SIZE = 200 * 1024 * 1024   # 超过只登记元数据（§7.7 ④）
MAX_DEPTH = 12
HASH_BLOCK = 8 * 1024 * 1024            # 分块流式，避免大文件占满内存


@dataclass
class ScanStats:
    scanned: int = 0
    registered: int = 0          # 新登记
    unchanged: int = 0           # 已在库且未变化
    path_updated: int = 0        # 身份命中但路径变了（文件被移动/改名）
    content_updated: int = 0     # 内容变了，需要重新索引
    duplicates: int = 0          # 内容与已有文件相同，复用 content
    skipped_excluded: int = 0
    skipped_ext: int = 0
    skipped_dataless: int = 0
    skipped_too_large: int = 0
    errors: int = 0

    @property
    def accounted(self) -> int:
        """已处理的文件数，应等于 scanned。用于对账，防止静默漏登记。"""
        return (self.registered + self.unchanged + self.path_updated
                + self.content_updated + self.skipped_ext
                + self.skipped_dataless + self.errors)


def classify_ext(ext: str) -> str:
    """返回 fulltext / metadata / ignore。

    **默认白名单而非黑名单** —— 黑名单永远列不全（§7.7 ③）。
    """
    e = ext.lower()
    if e in FULLTEXT_EXTS:
        return "fulltext"
    if e in METADATA_EXTS:
        return "metadata"
    return "ignore"


def should_skip_dir(name: str) -> bool:
    if name in EXCLUDED_DIRS or name.startswith("."):
        return True
    return Path(name).suffix.lower() in PACKAGE_EXTS


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(HASH_BLOCK):
            h.update(block)
    return h.hexdigest()


def iter_files(root: Path, max_depth: int = MAX_DEPTH):
    """遍历目录，应用排除规则。产出 (路径, 是否被排除跳过)。"""
    root = Path(root)
    root_depth = len(root.parts)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        if len(current.parts) - root_depth >= max_depth:
            dirnames.clear()
            continue

        skipped = [d for d in dirnames if should_skip_dir(d)]
        dirnames[:] = [d for d in dirnames if d not in skipped]
        for _ in skipped:
            yield None, True  # 计入 skipped_excluded

        for fn in filenames:
            if fn.startswith("."):
                continue
            yield current / fn, False


def register_file(conn, path: Path, source_id: int | None, stats: ScanStats) -> int | None:
    """登记单个文件，返回 file_id。已存在且未变化时直接返回原 id。"""
    try:
        ident = identify(path)
    except (OSError, PermissionError):
        stats.errors += 1
        return None

    ext = path.suffix.lower()
    kind = classify_ext(ext)
    if kind == "ignore":
        stats.skipped_ext += 1
        return None

    # 已登记过？先按身份查（§8：移动/改名后 inode 不变）
    row = conn.execute(
        "SELECT id, size, mtime, path FROM files WHERE volume_uuid = ? AND inode = ?",
        (ident.volume_uuid, ident.inode),
    ).fetchone()

    if row:
        # 路径变了但内容没变 → 只更新路径，不重新解析（§12.1 性能关键项）
        if row["size"] == ident.size and abs((row["mtime"] or 0) - ident.mtime) <= 1.0:
            if row["path"] != str(path):
                conn.execute(
                    "UPDATE files SET path = ?, name = ? WHERE id = ?",
                    (str(path), path.name, row["id"]),
                )
                stats.path_updated += 1
            else:
                stats.unchanged += 1
            return row["id"]

    # iCloud 占位文件：**绝不读取内容**，读了就会触发全量下载（§7.8）
    if ident.is_dataless:
        stats.skipped_dataless += 1
        content_id = None
        state = "cloud_placeholder"
    elif kind == "fulltext" and ident.size > MAX_FULLTEXT_SIZE:
        stats.skipped_too_large += 1
        content_id, state = _ensure_content(conn, path, ident, stats), "registered"
    else:
        content_id, state = _ensure_content(conn, path, ident, stats), "registered"

    now = time.time()
    mime = mimetypes.guess_type(path.name)[0]

    if row:  # 内容变了，更新既有记录
        conn.execute(
            "UPDATE files SET content_id=?, path=?, name=?, size=?, mtime=?, "
            "state=?, indexed_at=NULL WHERE id=?",
            (content_id, str(path), path.name, ident.size, ident.mtime, state, row["id"]),
        )
        stats.content_updated += 1
        return row["id"]

    cur = conn.execute(
        "INSERT INTO files (volume_uuid, inode, content_id, path, name, origin_path, "
        "source_id, ext, mime, size, state, is_dataless, mtime, detected_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ident.volume_uuid, ident.inode, content_id, str(path), path.name, str(path),
         source_id, ext, mime, ident.size, state, int(ident.is_dataless),
         ident.mtime, now),
    )
    stats.registered += 1
    return cur.lastrowid


def _ensure_content(conn, path: Path, ident: FileIdentity, stats: ScanStats) -> int | None:
    """按内容哈希去重。同一份内容只建一条 contents 记录（§9 contents）。"""
    try:
        digest = hash_file(path)
    except (OSError, PermissionError):
        stats.errors += 1
        return None

    row = conn.execute("SELECT id FROM contents WHERE sha256 = ?", (digest,)).fetchone()
    if row:
        stats.duplicates += 1
        return row["id"]  # 复用已有内容，不重复解析、不重复向量化

    cur = conn.execute(
        "INSERT INTO contents (sha256, size, parse_state) VALUES (?, ?, 'pending')",
        (digest, ident.size),
    )
    return cur.lastrowid


def scan_source(conn, source_id: int, root: Path | str, progress=None) -> ScanStats:
    """扫描一个来源目录。"""
    stats = ScanStats()
    root = Path(root)

    for path, was_excluded in iter_files(root):
        if was_excluded:
            stats.skipped_excluded += 1
            continue
        stats.scanned += 1
        register_file(conn, path, source_id, stats)

        if progress and stats.scanned % 100 == 0:
            conn.commit()
            progress(stats)

    conn.commit()
    return stats


def preview_source(root: Path | str, limit: int = 50000) -> dict:
    """启用前预扫描：只计数不入库（§7.7 ⑤）。

    让用户在点「启用」之前就看到"这个目录有 48 万个文件，将索引 1842 个"，
    把风险变成可见的决策点。
    """
    total = will_index = excluded = ignored = 0
    for path, was_excluded in iter_files(Path(root)):
        if was_excluded:
            excluded += 1
            continue
        total += 1
        if classify_ext(path.suffix) == "ignore":
            ignored += 1
        else:
            will_index += 1
        if total >= limit:
            break
    return {
        "total_files": total,
        "will_index": will_index,
        "ignored_by_ext": ignored,
        "excluded_dirs": excluded,
        "truncated": total >= limit,
    }
