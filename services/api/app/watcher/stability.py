"""文件稳定性检测 —— PLAN §11.1。

稳定性以文件自身连续采样为准（M0 实测修正版）：
  1. 文件名不带临时后缀  → .crdownload / .part / .tmp / .download / ~$ / . 开头
  2. (大小, mtime) 连续 N 次不变，且 mtime 已静置

``flock`` 是 advisory lock：普通写入者若没有主动加锁，另一个进程照样能取得
独占锁。因此它不能作为立即通过的快路径，更不能替代采样。

**必须监听 on_moved**（§11.1）：微信是"临时目录写完→move 到最终位置"，
浏览器是".crdownload 写入→改名去后缀" —— 两种主流下载模式里，
文件在最终位置出现的那一瞬，事件是 on_moved 而非 on_created。
只监听 on_created 会漏掉这两种情况。

大小轮询是所有事件类型共同的稳定性判据；on_moved 只影响事件来源，
不能跳过采样。微信/QQ/浏览器的主路径也必须经过这条判据。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

# 下载中的临时文件标记（§11.1 判据 1）
TEMP_PREFIXES = (".", "~$", ".~")
TEMP_SUFFIXES = (".crdownload", ".part", ".tmp", ".download", ".partial", ".wb")
TEMP_NAMES = {".DS_Store", ".localized", "Thumbs.db", "desktop.ini"}

SAMPLE_INTERVAL = 0.8   # 大小采样间隔（秒）
REQUIRED_STABLE = 3     # 连续几次采样不变才算稳定
STABILIZE_TIMEOUT = 60  # 超过则降级为低频后台轮询，不阻塞队列（§11.1）
MTIME_QUIET = 1.5       # mtime 至少静置这么久（挡住分段写入的间隙）


@dataclass
class StabilityResult:
    stable: bool
    reason: str  # 判据名，用于日志与调试


def looks_like_temp(name: str) -> bool:
    """判据 1：临时文件标记。"""
    if name in TEMP_NAMES:
        return True
    if name.startswith(TEMP_PREFIXES):
        return True
    low = name.lower()
    return any(low.endswith(s) for s in TEMP_SUFFIXES)


def _size_stable(path: str, samples: int = REQUIRED_STABLE) -> bool:
    """判据 2：(大小, mtime) 连续 samples 次采样不变。

    每次采样之间必须真的等待 SAMPLE_INTERVAL —— 原实现里"第三次确认"
    紧跟着上一次 stat，中间没有间隔，两个值几乎必然相等，等于只采了一次样。

    连 mtime 一起比：有些写入方式大小不变但内容在改（原地覆写）。
    """
    prev = None
    stable_count = 0

    for i in range(samples + 2):
        try:
            st = os.stat(path)
            cur = (st.st_size, st.st_mtime_ns)
        except OSError:
            return False

        if cur == prev:
            stable_count += 1
            # 采样不变 + mtime 已静置够久，两个条件都满足才算写完。
            # 只看采样会被分段写入的间隙骗过（见 stabilize 注释 ②）。
            if stable_count >= samples - 1:
                if time.time() - st.st_mtime >= MTIME_QUIET:
                    return True
        else:
            stable_count = 0
            prev = cur

        time.sleep(SAMPLE_INTERVAL)

    return False


def _has_exclusive_lock(path: str) -> bool:
    """探测 advisory flock（仅供诊断，不能作为稳定性通过条件）。

    macOS 下大部分程序（含微信）不会主动持有 flock。即使普通写入句柄仍开着，
    此函数也很可能返回 True；调用方不得据此断言文件已经写完。
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (ImportError, OSError):  # Windows 无 fcntl：诊断探测视为不持锁
        return False
    finally:
        os.close(fd)


def stabilize(path: str, moved_in: bool = False) -> StabilityResult:
    """判断文件是否已写完。

    moved_in=True 只保留事件来源信息，不再绕过采样。树内 rename 也可能发生在
    写入尚未结束时，而 advisory flock 无法证明普通写入句柄已经关闭。

    判据 1 恒为前置：文件名还带临时后缀，无论多大都直接判不稳定。
    """
    name = os.path.basename(path)

    if looks_like_temp(name):
        return StabilityResult(False, "temp_suffix")

    # 判据设计 —— 每一条都是被实测推翻后重写的：
    #
    # ① 原方案（§11.1）把 on_moved 当快路径主入口。**实测证伪**：
    #    macOS FSEvents 对「从监听树外部移入」的文件报 created，不报 moved
    #    （FSEvents 只给目录级合并事件，watchdog 据此合成 created）。
    #    on_moved 只在监听树**内部**重命名时才触发。
    #    → 微信/QQ「临时目录 → 移入」这条主路径上 moved_in 恒为 False。
    #    所以判据**不能依赖事件类型**，只能看文件自身的可观测状态。
    #
    # ② 光有 flock 不够：分段写入的进程在两次 write 之间并不持锁，
    #    撞上那个窗口就误判写完。实测一个每 1.1 秒 flush 的写入被判稳定两次。
    #
    # ③ 也不能只靠去抖：FSEvents 会合并事件，flush 间隔小于去抖窗口时
    #    照样可能让路径「看起来静默」。mtime 直接读文件系统，比事件可信。
    #
    # 最终判据：(size, mtime_ns) 连续采样不变 **且** mtime 已静置。
    # moved_in 不再提供绕过采样的快路径。
    try:
        os.stat(path)
    except OSError:
        return StabilityResult(False, "vanished")

    if _size_stable(path):
        return StabilityResult(True, "quiet")

    return StabilityResult(False, "still_growing")
