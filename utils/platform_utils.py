"""跨平台工具函数

提供在不同操作系统（Windows、macOS、Linux）上
运行一致性的功能：控制台隐藏、文件打开、路径查找等。
"""

import os
import sys
import platform
import subprocess
from utils.i18n import _
from utils.logging_utils import log_exc


def _thread_is_alive(t):
    """线程是否存活（未启动或已停止均视为不存活，避免 is_alive 抛 RuntimeError"""
    try:
        return t.is_alive()
    except (RuntimeError, AttributeError):
        return False


def get_app_dir():
    """获取应用程序目录（兼容 PyInstaller 打包模式）

    在开发模式下返回项目根目录，
    在打包为 exe 后返回可执行文件所在目录或 _MEIPASS 临时目录。

    Returns:
        str: 应用程序目录的绝对路径
    """
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后的路径
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            return meipass
        return os.path.dirname(sys.executable)
    else:
        # 开发模式：返回项目根目录（utils 的父目录的父目录）
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hide_console_window():
    """隐藏 Windows 控制台窗口

    在 Windows 上运行 .pyw 或打包后的 exe 时，
    隐藏后台的控制台窗口，只显示 GUI 窗口。
    同时设置 UTF-8 编码以支持中文输出。
    """
    if platform.system() != "Windows":
        return
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        console_window = kernel32.GetConsoleWindow()
        if console_window:
            user32.ShowWindow(console_window, 0)  # SW_HIDE = 0
    except Exception:
        log_exc()


def get_startupinfo():
    """返回 STARTUPINFO 配置以隐藏子进程的控制台窗口

    在通过 subprocess 调用外部命令（如 ExifTool）时使用，
    防止弹出的命令行窗口干扰 GUI。

    Returns:
        subprocess.STARTUPINFO or None: Windows 上返回配置对象，其他系统返回 None
    """
    if platform.system() == "Windows":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        return si
    return None


def open_file_with_system(file_path):
    """使用系统默认程序打开文件

    跨平台实现：
      - Windows: os.startfile
      - macOS: open 命令
      - Linux: xdg-open 命令

    Args:
        file_path: 文件路径

    Raises:
        Exception: 打开失败时抛出异常
    """
    abs_path = os.path.abspath(file_path)
    system = platform.system()
    if system == "Windows":
        try:
            os.startfile(abs_path)
        except OSError as e:
            raise Exception(_("无法打开文件") + f": {e}")
    elif system == "Darwin":
        try:
            subprocess.run(["open", abs_path], check=False, timeout=5)
        except subprocess.TimeoutExpired:
            raise Exception(_("打开文件超时"))
    else:
        try:
            subprocess.run(["xdg-open", abs_path], check=False, timeout=5)
        except subprocess.TimeoutExpired:
            raise Exception(_("打开文件超时"))
        except FileNotFoundError:
            raise Exception(_("当前系统不支持自动打开文件（未找到 xdg-open）"))


def show_file_in_explorer(file_path):
    """在文件管理器中显示文件位置

    跨平台实现：
      - Windows: explorer /select
      - macOS: open -R
      - Linux: xdg-open 目录

    Args:
        file_path: 文件路径
    """
    abs_path = os.path.abspath(file_path)
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.run(['explorer', '/select,', abs_path], check=False, timeout=5)
        elif system == "Darwin":
            subprocess.run(['open', '-R', abs_path], check=False, timeout=5)
        else:
            try:
                directory = os.path.dirname(abs_path)
                subprocess.run(['xdg-open', directory], check=False, timeout=5)
            except FileNotFoundError:
                pass
    except Exception:
        log_exc()
