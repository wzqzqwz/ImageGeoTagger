"""自定义消息框对话框（替代系统 tkinter 消息框）"""

import platform
import tkinter as tk
from tkinter import ttk
from geo_media_tool.utils.i18n import _


class _MessageBoxDialog:
    """通用的应用程序内对话框"""

    def __init__(self, title, message, parent=None, dialog_type='info'):
        self.result = False

        if parent is None or (hasattr(parent, 'winfo_exists') and not parent.winfo_exists()):
            parent = tk._default_root
            if parent is None or (hasattr(parent, 'winfo_exists') and not parent.winfo_exists()):
                try:
                    parent = tk.Tk()
                    parent.withdraw()
                except Exception:
                    import sys
                    print(f"FATAL: {title}: {message}", file=sys.stderr)
                    return
        self.parent = parent

        ww, wh = 320, 150
        try:
            x = parent.winfo_rootx() + parent.winfo_width() // 2 - ww // 2
            y = parent.winfo_rooty() + parent.winfo_height() // 2 - wh // 2
        except Exception:
            x, y = 100, 100

        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.geometry(f"{ww}x{wh}+{max(0,x)}+{max(0,y)}")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.grab_set()
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)

        if platform.system() == "Darwin":
            icon_map = {
                'info': ('i', '#1565C0'),
                'warning': ('!', '#E65100'),
                'error': ('X', '#C62828'),
                'question': ('?', '#1565C0'),
            }
        else:
            icon_map = {
                'info': ('ℹ', '#1565C0'),
                'warning': ('⚠', '#E65100'),
                'error': ('✕', '#C62828'),
                'question': ('?', '#1565C0'),
            }
        icon_char, icon_color = icon_map.get(dialog_type, ('', '#333'))

        main = ttk.Frame(self.window, padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(main)
        top.pack(fill=tk.BOTH, expand=True)

        icon_label = tk.Label(top, text=icon_char, font=('', 28, 'bold'),
                              fg=icon_color, width=2)
        icon_label.pack(side=tk.LEFT, padx=(0, 12))

        msg_frame = ttk.Frame(top)
        msg_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        msg_label = tk.Label(msg_frame, text=message, font=('', 10),
                             anchor=tk.W, justify=tk.LEFT, wraplength=260)
        msg_label.pack(fill=tk.BOTH, expand=True)

        btn_frame = ttk.Frame(main)
        btn_frame.pack(pady=(10, 0))

        if dialog_type == 'question':
            self._question_mode = True
            ttk.Button(btn_frame, text=_("是(Y)"), width=8,
                       command=self._on_yes).pack(side=tk.LEFT, padx=5)
            ttk.Button(btn_frame, text=_("否(N)"), width=8,
                       command=self._on_no).pack(side=tk.LEFT, padx=5)
            self.window.bind('<Return>', lambda e: self._on_yes())
            self.window.bind('<Escape>', lambda e: self._on_no())
        else:
            ttk.Button(btn_frame, text=_("确定"), width=10,
                       command=self._on_close).pack()
            self.window.bind('<Return>', lambda e: self._on_close())
            self.window.bind('<Escape>', lambda e: self._on_close())

        self.window.update_idletasks()
        w = max(300, self.window.winfo_reqwidth())
        h = self.window.winfo_reqheight()
        try:
            x = parent.winfo_rootx() + parent.winfo_width() // 2 - w // 2
            y = parent.winfo_rooty() + parent.winfo_height() // 2 - h // 2
        except Exception:
            pass
        self.window.geometry(f"{w}x{h}+{max(0,x)}+{max(0,y)}")
        self.window.wait_window()

    def _on_yes(self):
        self.result = True
        self.window.destroy()

    def _on_no(self):
        self.result = False
        self.window.destroy()

    def _on_close(self):
        if hasattr(self, '_question_mode') and self._question_mode:
            self.result = False
        self.window.destroy()


class _AskStringDialog:
    """自定义输入对话框"""

    def __init__(self, title, prompt, parent=None, initialvalue=''):
        self.result = None

        if parent is None:
            parent = tk._default_root
        self.parent = parent

        ww, wh = 360, 170
        try:
            x = parent.winfo_rootx() + parent.winfo_width() // 2 - ww // 2
            y = parent.winfo_rooty() + parent.winfo_height() // 2 - wh // 2
        except Exception:
            x, y = 100, 100

        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.geometry(f"{ww}x{wh}+{max(0,x)}+{max(0,y)}")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.grab_set()
        self.window.protocol("WM_DELETE_WINDOW", self._on_cancel)

        main = ttk.Frame(self.window, padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        prompt_label = tk.Label(main, text=prompt, font=('', 10),
                                anchor=tk.W, justify=tk.LEFT, wraplength=360)
        prompt_label.pack(fill=tk.X, pady=(0, 8))

        self.entry_var = tk.StringVar(value=initialvalue)
        self.entry = ttk.Entry(main, textvariable=self.entry_var, font=('', 10))
        self.entry.pack(fill=tk.X, pady=(0, 12))
        self.entry.select_range(0, tk.END)
        self.entry.icursor(tk.END)
        self.entry.focus_set()

        btn_frame = ttk.Frame(main)
        btn_frame.pack(pady=(4, 0))
        ttk.Button(btn_frame, text=_("确定"), width=8,
                   command=self._on_ok).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text=_("取消"), width=8,
                   command=self._on_cancel).pack(side=tk.LEFT, padx=5)

        self.window.bind('<Return>', lambda e: self._on_ok())
        self.window.bind('<Escape>', lambda e: self._on_cancel())

        self.window.wait_window()

    def _on_ok(self):
        self.result = self.entry_var.get()
        self.window.destroy()

    def _on_cancel(self):
        self.result = None
        self.window.destroy()


def askstring(title, prompt, parent=None, initialvalue=''):
    dlg = _AskStringDialog(title, prompt, parent, initialvalue)
    return dlg.result


def showinfo(title, message, parent=None):
    _MessageBoxDialog(title, message, parent, 'info')


def showwarning(title, message, parent=None):
    _MessageBoxDialog(title, message, parent, 'warning')


def showerror(title, message, parent=None):
    _MessageBoxDialog(title, message, parent, 'error')


def askyesno(title, message, parent=None):
    dlg = _MessageBoxDialog(title, message, parent, 'question')
    return dlg.result
