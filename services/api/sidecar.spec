# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— Inktable sidecar。

两个必须手动处理的原生依赖（PLAN §15 A0）：
  1. sqlite_vec/vec0.dylib —— SQLite 扩展，PyInstaller 不会自动收集 .dylib
  2. jieba 的词典数据      —— 纯数据文件，同样不在自动依赖图里

任一遗漏都会让冻结后的 /health 报 degraded，而开发环境完全正常。
这正是 A0 冒烟要提前到第一天的原因。
"""

import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

import sqlite_vec

vec_dylib = os.path.join(os.path.dirname(sqlite_vec.__file__), "vec0.dylib")

a = Analysis(
    ["app/main.py"],
    pathex=[],
    # tokenizers 是 Rust 原生扩展（V1.5 嵌入模型的分词器），
    # 与 vec0.dylib 一样需要显式收集
    binaries=[(vec_dylib, "sqlite_vec")] + collect_dynamic_libs("tokenizers"),
    datas=collect_data_files("jieba") + collect_data_files("model2vec"),
    hiddenimports=[
        "uvicorn.logging", "uvicorn.protocols", "uvicorn.lifespan",
        "model2vec", "tokenizers", "safetensors", "safetensors.numpy",
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
    name="inktable-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 压缩会破坏 dylib 签名，macOS 上必须关
    console=True,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)
