"""文件扫描与登记 —— PLAN §11.2、§7.7。

职责：把一个来源目录里的文件登记进 files/contents 表。

三条硬约束：
  · **只读，不碰任何文件**（§1 约束 1）
  · iCloud 占位文件不读取内容（§7.8）—— 读它会触发从云端下载
  · 排除规则先行，避免 node_modules 之类把库淹掉（§7.7）
"""

from __future__ import annotations

import hashlib
import itertools
import mimetypes
import os
import stat
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from app.domain.identity import FileIdentity, identify
from app.watcher.policy import canonical_source_path, is_drive_root

# 永不从「整盘/用户目录」来源向下遍历的基础设施目录。
#
# 规则只作用于来源根的子目录：用户若明确把其中某个目录（例如微信真正的
# msg/file）单独设为来源，根目录本身不会被这份名单误伤。
EXCLUDED_DIRS = {
    # 版本库、依赖与构建缓存
    "node_modules", ".git", ".svn", ".hg", "pods", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".tox", ".next", ".nuxt",
    ".cache", ".npm", ".gradle", ".m2", "site-packages", "testdata",
    "library", ".trash", ".spotlight-v100", ".fseventsd",
    ".documentrevisions-v100",
    # 系统、商店与回收站
    "windows", "program files", "program files (x86)", "programdata",
    "appdata", "windowsapps", "wpsystem", "deliveryoptimization",
    "$recycle.bin", "system volume information", "recovery", "perflogs",
    "windows.old", "msocache", "onedrivetemp",
    # 通用缩略图与临时缓存。来源不再按应用区分，盘内目录统一递归。
    "thumb", "thumbtemp", "rwtemp",
    # 游戏库与工具链
    "steam", "steamapps", "steamlibrary", "windows kits",
    # 解释器/语言工具链；其中的 README、NEWS、字典不属于个人知识资料
    "go", "python",
}

# 目录形态的应用/文档包不能当普通目录展开，否则会把内部资源当用户资料。
PACKAGE_EXTS = {
    ".app", ".photoslibrary", ".xcodeproj", ".xcworkspace", ".bundle",
    ".framework", ".rtfd", ".pages", ".numbers", ".key", ".sparsebundle",
}

# 进入目录后看到这些标记，说明整棵树是代码项目。整盘来源不收项目文档；
# 用户若确实需要某个项目的 README，可把项目目录显式设成来源（根目录豁免）。
CODE_PROJECT_MARKERS = {
    ".git", "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "cargo.toml", "go.mod", "pom.xml",
    "build.gradle", "build.gradle.kts", "cmakelists.txt", "makefile",
}

# 品牌化安装目录可安全识别；普通的 Documents/build 之类名称不在这里，
# 避免仅凭一个通用目录名误杀用户资料。
INSTALL_DIR_NAMES = {
    "microsoft vs code", "android studio", "visual studio",
    "microsoft visual studio", "vmware workstation",
}
INSTALL_DIR_PREFIXES = ("pycharm ", "intellij idea ")
INSTALL_MARKERS = (
    ("python.exe",),
    ("code.exe",),
    ("vmware.exe",),
    ("cmd", "git.exe"),
    ("bin", "pycharm64.exe"),
    ("bin", "idea64.exe"),
    ("bin", "studio64.exe"),
    ("bin", "mvn.cmd"),
    ("platform-tools", "adb.exe"),
    ("sdk", "platform-tools", "adb.exe"),
)

# 允许入库并解析正文的唯一白名单。
FULLTEXT_EXTS = {".txt", ".docx", ".pdf", ".md", ".csv", ".html", ".htm"}

# 源码与配置：**彻底不入库**（2026-08 与用户确认的筛选规则）
#
# 早期版本把它们降级为"仅登记元数据"，实测仍然是噪音主力：文件列表被
# package.json / *.yml 刷屏，而知识库要收的是"人读的资料"。搜代码内容
# 本就该用 ripgrep / IDE 全局搜索，连名字都不值得占一行。
#
# 注意 .md **不在此列**：普通资料目录里的 Markdown 仍是全文文档；
# 只有带明确项目标记的整棵代码树会在目录层剪掉。用户显式选择项目根时
# 又会豁免该目录规则，因此仍可主动收录某个项目的设计文档。
CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".cc", ".h", ".hpp",
    ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".m", ".mm",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat",
    ".sql", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".css", ".scss", ".less", ".vue", ".svelte",
    ".lock", ".gradle", ".cmake", ".mk",
}

