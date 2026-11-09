# -*- mode: python ; coding: utf-8 -*-
"""目憩 PyInstaller 打包配置（PyInstaller 6.x）。

关键点：
* 用 SPECPATH 与 cv2.data.haarcascades 在**构建时动态定位**资源，
  不写死任何本机 .venv 绝对路径，保证 spec 可移植。
* icon.ico 与 Haar 级联 XML 都收进包内 assets/，对应
  config/default.py 中 frozen 状态下的 RESOURCE_DIR/assets。
* collect_data_files("cv2") 额外保留 OpenCV 自带数据，双保险。
* console=False → --windowed，启动不弹控制台。
"""
from pathlib import Path

import cv2
from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)
cascade = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
icon_file = ROOT / "assets" / "icon.ico"
assert cascade.is_file(), "找不到 Haar 级联文件: %s" % cascade
assert icon_file.is_file(), "找不到应用图标: %s" % icon_file

datas = [
    (str(icon_file), "assets"),
    (str(cascade), "assets"),
]
datas += collect_data_files("cv2")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=["pystray._win32", "PIL.ImageTk", "cv2.data", "winsound"],
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
    a.binaries,
    a.datas,
    [],
    name="目憩",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(icon_file)],
)
