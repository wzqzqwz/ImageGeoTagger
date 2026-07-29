# -*- mode: python ; coding: utf-8 -*-

import os

a = Analysis(
    [os.path.join(SPECPATH, '__main__.py')],
    pathex=[SPECPATH, os.path.join(SPECPATH, '..')],
    binaries=[],
    datas=[(os.path.join(SPECPATH, '..', 'exiftool'), 'exiftool'),
           (os.path.join(SPECPATH, '..', 'icons', 'icon.ico'), '.'),
           (os.path.join(SPECPATH, '..', 'icons', 'icon.png'), '.')],
    hiddenimports=['geo_media_tool.main', 'geo_media_tool.ui.dialogs', 'geo_media_tool.ui.custom_msgbox', 'geo_media_tool.config', 'geo_media_tool.services.date_processor', 'geo_media_tool.services.geo_processor', 'geo_media_tool.utils.exif_utils', 'geo_media_tool.utils.platform_utils', 'geo_media_tool.utils.recycle_bin', 'piexif'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter.test', 'unittest', 'test', 'html', 'http', 'pydoc', 'doctest', 'argparse', 'difflib', 'pdb', 'profile', 'cProfile', 'lib2to3', 'ensurepip', 'idlelib', 'distutils', 'setuptools', 'pip', 'numpy.random._examples'],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    exclude_binaries=False,
    name='geo_media_tool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=os.path.join(SPECPATH, '..', 'icons', 'icon.ico'),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
