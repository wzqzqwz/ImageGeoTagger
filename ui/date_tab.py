"""日期处理标签页"""

import tkinter as tk
from tkinter import ttk, filedialog
from ui import custom_msgbox as messagebox
import os
import platform
import threading
import time
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.date_processor import (
    MediaDateRenamer, update_file_shooting_date
)
from utils.media_utils import (
    parse_datetime_from_filename, get_existing_datetime
)
from config import ALL_MEDIA_EXTENSIONS
from ui.dialogs import (
    EditShootingDateDialog, CompletionDialog
)
from utils.platform_utils import open_file_with_system, show_file_in_explorer
from utils.recycle_bin import send_to_recycle_bin
from ui.tk_safe import safe_after
from utils.i18n import _
from utils.logging_utils import log_exc
from services.export_service import csv_safe
from models.media_file import FileStatus, status_text, status_sort_key
try:
    from tkinterdnd2 import DND_FILES
except ImportError:
    DND_FILES = None


def _no_clobber_rename(src, dst):
    """原子重命名且不覆盖已存在目标（跨平台）

    Windows 的 os.rename 目标存在时抛 FileExistsError；
    macOS/Linux 的 os.rename 会静默覆盖目标，因此优先用
    os.link（目标已存在时抛 FileExistsError，POSIX 原子）+ os.unlink。
    文件系统不支持硬链接（FAT32/exFAT/网络挂载/EPERM 等）时，
    回退为"预检目标不存在 + os.rename"（存在极窄 TOCTOU 窗口，
    与 Windows 主路径语义一致，可接受）。
    跨分区时 os.link 抛 OSError(EXDEV)，由调用方按失败处理。
    """
    if os.name == 'nt':
        os.rename(src, dst)
    else:
        try:
            os.link(src, dst)
        except FileExistsError:
            raise
        except OSError:
            # 跨分区（EXDEV）/文件系统不支持硬链接等：
            # os.unlink 从未执行，源仍在，按原逻辑回退
            if not os.path.exists(src):
                return
            if os.path.exists(dst):
                raise FileExistsError(f"Destination exists: {dst}")
            os.rename(src, dst)
        else:
            # 硬链接已建立；源删除失败时若不清理目标，
            # 源与目标会同时存在（磁盘双份文件），且调用方
            # 会误以为"目标已存在"继续换编号名重试
            try:
                os.unlink(src)
            except OSError:
                try:
                    os.remove(dst)
                except OSError:
                    pass
                raise


# 内部 Treeview 列标识符（作为列 ID 使用，用户可见表头经 _() 翻译）
_DATE_COL_IDS = ('序号', '文件名', '文件拍摄日期', '文件名日期', '新文件名', '状态')


