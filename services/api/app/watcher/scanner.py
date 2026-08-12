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

# 内容读取失败不是「文件没有变化」。保留明确的可重试错误码，避免下一次
# 扫描仅因 size/mtime 相同就把它误判为 unchanged。
HASH_FAILED = "hash_failed"


class ContentReadError(RuntimeError):
    """读取文件正文以计算内容哈希失败，可在后续扫描中重试。"""


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
    marked_missing: int = 0      # 全量扫描发现"库里有、磁盘上没"
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


def should_skip_path(path: Path | str, root: Path | str,
                     max_depth: int = MAX_DEPTH) -> bool:
    """判断一个**文件**是否应按扫描规则排除。

    扫描与实时监听必须使用同一套规则：否则首次扫描避开 node_modules，
    监听却会在用户安装依赖时把其中几万个文件重新塞进库。这里以文件相对
    来源根目录的祖先目录为准；用户若显式把某个目录作为来源根，则它本身
    不因名字（例如 ``node_modules``）而被排除，这与 ``iter_files`` 一致。
    """
    p, base = Path(path), Path(root)
    try:
        rel = p.relative_to(base)
    except ValueError:
        # watchdog 在 macOS 上可能把 /var 规范化为 /private/var；扫描来源则
        # 保留用户输入。用解析后的路径再比一次，避免把合法事件误排除。
        try:
            rel = p.resolve(strict=False).relative_to(base.resolve(strict=False))
        except ValueError:
            # 事件不属于这个来源时绝不能当作可索引文件处理。
            return True

    parents = rel.parts[:-1]
    # iter_files 在当前目录深度 >= MAX_DEPTH 时不再处理其中的文件。
    if len(parents) >= max_depth:
        return True
    return any(should_skip_dir(part) for part in parents)


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
            path = current / fn
            if should_skip_path(path, root, max_depth):
                continue
            yield path, False


