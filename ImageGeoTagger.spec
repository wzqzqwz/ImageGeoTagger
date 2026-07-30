# -*- mode: python ; coding: utf-8 -*-

import os
import sys

block_cipher = None

app_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
entry_point = os.path.join(app_dir, '__main__.py')
locale_dir = os.path.join(app_dir, 'locales')
icon_dir = os.path.join(app_dir, 'icons')
exiftool_dir = os.path.join(app_dir, 'exiftool')

# Generate datas list for exiftool directory recursively
exiftool_datas = []
for root, dirs, files in os.walk(exiftool_dir):
    rel_root = os.path.relpath(root, app_dir)
    for f in files:
        src = os.path.join(root, f)
        exiftool_datas.append((src, rel_root))

a = Analysis(
    [entry_point],
    pathex=[app_dir],
    binaries=[],
    datas=[
        (locale_dir, 'locales'),
        (icon_dir, 'icons'),
        (os.path.join(app_dir, 'icon.ico'), '.'),
        (os.path.join(app_dir, 'icons', 'icon.png'), '.'),
    ] + exiftool_datas,
    hiddenimports=[
        'tkinterdnd2',
        'exifread',
        'piexif',
        'PIL',
        'PIL._tkinter_finder',
    ],
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
    name='ImageGeoTagger',
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
    icon=os.path.join(app_dir, 'icons', 'icon.ico'),
)
