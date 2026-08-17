"""Cross-platform system OCR facade."""

from __future__ import annotations

import sys
from pathlib import Path

from app.parsing import ocr_mac, ocr_windows

_runtime_enabled = True


def _backend():
    if sys.platform == "darwin":
        return ocr_mac
    if sys.platform == "win32":
        return ocr_windows
    return None


def engine_id() -> str:
    backend = _backend()
    return backend.ENGINE_ID if backend is not None else "unavailable"


def engine_label() -> str:
    backend = _backend()
    return backend.ENGINE_LABEL if backend is not None else "系统 OCR"


def is_available() -> bool:
    backend = _backend()
    return bool(backend is not None and backend.is_available())


def set_runtime_enabled(enabled: bool) -> None:
    global _runtime_enabled
    _runtime_enabled = bool(enabled)
    # Keep the old macOS module-level switch in sync for callers that import it
    # directly, while the parser uses this platform-neutral facade.
    ocr_mac.set_runtime_enabled(_runtime_enabled)


def runtime_enabled() -> bool:
    return _runtime_enabled


def ocr_pdf(path: Path) -> tuple[list[tuple[int, str]], list[str]]:
    backend = _backend()
    if backend is None or not backend.is_available():
        return [], ["未提取到文本，可能是扫描件（OCR 未启用或不可用）"]
    return backend.ocr_pdf(path)
