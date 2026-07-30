"""主应用程序窗口（包含标签页导航）"""

import json
import os
import tkinter as tk
from tkinter import ttk
import threading
import traceback

from ui.geo_tab import GeoTab
from ui.date_tab import DateTab
from utils.i18n import _, get_language, load_lang, get_supported_languages, set_language

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'window_config.json')


class MainWindow:
    """主应用程序窗口（带标签页界面）"""

    def __init__(self, root):
        self.root = root
        load_lang()
        self.root.title(_("图像地理位置信息处理工具"))
        self.root.minsize(480, 550)
        self.root.geometry(f"{max(self._calc_tab_width(0), 520)}x600")

        style = ttk.Style(self.root)
        import tkinter.font as _tkfont
        _tab_font_family = _tkfont.nametofont("TkDefaultFont").actual()["family"]
        style.configure("TNotebook.Tab",
                        font=(_tab_font_family, 10, 'bold'), padding=(10, 6))
        style.map("TNotebook.Tab",
                  font=[('selected', (_tab_font_family, 10, 'bold'))])

        self.a = []
        self.b = []
        self.gps_data = []
        self.initial_a_count = 0
        self.initial_b_count = 0
        self.processed_count = 0
        self.updated_count = 0

        self.is_processing = False
        self.current_thread = None
        self.lock = threading.Lock()
        self.edit_windows = []

        self.location_clipboard = {
            'latitude': None, 'longitude': None, 'altitude': None,
            'source_file': None, 'timestamp': None,
        }

        self.create_widgets()
        self._create_menu()
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self._restore_or_center_window()

    def _load_window_geometry(self):
        try:
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
                return cfg.get('geometry')
        except:
            return None

    def _save_window_geometry(self):
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump({'geometry': self.root.geometry()}, f)
        except:
            pass

    def _center_window(self):
        w = max(self._calc_tab_width(0), 520)
        h = 600
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        try:
            import ctypes
            SM_CYCAPTION = 4
            SM_CXFRAME = 32
            SM_CYFRAME = 33
            user32 = ctypes.windll.user32
            title_h = user32.GetSystemMetrics(SM_CYCAPTION)
            border_w = user32.GetSystemMetrics(SM_CXFRAME)
            border_h = user32.GetSystemMetrics(SM_CYFRAME)
        except:
            title_h, border_w, border_h = 31, 8, 8
        total_w = w + 2 * border_w
        total_h = h + title_h + border_h
        x = max(0, (sw - total_w) // 2)
        y = max(0, (sh - total_h) // 2)
        self.root.geometry(f"+{x}+{y}")
        self.root.deiconify()

    def _restore_or_center_window(self):
        geo = self._load_window_geometry()
        if geo:
            try:
                self.root.geometry(geo)
                self.root.deiconify()
                return
            except:
                pass
        self._center_window()

    def _create_menu(self):
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        lang_menu = tk.Menu(menubar, tearoff=0)
        langs = get_supported_languages()
        self._lang_vars = {}
        current_lang = get_language()
        for code, name in sorted(langs.items()):
            self._lang_vars[code] = tk.BooleanVar(value=(code == current_lang))
            lang_menu.add_checkbutton(label=name, variable=self._lang_vars[code],
                                      command=lambda c=code: self._switch_lang(c))
        self._menubar = menubar
        menubar.add_cascade(label=_("Language"), menu=lang_menu)
        self._lang_menu_idx = menubar.index('end')

    def _switch_lang(self, code):
        for c, v in self._lang_vars.items():
            v.set(c == code)
        set_language(code)
        self.root.title(_("图像地理位置信息处理工具"))
        self.notebook.tab(0, text=_("地理位置处理"))
        self.notebook.tab(1, text=_("日期处理"))
        self._menubar.entryconfig(self._lang_menu_idx, label=_("Language"))
        self.geo_tab.rebuild_ui()
        self.date_tab.rebuild_ui()
        idx = self.notebook.index(self.notebook.select())
        cur_h = self.root.winfo_height()
        new_width = max(self._calc_tab_width(idx), 520 if idx == 0 else 750)
        self.root.geometry(f"{new_width}x{cur_h}+{self.root.winfo_x()}+{self.root.winfo_y()}")

    def _on_window_close(self):
        try:
            self._save_window_geometry()
            for win in self.edit_windows:
                try:
                    if win.winfo_exists():
                        win.destroy()
                except Exception:
                    pass
            self.root.destroy()
        except Exception:
            self.root.destroy()

    def create_widgets(self):
        main = ttk.Frame(self.root, padding="00")
        main.pack(fill=tk.BOTH, expand=True)

        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.geo_tab = GeoTab(self.notebook, self)
        self.notebook.add(self.geo_tab.frame, text=_("地理位置处理"))

        self.date_tab = DateTab(self.notebook, self)
        self.notebook.add(self.date_tab.frame, text=_("日期处理"))

        self.notebook.bind('<<NotebookTabChanged>>', self._on_tab_changed)

    def _calc_tab_width(self, idx):
        import tkinter.font as _tkfont
        font = _tkfont.nametofont("TkDefaultFont")
        indicator_w = 25

        if idx == 0:
            label1 = font.measure(_("文件夹路径:"))
            entry1 = 200
            browse1 = font.measure(_("浏览...")) + 16
            row1 = label1 + 10 + entry1 + 10 + browse1

            label2 = font.measure(_("时间差阈值(分钟):"))
            entry2 = 55
            label3 = font.measure(_("(默认30分钟)"))
            chk = font.measure(_("只处理有原始日期的文件")) + indicator_w
            row2 = label2 + 5 + entry2 + 5 + label3 + 20 + chk

            buttons = (font.measure(_("提取图像信息")) + 16 +
                       font.measure(_("处理位置信息")) + 16 +
                       font.measure(_("显示结果")) + 16 +
                       font.measure(_("导出结果")) + 16 + 30)
        else:
            label1 = font.measure(_("文件夹路径:"))
            entry1 = 200
            browse1 = font.measure(_("浏览...")) + 16
            row1 = label1 + 10 + entry1 + 10 + browse1

            label2 = font.measure(_("模式:"))
            rb1 = font.measure(_("更改拍摄日期")) + indicator_w
            rb2 = font.measure(_("重命名文件")) + indicator_w
            row2 = label2 + 5 + rb1 + 10 + rb2

            label3 = font.measure(_("前缀:"))
            entry3 = 60
            date_lbl = font.measure(_("+拍摄日期+")) + 8
            label4 = font.measure(_("后缀:"))
            entry4 = 60
            chk = font.measure(_("文件名有日期跳过")) + indicator_w
            row3 = label3 + entry3 + 5 + date_lbl + 5 + label4 + entry4 + 10 + chk

            chk2 = font.measure(_("试运行模式")) + indicator_w
            row4 = chk2

            buttons = (font.measure(_("扫描文件")) + 16 +
                       font.measure(_("开始更改日期")) + 16 +
                       font.measure(_("清空列表")) + 16 +
                       font.measure(_("导出结果")) + 16 + 30)

        row_widths = [row1, row2, buttons]
        if idx == 1:
            row_widths.extend([row3, row4])
        needed = max(row_widths)
        return needed + 50

    def _on_tab_changed(self, event=None):
        try:
            sel = self.notebook.select()
            if not sel:
                return
            idx = self.notebook.index(sel)
            cur_h = self.root.winfo_height()

            new_width = max(self._calc_tab_width(idx), 520 if idx == 0 else 750)
            self.root.geometry(f"{new_width}x{cur_h}+{self.root.winfo_x()}+{self.root.winfo_y()}")
        except Exception:
            traceback.print_exc()
