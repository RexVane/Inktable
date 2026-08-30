"""Windows OCR backend using the built-in Windows.Media.Ocr API.

The sidecar is distributed as a PyInstaller executable, so adding a Python
WinRT binding would make the Windows build depend on another native runtime.
Windows PowerShell 5.1 is present on supported Windows versions; a short
PowerShell bridge lets us use the system OCR language packs without bundling
an OCR engine or sending document images off the machine.
"""

from __future__ import annotations

import base64
import functools
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("ordo.ocr.windows")

ENGINE_ID = "windows-media-ocr"
ENGINE_LABEL = "Windows 系统 OCR"

OCR_MAX_PAGES = 25
OCR_TOTAL_BUDGET = 150.0
OCR_PAGE_TIMEOUT = 30.0
OCR_DPI = 200
OCR_MAX_IMAGE_DIMENSION = 2400

# PowerShell's -EncodedCommand uses UTF-16LE. Keeping the image path in an
# environment variable avoids quoting/injection problems for arbitrary Windows
# paths (including non-ASCII names).
_POWERSHELL_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]

function Await-WinRt($operation, [Type] $resultType) {
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
            $_.GetParameters().Count -eq 1
        } | Select-Object -First 1
    if ($null -eq $asTask) { throw 'Windows Runtime task adapter unavailable' }
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $task.Wait()
    return $task.Result
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    $language = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages |
        Select-Object -First 1
    if ($null -ne $language) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    }
}
if ($null -eq $engine) { throw 'No Windows OCR language pack is installed' }

$imagePath = [Environment]::GetEnvironmentVariable('ORDO_OCR_IMAGE')
if ([string]::IsNullOrWhiteSpace($imagePath)) { throw 'OCR image path is missing' }

$file = Await-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imagePath)) `
    ([Windows.Storage.StorageFile])
$stream = Await-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) `
    ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) `
    ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) `
    ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await-WinRt ($engine.RecognizeAsync($bitmap)) `
    ([Windows.Media.Ocr.OcrResult])
[Console]::Out.Write($result.Text)
'''

_PROBE_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    $language = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages |
        Select-Object -First 1
    if ($null -ne $language) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    }
}
if ($null -eq $engine) { throw 'No Windows OCR language pack is installed' }
[Console]::Out.Write($engine.RecognizerLanguage.LanguageTag)
'''


def _encode_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


_ENCODED_SCRIPT = _encode_command(_POWERSHELL_SCRIPT)
_ENCODED_PROBE = _encode_command(_PROBE_SCRIPT)


def _powershell_executable() -> str | None:
    """Return the inbox Windows PowerShell executable, if present."""
    if sys.platform != "win32":
        return None
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidates = [
        Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0"
        / "powershell.exe",
    ]
    candidates.extend(
        Path(item) for item in (
            shutil.which("powershell.exe"),
            shutil.which("powershell"),
        ) if item
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _run_powershell(
    encoded_command: str,
    *,
    image_path: Path | None = None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    executable = _powershell_executable()
    if not executable:
        raise FileNotFoundError("Windows PowerShell is unavailable")
    env = os.environ.copy()
    if image_path is not None:
        env["ORDO_OCR_IMAGE"] = str(image_path.resolve())
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded_command,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
        creationflags=creationflags,
    )


@functools.lru_cache(maxsize=1)
def is_available() -> bool:
    """Whether Windows OCR and at least one user OCR language are available."""
    if sys.platform != "win32" or not _powershell_executable():
        return False
    try:
        result = _run_powershell(_ENCODED_PROBE, timeout=8.0)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Windows OCR probe failed: %s", exc)
        return False
    if result.returncode != 0:
        log.info("Windows OCR unavailable: %s", result.stderr.strip()[:240])
        return False
    return bool(result.stdout.strip())


def _normalize_text(text: str) -> str:
    """Remove OCR-inserted CJK spaces while retaining line boundaries."""
    lines = [" ".join(line.split()) for line in text.replace("\r\n", "\n").split("\n")]
    normalized = "\n".join(line for line in lines if line)
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", normalized)


def ocr_image(image_path: Path | str, timeout: float = OCR_PAGE_TIMEOUT) -> str:
    """Recognize one rendered page. Fail closed and let the caller add a warning."""
    if not is_available():
        return ""
    try:
        result = _run_powershell(
            _ENCODED_SCRIPT,
            image_path=Path(image_path),
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Windows OCR invocation failed: %s", exc)
        return ""
    if result.returncode != 0:
        log.warning("Windows OCR returned non-zero: %s", result.stderr.strip()[:240])
        return ""
    return _normalize_text(result.stdout)


def ocr_pdf(path: Path) -> tuple[list[tuple[int, str]], list[str]]:
    """OCR an image-only PDF page by page within bounded time and page budgets."""
    import pymupdf as fitz

    pages: list[tuple[int, str]] = []
    warnings: list[str] = []
    started = time.monotonic()
    doc = fitz.open(path)
    try:
        total_pages = doc.page_count
        limit = min(total_pages, OCR_MAX_PAGES)
        with tempfile.TemporaryDirectory(prefix="ordo-ocr-win-") as tmp:
            for pno in range(limit):
                if time.monotonic() - started > OCR_TOTAL_BUDGET:
                    warnings.append(
                        f"OCR 超出时间预算，只识别了前 {pno} 页（共 {total_pages} 页）"
                    )
                    break
                try:
                    page = doc.load_page(pno)
                    scale = min(
                        OCR_DPI / 72.0,
                        OCR_MAX_IMAGE_DIMENSION
                        / max(float(page.rect.width), float(page.rect.height), 1.0),
                    )
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
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