# 安装包/可执行/日志/下载残片：既不是"人读的资料"，也很少按名检索，
# 只会稀释列表与问答 —— 同样彻底不入库
NOISE_EXTS = {
    ".exe", ".msi", ".dll", ".sys", ".dmg", ".pkg", ".img", ".iso",
    ".apk", ".ipa", ".log", ".tmp", ".bak", ".crdownload", ".part", ".download",
}

# 当前没有“只登记元数据”格式：不在 FULLTEXT_EXTS 的文件一律不入库。
METADATA_EXTS: set[str] = set()

MAX_FULLTEXT_SIZE = 200 * 1024 * 1024   # 超过只登记元数据（§7.7 ④）
# 内容去重哈希的体积上限：元数据类超过此值不再读全文算 sha256 ——
# 整盘收录时给几十 GB 的视频/压缩包算哈希会把首扫拖成天级
#（实测 D 盘因此完全扫不动）。代价只是大媒体不参与「重复内容」归并。
MAX_DEDUP_SIZE = 64 * 1024 * 1024
MAX_DEPTH = 32
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
    content_ids: set[int] = field(default_factory=set)

    @property
    def accounted(self) -> int:
        """已处理的文件数，应等于 scanned。用于对账，防止静默漏登记。"""
        return (self.registered + self.unchanged + self.path_updated
                + self.content_updated + self.skipped_ext
                + self.skipped_dataless + self.errors)


def classify_ext(ext: str) -> str:
    """返回 fulltext / metadata / ignore。

    **默认白名单而非黑名单** —— 黑名单永远列不全（§7.7 ③）。
    CODE/NOISE 的显式列举只是让意图可读，兜底仍然是"不认识就 ignore"。
    """
    e = ext.lower()
    if e in FULLTEXT_EXTS:
        return "fulltext"
    if e in CODE_EXTS or e in NOISE_EXTS:
        return "ignore"
    if e in METADATA_EXTS:
        return "metadata"
    return "ignore"


def should_skip_dir(name: str) -> bool:
    """按目录名判断明确的基础设施/包目录（大小写不敏感）。"""
    low = name.casefold()
    if low in EXCLUDED_DIRS or low.startswith("."):
        return True
    if Path(name).suffix.casefold() in PACKAGE_EXTS:
        return True
    return low in INSTALL_DIR_NAMES or low.startswith(INSTALL_DIR_PREFIXES)


def _normal_path(path: Path | str) -> str:
    """用于来源归属比较的规范路径；Windows 自动折叠大小写。"""
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


def path_is_within(path: Path | str, root: Path | str, *, strict: bool = False) -> bool:
    """路径是否位于 root 内；避免易错的字符串前缀匹配。"""
    child, base = _normal_path(path), _normal_path(root)
    if strict and child == base:
        return False
    try:
        return os.path.commonpath((child, base)) == base
    except ValueError:  # 不同盘符
        return False


def _is_reparse_dir(path: Path) -> bool:
    """拒绝符号链接/junction/挂载点，防止环路与重复收录。"""
    try:
        st = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _is_profile_or_drive_root(path: Path) -> bool:
    """家目录与盘根永远不算代码项目。

    实测踩到的坑：用户家目录 `C:\\Users\\guica` 里有一个 `.git`（dotfiles 仓库
    或工具留下的），于是 `_looks_like_code_project` 对家目录返回 True，家目录
    **以下所有**文件都被判成"在代码项目内" —— 打开整盘剪枝时会把 OneDrive、
    WPSDrive、Desktop 里的个人简历、证件、升学材料一起排除，6266 个可见文件
    里 5409 个消失。家目录级别的标记是 dotfiles，不是"这棵树是代码项目"。
    """
    try:
        normalized = _normal_path(path)
        if normalized == _normal_path(path.anchor or path):
            return True
        return normalized == _normal_path(Path.home())
    except (OSError, RuntimeError):
        return False


