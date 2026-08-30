# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— Ordo sidecar。

必须手动处理的原生依赖（PLAN §15 A0）：
  1. sqlite_vec/vec0.dylib —— SQLite 扩展，PyInstaller 不会自动收集 .dylib
  2. jieba 的词典数据      —— 纯数据文件，同样不在自动依赖图里
  3. onnxruntime/tokenizers —— Cross-Encoder 由运行时动态导入，必须显式
                              收集包内 DLL/PYD、providers 与隐藏子模块

任一遗漏都会让冻结后的 /health 报 degraded，而开发环境完全正常。
这正是 A0 冒烟要提前到第一天的原因。

入口使用 app/entrypoint.py，而不是继续往已经很大的 app/main.py 塞 feature
路由。entrypoint 只做路由组合，生命周期/认证/数据库仍由 app.main 负责。
"""

import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files

import sqlite_vec

# sqlite-vec 的原生扩展按平台命名：macOS .dylib / Windows .dll / Linux .so
_vec_lib = {"darwin": "vec0.dylib", "win32": "vec0.dll"}.get(sys.platform, "vec0.so")
vec_dylib = os.path.join(os.path.dirname(sqlite_vec.__file__), _vec_lib)

ort_datas, ort_binaries, ort_hiddenimports = collect_all("onnxruntime")
tokenizer_datas, tokenizer_binaries, tokenizer_hiddenimports = collect_all("tokenizers")

a = Analysis(
    ["app/entrypoint.py"],
    pathex=[],
    binaries=[(vec_dylib, "sqlite_vec"), *ort_binaries, *tokenizer_binaries],
    datas=[*collect_data_files("jieba"), *ort_datas, *tokenizer_datas],
    hiddenimports=[
        "uvicorn.logging", "uvicorn.protocols", "uvicorn.lifespan",
        *ort_hiddenimports,
        *tokenizer_hiddenimports,
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ordo-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 压缩会破坏 dylib 签名，macOS 上必须关
    console=True,
    target_arch="arm64" if sys.platform == "darwin" else None,
    codesign_identity=None,
    entitlements_file=None,
)
