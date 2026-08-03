"""跨平台文件删除（移至回收站/垃圾箱）

提供在 Windows、macOS、Linux 上将文件移至回收站的功能。
删除的文件不会被永久删除，而是可以恢复。
"""

import os
import platform
import subprocess
import shutil
from pathlib import Path

from utils.i18n import _


def send_to_recycle_bin(file_paths):
    """将文件发送到回收站/垃圾箱（跨平台）

    Args:
        file_paths: 单个文件路径或路径列表

    Returns:
        tuple: (成功数, 失败列表[(文件名, 错误信息)])
    """
    if isinstance(file_paths, (str, Path)):
        file_paths = [str(file_paths)]
    else:
        file_paths = [str(p) for p in file_paths]

    success_count = 0
    failed = []
    system = platform.system()

    if system == "Windows":
        success_count, failed = _windows_recycle(file_paths)
    elif system == "Darwin":
        success_count, failed = _macos_trash(file_paths)
    else:
        success_count, failed = _linux_trash(file_paths)

    return success_count, failed


if platform.system() == "Windows":
    import ctypes
    from ctypes import wintypes

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    _FO_DELETE = 0x0003
    _FOF_ALLOWUNDO = 0x0040
    _FOF_NOCONFIRMATION = 0x0010
    _FOF_SILENT = 0x0004


def _windows_recycle(file_paths):
    """Windows 回收站操作（通过 Windows Shell API）"""
    success_count = 0
    failed = []

    for fp in file_paths:
        if not os.path.exists(fp):
            failed.append((fp, _("文件不存在")))
            continue
        try:
            path_buf = fp + '\0\0'
            op = _SHFILEOPSTRUCTW()
            op.hwnd = None
            op.wFunc = _FO_DELETE
            op.pFrom = path_buf
            op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT
            result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            if result == 0 and not op.fAnyOperationsAborted:
                success_count += 1
            else:
                failed.append((fp, _("错误码: ") + str(result) if result else _("操作被取消")))
        except Exception as e:
            failed.append((fp, str(e)))

    return success_count, failed


def _macos_trash(file_paths):
    """macOS 垃圾箱操作（通过 AppleScript 调用 Finder）

    使用 osascript 命令告诉 Finder 将文件移到废纸篓。
    """
    success_count = 0
    failed = []
    for fp in file_paths:
        if not os.path.exists(fp):
            failed.append((fp, _("文件不存在")))
            continue
        try:
            posix_path = os.path.abspath(fp)
            # AppleScript 字符串必须使用双引号（shlex.quote 的 POSIX 单引号会导致语法错误），
            # 需要转义路径中可能存在的双引号与反斜杠
            safe_path = posix_path.replace('\\', '\\\\').replace('"', '\\"')
            result = subprocess.run(
                ['osascript', '-e',
                 'tell application "Finder" to delete (POSIX file "' + safe_path + '" as POSIX file)'],
                capture_output=True, text=True, timeout=15,
                errors='replace'
            )
            if result.returncode == 0:
                success_count += 1
            else:
                failed.append((fp, result.stderr.strip() or _("osascript 执行失败")))
        except Exception as e:
            failed.append((fp, str(e)))
    return success_count, failed


def _linux_trash(file_paths):
    """Linux 垃圾箱操作

    尝试使用 gio trash（GNOME 环境）或 trash-put（FreeDesktop 标准）。
    如果都没有找到，则提示安装。
    """
    trash_cmd = None
    for cmd_name in ['gio', 'trash-put']:
        if shutil.which(cmd_name):
            trash_cmd = cmd_name
            break

    success_count = 0
    failed = []
    for fp in file_paths:
        if not os.path.exists(fp):
            failed.append((fp, _("文件不存在")))
            continue
        try:
            if trash_cmd == 'gio':
                result = subprocess.run(
                    ['gio', 'trash', fp],
                    capture_output=True, text=True, timeout=15, errors='replace'
                )
            elif trash_cmd == 'trash-put':
                result = subprocess.run(
                    ['trash-put', fp],
                    capture_output=True, text=True, timeout=15, errors='replace'
                )
            else:
                failed.append((fp, _("未找到垃圾箱工具（需要安装 gio 或 trash-put）")))
                continue

            if result.returncode == 0:
                success_count += 1
            else:
                failed.append((fp, result.stderr.strip() or _("执行失败: ") + trash_cmd))
        except Exception as e:
            failed.append((fp, str(e)))
    return success_count, failed