def _looks_like_code_project(path: Path) -> bool:
    if _is_profile_or_drive_root(path):
        return False
    try:
        with os.scandir(path) as entries:
            names = {entry.name.casefold() for entry in entries}
        return bool(names & CODE_PROJECT_MARKERS) or any(
            name.endswith((".sln", ".xcodeproj")) for name in names
        )
    except OSError:
        return False


def _listing_is_code_project(dirnames: list[str], filenames: list[str]) -> bool:
    names = {n.casefold() for n in (*dirnames, *filenames)}
    if names & CODE_PROJECT_MARKERS:
        return True
    return any(n.endswith((".sln", ".xcodeproj")) for n in names)


def _looks_like_install_dir(path: Path) -> bool:
    low = path.name.casefold()
    if low in INSTALL_DIR_NAMES or low.startswith(INSTALL_DIR_PREFIXES):
        return True
    try:
        return any(path.joinpath(*parts).is_file() for parts in INSTALL_MARKERS)
    except OSError:
        return False


# 样板文档分两层，因为它们的"是不是噪声"取决于位置的程度不同。
#
# 第一层：AI agent 与编辑器的机器配置。它们在**任何位置**都不是用户写下的
# 知识，而是给工具读的配置，所以无条件挡掉。实测 skill.md 一项就 265 个。
AGENT_CONFIG_DOC_STEMS = {
    "skill", "agents", "claude", "gemini", "cursorrules", "copilotinstructions",
}
# 第二层：代码仓库样板。同一个文件名在不同位置含义完全不同 ——
# 用户 Documents 里的 README 可能是他自己写的说明，而 git 仓库里的
# README 是仓库样板。所以这一层**只在祖先目录是代码项目时**才挡。
# 实测真实库里 readme.md 有 348 个副本，是 .md 噪声的头号来源。
REPO_BOILERPLATE_DOC_STEMS = {
    "readme", "changelog", "changes", "contributing", "codeofconduct",
    "security", "authors", "maintainers", "notice", "history",
}
# 本地化与序号变体的基名：readme.en / README_CN / changelog-zh / README2
_BOILERPLATE_VARIANT_BASES = ("readme", "changelog", "contributing")
_DOC_SUFFIXES = {"md", "markdown", "txt", "rst"}


def _doc_stem(name: str) -> str | None:
    """取文档类文件的规范化主名；非文档扩展名返回 None。"""
    lowered = name.casefold()
    stem, _, suffix = lowered.rpartition(".")
    if not stem or suffix not in _DOC_SUFFIXES:
        return None
    return (
        stem.replace("_", "").replace("-", "").replace(" ", "").replace(".", "")
    )


def _is_agent_config_doc(name: str) -> bool:
    """第一层：agent / 编辑器机器配置，任何位置都挡。"""
    stem = _doc_stem(name)
    return stem in AGENT_CONFIG_DOC_STEMS if stem else False


def _is_repo_boilerplate_doc(name: str) -> bool:
    """第二层：仓库样板文档名。是否真的挡由调用方结合路径决定。"""
    stem = _doc_stem(name)
    if not stem:
        return False
    if stem in REPO_BOILERPLATE_DOC_STEMS:
        return True
    # 本地化/序号变体：余下部分必须是**纯 ASCII 短标记**才算样板。
    # 这道限制保住 readme笔记.md 这类 —— 带中文后缀说明是用户自己写的内容。
    for base in _BOILERPLATE_VARIANT_BASES:
        if stem.startswith(base):
            rest = stem[len(base):]
            if not rest:
                return True
            if len(rest) <= 6 and rest.isascii() and rest.isalnum():
                return True
    return False


