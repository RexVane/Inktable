# Linux

Ordo on Linux uses the same **local-disk source model** as Windows and macOS:
top-level sources are `/` plus extra local mounts (`/mnt/…`, `/media/…`,
`/run/media/…`, or a separate partition such as `/data`). Chat apps, browsers,
and Downloads are not listed as sources.

Linux 与 Windows / macOS 同一套**本地磁盘来源**：顶层是 `/`，外加本机挂载盘。
微信、浏览器、下载目录不单独列为来源。

## Data directory / 数据目录

`~/.local/share/Ordo` (or `$XDG_DATA_HOME/Ordo`). Existing Inktable libraries
are still adopted if present.

## Packaging / 打包

```bash
cd services/api && uv run --group dev pyinstaller sidecar.spec --clean --noconfirm
cd ../../apps/desktop && npm run dist -- --linux --x64
```

Artifacts: AppImage and `.deb` (x64). Window chrome is the native GTK/Qt
title bar (Electron has no Windows-style overlay on Linux).

## OCR

System OCR is macOS Vision / Windows.Media.Ocr only. On Linux, scanned PDFs
without a text layer stay “no text extracted” unless you add a text layer
yourself. Search and Q&A still work for born-digital files.

扫描件 OCR 目前只有 macOS / Windows 系统引擎。Linux 上无文本层的 PDF 不会
自动 OCR；可检索的仍是本身带文字的文档。