def register_file(conn, path: Path, source_id: int | None, stats: ScanStats) -> int | None:
    """登记单个文件，返回 file_id。已存在且未变化时直接返回原 id。"""
    ext = path.suffix.lower()
    mime = mimetypes.guess_type(path.name)[0]
    try:
        ident = identify(path)
    except (OSError, PermissionError):
        # 对已知路径留下可重试记录；不能因一次临时权限/读取失败而把旧
        # content 解关联，更不能下一轮把它当 unchanged。
        row = conn.execute("SELECT id FROM files WHERE path = ?", (str(path),)).fetchone()
        if row:
            conn.execute(
                """UPDATE files
                   SET source_id = ?, name = ?, ext = ?, mime = ?, state = 'failed',
                       error_code = ?, retry_count = retry_count + 1
                   WHERE id = ?""",
                (source_id, path.name, ext, mime, HASH_FAILED, row["id"]),
            )
        stats.errors += 1
        return None

    kind = classify_ext(ext)

    # 已登记过？先按身份查（§8：移动/改名后 inode 不变）
    row = conn.execute(
        "SELECT id, size, mtime, path, state, source_id, ext, mime, is_dataless, "
        "content_id, error_code, volume_uuid, inode FROM files "
        "WHERE volume_uuid = ? AND inode = ?",
        (ident.volume_uuid, ident.inode),
    ).fetchone()
    identity_matched = row is not None
    if row is None:
        # 原子替换会让同一路径得到新 inode。路径兜底复用原 file_id，避免
        # 同一路径留下两条记录；由于身份不同，下面不会走 unchanged 快路径。
        row = conn.execute(
            "SELECT id, size, mtime, path, state, source_id, ext, mime, is_dataless, "
            "content_id, error_code, volume_uuid, inode FROM files WHERE path = ? "
            "ORDER BY id LIMIT 1",
            (str(path),),
        ).fetchone()

    if row:
        if kind == "ignore":
            # 已索引文件改名到白名单外，或同一路径被原子替换成忽略类型：
            # 保留身份记录以继续追踪，但立即解除 content，避免旧全文仍以
            # 新文件名/类型出现在检索结果里。孤儿 content 由既有清扫流程处理。
            conn.execute(
                """UPDATE files
                   SET volume_uuid = ?, inode = ?, content_id = NULL, path = ?, name = ?,
                       source_id = ?, ext = ?, mime = ?, size = ?, mtime = ?,
                       state = 'ignored', is_dataless = ?, indexed_at = NULL,
                       error_code = NULL, retry_count = 0, missing_since = NULL
                   WHERE id = ?""",
                (ident.volume_uuid, ident.inode, str(path), path.name, source_id, ext, mime,
                 ident.size, ident.mtime, int(ident.is_dataless), row["id"]),
            )
            stats.skipped_ext += 1
            return None

        # 路径变了但内容没变 → 只更新路径，不重新解析（§12.1 性能关键项）
        # 但 hash/read 失败的文件是例外：其 metadata 即使没有变化，也必须
        # 在下一次扫描重新尝试读取，不能进入永久 unchanged。
        if (identity_matched
                and row["size"] == ident.size
                and row["mtime"] == ident.mtime
                and row["error_code"] != HASH_FAILED
                and (row["content_id"] is not None or ident.is_dataless)):
            changed = any((
                row["path"] != str(path),
                row["source_id"] != source_id,
                row["ext"] != ext,
                row["mime"] != mime,
                bool(row["is_dataless"]) != ident.is_dataless,
            ))
            conn.execute(
                """UPDATE files
                   SET path = ?, name = ?, source_id = ?, ext = ?, mime = ?,
                       is_dataless = ?,
                       state = CASE WHEN state = 'missing' THEN 'registered' ELSE state END,
                       missing_since = NULL
                   WHERE id = ?""",
                (str(path), path.name, source_id, ext, mime, int(ident.is_dataless), row["id"]),
            )
            if changed:
                stats.path_updated += 1
            else:
                stats.unchanged += 1
            return row["id"]

    # 新的白名单外文件不入库；已登记文件已在上面更新为 ignored 并解除全文。
    if kind == "ignore":
        stats.skipped_ext += 1
        return None

    # iCloud 占位文件：**绝不读取内容**，读了就会触发全量下载（§7.8）
    if ident.is_dataless:
        stats.skipped_dataless += 1
        content_id = None
        state = "cloud_placeholder"
    elif kind == "fulltext" and ident.size > MAX_FULLTEXT_SIZE:
        stats.skipped_too_large += 1
        try:
            content_id = _ensure_content(conn, path, ident, stats)
        except ContentReadError:
            _record_content_read_failure(conn, row, path, source_id, ident, ext, mime, stats)
            return None
        state = "registered"
    else:
        try:
            content_id = _ensure_content(conn, path, ident, stats)
        except ContentReadError:
            _record_content_read_failure(conn, row, path, source_id, ident, ext, mime, stats)
            return None
        state = "registered"

    now = time.time()
    if row:  # 内容变了，更新既有记录
        conn.execute(
            """UPDATE files
               SET volume_uuid = ?, inode = ?, content_id = ?, path = ?, name = ?,
                   source_id = ?, ext = ?, mime = ?,
                   size = ?, mtime = ?, state = ?, is_dataless = ?, indexed_at = NULL,
                   error_code = NULL, retry_count = 0, missing_since = NULL
               WHERE id = ?""",
            (ident.volume_uuid, ident.inode, content_id, str(path), path.name, source_id,
             ext, mime, ident.size, ident.mtime, state, int(ident.is_dataless), row["id"]),
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

    # 新文件跑一遍分类规则（A6）。规则只碰未归类文件，永不覆盖用户决定。
    try:
        from app.organize.classify import classify_new_file

        classify_new_file(conn, cur.lastrowid)
    except Exception:
        pass  # 分类失败不该影响登记

    return cur.lastrowid


def _ensure_content(conn, path: Path, ident: FileIdentity, stats: ScanStats) -> int | None:
    """按内容哈希去重。同一份内容只建一条 contents 记录（§9 contents）。"""
    try:
        digest = hash_file(path)
    except (OSError, PermissionError) as e:
        raise ContentReadError(str(e)) from e

    row = conn.execute("SELECT id FROM contents WHERE sha256 = ?", (digest,)).fetchone()
    if row:
        stats.duplicates += 1
        return row["id"]  # 复用已有内容，不重复解析、不重复向量化

    cur = conn.execute(
        "INSERT INTO contents (sha256, size, parse_state) VALUES (?, ?, 'pending')",
        (digest, ident.size),
    )
    return cur.lastrowid


def _record_content_read_failure(conn, row, path: Path, source_id: int | None,
                                 ident: FileIdentity, ext: str, mime: str | None,
                                 stats: ScanStats) -> None:
    """记录可重试的哈希失败，同时保留已关联的旧 content。"""
    now = time.time()
    if row:
        # 特别不要写 content_id = NULL：旧索引仍是最后一个已知可靠版本，
        # 在下一次读取成功前必须保留。
        conn.execute(
            """UPDATE files
               SET volume_uuid = ?, inode = ?, path = ?, name = ?, source_id = ?,
                   ext = ?, mime = ?, size = ?, mtime = ?,
                   is_dataless = ?, state = 'failed', error_code = ?,
                   retry_count = retry_count + 1, missing_since = NULL
               WHERE id = ?""",
            (ident.volume_uuid, ident.inode, str(path), path.name, source_id, ext, mime,
             ident.size, ident.mtime, int(ident.is_dataless), HASH_FAILED, row["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO files
               (volume_uuid, inode, content_id, path, name, origin_path, source_id, ext, mime,
                size, state, error_code, retry_count, is_dataless, mtime, detected_at)
               VALUES (?,?,NULL,?,?,?,?,?,?,?,'failed',?,1,?,?,?)""",
            (ident.volume_uuid, ident.inode, str(path), path.name, str(path), source_id,
             ext, mime, ident.size, HASH_FAILED, int(ident.is_dataless), ident.mtime, now),
        )
    stats.errors += 1


def scan_source(conn, source_id: int, root: Path | str, progress=None) -> ScanStats:
    """扫描一个来源目录。

    扫描同时是**消失检测的兜底**（§11.1）：FSEvents 可能漏事件
    （应用没开着、网络卷、事件缓冲溢出），全量扫描比对"库里有、
    磁盘上没"的文件并标 missing —— 保留索引，等待重现自动恢复。
    """
    stats = ScanStats()
    root = Path(root)

    seen_ids: set[int] = set()
    for path, was_excluded in iter_files(root):
        if was_excluded:
            stats.skipped_excluded += 1
            continue
        stats.scanned += 1
        fid = register_file(conn, path, source_id, stats)
        if fid is not None:
            seen_ids.add(fid)

        if progress and stats.scanned % 100 == 0:
            conn.commit()
            progress(stats)

    # 库里属于本来源、这次没扫到、磁盘上也确实不在 → missing。
    # 双重确认（not exists）防误伤：文件可能刚被移出扫描深度/排除目录，
    # 但仍在原地 —— 那不算消失。
    rows = conn.execute(
        "SELECT id, path FROM files WHERE source_id = ? AND state != 'missing'",
        (source_id,),
    ).fetchall()
    now = time.time()
    for r in rows:
        if r["id"] in seen_ids:
            continue
        if Path(r["path"]).exists():
            continue
        conn.execute(
            "UPDATE files SET state = 'missing', missing_since = ? WHERE id = ?",
            (now, r["id"]),
        )
        stats.marked_missing += 1

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
