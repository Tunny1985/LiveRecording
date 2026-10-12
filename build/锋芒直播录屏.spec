# -*- mode: python ; coding: utf-8 -*-
# 路径按 spec 自己的位置推项目根目录（build/ 的上一层），项目挪到哪都能打包。
from pathlib import Path

APP = Path(SPECPATH).parent

a = Analysis(
    [str(APP / 'app.py')],
    pathex=[],
    binaries=[],
    datas=[(str(APP / 'ui.bin'), '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='锋芒直播录屏',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(APP / 'ui' / 'app.ico')],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='锋芒直播录屏',
)