class DateTab:
    """日期处理标签页"""

    def __init__(self, notebook, app):
        self.app = app
        self.frame = ttk.Frame(notebook, padding="10")

        self.date_renamer = MediaDateRenamer(dry_run=True)
        self.selected_directory = tk.StringVar()
        self.operation_mode = tk.StringVar(value="change_date")
        # 上次真正生效的模式：_on_mode_change 被扫描/处理守卫拦截时
        # 变量已被 tkinter 改写，需还原到该值（见 _on_mode_change 说明）
        self._applied_mode = "change_date"
        self._exporting = False
        self.dry_run = tk.BooleanVar(value=True)
        self.recursive = tk.BooleanVar(value=True)
        self.skip_existing = tk.BooleanVar(value=True)
        self.rename_prefix = tk.StringVar(value="")
        self.rename_suffix = tk.StringVar(value="")
        self.rename_prefix.trace_add('write', self._on_rename_params_change_delayed)
        self.rename_suffix.trace_add('write', self._on_rename_params_change_delayed)
        self.skip_files_with_date = tk.BooleanVar(value=True)
        self.files_to_process = []
        # 预览/刷新重命名目标名时的存在性缓存（每次预览重建，worker 处理期间为 None）
        self._name_exists_cache = None

        self.date_sort_column = None
        self.date_sort_reverse = False

        self.create_interface()

    def rebuild_ui(self):
        self.status_var.set(_("就绪"))
        self.process_btn.config(text=_("开始更改日期"))
        self.scan_btn.config(text=_("扫描文件"))
        self._lbl_folder.config(text=_("文件夹路径:"))
        self._btn_browse.config(text=_("浏览..."))
        self._lbl_mode.config(text=_("模式:"))
        self._rb_change_date.config(text=_("更改拍摄日期"))
        self._rb_rename.config(text=_("重命名文件"))
        self._lbl_prefix.config(text=_("前缀:"))
        self._lbl_date_label.config(text=_("+拍摄日期+"))
        self._lbl_suffix.config(text=_("后缀:"))
        self._chk_skip_date.config(text=_("文件名有日期跳过"))
        self.dry_run_check.config(text=_("试运行模式"))
        self._chk_skip_existing.config(text=_("跳过已有拍摄日期的文件"))
        self._btn_clear.config(text=_("清空列表"))
        self._btn_export.config(text=_("导出结果"))
        self._preview_frame.config(text=_("文件预览"))
        self._log_frame.config(text=_("处理日志"))
        for cid in _DATE_COL_IDS:
            self.date_tree.heading(cid, text=_(cid))
        self._on_mode_change()
        self._show_usage_instructions()

    def create_interface(self):
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(3, weight=1)

        folder_frame = ttk.LabelFrame(
            self.frame, text="", padding="2")
        folder_frame.grid(row=0, column=0, columnspan=3,
                          sticky=(tk.W, tk.E), pady=(1, 2))
        folder_frame.columnconfigure(1, weight=1)

        self._lbl_folder = ttk.Label(folder_frame, text=_("文件夹路径:"))
        self._lbl_folder.grid(
            row=0, column=0, sticky=tk.W, padx=(0, 10))
        entry_frame = ttk.Frame(folder_frame)
        entry_frame.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=(0, 10))
        entry_frame.columnconfigure(0, weight=1)
        self.folder_entry = tk.Entry(entry_frame, textvariable=self.selected_directory,
                  width=40, font=('', 10))
        self.folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        if DND_FILES is not None:
            entry_frame.drop_target_register(DND_FILES)
            entry_frame.dnd_bind('<<Drop>>', self._on_folder_drop)
            self.folder_entry.drop_target_register(DND_FILES)
            self.folder_entry.dnd_bind('<<Drop>>', self._on_folder_drop)
        self._btn_browse = ttk.Button(entry_frame, text=_("浏览..."),
                   command=self.browse_folder)
        self._btn_browse.pack(side=tk.RIGHT, padx=(5, 0))

        options_frame = ttk.Frame(self.frame)
        options_frame.grid(row=1, column=0, columnspan=3,
                           sticky=(tk.W, tk.E), pady=(0, 2))

        mode_frame = ttk.Frame(options_frame)
        mode_frame.grid(row=0, column=0, columnspan=2,
                        sticky=(tk.W, tk.E), pady=(0, 5))
        self._lbl_mode = ttk.Label(mode_frame, text=_("模式:"))
        self._lbl_mode.pack(side=tk.LEFT, padx=(0, 5))
        self._rb_change_date = ttk.Radiobutton(mode_frame, text=_("更改拍摄日期"),
                        variable=self.operation_mode, value="change_date",
                        command=self._on_mode_change)
        self._rb_change_date.pack(side=tk.LEFT, padx=(0, 10))
        self._rb_rename = ttk.Radiobutton(mode_frame, text=_("重命名文件"),
                        variable=self.operation_mode, value="rename_file",
                        command=self._on_mode_change)
        self._rb_rename.pack(side=tk.LEFT)

        self.rename_frame = ttk.Frame(mode_frame)
        self.rename_frame.pack(side=tk.LEFT, padx=(20, 0))
        self._lbl_prefix = ttk.Label(self.rename_frame, text=_("前缀:"))
        self._lbl_prefix.pack(side=tk.LEFT)
        self._prefix_entry = ttk.Entry(self.rename_frame, textvariable=self.rename_prefix,
                  width=8)
        self._prefix_entry.pack(side=tk.LEFT, padx=(0, 5))
        self._lbl_date_label = ttk.Label(self.rename_frame, text=_("+拍摄日期+"),
                               relief='solid', borderwidth=1)
        self._lbl_date_label.pack(side=tk.LEFT, padx=(0, 5))
        self._lbl_suffix = ttk.Label(self.rename_frame, text=_("后缀:"))
        self._lbl_suffix.pack(side=tk.LEFT)
        self._suffix_entry = ttk.Entry(self.rename_frame, textvariable=self.rename_suffix,
                  width=8)
        self._suffix_entry.pack(side=tk.LEFT, padx=(0, 5))
        self._chk_skip_date = ttk.Checkbutton(self.rename_frame, text=_("文件名有日期跳过"),
                        variable=self.skip_files_with_date)
        self._chk_skip_date.pack(side=tk.LEFT, padx=(10, 0))

        opts2_frame = ttk.Frame(options_frame)
        opts2_frame.grid(row=1, column=0, columnspan=2,
                         sticky=(tk.W, tk.E), pady=(0, 5))
        self.dry_run_check = ttk.Checkbutton(opts2_frame, text=_("试运行模式"),
                                              variable=self.dry_run)
        self.dry_run_check.pack(side=tk.LEFT, padx=(0, 10))
        self._chk_skip_existing = ttk.Checkbutton(
            opts2_frame, text=_("跳过已有拍摄日期的文件"),
            variable=self.skip_existing)
        self._chk_skip_existing.pack(side=tk.LEFT)

        btn_frame = ttk.Frame(self.frame)
        btn_frame.grid(row=2, column=0, columnspan=3, pady=(0, 2))

        self.scan_btn = ttk.Button(btn_frame, text=_("扫描文件"),
                                   command=self.scan_files)
        self.scan_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.process_btn = ttk.Button(btn_frame, text=_("开始更改日期"),
                                      command=self.start_processing)
        self.process_btn.pack(side=tk.LEFT, padx=(0, 10))

        self._btn_clear = ttk.Button(btn_frame, text=_("清空列表"),
                   command=self.clear_list)
        self._btn_clear.pack(side=tk.LEFT, padx=(0, 10))
        self._btn_export = ttk.Button(btn_frame, text=_("导出结果"),
                   command=self.export_results)
        self._btn_export.pack(side=tk.LEFT)

        paned = ttk.PanedWindow(self.frame, orient=tk.VERTICAL)
        paned.grid(row=3, column=0, columnspan=3, rowspan=2,
                   sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 1))

        self._preview_frame = ttk.LabelFrame(paned, text=_("文件预览"), padding="2")
        self._preview_frame.columnconfigure(0, weight=1)
        self._preview_frame.rowconfigure(0, weight=1)

        columns = _DATE_COL_IDS
        self.date_tree = ttk.Treeview(self._preview_frame, columns=columns,
                                       show='headings', height=8,
                                       selectmode='extended')

        for cid in _DATE_COL_IDS:
            self.date_tree.heading(cid, text=_(cid),
                                   command=lambda c=cid: self._sort_tree(c))

        self.date_tree.column('序号', width=45, stretch=False, anchor=tk.CENTER)
        self.date_tree.column('文件名', width=130, stretch=True)
        self.date_tree.column('文件拍摄日期', width=140, stretch=False, anchor=tk.CENTER)
        self.date_tree.column('文件名日期', width=130, stretch=False, anchor=tk.CENTER)
        self.date_tree.column('新文件名', width=130, stretch=True)
        self.date_tree.column('状态', width=70, stretch=False, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(self._preview_frame, orient=tk.VERTICAL,
                                  command=self.date_tree.yview)
        self.date_tree.configure(yscrollcommand=scrollbar.set)
        self.date_tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))

        self._bind_tree_events()

        self._log_frame = ttk.LabelFrame(paned, text=_("处理日志"), padding="2")
        self._log_frame.columnconfigure(0, weight=1)
        self._log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(self._log_frame, wrap=tk.WORD, height=6)
        log_scrollbar = ttk.Scrollbar(self._log_frame, orient=tk.VERTICAL,
                                       command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        log_scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))
        self._show_usage_instructions()

        paned.add(self._preview_frame, weight=2)
        paned.add(self._log_frame, weight=1)

        status_frame = ttk.Frame(self.frame)
        status_frame.grid(row=5, column=0, columnspan=3,
                          sticky=(tk.W, tk.E), pady=(0, 1))
        self.status_var = tk.StringVar(value=_("就绪"))
        ttk.Label(status_frame, textvariable=self.status_var).pack(side=tk.LEFT)
        self.progress_var = tk.DoubleVar()
        self.progress = ttk.Progressbar(status_frame, orient=tk.HORIZONTAL,
                                         length=200, mode='determinate',
                                         variable=self.progress_var)
        self.progress.pack(side=tk.RIGHT)

    def _show_usage_instructions(self):
        self.log_text.config(state='normal')
        self.log_text.delete(1.0, tk.END)
        self.log_text.insert(tk.END, _("请先选择文件夹并扫描文件...") + "\n\n")
        self.log_text.insert(tk.END, _("=== 日期处理功能使用说明 ===") + "\n")
        self.log_text.insert(tk.END, _("1. 更改拍摄日期模式：") + "\n")
        self.log_text.insert(tk.END, _("   • 根据文件名中的日期信息重写文件的拍摄日期") + "\n")
        self.log_text.insert(tk.END, _("   • 支持格式：") + "\n")
        self.log_text.insert(tk.END, _("     - YYYY-MM-DD_HH-MM-SS 或 YYYY-MM-DD HH-MM-SS") + "\n")
        self.log_text.insert(tk.END, _("     - YYYYMMDD_HHMMSS 或 YYYYMMDD HHMMSS") + "\n")
        self.log_text.insert(tk.END, _("     - HH-MM-SS_YYYY-MM-DD 或 HH-MM-SS YYYY-MM-DD") + "\n")
        self.log_text.insert(tk.END, _("     - HHMMSS_YYYYMMDD 或 HHMMSS YYYYMMDD") + "\n")
        self.log_text.insert(tk.END, _("     - YYYY-MM-DD 或 YYYYMMDD（仅日期）") + "\n")
        self.log_text.insert(tk.END, _("     - YYYY-MM-DD HH-MM-SS") + "\n")
        self.log_text.insert(tk.END, _("     - YYYYMMDD_HHMMSS、Screenshot_YYYYMMDD_HHMMSS") + "\n")
        self.log_text.insert(tk.END, _("     - YYYYMMDDHHMMSS") + "\n")
        self.log_text.insert(tk.END, _("     - 可有非数字前后缀") + "\n")
        self.log_text.insert(tk.END, _("   • 已手动编辑过拍摄日期的文件会被跳过（显示'已手动编辑'状态）") + "\n\n")
        self.log_text.insert(tk.END, _("2. 重命名文件模式：") + "\n")
        self.log_text.insert(tk.END, _("   • 根据文件现有的拍摄日期重命名文件") + "\n")
        self.log_text.insert(tk.END, _("   • 格式默认：YYYY-MM-DD_HH-MM-SS.扩展名") + "\n")
        self.log_text.insert(tk.END, _("   • 已手动重命名过的文件会被跳过（显示'已手动重命名'状态）") + "\n\n")
        self.log_text.insert(tk.END, _("3. 手动操作功能：") + "\n")
        self.log_text.insert(tk.END, _("   • 右键点击文件可进行：重命名文件、编辑拍摄日期、从序列中删除") + "\n")
        self.log_text.insert(tk.END, _("   • 手动操作后的文件会被标记，不再自动处理") + "\n")
        self.log_text.insert(tk.END, _("   • 所有操作都会在处理日志中记录") + "\n\n")
        self.log_text.insert(tk.END, _("4. 选项说明：") + "\n")
        self.log_text.insert(tk.END, _("   • 文件名有日期跳过：重命名时跳过文件名已包含日期的文件") + "\n")
        self.log_text.insert(tk.END, _("   • 试运行模式：只预览操作结果，不实际修改文件") + "\n\n")
        self.log_text.insert(tk.END, _("5. 支持的文件格式：") + "\n")
        self.log_text.insert(tk.END, _("   • 图像：JPG、JPEG、PNG、TIFF、RAW（NEF、CR2、ARW等）") + "\n")
        self.log_text.insert(tk.END, _("   • 视频：MP4、MOV、AVI、MKV等") + "\n")
        self.log_text.insert(tk.END, _("   • 音频：MP3、WAV、M4A等") + "\n\n")
        self.log_text.insert(tk.END, _("6. 注意事项：") + "\n")
        self.log_text.insert(tk.END, _("   • 建议先使用试运行模式测试操作结果") + "\n")
        self.log_text.insert(tk.END, _("   • 重要文件请提前备份") + "\n\n")
        self.log_text.insert(tk.END, _("========================================") + "\n\n")
        self.log_text.config(state='disabled')

        self._on_mode_change()

    def browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.selected_directory.set(folder)

    def _on_folder_drop(self, event):
        import re as _re
        raw = event.data.strip()
        candidates = _re.findall(r'\{([^}]*)\}', raw)
        if not candidates:
            candidates = _re.findall(r'"([^"]*)"', raw)
        if not candidates:
            candidates = [raw.strip().strip('"\'')]
        for p in candidates:
            p = p.strip().strip('"\'')
            if p.startswith('file://localhost/'):
                p = p[17:]
            elif p.startswith('file:///'):
                p = p[8:]
            elif p.startswith('file:'):
                p = p[5:]
            if os.path.isdir(p):
                self.selected_directory.set(p)
                return

    def _on_mode_change(self):
        # Radiobutton 的 command 回调触发时，变量已被 tkinter 先改写为新值，
        # 无法从变量本身取回旧值：_applied_mode 记录上次真正生效的模式，
        # 被守卫拦截时还原到它，避免 UI 与变量错位、后续处理按错误模式执行
        if getattr(self, '_processing', False):
            # 处理中不允许切换模式，避免主线程改写 fi['status'] 与 worker 并发
            messagebox.showwarning(_("警告"), _("正在处理中，请等待当前任务完成"))
            self.operation_mode.set(self._applied_mode)
            return
        if getattr(self, '_scanning', False):
            # 扫描中同样禁止切换，避免扫描结果状态与当前 UI 模式不一致
            messagebox.showwarning(_("警告"), _("正在扫描中，请等待扫描完成"))
            self.operation_mode.set(self._applied_mode)
            return
        self._applied_mode = self.operation_mode.get()
        mode = self.operation_mode.get()
        if mode == "change_date":
            self.process_btn.config(text=_("开始更改日期"))
            self.rename_frame.pack_forget()
            self.date_tree.heading('文件名日期', text=_('文件名日期'))
            self.date_tree.heading('新文件名', text=' ')
        else:
            self.process_btn.config(text=_("开始重命名"))
            self.rename_frame.pack(side=tk.LEFT, padx=(20, 0))
            self.date_tree.heading('文件名日期', text=' ')
            self.date_tree.heading('新文件名', text=_('新文件名'))
        self._apply_column_visibility()
        self._refilter_files_for_mode()

    def _cached_original_dt(self, fi):
        """解析并缓存 original_date 为 datetime，避免主线程逐文件重复 strptime

        缓存键为 original_date 原始字符串：日期被处理/手动编辑更新后
        字符串变化即自动失效，无需显式失效逻辑。
        """
        cached = fi.get('original_date')
        cache = fi.get('_original_dt_cache')
        if cache is None or cache[0] != cached:
            dt = None
            if cached:
                try:
                    dt = datetime.strptime(cached, '%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError):
                    dt = None
            cache = (cached, dt)
            fi['_original_dt_cache'] = cache
        return cache[1]

    def _refilter_files_for_mode(self):
        mode = self.operation_mode.get()
        for fi in self.files_to_process:
            if not isinstance(fi, dict):
                continue
            # 手动重命名/手动编辑过的文件保持原状态：
            # 重置会丢失 MANUALLY_RENAMED 标记，之后被当成普通待处理文件
            # 反复包含进处理流程，并可能在批量重命名中误改名
            if fi.get('manual_rename') or fi.get('manual_edit_date') or \
                    fi.get('status') in (FileStatus.MANUALLY_RENAMED, FileStatus.MANUALLY_EDITED):
                continue
            path = fi.get('path', '')
            if not path:
                continue
            # 复用扫描时缓存的 original_date，避免主线程逐文件重读 EXIF 卡顿
            cached = fi.get('original_date')
            existing = self._cached_original_dt(fi) if cached else None
            if mode == "change_date":
                if existing is not None and existing != datetime.min:
                    fi['status'] = FileStatus.NO_DATE_NEEDED
                elif fi.get('new_date'):
                    fi['status'] = FileStatus.PENDING_DATE_CHANGE
                else:
                    fi['status'] = FileStatus.PARSE_FAILED
            else:
                if self.skip_files_with_date.get() and parse_datetime_from_filename(str(path)):
                    fi['status'] = FileStatus.SKIPPED
                elif existing is None or existing == datetime.min:
                    fi['status'] = FileStatus.SKIPPED
                else:
                    fi['status'] = FileStatus.PENDING_RENAME
        self._update_preview()

    def _on_rename_params_change(self, *args):
        self._refresh_new_filenames()

    def _on_rename_params_change_delayed(self, *args):
        if hasattr(self, '_rename_after_id'):
            try:
                self.app.root.after_cancel(self._rename_after_id)
            except Exception:
                log_exc()
        # safe_after：主窗口已销毁时静默跳过，避免回调在销毁的 Treeview
        # 上执行抛 TclError（_refresh_new_filenames 内部无 winfo_exists 兜底）
        self._rename_after_id = safe_after(
            self.app.root, 500, self._on_rename_params_change)

    def _apply_column_visibility(self):
        mode = self.operation_mode.get()
        if mode == "change_date":
            self.date_tree.column('文件名日期', width=130, minwidth=100)
            self.date_tree.column('新文件名', width=0, minwidth=0)
        else:
            self.date_tree.column('新文件名', width=200, minwidth=100)
            self.date_tree.column('文件名日期', width=0, minwidth=0)

    def _bind_tree_events(self):
        def on_double_click(event):
            sel = self.date_tree.selection()
            if not sel:
                return
            values = self.date_tree.item(sel[0], 'values')
            if not values or len(values) < 7:
                return
            file_path = values[6]
            file_info = None
            for f in self.files_to_process:
                fp = str(f.get('path', ''))
                if fp == file_path:
                    file_info = f
                    break
            if not file_info:
                return
            if self.operation_mode.get() == "change_date":
                EditShootingDateDialog(
                    self.app, file_info, self.date_tree, sel[0],
                    self.app.root, self, is_date_tab=True)
            else:
                self._rename_single(file_info, sel[0])

        self.date_tree.bind('<Double-1>', on_double_click)

        if platform.system() == "Darwin":
            self.date_tree.bind("<Button-2>", self._show_context_menu)
            self.date_tree.bind("<Control-Button-1>", self._show_context_menu)
            self.date_tree.bind("<Button-3>", self._show_context_menu)
        else:
            self.date_tree.bind("<Button-3>", self._show_context_menu)

    def _show_context_menu(self, event):
        item_id = self.date_tree.identify_row(event.y)
        if not item_id:
            return

        sel = self.date_tree.selection()
        if item_id not in sel:
            self.date_tree.selection_set(item_id)
            sel = [item_id]

        menu = tk.Menu(self.date_tree, tearoff=0)

        if len(sel) == 1:
            values = self.date_tree.item(item_id, 'values')
            if values and len(values) >= 7:
                file_path = values[6]
                file_info = None
                for f in self.files_to_process:
                    fp = str(f.get('path', ''))
                    if fp == file_path:
                        file_info = f
                        break

                menu.add_command(label=_("打开"),
                                 command=lambda: open_file_with_system(file_path))
                menu.add_command(
                    label=_("重命名文件"),
                    command=lambda: self._rename_single(file_info, item_id))
                menu.add_command(
                    label=_("编辑拍摄日期"),
                    command=lambda: EditShootingDateDialog(
                        self.app, file_info, self.date_tree, item_id,
                        self.app.root, self, is_date_tab=True))
                menu.add_command(
                    label=_("显示文件位置"),
                    command=lambda: show_file_in_explorer(file_path))
                menu.add_separator()
                menu.add_command(
                    label=_("从序列中删除"),
                    command=lambda: self._remove_items(sel))
                menu.add_command(
                    label=_("从磁盘中删除"),
                    command=lambda: self._delete_items(sel))
        else:
            menu.add_command(label=_("从序列中删除"),
                             command=lambda: self._remove_items(sel))
            menu.add_command(label=_("从磁盘中删除"),
                             command=lambda: self._delete_items(sel))
            menu.add_separator()
            path_to_file = {}
            for f in self.files_to_process:
                p = str(f.get('path', ''))
                path_to_file[p] = f
            menu.add_command(
                label=_("批量重命名文件"),
                command=lambda sel=sel, ptf=path_to_file: self._batch_rename_items(
                    sel, ptf))
            menu.add_command(
                label=_("批量修改拍摄日期"),
                command=lambda sel=sel, ptf=path_to_file: self._batch_set_dates_from_filename(
                    sel, ptf))

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _remove_items(self, selected_items):
        if getattr(self, '_processing', False):
            messagebox.showwarning(_("警告"), _("正在处理中，请等待当前任务完成"))
            return
        if getattr(self, '_scanning', False):
            messagebox.showwarning(_("警告"), _("正在扫描中，请等待扫描完成"))
            return
        count = len(selected_items)
        if not messagebox.askyesno(_("确认删除"),
                                    _("确定要从处理序列中删除这 ") + str(count) + _(" 个文件吗？")):
            return

        items_to_remove = []
        for item_id in selected_items:
            values = self.date_tree.item(item_id, 'values')
            if values and len(values) >= 7:
                items_to_remove.append(values[6])

        # 路径映射查找，避免对每个选中行线性扫描 files_to_process（O(sel×n)）
        remove_paths = set(items_to_remove)
        self.files_to_process[:] = [
            f for f in self.files_to_process if str(f.get('path', '')) not in remove_paths]

        for item_id in selected_items:
            try:
                self.date_tree.delete(item_id)
            except Exception:
                log_exc()

        self._update_preview()
        self.status_var.set(_("就绪 - 共 ") + str(len(self.files_to_process)) + _(" 个文件"))
        self._append_log(_("已从序列中删除 ") + str(count) + _(" 个文件\n"))
        self._pulse_progress()

    def _delete_items(self, selected_items):
        if getattr(self, '_processing', False):
            messagebox.showwarning(_("警告"), _("正在处理中，请等待当前任务完成"))
            return
        if getattr(self, '_scanning', False):
            messagebox.showwarning(_("警告"), _("正在扫描中，请等待扫描完成"))
            return
        count = len(selected_items)
        if count > 1:
            msg = _("确定要将这 ") + str(count) + _(" 个文件移至回收站吗？")
        else:
            msg = _("确定要将这个文件移至回收站吗？")
        if not messagebox.askyesno(_("确认移至回收站"), msg):
            return

        # 全局互斥：防止与后台 GEO 写盘/处理并发时移动文件，避免写入截断损坏
        if not self.app.acquire_processing():
            messagebox.showwarning(_("警告"), _("其他任务正在处理中，请等待完成"), parent=self.app.root)
            return
        # 主线程解析路径（Tk 调用不能在 worker 线程执行）；
        # 去重：同一路径被选中多次时只删除一次，避免重复删除误报失败
        seen = set()
        paths = []
        for item_id in selected_items:
            values = self.date_tree.item(item_id, 'values')
            if values and len(values) >= 7 and values[6] not in seen:
                seen.add(values[6])
                paths.append(values[6])

        def worker():
            # 顶层兜底：回收站调用异常时线程不能静默死亡，
            # 否则 release_processing 永不执行、全局互斥锁被永久占用
            try:
                success, failed = send_to_recycle_bin(paths)
            except Exception as e:
                log_exc()
                # 兜底必须保存全路径：_apply_delete_results 用全路径
                # 与列表项匹配，存 basename 会导致"文件未删但列表全移除"
                success, failed = 0, [(p, str(e)) for p in paths]
            self.app.post_to_ui(
                lambda s=success, f=failed, p=paths: self._apply_delete_results(s, f, p))

        thread = threading.Thread(target=worker, daemon=True)
        self.app.register_thread(thread)
        thread.start()

    def _apply_delete_results(self, success, failed, paths):
        try:
            failed_paths = set(p for p, _ in failed) if failed else set()
            # O(n) 重建列表：避免对每个选中行线性扫描 files_to_process（O(sel×n)）
            removed_paths = set(paths) - failed_paths
            self.files_to_process[:] = [
                f for f in self.files_to_process
                if str(f.get('path', '')) not in removed_paths]

            for item_id in list(self.date_tree.get_children()):
                values = self.date_tree.item(item_id, 'values')
                if values and len(values) >= 7 and values[6] in removed_paths:
                    try:
                        self.date_tree.delete(item_id)
                    except Exception:
                        log_exc()

            self._update_preview()
            self.status_var.set(_("已将 ") + str(success) + _(" 个文件移至回收站"))
            self._append_log(_("已将 ") + str(success) + _(" 个文件移至回收站\n"))
            if failed:
                for name, err in failed:
                    self._append_log(_("失败: ") + name + " - " + err + "\n")
            self._pulse_progress()
        except Exception:
            log_exc()
        finally:
            self.app.release_processing()

    def _rename_single(self, file_info, item_id):
        if getattr(self, '_processing', False):
            messagebox.showwarning(_("警告"), _("正在处理中，请等待当前任务完成"))
            return
        if getattr(self, '_scanning', False):
            # 扫描中列表即将被替换：对旧快照改名会白做并让用户误以为成功
            messagebox.showwarning(_("警告"), _("正在扫描中，请等待扫描完成"))
            return
        if not file_info:
            return
        # 全局互斥：防止与地理页后台 GPS 写盘等并发写同一文件导致损坏
        if not self.app.acquire_processing():
            messagebox.showwarning(_("警告"), _("其他任务正在处理中，请等待完成"))
            return
        try:
            self._rename_single_locked(file_info, item_id)
        finally:
            self.app.release_processing()

    def _rename_single_locked(self, file_info, item_id):
        current = file_info.get('filename', '')
        file_path = file_info.get('path', '')
        new_name = messagebox.askstring(_("重命名文件"),
                                         _("请输入新的文件名（当前：") + current + _("）:"),
                                         initialvalue=current,
                                         parent=self.app.root)
        if not new_name or new_name == current:
            return

        if new_name != os.path.basename(new_name):
            messagebox.showerror(_("错误"), _("文件名不能包含路径分隔符"))
            return

        import re
        if platform.system() == 'Windows':
            if not re.match(r'^[^<>:"/\\|?*]+$', new_name):
                messagebox.showerror(_("错误"), _("文件名包含非法字符"))
                return
        elif platform.system() == 'Darwin':
            if ':' in new_name or '/' in new_name:
                messagebox.showerror(_("错误"), _("文件名包含非法字符"))
                return
        else:
            if '/' in new_name:
                messagebox.showerror(_("错误"), _("文件名包含非法字符"))
                return

        new_path = os.path.join(os.path.dirname(str(file_path)), new_name)

        # 目标文件存在性检查：跨平台防止 os.rename 静默覆盖已有文件
        if os.path.exists(new_path):
            messagebox.showerror(_("错误"), _("目标文件已存在: ") + new_name)
            return

        try:
            _no_clobber_rename(str(file_path), new_path)
        except FileExistsError:
            messagebox.showerror(_("错误"), _("目标文件已存在: ") + new_name)
            return
        except OSError as e:
            messagebox.showerror(_("错误"), _("重命名失败: ") + str(e))
            return
        file_info['path'] = new_path
        file_info['filename'] = new_name
        file_info['status'] = FileStatus.MANUALLY_RENAMED
        file_info['manual_rename'] = True
        self._append_log(_("已重命名") + ": " + os.path.basename(file_path) + " → " + new_name + "\n")
        self._update_preview()

    def _batch_rename_items(self, selected_items, path_to_file):
        if getattr(self, '_processing', False):
            messagebox.showwarning(_("警告"), _("正在处理中，请等待当前任务完成"))
            return
        if getattr(self, '_scanning', False):
            messagebox.showwarning(_("警告"), _("正在扫描中，请等待扫描完成"))
            return
        to_rename = []
        for item_id in selected_items:
            vals = self.date_tree.item(item_id, 'values')
            if len(vals) < 7:
                continue
            file_path = vals[6]
            if file_path not in path_to_file:
                continue
            fi = path_to_file.get(file_path)
            if fi is None:
                continue
            if fi.get('status') != FileStatus.PENDING_RENAME:
                continue
            # 不能用树列显示文本当目标名：_gen_new_name 返回 None 时
            # 该列显示的是本地化占位符（"已手动重命名"/"与原文件名相同"），
            # 直接使用会把文件改成垃圾名。这里重新计算真实目标名。
            existing = None
            cached = fi.get('original_date')
            if cached:
                try:
                    existing = datetime.strptime(cached, '%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError):
                    existing = None
            new_fn = self._gen_new_name(file_path, existing, fi)
            if not new_fn:
                continue
            to_rename.append((fi, file_path, new_fn))
        if not to_rename:
            messagebox.showinfo(_("批量重命名"), _("选中的文件中没有需要重命名的项"), parent=self.app.root)
            return
        count = len(to_rename)
        if not messagebox.askyesno(
                _("确认批量重命名"),
                _("确定要对 ") + str(count) + _(" 个文件执行重命名操作吗？"),
                parent=self.app.root):
            return
        # 全局互斥：防止与地理页处理等其它任务同时写文件
        if not self.app.acquire_processing():
            messagebox.showwarning(_("警告"), _("其他任务正在处理中，请等待完成"))
            return
        self._processing = True
        self._set_ui_processing_state(True)

        def worker():
            try:
                renamed = 0
                total_rename = len(to_rename)
                used_targets = set()
                self.app.post_to_ui(lambda: self.progress_var.set(0))
                self.app.post_to_ui(lambda t=total_rename: self.status_var.set(_("正在重命名... 0/") + str(t)))
                for i, (fi, fp, new_fn) in enumerate(to_rename):
                    base_dir = os.path.dirname(fp)
                    orig_stem, ext = os.path.splitext(new_fn)
                    target = os.path.join(base_dir, new_fn)
                    counter = 1
                    # 同时检查批内已用目标和磁盘上真实存在的文件，避免覆盖
                    while (target in used_targets or os.path.exists(target)) and counter < 10000:
                        candidate = f"{orig_stem}_{counter:03d}{ext}"
                        target = os.path.join(base_dir, candidate)
                        counter += 1
                    if counter >= 10000:
                        self.app.post_to_ui(lambda n=os.path.basename(fp), nf=new_fn: self._append_log(
                            _("重命名失败（无法生成唯一文件名）") + ": " + n + " → " + nf + "\n"))
                        continue
                    actual_new_fn = os.path.basename(target)
                    try:
                        _no_clobber_rename(fp, target)
                        self.app.post_to_ui(lambda n=os.path.basename(fp), an=actual_new_fn: self._append_log(
                            _("已重命名") + ": " + n + " → " + an + "\n"))
                        # 磁盘操作完成后统一更新内存状态：若先把 status 置为
                        # MANUALLY_RENAMED，post_to_ui 回调可能立即在主线程触发
                        # _update_preview，读到"已标记但 path/filename 未更新"
                        # 的中间态
                        fi['path'] = target
                        fi['filename'] = actual_new_fn
                        fi['status'] = FileStatus.MANUALLY_RENAMED
                        fi['manual_rename'] = True
                        used_targets.add(target)
                        if actual_new_fn != new_fn:
                            self.app.post_to_ui(lambda an=actual_new_fn, o=new_fn: self._append_log(
                                "  （" + _("原目标") + " " + o + " " + _("已存在，自动使用") + " " + an + "）\n"))
                        renamed += 1
                    except FileExistsError:
                        self.app.post_to_ui(lambda n=os.path.basename(fp), nf=new_fn: self._append_log(
                            _("重命名失败（目标文件已存在）") + ": " + n + " → " + nf + "\n"))
                    except Exception as e:
                        self.app.post_to_ui(lambda n=os.path.basename(fp), e=str(e): self._append_log(
                            _("重命名失败") + ": " + n + " → " + e + "\n"))
                    done = i + 1
                    self.app.post_to_ui(lambda d=done, t=total_rename: (
                        self.progress_var.set(d / t * 100),
                        self.status_var.set(_("正在重命名... ") + str(d) + "/" + str(t))))
                failed = total_rename - renamed
                self.app.post_to_ui(self._update_preview)
                self.app.post_to_ui(lambda r=renamed: self.status_var.set(
                    _("批量重命名完成，共重命名 ") + str(r) + _(" 个文件")))
                if renamed:
                    msg = _("批量重命名完成\n成功: ") + str(renamed)
                    if failed:
                        msg += _("\n失败: ") + str(failed)
                    self.app.post_to_ui(lambda m=msg: messagebox.showinfo(
                        _("批量重命名"), m, parent=self.app.root))
            finally:
                self.app.post_to_ui(lambda: setattr(self, '_processing', False))
                self.app.post_to_ui(lambda: self._set_ui_processing_state(False))
                self.app.post_to_ui(self.app.release_processing)

        thread = threading.Thread(target=worker, daemon=True)
        self.app.register_thread(thread)
        thread.start()

    def scan_files(self):
        folder = self.selected_directory.get()
        if not folder:
            messagebox.showwarning(_("警告"), _("请先选择文件夹"))
            return
        if getattr(self, '_scanning', False):
            return
        if getattr(self, '_processing', False):
            messagebox.showwarning(_("警告"), _("正在处理中，请等待当前任务完成"))
            return
        # 全局互斥：地理页等其它任务进行中时禁止启动
        if not self.app.acquire_processing():
            messagebox.showwarning(_("警告"), _("其他任务正在处理中，请等待完成"))
            return

        self._scanning = True
        # 扫描期间禁用模式单选/处理/扫描等控件：扫描线程已快照模式值，
        # 用户此时改模式会被 _on_mode_change 拦截（变量还原由该函数兜底），
        # 直接禁用从根源上避免 UI 与扫描数据模式错位
        self._set_ui_processing_state(True)
        scan_mode = self.operation_mode.get()
        scan_skip_with_date = self.skip_files_with_date.get()
        thread = threading.Thread(
            target=self._scan_thread, args=(folder, scan_mode, scan_skip_with_date))
        thread.daemon = True
        self.app.register_thread(thread)
        thread.start()

    def _scan_thread(self, folder, scan_mode, scan_skip_with_date):
        try:
            self.app.post_to_ui(lambda: self.status_var.set(_("正在扫描文件...")))
            self.app.post_to_ui(lambda: self.progress_var.set(0))

            # 第一阶段：先完整收集文件列表，确定总文件数，
            # 避免边遍历边处理时总数未定导致进度条来回跳动
            all_files = []
            # onerror 跳过无法访问的目录，避免一个无权限子目录中断整个扫描
            def _on_walk_error(exc):
                pass
            for r, _dirs, fs in os.walk(folder, onerror=_on_walk_error):
                for f in fs:
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in ALL_MEDIA_EXTENSIONS:
                        continue
                    all_files.append(os.path.join(r, f))
            total = len(all_files)

            results = []
            with ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 4) * 2)) as pool:
                # 分块提交任务，避免一次性持有全部路径/任务对象导致内存峰值
                pending = {}
                count = 0

                def _process_batch():
                    nonlocal count
                    for fut in as_completed(pending):
                        pending.pop(fut, None)
                        try:
                            results.append(fut.result())
                        except Exception:
                            log_exc()
                        count += 1
                        if count % 10 == 0 or count == total:
                            pct = count / total * 100 if total else 100
                            self.app.post_to_ui(lambda p=pct: self.progress_var.set(p))
                            self.app.post_to_ui(
                                lambda c=count, t=total: self.status_var.set(
                                    _("正在扫描文件... ") + str(c) + "/" + str(t)))

                def process_one(fp, mode=scan_mode, skip_with_date=scan_skip_with_date):
                    p = Path(fp)
                    parsed = parse_datetime_from_filename(p.name)
                    # change_date 模式只依据 EXIF/QuickTime 日期判断，
                    # 避免文件系统创建/修改时间兜底把无 EXIF 日期的文件误标为已有日期
                    existing = get_existing_datetime(
                        p, fallback_to_fs=(mode != "change_date"))
                    if mode == "change_date":
                        if existing and existing != datetime.min:
                            status = FileStatus.NO_DATE_NEEDED
                        elif parsed:
                            status = FileStatus.PENDING_DATE_CHANGE
                        else:
                            status = FileStatus.PARSE_FAILED
                    else:
                        if not existing or existing == datetime.min:
                            status = FileStatus.SKIPPED
                        elif skip_with_date and parsed:
                            status = FileStatus.SKIPPED
                        else:
                            status = FileStatus.PENDING_RENAME

                    return {
                        'path': str(p),
                        'filename': p.name,
                        'original_date': existing.strftime('%Y-%m-%d %H:%M:%S') if existing and existing != datetime.min else None,
                        'new_date': parsed.strftime('%Y-%m-%d %H:%M:%S') if parsed else None,
                        'status': status,
                    }

                for fp in all_files:
                    pending[pool.submit(process_one, fp)] = fp
                    if len(pending) >= 500:
                        _process_batch()
                if pending:
                    _process_batch()

            self.app.post_to_ui(lambda r=results: setattr(self, 'files_to_process', r))
            self.app.post_to_ui(self._update_preview)
            self.app.post_to_ui(
                lambda r=results: self.status_var.set(
                    _("扫描完成，找到 ") + str(len(r)) + _(" 个文件")))
            self.app.post_to_ui(lambda: self.progress_var.set(100))
            self.app.post_to_ui(lambda r=results: self._append_log(
                _("扫描完成，共找到 ") + str(len(r)) + _(" 个文件\n")))
        except Exception:
            log_exc()
            self.app.post_to_ui(
                lambda: self.status_var.set(_("扫描失败")))
            self.app.post_to_ui(
                lambda: self._append_log(_("扫描失败\n")))
        finally:
            self.app.post_to_ui(lambda: setattr(self, '_scanning', False))
            self.app.post_to_ui(lambda: self._set_ui_processing_state(False))
            self.app.post_to_ui(self.app.release_processing)

    def _update_preview(self):
        # 批量删除所有行，避免每个 item 一次 Tcl 往返
        children = self.date_tree.get_children()
        if children:
            self.date_tree.delete(*children)

        # 本预览周期内复用候选目标名的存在性判断，避免主线程逐行重复 stat
        self._name_exists_cache = {}
        no_date = '9999-12-31 23:59:59'
        sort_col = getattr(self, 'date_sort_column', None)
        if sort_col == '文件名':
            sorted_files = sorted(
                self.files_to_process,
                key=lambda x: str(x.get('filename', '')).lower(),
                reverse=self.date_sort_reverse)
        elif sort_col == '状态':
            sorted_files = sorted(
                self.files_to_process,
                key=lambda x: status_sort_key(x.get('status')),
                reverse=self.date_sort_reverse)
        else:
            # 默认按拍摄日期升序；用户点了表头排序后保持其列与方向，
            # 不再被预览刷新无条件打回日期序
            key = (lambda x: x.get('original_date') if x.get('original_date') is not None else no_date)
            if sort_col == '文件拍摄日期':
                sorted_files = sorted(self.files_to_process, key=key,
                                      reverse=self.date_sort_reverse)
            else:
                sorted_files = sorted(self.files_to_process, key=key)

        rename_mode = self.operation_mode.get() == "rename_file"
        for i, fi in enumerate(sorted_files, start=1):
            filename = fi.get('filename', '')
            orig_date = fi.get('original_date') or _('无')
            new_date = fi.get('new_date') if fi.get('new_date') else _('无法解析')
            st = fi.get('status')
            file_path = fi.get('path', '')
            new_filename = ''

            if rename_mode:
                # 复用扫描时缓存的 original_date，避免主线程逐文件重读 EXIF 卡顿
                cached = fi.get('original_date')
                existing = self._cached_original_dt(fi) if cached else None
                if st == FileStatus.SKIPPED:
                    if not existing or existing == datetime.min:
                        new_filename = _('无拍摄日期')
                    else:
                        new_filename = _('原文件名有日期')
                else:
                    if existing and existing != datetime.min:
                        new_filename = self._gen_new_name(file_path, existing, fi,
                                                          exists_cache=self._name_exists_cache)
                        if new_filename is None:
                            if fi.get('manual_rename'):
                                new_filename = _('已手动重命名')
                            else:
                                new_filename = _('与原文件名相同')
                    else:
                        new_filename = _('无拍摄日期')

            display_status = status_text(st, fi.get('status_detail', ''))
            if rename_mode:
                item_id = self.date_tree.insert('', tk.END, values=(
                    str(i), filename, orig_date, '', new_filename, display_status, str(fi.get('path', ''))))
            else:
                item_id = self.date_tree.insert('', tk.END, values=(
                    str(i), filename, orig_date, new_date, '', display_status, str(fi.get('path', ''))))
            fi['_tree_id'] = item_id

    def _update_preview_row(self, fi):
        item_id = fi.get('_tree_id')
        if not item_id or not self.date_tree.exists(item_id):
            return
        filename = fi.get('filename', '')
        orig_date = fi.get('original_date') or _('无')
        new_date = fi.get('new_date') if fi.get('new_date') else _('无法解析')
        st = fi.get('status')
        file_path = str(fi.get('path', ''))
        display_status = status_text(st, fi.get('status_detail', ''))
        if self.operation_mode.get() == "rename_file":
            new_fn = fi.get('_new_filename_display', '')
            self.date_tree.item(item_id, values=(
                '', filename, orig_date, '', new_fn, display_status, file_path))
        else:
            self.date_tree.item(item_id, values=(
                '', filename, orig_date, new_date, '', display_status, file_path))
        seq = self.date_tree.index(item_id) + 1
        values = list(self.date_tree.item(item_id, 'values'))
        values[0] = seq
        self.date_tree.item(item_id, values=tuple(values))

    def _refresh_new_filenames(self):
        if self.operation_mode.get() != "rename_file":
            return
        # 处理期间 worker 对磁盘进行重命名，此时重算预览既卡主线程又与 worker 快照不一致
        if getattr(self, '_processing', False):
            return
        # 扫描进行中 files_to_process 尚未就绪，跳过避免重复工作
        if getattr(self, '_scanning', False) or not self.files_to_process:
            return
        # 本刷新周期内复用候选目标名的存在性判断
        self._name_exists_cache = {}
        for fi in self.files_to_process:
            item_id = fi.get('_tree_id')
            if not item_id or not self.date_tree.exists(item_id):
                continue
            st = fi.get('status')
            no_date = fi.get('original_date') is None
            if st == FileStatus.SKIPPED:
                new_fn = _('原文件名有日期') if not no_date else _('无拍摄日期')
            elif no_date:
                new_fn = _('无拍摄日期')
            else:
                dt = self._cached_original_dt(fi)
                if dt is None:
                    new_fn = _('无拍摄日期')
                else:
                    fp = fi.get('path', '')
                    new_name = self._gen_new_name(fp, dt, fi,
                                                  exists_cache=self._name_exists_cache)
                    if new_name is None:
                        new_fn = _('已手动重命名') if fi.get('manual_rename') else _('与原文件名相同')
                    else:
                        new_fn = new_name
            values = list(self.date_tree.item(item_id, 'values'))
            if len(values) >= 6:
                values[4] = new_fn
                self.date_tree.item(item_id, values=tuple(values))

    def _gen_new_name(self, original_path, date_to_use, file_info=None,
                       prefix=None, suffix=None, exists_cache=None):
        if file_info and file_info.get('manual_rename'):
            return None
        ext = os.path.splitext(original_path)[1]
        orig_name = os.path.basename(original_path)
        base_dir = os.path.dirname(original_path)

        date_str = date_to_use.strftime('%Y-%m-%d_%H-%M-%S')
        if prefix is None:
            prefix = self.rename_prefix.get()
        if suffix is None:
            suffix = self.rename_suffix.get()
        base_new_name = f"{prefix}{date_str}{suffix}{ext}"
        if base_new_name != os.path.basename(base_new_name):
            return None

        if base_new_name == orig_name:
            return None

        if not self._path_exists(os.path.join(base_dir, base_new_name), exists_cache):
            return base_new_name

        counter = 1
        while counter < 10000:
            candidate = f"{prefix}{date_str}{suffix}_{counter:03d}{ext}"
            if candidate == orig_name:
                return None
            if not self._path_exists(os.path.join(base_dir, candidate), exists_cache):
                return candidate
            counter += 1
        return None

    def _path_exists(self, path, cache):
        """带可选缓存的 os.path.exists：预览路径复用同一次刷新的判断结果，
        避免主线程对同一目标名重复 stat（worker 处理路径传入 None 保持实时判断）。"""
        if cache is None:
            return os.path.exists(path)
        if path in cache:
            return cache[path]
        exists = os.path.exists(path)
        cache[path] = exists
        return exists

    def _sort_tree(self, column):
        if column == '序号':
            return
        items = list(self.date_tree.get_children(''))
        if not items:
            return
        if self.date_sort_column == column:
            self.date_sort_reverse = not self.date_sort_reverse
        else:
            self.date_sort_reverse = False
            self.date_sort_column = column

        col_idx = _DATE_COL_IDS.index(column)

        # 预构建 _tree_id -> fi 映射（避免状态列对每个树行线性遍历 files_to_process O(n²)）
        tid_map = {fi.get('_tree_id'): fi for fi in self.files_to_process if fi.get('_tree_id')}

        # 每行排序键与显示值一次性预计算，避免 sort_key 中反复调用 tree.item()/strptime。
        # 'YYYY-MM-DD HH:MM:SS' 字符串可按字典序直接比较，无需解析为 datetime。
        no_date = '9999-12-31 23:59:59'
        key_map = {}
        values_map = {}
        selected = set(self.date_tree.selection())
        selected_paths = set()
        for item in items:
            values = self.date_tree.item(item, 'values')
            values_map[item] = values
            if item in selected and values and len(values) >= 7:
                selected_paths.add(values[6])
            if column == '状态':
                fi = tid_map.get(item)
                key_map[item] = status_sort_key(fi.get('status')) if fi is not None else 999
            elif column == '文件拍摄日期':
                fi = tid_map.get(item)
                raw = fi.get('original_date') if fi is not None else None
                key_map[item] = raw if raw else no_date
            else:
                val = values[col_idx] if values and len(values) > col_idx else ""
                key_map[item] = val.lower() if isinstance(val, str) else str(val)

        items.sort(key=lambda it: key_map[it], reverse=self.date_sort_reverse)

        # 重建树而不是逐行 move：move 对每行一次 Tcl 往返，大列表时极慢。
        # 一次性删除全部行，再按排序结果顺序重建（序号在插入时直接写入，
        # 省掉原来第二遍读值改序号的遍历）。重建后 item id 全部变化，
        # 需同步更新各 fi['_tree_id']，保证双击/右键/增量刷新仍能定位。
        try:
            yview_frac = self.date_tree.yview()[0]
        except Exception:
            yview_frac = 0.0
        self.date_tree.delete(*items)
        new_ids = {}
        for i, item in enumerate(items):
            values = list(values_map.get(item) or [])
            if len(values) < 7:
                values = [str(i + 1), '', '', '', '', '', '']
            else:
                values[0] = i + 1
            new_id = self.date_tree.insert('', tk.END, values=tuple(values))
            new_ids[item] = new_id
            if values[6] in selected_paths:
                self.date_tree.selection_add(new_id)
        for old_id, new_id in new_ids.items():
            fi = tid_map.get(old_id)
            if fi is not None:
                fi['_tree_id'] = new_id
        try:
            self.date_tree.yview_moveto(yview_frac)
        except Exception:
            pass
        self._update_sort_indicators()

    def _update_sort_indicators(self):
        for cid in _DATE_COL_IDS:
            display = _(cid)
            if cid == self.date_sort_column:
                ind = " ↓" if self.date_sort_reverse else " ↑"
                self.date_tree.heading(cid, text=display + ind)
            else:
                self.date_tree.heading(cid, text=display)

    def start_processing(self):
        if getattr(self, '_processing', False):
            messagebox.showwarning(_("警告"), _("正在处理中，请等待当前任务完成"))
            return
        if getattr(self, '_scanning', False):
            messagebox.showwarning(_("警告"), _("正在扫描中，请等待扫描完成"))
            return
        if not self.files_to_process:
            messagebox.showwarning(_("警告"), _("请先扫描文件"))
            return
        # 全局互斥：地理页等其它任务进行中时禁止启动
        if not self.app.acquire_processing():
            messagebox.showwarning(_("警告"), _("其他任务正在处理中，请等待完成"))
            return
        self._processing = True
        dry_run = self.dry_run.get()
        operation_mode = self.operation_mode.get()
        skip_existing = self.skip_existing.get()
        rename_prefix_val = self.rename_prefix.get()
        rename_suffix_val = self.rename_suffix.get()
        self.date_renamer.dry_run = dry_run
        thread = threading.Thread(
            target=self._process_thread,
            args=(dry_run, operation_mode, skip_existing,
                  rename_prefix_val, rename_suffix_val))
        thread.daemon = True
        self.app.register_thread(thread)
        thread.start()

    def _set_ui_processing_state(self, disabled):
        state = "disabled" if disabled else "normal"
        self.app.post_to_ui(lambda: self.process_btn.config(state=state))
        self.app.post_to_ui(lambda: self.scan_btn.config(state=state))
        self.app.post_to_ui(lambda: self.dry_run_check.config(state=state))
        # 处理期间禁用模式单选按钮，防止 _refilter_files_for_mode 与 worker 并发改写状态
        self.app.post_to_ui(lambda: self._rb_change_date.config(state=state))
        self.app.post_to_ui(lambda: self._rb_rename.config(state=state))
        # 处理期间禁用重命名前缀/后缀输入框：worker 已快照其值，
        # 改动会导致预览与实际结果不一致，并在主线程触发全表重算
        for _entry in (getattr(self, '_prefix_entry', None),
                       getattr(self, '_suffix_entry', None)):
            if _entry is not None:
                self.app.post_to_ui(lambda e=_entry: e.config(state=state))

    def _get_eligible_files(self, operation_mode, dry_run):
        files_copy = list(self.files_to_process)
        to_process = []
        for fi in files_copy:
            st = fi.get('status')
            if st in (FileStatus.NO_DATE_NEEDED, FileStatus.NO_RENAME_NEEDED,
                      FileStatus.SKIPPED, FileStatus.RENAMED, FileStatus.DATE_CHANGED):
                continue
            # 手动重命名/手动编辑过的文件不再自动处理（与 _refilter_files_for_mode 一致）：
            # 否则重命名模式会按手动编辑后的日期再次改名、
            # 更改日期模式会重写手动重命名后文件的 EXIF 日期
            if fi.get('manual_rename') or fi.get('manual_edit_date') or \
                    st in (FileStatus.MANUALLY_RENAMED, FileStatus.MANUALLY_EDITED):
                continue
            # 试运行状态仅表示预览结果：再次试运行（如修改前缀/后缀后）
            # 必须重新执行才能刷新预览，正式处理也须落地修改，
            # 因此 DRY_RUN 文件一律重新纳入待处理列表
            if operation_mode == "change_date":
                if fi.get('manual_edit_date'):
                    continue
                if fi.get('new_date') is None:
                    continue
            to_process.append(fi)
        return files_copy, to_process

    def _handle_no_files_to_process(self, total):
        self.app.post_to_ui(lambda: self.status_var.set(_("处理完成（没有需要处理的文件）")))
        self.app.post_to_ui(lambda: self._append_log(
            "\n" + _("处理完成") + "！\n" + _("成功: ") + "0\n" + _("跳过: ") + str(total) + "\n" + _("失败: ") + "0\n"))
        self.app.post_to_ui(lambda: CompletionDialog(self.app.root, 0, total, 0))

    def _process_single_file_change_date(self, fi, dry_run, skip_existing):
        if not dry_run:
            ok, msg, skipped = self.date_renamer.process_file(
                fi['path'], skip_existing=skip_existing)
            if ok:
                if skipped:
                    # 跳过不等于成功：不标 DATE_CHANGED、不覆盖 original_date
                    fi['status'] = FileStatus.NO_DATE_NEEDED
                else:
                    fi['status'] = FileStatus.DATE_CHANGED
                    fi['original_date'] = fi.get('new_date', None)
            else:
                fi['status'] = FileStatus.FAILED
                fi['status_detail'] = msg
        else:
            fi['status'] = FileStatus.DRY_RUN_DATE_CHANGED

    def _process_single_file_rename(self, fi, dry_run, prefix_val, suffix_val):
        fp = fi['path']
        # 复用扫描时缓存的 original_date（扫描已按 fallback_to_fs 解析过），
        # 避免重命名模式对每个文件重复读盘解析 EXIF
        cached = fi.get('original_date')
        existing = self._cached_original_dt(fi) if cached else None
        if existing and existing != datetime.min:
            new_name = self._gen_new_name(fp, existing, fi, prefix=prefix_val, suffix=suffix_val)
            if new_name is None:
                if fi.get('manual_rename'):
                    fi['_new_filename_display'] = _('已手动重命名')
                else:
                    fi['status'] = FileStatus.NO_RENAME_NEEDED
                    fi['_new_filename_display'] = _('与原文件名相同')
            elif new_name:
                fi['_new_filename_display'] = new_name
                if not dry_run:
                    new_path = os.path.join(os.path.dirname(fp), new_name)
                    # 目标存在性不在此预检：直接进入下方编号重试循环，
                    # 与预览/批量路径的"存在则自动编号"行为保持一致
                    base_dir = os.path.dirname(fp)
                    orig_stem, ext = os.path.splitext(new_name)
                    target = new_path
                    counter = 1
                    renamed_to = None
                    while True:
                        try:
                            _no_clobber_rename(fp, target)
                            renamed_to = os.path.basename(target)
                            break
                        except FileExistsError:
                            # 编号名穷尽（10000 个候选全被占用）视为目标已存在
                            if counter >= 10000:
                                break
                            target = os.path.join(base_dir, f"{orig_stem}_{counter:03d}{ext}")
                            counter += 1
                        except OSError as e:
                            # 真实系统错误（权限/磁盘/跨分区等）：报出实际原因，
                            # 不再误报为"目标文件已存在"
                            fi['status_detail'] = _("重命名失败: ") + str(e)
                            return
                    if renamed_to is None:
                        fi['status'] = FileStatus.FAILED
                        fi['status_detail'] = _("目标文件已存在: ") + new_name
                        return
                    fi['status'] = FileStatus.RENAMED
                    fi['filename'] = renamed_to
                    fi['path'] = target
                else:
                    fi['status'] = FileStatus.DRY_RUN
                    fi['status_detail'] = new_name
        else:
            fi['status'] = FileStatus.NO_DATE_TAKEN
            fi['_new_filename_display'] = _('无拍摄日期')

    def _process_single_file(self, fi, operation_mode, dry_run, skip_existing,
                             rename_prefix_val, rename_suffix_val):
        try:
            if operation_mode == "change_date":
                self._process_single_file_change_date(fi, dry_run, skip_existing)
            else:
                self._process_single_file_rename(fi, dry_run, rename_prefix_val, rename_suffix_val)
        except Exception as e:
            # 编程错误/意外异常也写入日志，避免静默变成"失败: xxx"且无法排障
            log_exc()
            fi['status'] = FileStatus.FAILED
            fi['status_detail'] = str(e)

        st = fi.get('status')
        if st in (FileStatus.DATE_CHANGED, FileStatus.RENAMED, FileStatus.DRY_RUN, FileStatus.DRY_RUN_DATE_CHANGED):
            return 'success'
        elif st == FileStatus.FAILED:
            return 'failed'
        else:
            return 'skipped'

    def _execute_retry_loop(self, to_process, operation_mode, dry_run, skip_existing,
                            rename_prefix_val, rename_suffix_val):
        lock = threading.Lock()
        max_workers = min(32, (os.cpu_count() or 4) * 2)
        prev_failed_ids = None
        for retry_round in range(5):
            done = 0
            total_to_process = len(to_process)
            if not to_process:
                break
            if retry_round > 0:
                time.sleep(0.5 * retry_round ** 2)
                self.app.post_to_ui(lambda r=retry_round, c=len(to_process): (
                    self.status_var.set(_("重试第 ") + str(r) + _(" 轮, 剩余 ") + str(c) + _(" 个文件..."))))

            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futs = {}
                for fi in to_process:
                    futs[pool.submit(self._process_single_file, fi, operation_mode,
                                     dry_run, skip_existing, rename_prefix_val, rename_suffix_val)] = fi
                for fut in as_completed(futs):
                    fi = futs[fut]
                    fut.result()
                    with lock:
                        done += 1
                    # 节流：每 5 个文件刷新一行，避免万级文件排队大量 Tk 回调
                    if done % 5 == 0 or done == total_to_process:
                        self.app.post_to_ui(lambda f=fi: self._update_preview_row(f))
                    if done % 5 == 0 or done == total_to_process:
                        pct = done / total_to_process * 100
                        self.app.post_to_ui(lambda p=pct, d=done, t=total_to_process: (
                            self.progress_var.set(p),
                            self.status_var.set(_("进度: ") + str(d) + "/" + str(t))))

            if retry_round < 4:
                to_process = [fi for fi in to_process if fi.get('status') == FileStatus.FAILED]
                new_ids = {id(fi) for fi in to_process}
                # 连续两轮失败文件集合完全相同说明失败是永久性的（权限/目录只读/格式不支持等），
                # 继续重试只会空耗时间，提前结束
                if prev_failed_ids is not None and new_ids == prev_failed_ids:
                    break
                prev_failed_ids = new_ids
        return to_process

    def _process_thread(self, dry_run, operation_mode, skip_existing,
                        rename_prefix_val='', rename_suffix_val=''):
        try:
            self.app.post_to_ui(lambda: self.status_var.set(_("处理中...")))
            self._set_ui_processing_state(True)

            files_copy, to_process = self._get_eligible_files(operation_mode, dry_run)
            total = len(files_copy)

            if not to_process:
                self._handle_no_files_to_process(total)
                return

            self.app.post_to_ui(lambda: self.progress_var.set(0))
            self._execute_retry_loop(to_process, operation_mode, dry_run,
                                     skip_existing, rename_prefix_val, rename_suffix_val)

            self.app.post_to_ui(lambda: self.progress_var.set(100))
            self.app.post_to_ui(lambda: self.status_var.set(_("处理完成")))
            # 兜底刷新预览，保证节流跳过的行最终一致
            self.app.post_to_ui(self._update_preview)

            final_success = sum(1 for fi in self.files_to_process
                                if fi.get('status') in (FileStatus.DATE_CHANGED, FileStatus.RENAMED,
                                                         FileStatus.DRY_RUN, FileStatus.DRY_RUN_DATE_CHANGED))
            final_failed = sum(1 for fi in self.files_to_process if fi.get('status') == FileStatus.FAILED)
            final_skipped = total - final_success - final_failed
            self.app.post_to_ui(
                lambda: self._append_log(
                    "\n" + _("处理完成") + "！\n" + _("成功: ") + str(final_success) +
                    _("\n跳过: ") + str(final_skipped) + _("\n失败: ") + str(final_failed) + "\n"))
            self.app.post_to_ui(
                lambda s=final_success, k=final_skipped, f=final_failed:
                    CompletionDialog(self.app.root, s, k, f))
        except Exception:
            log_exc()
            self.app.post_to_ui(lambda: self.status_var.set(_("处理失败")))
        finally:
            self._set_ui_processing_state(False)
            self.app.post_to_ui(lambda: setattr(self, '_processing', False))
            self.app.post_to_ui(self.app.release_processing)

    def _pulse_progress(self):
        self.progress.config(mode='determinate')
        self.progress_var.set(20)
        for i in range(40, 101, 20):
            safe_after(self.app.root, int((i - 20) / 80 * 150), lambda v=i: self.progress_var.set(v))



    def _append_log(self, text):
        self.log_text.config(state='normal')
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')

    def clear_list(self):
        if getattr(self, '_processing', False):
            messagebox.showwarning(_("警告"), _("正在处理中，请等待当前任务完成"))
            return
        if getattr(self, '_scanning', False):
            messagebox.showwarning(_("警告"), _("正在扫描中，请等待扫描完成"))
            return
        self.files_to_process = []
        self._update_preview()
        self.log_text.config(state='normal')
        self.log_text.delete(1.0, tk.END)
        self.log_text.insert(tk.END, _("列表已清空\n"))
        self.log_text.config(state='disabled')

    def _batch_set_dates_from_filename(self, selected_items, path_to_file):
        if getattr(self, '_processing', False):
            messagebox.showwarning(_("警告"), _("正在处理中，请等待当前任务完成"))
            return
        if getattr(self, '_scanning', False):
            messagebox.showwarning(_("警告"), _("正在扫描中，请等待扫描完成"))
            return
        files_to_process = []
        skipped_no_date = []
        skipped_not_ready = []
        skipped_already = []

        for item_id in selected_items:
            values = self.date_tree.item(item_id, 'values')
            if len(values) < 7:
                continue
            file_path = values[6]
            fi = path_to_file.get(file_path)
            if not fi:
                continue

            st = fi.get('status')
            new_date_str = fi.get('new_date', '')

            if st != FileStatus.PENDING_DATE_CHANGE:
                if st == FileStatus.NO_DATE_NEEDED:
                    skipped_already.append(fi)
                elif st == FileStatus.PARSE_FAILED:
                    skipped_no_date.append(fi)
                else:
                    skipped_not_ready.append(fi)
                continue

            if new_date_str is None or not new_date_str:
                skipped_no_date.append(fi)
                continue

            files_to_process.append(fi)

        if not files_to_process:
            msg = _("没有符合条件的文件可处理。\n")
            if skipped_already:
                msg += _("· ") + str(len(skipped_already)) + _(" 个文件已有拍摄日期（无需更改日期）") + "\n"
            if skipped_no_date:
                msg += _("· ") + str(len(skipped_no_date)) + _(" 个文件无法从文件名解析日期") + "\n"
            if skipped_not_ready:
                msg += _("· ") + str(len(skipped_not_ready)) + _(" 个文件状态不符合") + "\n"
            messagebox.showinfo(_("批量修改拍摄日期"), msg, parent=self.app.root)
            return

        summary = _("确定要将以下 ") + str(len(files_to_process)) + _(" 个文件的拍摄日期修改为文件名中的日期？")
        detail_parts = [summary]
        if skipped_already:
            detail_parts.append(_("· ") + str(len(skipped_already)) + _(" 个文件已有拍摄日期，将跳过"))
        if skipped_no_date:
            detail_parts.append(_("· ") + str(len(skipped_no_date)) + _(" 个文件无法解析日期，将跳过"))
        if skipped_not_ready:
            detail_parts.append(_("· ") + str(len(skipped_not_ready)) + _(" 个文件状态不符，将跳过"))
        detail = "\n".join(detail_parts)

        if not messagebox.askyesno(_("确认批量修改"), detail, parent=self.app.root):
            return
        # 全局互斥：防止与地理页处理同时写文件
        if not self.app.acquire_processing():
            messagebox.showwarning(_("警告"), _("其他任务正在处理中，请等待完成"))
            return
        # 与 _batch_rename_items 一致：置位 _processing 并禁用 UI，
        # 防止 worker 写 fi 状态与主线程模式切换/右键操作互相覆盖
        self._processing = True
        self._set_ui_processing_state(True)

        def process_one(fi):
            try:
                new_date_str = fi['new_date']
                new_dt = datetime.strptime(new_date_str, '%Y-%m-%d %H:%M:%S')
                fp = fi.get('path', '')
                ext = os.path.splitext(str(fp))[1].lower()
                update_file_shooting_date(str(fp), new_dt, ext)
                # 工作线程只写磁盘；fi 内存状态（original_date/status 等）
                # 由主线程 _apply_batch_date_moves 加锁统一回填，
                # 避免处理中主线程 UI 回调读到中间态
                return True, None, fi, new_dt
            except Exception as e:
                return False, str(e), fi, None

        def process():
            try:
                self.app.post_to_ui(lambda: self.log_text.config(state='normal'))
                self.app.post_to_ui(lambda: self.log_text.insert(tk.END,
                    _("批量修改拍摄日期") + "：" + _("共选择 ") + str(len(selected_items)) + _(" 个文件，")
                    + _("符合条件 ") + str(len(files_to_process)) + _(" 个\n")))
                if skipped_already:
                    self.app.post_to_ui(lambda: self.log_text.insert(tk.END,
                        _("跳过已有拍摄日期的文件") + "：" + str(len(skipped_already)) + _(" 个\n")))
                if skipped_no_date:
                    self.app.post_to_ui(lambda: self.log_text.insert(tk.END,
                        _("跳过无法解析日期的文件") + "：" + str(len(skipped_no_date)) + _(" 个\n")))
                self.app.post_to_ui(lambda: self.log_text.see(tk.END))
                self.app.post_to_ui(lambda: self.progress_var.set(0))
                self.app.post_to_ui(lambda: self.status_var.set(
                    _("进度: 0/") + str(len(files_to_process))))

                total = len(files_to_process)
                success = 0
                failed = 0
                errors = []
                done = 0
                moves = []

                with ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 4) * 2)) as pool:
                    futs = {pool.submit(process_one, fi): fi for fi in files_to_process}
                    for fut in as_completed(futs):
                        ok, err, fi, new_dt = fut.result()
                        if ok:
                            success += 1
                            # 循环在 process 后台线程内单线程执行，收集无需加锁
                            moves.append((fi, new_dt))
                        else:
                            failed += 1
                            if err:
                                errors.append(err)
                        done += 1
                        if done % 5 == 0 or done == total:
                            self.app.post_to_ui(lambda d=done, t=total: (
                                self.progress_var.set(d / t * 100),
                                self.status_var.set(_("进度: ") + str(d) + "/" + str(t))
                            ))

                self.app.post_to_ui(lambda m=moves: self._apply_batch_date_moves(m))
                self.app.post_to_ui(self._update_preview)
                self.app.post_to_ui(lambda: (
                    self.log_text.config(state='normal'),
                    self.log_text.insert(tk.END,
                        _("批量编辑完成！\n成功: ") + str(success) + _(" 个, 失败: ") + str(failed) + _(" 个\n")
                        + (_("错误: ") + errors[0] if errors else "") + "\n"),
                    self.log_text.see(tk.END),
                    self.log_text.config(state='disabled'),
                    self.progress_var.set(100),
                    self.status_var.set(_("进度: ") + str(total) + "/" + str(total))
                ))
                self.app.post_to_ui(lambda s=success, f=failed, sk=len(skipped_already) + len(skipped_no_date): (
                    self.status_var.set(_("进度: ") + str(s + f) + "/" + str(s + f) + _(" 成功: ") + str(s) + _(" 失败: ") + str(f) + _(" 跳过: ") + str(sk))
                ))
            except Exception as e:
                # 显式绑定 e，避免延迟执行的 lambda 中引用已删除的异常变量
                self.app.post_to_ui(lambda e=e: (
                    self.log_text.config(state='normal'),
                    self.log_text.insert(tk.END, _("处理失败") + ": " + str(e) + "\n"),
                    self.log_text.see(tk.END),
                    self.log_text.config(state='disabled')))
            finally:
                self.app.post_to_ui(lambda: setattr(self, '_processing', False))
                self.app.post_to_ui(lambda: self._set_ui_processing_state(False))
                self.app.post_to_ui(self.app.release_processing)

        t = threading.Thread(target=process, daemon=True)
        self.app.register_thread(t)
        t.start()

    def _apply_batch_date_moves(self, moves):
        """主线程加锁回填批量改日期结果（worker 线程只写磁盘）"""
        if not moves:
            return
        with self.app.lock:
            for fi, new_dt in moves:
                try:
                    fi['original_date'] = new_dt.strftime('%Y-%m-%d %H:%M:%S')
                    fi['manual_edit_date'] = True
                    fi['status'] = FileStatus.DATE_CHANGED
                except Exception:
                    log_exc()

    def export_results(self):
        if getattr(self, '_processing', False):
            messagebox.showwarning(_("警告"), _("正在处理中，请等待当前任务完成"))
            return
        if getattr(self, '_scanning', False):
            messagebox.showwarning(_("警告"), _("正在扫描中，请等待扫描完成"))
            return
        if getattr(self, '_exporting', False):
            messagebox.showwarning(_("警告"), _("正在导出，请稍候"))
            return
        if not self.files_to_process:
            messagebox.showwarning(_("警告"), _("没有可导出的数据"))
            return

        import csv
        filename = filedialog.asksaveasfilename(
            title=_("保存文件"),
            defaultextension=".csv",
            filetypes=[(_("CSV文件"), "*.csv"), (_("文本文件"), "*.txt"), (_("所有文件"), "*.*")])
        if not filename:
            return

        # 写盘移入 worker 线程，避免万级文件时逐行写盘冻结 UI；
        # 快照在锁下拷贝，扫描完成整体替换列表不影响本次导出的一致性
        with self.app.lock:
            snapshot = list(self.files_to_process)
        ext = os.path.splitext(filename)[1].lower()
        mode = self.operation_mode.get()

        def worker():
            try:
                if ext == '.txt':
                    with open(filename, 'w', encoding='utf-8', newline='') as f:
                        f.write(_("媒体文件处理结果报告") + "\n")
                        f.write("=" * 60 + "\n\n")
                        for i, fi in enumerate(snapshot, 1):
                            f.write(f"{i}. {fi.get('filename', '')}\n")
                            f.write(_("   拍摄日期: ") + (fi.get('original_date') or '') + "\n")
                            f.write(_("   文件名日期: ") + (fi.get('new_date') or '') + "\n")
                            f.write(_("   状态: ") + status_text(fi.get('status'), fi.get('status_detail', '')) + "\n")
                            f.write("-" * 40 + "\n")
                else:
                    with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
                        w = csv.writer(f)
                        w.writerow([_('文件名'), _('文件路径'), _('拍摄时间'), _('文件名日期'), _('状态'), _('操作模式')])
                        for fi in snapshot:
                            w.writerow([
                                csv_safe(fi.get('filename', '')),
                                csv_safe(str(fi.get('path', ''))),
                                fi.get('original_date') or '',
                                fi.get('new_date') or '',
                                status_text(fi.get('status'), fi.get('status_detail', '')),
                                # 导出开始时的模式：与快照数据一致，
                                # 避免导出期间切换模式导致模式与数据错位
                                mode,
                            ])
                self.app.post_to_ui(lambda: messagebox.showinfo(
                    _("成功"), _("文件已保存到:\n") + filename))
            except Exception as e:
                log_exc()
                self.app.post_to_ui(lambda e=str(e): messagebox.showerror(
                    _("错误"), _("导出失败:\n") + e))
            finally:
                self.app.post_to_ui(lambda: setattr(self, '_exporting', False))
                self.app.post_to_ui(lambda: self._btn_export.config(
                    state="disabled" if getattr(self, '_processing', False)
                    or getattr(self, '_scanning', False) else "normal"))

        self._exporting = True
        self._btn_export.config(state="disabled")
        t = threading.Thread(target=worker, daemon=True)
        self.app.register_thread(t)
        t.start()
