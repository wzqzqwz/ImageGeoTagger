"""日期处理标签页"""

import tkinter as tk
from tkinter import ttk, filedialog
from ui import custom_msgbox as messagebox
import os
import platform
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.date_processor import (
    MediaDateRenamer, update_file_shooting_date
)
from utils.media_utils import (
    parse_datetime_from_filename, get_existing_datetime, is_media_file
)
from config import ALL_MEDIA_EXTENSIONS
from ui.dialogs import (
    EditShootingDateDialog, CompletionDialog
)
from utils.platform_utils import open_file_with_system, show_file_in_explorer
from utils.recycle_bin import send_to_recycle_bin
from utils.i18n import _
from models.media_file import FileStatus, status_text, status_sort_key
try:
    from tkinterdnd2 import DND_FILES
except ImportError:
    DND_FILES = None


# Fixed column identifiers (not translated - used as internal Treeview column IDs)
_DATE_COL_IDS = ('序号', '文件名', '文件拍摄日期', '文件名日期', '新文件名', '状态')


class DateTab:
    """日期处理标签页"""

    def __init__(self, notebook, app):
        self.app = app
        self.frame = ttk.Frame(notebook, padding="10")

        self.date_renamer = MediaDateRenamer(dry_run=True)
        self.selected_directory = tk.StringVar()
        self.operation_mode = tk.StringVar(value="change_date")
        self.dry_run = tk.BooleanVar(value=True)
        self.recursive = tk.BooleanVar(value=True)
        self.skip_existing = tk.BooleanVar(value=True)
        self.rename_prefix = tk.StringVar(value="")
        self.rename_suffix = tk.StringVar(value="")
        self.rename_prefix.trace_add('write', self._on_rename_params_change_delayed)
        self.rename_suffix.trace_add('write', self._on_rename_params_change_delayed)
        self.skip_files_with_date = tk.BooleanVar(value=True)
        self.files_to_process = []

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
        ttk.Entry(self.rename_frame, textvariable=self.rename_prefix,
                  width=8).pack(side=tk.LEFT, padx=(0, 5))
        self._lbl_date_label = ttk.Label(self.rename_frame, text=_("+拍摄日期+"),
                               relief='solid', borderwidth=1)
        self._lbl_date_label.pack(side=tk.LEFT, padx=(0, 5))
        self._lbl_suffix = ttk.Label(self.rename_frame, text=_("后缀:"))
        self._lbl_suffix.pack(side=tk.LEFT)
        ttk.Entry(self.rename_frame, textvariable=self.rename_suffix,
                  width=8).pack(side=tk.LEFT, padx=(0, 5))
        self._chk_skip_date = ttk.Checkbutton(self.rename_frame, text=_("文件名有日期跳过"),
                        variable=self.skip_files_with_date)
        self._chk_skip_date.pack(side=tk.LEFT, padx=(10, 0))

        opts2_frame = ttk.Frame(options_frame)
        opts2_frame.grid(row=1, column=0, columnspan=2,
                         sticky=(tk.W, tk.E), pady=(0, 5))
        self.dry_run_check = ttk.Checkbutton(opts2_frame, text=_("试运行模式"),
                                              variable=self.dry_run)
        self.dry_run_check.pack(side=tk.LEFT, padx=(0, 10))

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

    def _refilter_files_for_mode(self):
        mode = self.operation_mode.get()
        for fi in self.files_to_process:
            if not isinstance(fi, dict):
                continue
            path = fi.get('path', '')
            if not path:
                continue
            existing = get_existing_datetime(path)
            if mode == "change_date":
                if existing and existing != datetime.min:
                    fi['status'] = FileStatus.NO_DATE_NEEDED
                elif fi.get('new_date') and fi['new_date'] != _('无法解析'):
                    fi['status'] = FileStatus.PENDING_DATE_CHANGE
                else:
                    fi['status'] = FileStatus.PARSE_FAILED
            else:
                if self.skip_files_with_date.get() and parse_datetime_from_filename(str(path)):
                    fi['status'] = FileStatus.SKIPPED
                elif not existing or existing == datetime.min:
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
                traceback.print_exc()
        self._rename_after_id = self.app.root.after(500, self._on_rename_params_change)

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
        count = len(selected_items)
        if not messagebox.askyesno(_("确认删除"),
                                    _("确定要从处理序列中删除这 ") + str(count) + _(" 个文件吗？")):
            return

        items_to_remove = []
        for item_id in selected_items:
            values = self.date_tree.item(item_id, 'values')
            if values and len(values) >= 7:
                tree_path = values[6]
                for f in self.files_to_process:
                    fp = str(f.get('path', ''))
                    if fp == tree_path:
                        items_to_remove.append(f)
                        break

        for f in items_to_remove:
            if f in self.files_to_process:
                self.files_to_process.remove(f)

        for item_id in selected_items:
            try:
                self.date_tree.delete(item_id)
            except Exception:
                traceback.print_exc()

        self._update_preview()
        self.status_var.set(_("就绪 - 共 ") + str(len(self.files_to_process)) + _(" 个文件"))
        self._append_log(_("已从序列中删除 ") + str(count) + _(" 个文件\n"))
        self._pulse_progress()

    def _delete_items(self, selected_items):
        count = len(selected_items)
        if count > 1:
            msg = _("确定要将这 ") + str(count) + _(" 个文件移至回收站吗？")
        else:
            msg = _("确定要将这个文件移至回收站吗？")
        if not messagebox.askyesno(_("确认移至回收站"), msg):
            return

        paths = []
        for item_id in selected_items:
            values = self.date_tree.item(item_id, 'values')
            if values and len(values) >= 7:
                paths.append(values[6])

        success, failed = send_to_recycle_bin(paths)
        failed_names = set(f[0] for f in failed) if failed else set()

        for item_id in selected_items:
            values = self.date_tree.item(item_id, 'values')
            if values and len(values) >= 7:
                tree_path = values[6]
                basename = os.path.basename(tree_path)
                if basename not in failed_names:
                    for f in self.files_to_process[:]:
                        fp = str(f.get('path', ''))
                        if fp == tree_path:
                            self.files_to_process.remove(f)
                            break
            try:
                self.date_tree.delete(item_id)
            except Exception:
                traceback.print_exc()

        self._update_preview()
        self.status_var.set(_("已将 ") + str(success) + _(" 个文件移至回收站"))
        self._append_log(_("已将 ") + str(success) + _(" 个文件移至回收站\n"))
        if failed:
            for name, err in failed:
                self._append_log(_("失败: ") + name + " - " + err + "\n")
        self._pulse_progress()

    def _rename_single(self, file_info, item_id):
        if not file_info:
            return
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

        try:
            os.rename(str(file_path), new_path)
        except FileExistsError:
            messagebox.showerror(_("错误"), _("目标文件已存在: ") + new_name)
            return
        except OSError:
            messagebox.showerror(_("错误"), _("重命名失败（可能跨分区），请使用复制后删除的方式"))
            return
        file_info['path'] = new_path
        file_info['path'] = new_path
        file_info['filename'] = new_name
        file_info['status'] = FileStatus.MANUALLY_RENAMED
        file_info['manual_rename'] = True
        self._append_log(_("已重命名") + ": " + os.path.basename(file_path) + " → " + new_name + "\n")
        self._update_preview()

    def _batch_rename_items(self, selected_items, path_to_file):
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
            new_fn = vals[4]
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
        renamed = 0
        total_rename = len(to_rename)
        used_targets = set()
        self.progress_var.set(0)
        self.status_var.set(_("正在重命名... 0/") + str(total_rename))
        for i, (fi, fp, new_fn) in enumerate(to_rename):
            base_dir = os.path.dirname(fp)
            orig_stem, ext = os.path.splitext(new_fn)
            target = os.path.join(base_dir, new_fn)
            counter = 1
            while target in used_targets and counter < 10000:
                candidate = f"{orig_stem}_{counter:03d}{ext}"
                target = os.path.join(base_dir, candidate)
                counter += 1
            if counter >= 10000:
                self._append_log(_("重命名失败（无法生成唯一文件名）") + ": " + os.path.basename(fp) + " → " + new_fn + "\n")
                continue
            actual_new_fn = os.path.basename(target)
            try:
                os.rename(fp, target)
                fi['path'] = target
                fi['path'] = target
                fi['filename'] = actual_new_fn
                fi['status'] = FileStatus.MANUALLY_RENAMED
                fi['manual_rename'] = True
                self._append_log(_("已重命名") + ": " + os.path.basename(fp) + " → " + actual_new_fn + "\n")
                used_targets.add(target)
                if actual_new_fn != new_fn:
                    self._append_log("  （" + _("原目标") + " " + new_fn + " " + _("已存在，自动使用") + " " + actual_new_fn + "）\n")
                renamed += 1
            except FileExistsError:
                self._append_log(_("重命名失败（目标文件已存在）") + ": " + os.path.basename(fp) + " → " + new_fn + "\n")
            except Exception as e:
                self._append_log(_("重命名失败") + ": " + os.path.basename(fp) + " → " + str(e) + "\n")
            self.progress_var.set((i + 1) / total_rename * 100)
            self.status_var.set(_("正在重命名... ") + str(i + 1) + "/" + str(total_rename))
            self.app.root.update_idletasks()
        self._update_preview()
        failed = total_rename - renamed
        if renamed:
            self.status_var.set(_("批量重命名完成，共重命名 ") + str(renamed) + _(" 个文件"))
            msg = _("批量重命名完成\n成功: ") + str(renamed)
            if failed:
                msg += _("\n失败: ") + str(failed)
            messagebox.showinfo(_("批量重命名"), msg, parent=self.app.root)

    def scan_files(self):
        folder = self.selected_directory.get()
        if not folder:
            messagebox.showwarning(_("警告"), _("请先选择文件夹"))
            return
        if getattr(self, '_scanning', False):
            return

        self._scanning = True
        scan_mode = self.operation_mode.get()
        scan_skip_with_date = self.skip_files_with_date.get()
        thread = threading.Thread(
            target=self._scan_thread, args=(folder, scan_mode, scan_skip_with_date))
        thread.daemon = True
        thread.start()

    def _scan_thread(self, folder, scan_mode, scan_skip_with_date):
        try:
            self.app.root.after(0, lambda: self.status_var.set(_("正在扫描文件...")))
            self.app.root.after(0, lambda: self.progress_var.set(0))

            files = []
            for r, _dirs, fs in os.walk(folder):
                for f in fs:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in ALL_MEDIA_EXTENSIONS:
                        files.append(os.path.join(r, f))

            total = len(files)
            if total == 0:
                self.app.root.after(0, lambda: self.status_var.set(_("未找到媒体文件")))
                self.app.root.after(0, lambda: setattr(self, '_scanning', False))
                return

            def process_one(fp, mode=scan_mode, skip_with_date=scan_skip_with_date):
                p = Path(fp)
                parsed = parse_datetime_from_filename(p.name)
                existing = get_existing_datetime(p)
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

            results = []
            with ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 4) * 2)) as pool:
                futs = [pool.submit(process_one, fp) for fp in files]
                count = 0
                for fut in as_completed(futs):
                    results.append(fut.result())
                    count += 1
                    if count % 10 == 0 or count == total:
                        pct = count / total * 100
                        self.app.root.after(0, lambda p=pct: self.progress_var.set(p))
                        self.app.root.after(
                            0, lambda c=count, t=total: self.status_var.set(
                                _("正在扫描文件... ") + str(c) + "/" + str(t)))

            self.app.root.after(0, lambda r=results: setattr(self, 'files_to_process', r))
            self.app.root.after(0, self._update_preview)
            self.app.root.after(
                0, lambda r=results: self.status_var.set(
                    _("扫描完成，找到 ") + str(len(r)) + _(" 个文件")))
            self.app.root.after(0, lambda: self.progress_var.set(100))
            self.app.root.after(0, lambda r=results: self._append_log(
                _("扫描完成，共找到 ") + str(len(r)) + _(" 个文件\n")))
            self.app.root.after(0, lambda: setattr(self, '_scanning', False))
        except Exception:
            traceback.print_exc()
            self.app.root.after(
                0, lambda: self.status_var.set(_("扫描失败")))
            self.app.root.after(
                0, lambda: self._append_log(_("扫描失败\n")))
            self.app.root.after(0, lambda: setattr(self, '_scanning', False))

    def _update_preview(self):
        for item in self.date_tree.get_children():
            self.date_tree.delete(item)

        sorted_files = sorted(
            self.files_to_process,
            key=lambda x: x.get('original_date', '') if x.get('original_date') is not None else '9999-12-31 23:59:59')

        for fi in sorted_files:
            filename = fi.get('filename', '')
            orig_date = fi.get('original_date') or _('无')
            new_date = fi.get('new_date') if fi.get('new_date') else _('无法解析')
            st = fi.get('status')
            file_path = fi.get('path', '')
            new_filename = ''

            if self.operation_mode.get() == "rename_file":
                existing = get_existing_datetime(file_path)
                if st == FileStatus.SKIPPED:
                    if not existing or existing == datetime.min:
                        new_filename = _('无拍摄日期')
                    else:
                        new_filename = _('原文件名有日期')
                else:
                    if existing and existing != datetime.min:
                        new_filename = self._gen_new_name(file_path, existing, fi)
                        if new_filename is None:
                            if fi.get('manual_rename'):
                                new_filename = _('已手动重命名')
                            else:
                                new_filename = _('与原文件名相同')
                    else:
                        new_filename = _('无拍摄日期')

            display_status = status_text(st, fi.get('status_detail', ''))
            if self.operation_mode.get() == "rename_file":
                item_id = self.date_tree.insert('', tk.END, values=(
                    '', filename, orig_date, '', new_filename, display_status, str(fi.get('path', ''))))
            else:
                item_id = self.date_tree.insert('', tk.END, values=(
                    '', filename, orig_date, new_date, '', display_status, str(fi.get('path', ''))))
            fi['_tree_id'] = item_id

        for i, item in enumerate(self.date_tree.get_children()):
            values = list(self.date_tree.item(item, 'values'))
            values[0] = i + 1
            self.date_tree.item(item, values=tuple(values))

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
                try:
                    dt = datetime.strptime(fi['original_date'], '%Y-%m-%d %H:%M:%S')
                except Exception:
                    new_fn = _('无拍摄日期')
                else:
                    fp = fi.get('path', '')
                    new_name = self._gen_new_name(fp, dt, fi)
                    if new_name is None:
                        new_fn = _('已手动重命名') if fi.get('manual_rename') else _('与原文件名相同')
                    else:
                        new_fn = new_name
            values = list(self.date_tree.item(item_id, 'values'))
            if len(values) >= 6:
                values[4] = new_fn
                self.date_tree.item(item_id, values=tuple(values))

    def _gen_new_name(self, original_path, date_to_use, file_info=None,
                       prefix=None, suffix=None):
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

        if not os.path.exists(os.path.join(base_dir, base_new_name)):
            return base_new_name

        counter = 1
        while counter < 10000:
            candidate = f"{prefix}{date_str}{suffix}_{counter:03d}{ext}"
            if candidate == orig_name:
                return None
            if not os.path.exists(os.path.join(base_dir, candidate)):
                return candidate
            counter += 1
        return None

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

        def sort_key(item):
            values = self.date_tree.item(item, 'values')
            if not values or len(values) <= col_idx:
                return ""
            val = values[col_idx]
            if column == '文件拍摄日期':
                try:
                    return datetime.strptime(val, '%Y-%m-%d %H:%M:%S')
                except Exception:
                    return datetime(9999, 12, 31)
            if column == '状态':
                for fi in self.files_to_process:
                    if fi.get('_tree_id') == item:
                        return status_sort_key(fi.get('status'))
                return 999
            return val.lower() if isinstance(val, str) else str(val)

        items.sort(key=sort_key, reverse=self.date_sort_reverse)
        for i, item in enumerate(items):
            self.date_tree.move(item, '', i)
            values = list(self.date_tree.item(item, 'values'))
            values[0] = i + 1
            self.date_tree.item(item, values=tuple(values))
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
        if not self.files_to_process:
            messagebox.showwarning(_("警告"), _("请先扫描文件"))
            return
        dry_run = self.dry_run.get()
        operation_mode = self.operation_mode.get()
        self._export_mode = operation_mode
        skip_existing = self.skip_existing.get()
        rename_prefix_val = self.rename_prefix.get()
        rename_suffix_val = self.rename_suffix.get()
        self.date_renamer.dry_run = dry_run
        thread = threading.Thread(
            target=self._process_thread,
            args=(dry_run, operation_mode, skip_existing,
                  rename_prefix_val, rename_suffix_val))
        thread.daemon = True
        thread.start()

    def _set_ui_processing_state(self, disabled):
        state = "disabled" if disabled else "normal"
        self.app.root.after(0, lambda: self.process_btn.config(state=state))
        self.app.root.after(0, lambda: self.scan_btn.config(state=state))
        self.app.root.after(0, lambda: self.dry_run_check.config(state=state))

    def _get_eligible_files(self, operation_mode):
        files_copy = list(self.files_to_process)
        to_process = []
        for fi in files_copy:
            st = fi.get('status')
            if st in (FileStatus.NO_DATE_NEEDED, FileStatus.NO_RENAME_NEEDED,
                      FileStatus.SKIPPED, FileStatus.RENAMED, FileStatus.DATE_CHANGED,
                      FileStatus.DRY_RUN, FileStatus.DRY_RUN_DATE_CHANGED):
                continue
            if operation_mode == "change_date":
                if fi.get('manual_edit_date'):
                    continue
                if fi.get('new_date') is None:
                    continue
            to_process.append(fi)
        return files_copy, to_process

    def _handle_no_files_to_process(self, total):
        self.app.root.after(0, lambda: self.status_var.set(_("处理完成（没有需要处理的文件）")))
        self.app.root.after(0, lambda: self._append_log(
            "\n" + _("处理完成") + "！\n" + _("成功: ") + "0\n" + _("跳过: ") + str(total) + "\n" + _("失败: ") + "0\n"))
        self.app.root.after(0, lambda: CompletionDialog(self.app.root, 0, total, 0))

    def _process_single_file_change_date(self, fi, dry_run, skip_existing):
        if not dry_run:
            ok, msg = self.date_renamer.process_file(
                fi['path'], skip_existing=skip_existing)
            if ok:
                fi['status'] = FileStatus.DATE_CHANGED
                fi['original_date'] = fi.get('new_date', None)
            else:
                fi['status'] = FileStatus.FAILED
                fi['status_detail'] = msg
        else:
            fi['status'] = FileStatus.DRY_RUN_DATE_CHANGED

    def _process_single_file_rename(self, fi, dry_run, prefix_val, suffix_val):
        fp = fi['path']
        existing = get_existing_datetime(fp)
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
                    os.rename(fp, new_path)
                    fi['status'] = FileStatus.RENAMED
                    fi['filename'] = new_name
                    fi['path'] = new_path
                    fi['path'] = new_path
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
        for retry_round in range(5):
            done = 0
            total_to_process = len(to_process)
            if not to_process:
                break
            if retry_round > 0:
                time.sleep(0.5 * retry_round ** 2)
                self.app.root.after(0, lambda r=retry_round, c=len(to_process): (
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
                    self.app.root.after(0, lambda f=fi: self._update_preview_row(f))
                    if done % 5 == 0 or done == total_to_process:
                        pct = done / total_to_process * 100
                        self.app.root.after(0, lambda p=pct, d=done, t=total_to_process: (
                            self.progress_var.set(p),
                            self.status_var.set(_("进度: ") + str(d) + "/" + str(t))))

            if retry_round < 4:
                to_process = [fi for fi in to_process if fi.get('status') == FileStatus.FAILED]
        return to_process

    def _process_thread(self, dry_run, operation_mode, skip_existing,
                        rename_prefix_val='', rename_suffix_val=''):
        try:
            self.app.root.after(0, lambda: self.status_var.set(_("处理中...")))
            self._set_ui_processing_state(True)

            files_copy, to_process = self._get_eligible_files(operation_mode)
            total = len(files_copy)

            if not to_process:
                self._handle_no_files_to_process(total)
                return

            self.app.root.after(0, lambda: self.progress_var.set(0))
            self._execute_retry_loop(to_process, operation_mode, dry_run,
                                     skip_existing, rename_prefix_val, rename_suffix_val)

            self.app.root.after(0, lambda: self.progress_var.set(100))
            self.app.root.after(0, lambda: self.status_var.set(_("处理完成")))

            final_success = sum(1 for fi in self.files_to_process
                                if fi.get('status') in (FileStatus.DATE_CHANGED, FileStatus.RENAMED,
                                                         FileStatus.DRY_RUN, FileStatus.DRY_RUN_DATE_CHANGED))
            final_failed = sum(1 for fi in self.files_to_process if fi.get('status') == FileStatus.FAILED)
            final_skipped = total - final_success - final_failed
            self.app.root.after(
                0, lambda: self._append_log(
                    "\n" + _("处理完成") + "！\n" + _("成功: ") + str(final_success) +
                    _("\n跳过: ") + str(final_skipped) + _("\n失败: ") + str(final_failed) + "\n"))
            self.app.root.after(
                0, lambda s=final_success, k=final_skipped, f=final_failed:
                    CompletionDialog(self.app.root, s, k, f))
        except Exception:
            traceback.print_exc()
            self.app.root.after(0, lambda: self.status_var.set(_("处理失败")))
        finally:
            self._set_ui_processing_state(False)

    def _pulse_progress(self):
        self.progress.config(mode='determinate')
        self.progress_var.set(20)
        for i in range(40, 101, 20):
            self.app.root.after(int((i - 20) / 80 * 150), lambda v=i: self.progress_var.set(v))



    def _append_log(self, text):
        self.log_text.config(state='normal')
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')

    def clear_list(self):
        self.files_to_process = []
        self._update_preview()
        self.log_text.config(state='normal')
        self.log_text.delete(1.0, tk.END)
        self.log_text.insert(tk.END, _("列表已清空\n"))
        self.log_text.config(state='disabled')

    def _batch_set_dates_from_filename(self, selected_items, path_to_file):
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

        def process_one(fi):
            try:
                new_date_str = fi['new_date']
                new_dt = datetime.strptime(new_date_str, '%Y-%m-%d %H:%M:%S')
                fp = fi.get('path', '')
                ext = os.path.splitext(str(fp))[1].lower()
                update_file_shooting_date(str(fp), new_dt, ext)
                fi['original_date'] = new_dt.strftime('%Y-%m-%d %H:%M:%S')
                fi['manual_edit_date'] = True
                fi['status'] = FileStatus.DATE_CHANGED
                return True, None, fi
            except Exception as e:
                return False, str(e), fi

        def process():
            try:
                self.app.root.after(0, lambda: self.log_text.config(state='normal'))
                self.app.root.after(0, lambda: self.log_text.insert(tk.END,
                    _("批量修改拍摄日期") + "：" + _("共选择 ") + str(len(selected_items)) + _(" 个文件，")
                    + _("符合条件 ") + str(len(files_to_process)) + _(" 个\n")))
                if skipped_already:
                    self.app.root.after(0, lambda: self.log_text.insert(tk.END,
                        _("跳过已有拍摄日期的文件") + "：" + str(len(skipped_already)) + _(" 个\n")))
                if skipped_no_date:
                    self.app.root.after(0, lambda: self.log_text.insert(tk.END,
                        _("跳过无法解析日期的文件") + "：" + str(len(skipped_no_date)) + _(" 个\n")))
                self.app.root.after(0, lambda: self.log_text.see(tk.END))
                self.app.root.after(0, lambda: self.progress_var.set(0))
                self.app.root.after(0, lambda: self.status_var.set(
                    _("进度: 0/") + str(len(files_to_process))))

                total = len(files_to_process)
                success = 0
                failed = 0
                errors = []
                done = 0

                with ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 4) * 2)) as pool:
                    futs = {pool.submit(process_one, fi): fi for fi in files_to_process}
                    for fut in as_completed(futs):
                        ok, err, fi = fut.result()
                        if ok:
                            success += 1
                        else:
                            failed += 1
                            if err:
                                errors.append(err)
                        done += 1
                        self.app.root.after(0, lambda d=done, t=total: (
                            self.progress_var.set(d / t * 100),
                            self.status_var.set(_("进度: ") + str(d) + "/" + str(t))
                        ))

                self.app.root.after(0, self._update_preview)
                self.app.root.after(0, lambda: (
                    self.log_text.config(state='normal'),
                    self.log_text.insert(tk.END,
                        _("批量编辑完成！\n成功: ") + str(success) + _(" 个, 失败: ") + str(failed) + _(" 个\n")
                        + (_("错误: ") + errors[0] if errors else "") + "\n"),
                    self.log_text.see(tk.END),
                    self.log_text.config(state='disabled'),
                    self.progress_var.set(100),
                    self.status_var.set(_("进度: ") + str(total) + "/" + str(total))
                ))
                self.app.root.after(0, lambda s=success, f=failed, sk=len(skipped_already) + len(skipped_no_date): (
                    self.status_var.set(_("进度: ") + str(s + f) + "/" + str(s + f) + _(" 成功: ") + str(s) + _(" 失败: ") + str(f) + _(" 跳过: ") + str(sk))
                ))
            except Exception as e:
                self.app.root.after(0, lambda: (
                    self.log_text.config(state='normal'),
                    self.log_text.insert(tk.END, _("处理失败") + ": " + str(e) + "\n"),
                    self.log_text.see(tk.END),
                    self.log_text.config(state='disabled')))

        t = threading.Thread(target=process, daemon=True)
        t.start()

    def export_results(self):
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

        try:
            ext = os.path.splitext(filename)[1].lower()
            if ext == '.txt':
                with open(filename, 'w', encoding='utf-8', newline='') as f:
                    f.write(_("媒体文件处理结果报告") + "\n")
                    f.write("=" * 60 + "\n\n")
                    for i, fi in enumerate(self.files_to_process, 1):
                        f.write(f"{i}. {fi.get('filename', '')}\n")
                        f.write(_("   拍摄日期: ") + fi.get('original_date', '') + "\n")
                        f.write(_("   文件名日期: ") + fi.get('new_date', '') + "\n")
                        f.write(_("   状态: ") + status_text(fi.get('status'), fi.get('status_detail', '')) + "\n")
                        f.write("-" * 40 + "\n")
            else:
                with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
                    w = csv.writer(f)
                    w.writerow([_('文件名'), _('文件路径'), _('拍摄时间'), _('文件名日期'), _('状态'), _('操作模式')])
                    for fi in self.files_to_process:
                        w.writerow([
                            fi.get('filename', ''),
                            str(fi.get('path', '')),
                            fi.get('original_date', ''),
                            fi.get('new_date', ''),
                            status_text(fi.get('status'), fi.get('status_detail', '')),
                            getattr(self, '_export_mode', self.operation_mode.get()),
                        ])
            messagebox.showinfo(_("成功"), _("文件已保存到:\n") + filename)
        except Exception as e:
            messagebox.showerror(_("错误"), _("导出失败:\n") + str(e))
