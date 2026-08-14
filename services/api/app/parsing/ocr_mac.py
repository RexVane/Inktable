"""扫描件 OCR —— macOS 自带 Vision 框架，零新增依赖。

实现路线：pymupdf 把 PDF 页渲染成 PNG → 经 `/usr/bin/osascript`（JXA）
调用系统 Vision 的 VNRecognizeTextRequest 识别中英文。不引入 pyobjc /
tesseract / onnx 等重依赖，打包体积零增长；非 macOS 或系统过旧时
`is_available()` 为 False，解析器保持原有"扫描件不支持"的提示。

预算纪律：每页一次 osascript 进程调用（实测每页约 1-2 秒），
上限 OCR_MAX_PAGES 页、总预算 OCR_TOTAL_BUDGET 秒，超限截断并留 warning ——
扫描件通常是合同/报告，前 25 页足以支撑检索。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("inktable.ocr")

OCR_MAX_PAGES = 25
OCR_TOTAL_BUDGET = 150.0   # 秒
OCR_PAGE_TIMEOUT = 30.0    # 秒/页
OCR_DPI = 200

# 运行时开关：由索引管线按 settings 表的 ocr_enabled 设置（默认开）。
# 解析器拿不到数据库连接，经模块级状态传递 —— 单写者（索引循环）场景安全。
_runtime_enabled = True

_JXA = r"""
ObjC.import('Foundation');
ObjC.import('Vision');
function run(argv) {
  const url = $.NSURL.fileURLWithPath(argv[0]);
  const handler = $.VNImageRequestHandler.alloc.initWithURLOptions(url, $({}));
  const request = $.VNRecognizeTextRequest.alloc.init;
  request.recognitionLevel = $.VNRequestTextRecognitionLevelAccurate;
  request.recognitionLanguages = $(['zh-Hans', 'zh-Hant', 'en-US']);
  request.usesLanguageCorrection = true;
  handler.performRequestsError($([request]), null);
  const results = request.results;
  const lines = [];
  if (!results.isNil()) {
    for (let i = 0; i < results.count; i++) {
      const top = results.objectAtIndex(i).topCandidates(1);
      if (top.count > 0) lines.push(ObjC.unwrap(top.objectAtIndex(0).string));
    }
  }
  return lines.join('\n');
}
"""


def set_runtime_enabled(enabled: bool) -> None:
    global _runtime_enabled
    _runtime_enabled = bool(enabled)


def runtime_enabled() -> bool:
    return _runtime_enabled


def is_available() -> bool:
    return sys.platform == "darwin" and Path("/usr/bin/osascript").is_file()


def ocr_image(image_path: Path | str, timeout: float = OCR_PAGE_TIMEOUT) -> str:
    """识别单张图片，返回按行拼接的文本。失败返回空串（调用方计 warning）。"""
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", _JXA,
             str(Path(image_path).resolve())],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("OCR 调用失败：%s", exc)
        return ""
    if proc.returncode != 0:
        log.warning("OCR 返回非零：%s", (proc.stderr or "").strip()[:200])
        return ""
    return proc.stdout.strip()


def ocr_pdf(path: Path) -> tuple[list[tuple[int, str]], list[str]]:
    """对无文本层的 PDF 做逐页 OCR。

    返回 ([(页码, 文本), ...], warnings)。页码从 1 开始。
    """
    import pymupdf as fitz

    pages: list[tuple[int, str]] = []
    warnings: list[str] = []
    started = time.monotonic()

    doc = fitz.open(path)
    try:
        total_pages = doc.page_count
        limit = min(total_pages, OCR_MAX_PAGES)
        with tempfile.TemporaryDirectory(prefix="inktable-ocr-") as tmp:
            for pno in range(limit):
                if time.monotonic() - started > OCR_TOTAL_BUDGET:
                    warnings.append(
                        f"OCR 超出时间预算，只识别了前 {pno} 页（共 {total_pages} 页）")
                    break
                try:
                    pix = doc.load_page(pno).get_pixmap(dpi=OCR_DPI)
                    png = Path(tmp) / f"p{pno}.png"
                    pix.save(str(png))
                except Exception:
                    warnings.append(f"第 {pno + 1} 页渲染失败，OCR 跳过")
                    continue
                text = ocr_image(png)
                if text:
                    pages.append((pno + 1, text))
        if total_pages > limit and not any("预算" in w for w in warnings):
            warnings.append(f"扫描件较长，OCR 只识别了前 {limit} 页（共 {total_pages} 页）")
    finally:
        doc.close()

    return pages, warnings