def should_skip_file_name(name: str) -> bool:
    """过滤隐藏/临时文件、软件许可证巨型文本，以及 agent 机器配置文档。

    仓库样板（README / CHANGELOG …）**不在这里**挡：它同一个名字在
    用户资料目录里可能是真内容，只有落在代码项目里才是样板，
    所以交给 should_skip_path 结合路径判断。
    """
    if name.startswith(".") or name.startswith("~$"):
        return True
    if _is_agent_config_doc(name):
        return True
    compact = name.casefold().replace("_", "").replace("-", "").replace(" ", "")
    return (
        compact.startswith("thirdpartynotice")
        or compact.startswith("licenses.chromium")
        or compact.startswith("eula.")
        or compact.startswith("license.")
        or compact in {"license", "license.txt", "license.md", "copying", "copying.txt"}
    )


def should_skip_path(path: Path | str, root: Path | str,
                     max_depth: int = MAX_DEPTH, *, check_markers: bool = True) -> bool:
    """判断一个**文件**是否应跳过。

    扫描与实时监听共用同一套规则。来源根本身不受目录名/项目标记影响，
    因而用户的显式来源永远优先于整盘默认过滤。
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
    if len(parents) >= max_depth:
        return True
    if should_skip_file_name(p.name):
        return True

    # 样板文件名先做**纯字符串**预判，只有命中才去付 scandir 探项目标记的
    # 代价。反过来写会让整盘扫描的每个文件都对每层祖先 scandir 一次。
    boilerplate = _is_repo_boilerplate_doc(p.name)

    current = base
    for part in parents:
        current /= part
        if should_skip_dir(part):
            return True
        if (check_markers or boilerplate) and _looks_like_code_project(current):
            # 整盘收录时不剪整棵项目树：实测剪了会把用户写在带代码标记目录里的
            # 面试笔记、实现路线图一起排除。但项目里的 README / CHANGELOG
            # 仍然是样板而非知识，这里单独挡掉。
            #
            # 曾经试过把 docs/ 目录也一并算作项目文档（能多清 1200 个 .md），
            # 但它同时会删掉用户**自己**项目的设计文档 —— InkHole/docs 下的
            # 「墨洞项目计划」正是 gold 评测 X07 题依赖的资料。第三方克隆项目
            # 与自己的项目在路径上无法区分，所以这条不做成规则，交给用户按
            # 目录排除（见 sources 的排除机制）。
            return True
        if check_markers and _looks_like_install_dir(current):
            return True
    return False


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(HASH_BLOCK):
            h.update(block)
    return h.hexdigest()


def iter_files(root: Path, max_depth: int = MAX_DEPTH,
               prune_roots: tuple[Path | str, ...] = (),
               prune_projects: bool = True):
    """遍历目录树，应用通用目录、包、重解析点与嵌套来源规则。

    ``prune_projects`` is disabled for fixed-disk ingestion: the file suffix
    whitelist, rather than a project marker in an ancestor, decides what is
    registered inside a selected disk.
    """
    root = Path(root)
    root_depth = len(root.parts)
    pruned = {_normal_path(p) for p in prune_roots}

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        depth = len(current.parts) - root_depth
        if depth >= max_depth:
            dirnames.clear()
            continue

        # 显式来源根豁免；整盘扫描可选择跳过代码项目/安装树。
        if prune_projects and current != root and (
            _listing_is_code_project(dirnames, filenames)
            or (depth <= 3 and _looks_like_install_dir(current))
        ):
            dirnames.clear()
            yield None, True
            continue

        kept: list[str] = []
        skipped = 0
        for dirname in dirnames:
            candidate = current / dirname
            if (
                should_skip_dir(dirname)
                or _normal_path(candidate) in pruned
                or _is_reparse_dir(candidate)
            ):
                skipped += 1
            else:
                kept.append(dirname)
        dirnames[:] = kept
        for _ in range(skipped):
            yield None, True

        for fn in filenames:
            if should_skip_file_name(fn):
                continue
            path = current / fn
            # 当前目录的项目/安装判定已在上面做过，逐文件无需重复 stat 标记。
            if should_skip_path(path, root, max_depth, check_markers=False):
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

    # 超大文件不做内容哈希：全文类超过解析上限、元数据类超过去重上限
    skip_dedup = (
        (kind == "fulltext" and ident.size > MAX_FULLTEXT_SIZE)
        or (kind == "metadata" and ident.size > MAX_DEDUP_SIZE)
    )

    # 已登记过？先按身份查（§8：移动/改名后 inode 不变）
    row = conn.execute(
        "SELECT id, size, mtime, path, state, source_id, ext, mime, is_dataless, "
        "content_id, error_code, volume_uuid, inode FROM files "
        "WHERE volume_uuid = ? AND inode = ?",
        (ident.volume_uuid, ident.inode),
    ).fetchone()
    identity_matched = row is not None
    if row is None:
        row = conn.execute(
            "SELECT id, size, mtime, path, state, source_id, ext, mime, is_dataless, "
            "content_id, error_code, volume_uuid, inode FROM files WHERE path = ? "
            "ORDER BY id LIMIT 1",
            (str(path),),
        ).fetchone()

    if row:
        if kind == "ignore":
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

        if (identity_matched
                and row["size"] == ident.size
                and row["mtime"] == ident.mtime
                and row["error_code"] != HASH_FAILED
                and (row["content_id"] is not None or ident.is_dataless
                     or skip_dedup)):
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

    # 新的白名单外文件不入库
    if kind == "ignore":
        stats.skipped_ext += 1
        return None

    # iCloud 占位文件
    elif ident.is_dataless:
        stats.skipped_dataless += 1
        state = "cloud_placeholder"
        content_id = None

    # 过大文件
    elif skip_dedup:
        if kind == "fulltext":
            stats.skipped_too_large += 1
        state = "registered"
        content_id = None

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

    # 新文件跑一遍分类规则（A6）
    try:
        from app.organize.classify import classify_new_file

        classify_new_file(conn, cur.lastrowid)
    except Exception:
        pass

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


def _user_exclusions(conn) -> list[str]:
    """读取用户排除的目录。表可能还不存在（老库首次升级），此时视为无排除。"""
    try:
        return [row[0] for row in conn.execute("SELECT path FROM excluded_paths")]
    except Exception:  # noqa: BLE001 - 排除表缺失不能让扫描失败
        return []


def scan_source(conn, source_id: int, root: Path | str, progress=None,
                lock=None, prune_projects: bool = True) -> ScanStats:
    """扫描一个来源目录。

    扫描同时是**消失检测的兜底**（§11.1）：FSEvents 可能漏事件
    （应用没开着、网络卷、事件缓冲溢出），全量扫描比对"库里有、
    磁盘上没"的文件并标 missing —— 保留索引，等待重现自动恢复。

    ``lock``：传入全局写锁时按**小批次进出**（目录遍历不持锁，每批
    登记 + 提交后立即释放）。整盘首扫单来源可达几十分钟，若整个来源
    一把锁，/ask 这类持锁请求会排队等它扫完（实测问答被饿数分钟）。
    每批都 commit —— 释放 Python 锁的同时必须结束 SQLite 写事务，
    否则其它连接的写入仍会 database is locked。
    """
    stats = ScanStats()
    root = Path(root)
    guard = lock if lock is not None else nullcontext()

    # 来源允许嵌套，但一个文件只能归最具体来源。广域来源遍历时直接剪掉
    # 已启用的子来源，避免「QQ 扫完归 QQ，随后 B 盘又把它抢走」的来回改写。
    with guard:
        source_rows = conn.execute(
            "SELECT id, path FROM sources WHERE enabled = 1 AND id != ?",
            (source_id,),
        ).fetchall()
    nested_sources = [
        (r["id"], Path(r["path"])) for r in source_rows
        if path_is_within(r["path"], root, strict=True)
    ]
    # 用户排除的目录直接并进 prune_roots：iter_files 本来就按子树剪，
    # 不需要为排除功能另铺一套管道。排除只作用于索引层，不动磁盘文件。
    with guard:
        excluded_roots = tuple(
            Path(path) for path in _user_exclusions(conn)
            if path_is_within(path, root)
        )

    seen_ids: set[int] = set()
    files_iter = iter_files(
        root,
        prune_roots=tuple(p for _, p in nested_sources) + excluded_roots,
        prune_projects=prune_projects,
    )
    while True:
        # 文件系统遍历在锁外进行；每批 15 个进锁登记
        chunk = list(itertools.islice(files_iter, 15))
        if not chunk:
            break
        with guard:
            for path, was_excluded in chunk:
                if was_excluded:
                    stats.skipped_excluded += 1
                    continue
                stats.scanned += 1
                fid = register_file(conn, path, source_id, stats)
                if fid is not None:
                    seen_ids.add(fid)
                    content_row = conn.execute(
                        "SELECT content_id FROM files WHERE id = ?", (fid,)
                    ).fetchone()
                    if content_row and content_row["content_id"] is not None:
                        stats.content_ids.add(content_row["content_id"])
            conn.commit()
        if progress and stats.scanned % 100 < 15:
            progress(stats)

    # 对历史记录做三路收敛：
    # 1) 属于子来源 → 修正归属；2) 现已命中过滤规则 → 隐藏并解除索引；
    # 3) 磁盘确实不存在 → missing。原文件始终不动。
    with guard:
        rows = conn.execute(
            "SELECT id, path, ext, state FROM files "
            "WHERE source_id = ? AND state != 'missing'",
            (source_id,),
        ).fetchall()
    reassign: list[tuple[int, int]] = []
    ignored_ids: list[int] = []
    gone_ids: list[int] = []
    for row in rows:
        if row["id"] in seen_ids:
            continue
        owners = [
            (child_id, child_root) for child_id, child_root in nested_sources
            if path_is_within(row["path"], child_root)
        ]
        if owners:
            owner_id, _ = max(owners, key=lambda item: len(_normal_path(item[1])))
            reassign.append((owner_id, row["id"]))
            continue
        disk_path = Path(row["path"])
        if not disk_path.exists():
            gone_ids.append(row["id"])
        elif (
            classify_ext(row["ext"] or "") == "ignore"
            or should_skip_path(disk_path, root, check_markers=prune_projects)
        ):
            ignored_ids.append(row["id"])

    now = time.time()
    with guard:
        for owner_id, file_id in reassign:
            conn.execute(
                "UPDATE files SET source_id = ?, missing_since = NULL WHERE id = ?",
                (owner_id, file_id),
            )
        for file_id in ignored_ids:
            conn.execute(
                """UPDATE files SET content_id = NULL, state = 'ignored',
                   indexed_at = NULL, error_code = NULL, missing_since = NULL
                   WHERE id = ?""",
                (file_id,),
            )
        for gone_id in gone_ids:
            conn.execute(
                "UPDATE files SET state = 'missing', missing_since = ? WHERE id = ?",
                (now, gone_id),
            )
            stats.marked_missing += 1
        conn.commit()
    return stats


def preview_source(
    root: Path | str,
    limit: int = 50000,
    *,
    prune_projects: bool = True,
    prune_roots: tuple[Path | str, ...] = (),
) -> dict:
    """Preview exactly the directory policy used by a source scan."""
    root = canonical_source_path(root)
    total = will_index = excluded = ignored = 0
    stopped_early = False
    files_iter = iter(iter_files(
        root, prune_roots=prune_roots, prune_projects=prune_projects,
    ))
    for path, was_excluded in files_iter:
        if was_excluded:
            excluded += 1
            continue
        total += 1
        if classify_ext(path.suffix) == "ignore":
            ignored += 1
        else:
            will_index += 1
        if total >= limit:
            # Peek once so exactly-limit files are not falsely reported as
            # truncated when the iterator is actually exhausted.
            stopped_early = next(files_iter, None) is not None
            break
    return {
        "total_files": total,
        "will_index": will_index,
        "ignored_by_ext": ignored,
        "excluded_dirs": excluded,
        "truncated": stopped_early,
        "policy": {"prune_projects": prune_projects,
                    "root": str(root),
                    "prune_roots": [str(item) for item in prune_roots]},
    }
