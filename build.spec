# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 构建脚本。

用法:
  pyinstaller build.spec

生成单文件 EXE 到 dist/ 目录。
"""
import os
import sys
from pathlib import Path

block_cipher = None
project_root = Path(SPECPATH if 'SPECPATH' in dir() else os.getcwd())

a = Analysis(
    ['app.py'],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        ('static', 'static'),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'test', 'pydoc'],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='PhysicsStudyOS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    icon=None,
)
