"""主入口模块 - 应用程序启动入口

负责初始化 tkinter 根窗口、设置窗口图标、处理窗口关闭事件，
以及捕获启动时的异常并显示错误信息。
"""

import tkinter as tk
from ImageGeoTagger.ui import custom_msgbox as messagebox
import os
import platform
import traceback

# 拖拽支持库，允许用户拖拽文件夹到窗口中
try:
    from tkinterdnd2 import TkinterDnD
    _HAS_DND = True
except ImportError:
    _HAS_DND = False
    TkinterDnD = None
from ImageGeoTagger.ui.main_window import MainWindow
from ImageGeoTagger.utils.platform_utils import hide_console_window, get_app_dir
from ImageGeoTagger.utils.i18n import _, load_lang


def main():
    """应用程序主入口函数"""
    # 隐藏 Windows 控制台窗口（对于打包后的 exe 程序）
    hide_console_window()

    root = None
    try:
        # 创建支持拖拽的 Tk 根窗口
        if _HAS_DND:
            root = TkinterDnD.Tk()
        else:
            root = tk.Tk()

        # 尝试设置窗口图标
        # 先尝试 .ico 格式（Windows 原生），再尝试 .png 格式
        try:
            app_dir = get_app_dir()
            icon_ico = os.path.join(app_dir, 'icon.ico')
            icon_png = os.path.join(app_dir, 'icon.png')
            if platform.system() == "Windows" and os.path.isfile(icon_ico):
                root.iconbitmap(icon_ico)
            if os.path.isfile(icon_png):
                root._icon_photo = tk.PhotoImage(file=icon_png)
                root.iconphoto(True, root._icon_photo)
        except Exception:
            traceback.print_exc()

        app = MainWindow(root)

        root.mainloop()

    except Exception as e:
        # 如果启动失败，显示错误信息
        try:
            load_lang()
            messagebox.showerror(_("启动错误"), _("应用程序启动失败:\n") + str(e), parent=root)
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    main()
