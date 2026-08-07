"""地理位置处理标签页"""

import tkinter as tk
from tkinter import ttk, filedialog
from ui import custom_msgbox as messagebox
import os
import platform as _platform
import threading
import urllib.parse
from datetime import datetime

from config import DEFAULT_TIME_THRESHOLD_MINUTES
from services.media_scanner import scan_folder
from services.geo_processor import process_location_info
from services.export_service import (
    export_to_txt, export_to_csv, export_to_json, export_to_gpx,
    generate_statistics,
)
from utils.i18n import _
from utils.logging_utils import log_exc
from utils.platform_utils import _thread_is_alive
try:
    from tkinterdnd2 import DND_FILES
except ImportError:
    DND_FILES = None


class GeoTab:
    """地理位置处理标签页"""

    def __init__(self, notebook, app):
        self.app = app
        self.frame = ttk.Frame(notebook, padding="10")

        self.time_threshold = tk.IntVar(value=DEFAULT_TIME_THRESHOLD_MINUTES)
        self.only_process_with_date = tk.BooleanVar(value=True)

        self.exiftool_available = None
        self.exiftool_path = None
        self.result_window = None
        # 上次有效的时间差阈值缓存（首次非法输入时用于还原）
        self._threshold_min_cache = DEFAULT_TIME_THRESHOLD_MINUTES
        # 导出进行中标志：防止重复点击并发写同一文件
        self._exporting = False

        self.create_interface()

    def rebuild_ui(self):
        self.extract_btn.config(text=_("提取图像信息"))
        self.process_btn.config(text=_("处理位置信息"))
        self.status_var.set(_("就绪：请选择文件夹"))
        self._lbl_folder.config(text=_("文件夹路径:"))
        self._btn_browse.config(text=_("浏览..."))
        self._lbl_threshold.config(text=_("时间差阈值(分钟):"))
        self._lbl_default.config(text=_("(默认30分钟)"))
        self._chk_only_date.config(text=_("只处理有原始日期的文件"))
        self._btn_show.config(text=_("显示结果"))
        self._btn_export.config(text=_("导出结果"))
        self._lbl_result.config(text=_("处理结果:"))
        self.show_usage_instructions()

    def create_interface(self):
        folder_frame = ttk.LabelFrame(
            self.frame,
            text="",
            padding="5"
        )
        folder_frame.pack(fill=tk.X)
        folder_frame.columnconfigure(1, weight=1)

        self._lbl_folder = ttk.Label(folder_frame, text=_("文件夹路径:"))
        self._lbl_folder.grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        self.geo_selected_directory = tk.StringVar()
        self.geo_folder_entry = tk.Entry(folder_frame, textvariable=self.geo_selected_directory)
        self.geo_folder_entry.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=(0, 10))
        if DND_FILES is not None:
            self.geo_folder_entry.drop_target_register(DND_FILES)
            self.geo_folder_entry.dnd_bind('<<Drop>>', self._on_folder_drop)
        self._btn_browse = ttk.Button(folder_frame, text=_("浏览..."),
                   command=self.browse_folder)
        self._btn_browse.grid(row=0, column=2)

        tfrm = ttk.Frame(self.frame)
        tfrm.pack(fill=tk.X, pady=(0, 2))
        self._lbl_threshold = ttk.Label(tfrm, text=_("时间差阈值(分钟):"))
        self._lbl_threshold.pack(side=tk.LEFT)
        ttk.Entry(tfrm, textvariable=self.time_threshold,
                  width=5).pack(side=tk.LEFT, padx=(5, 5))
        self._lbl_default = ttk.Label(tfrm, text=_("(默认30分钟)"))
        self._lbl_default.pack(side=tk.LEFT)
        self._chk_only_date = ttk.Checkbutton(tfrm, text=_("只处理有原始日期的文件"),
                        variable=self.only_process_with_date)
        self._chk_only_date.pack(side=tk.LEFT, padx=(20, 0))

        bfrm = ttk.Frame(self.frame)
        bfrm.pack(fill=tk.X, pady=(5, 2))
        button_container = ttk.Frame(bfrm)
        button_container.pack(expand=True)

        self.extract_btn = ttk.Button(
            button_container, text=_("提取图像信息"),
            command=self.start_extract_thread)
        self.extract_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.process_btn = ttk.Button(
            button_container, text=_("处理位置信息"),
            command=self.start_process_thread)
        self.process_btn.pack(side=tk.LEFT, padx=5)

        self._btn_show = ttk.Button(button_container, text=_("显示结果"),
                   command=self.show_results)
        self._btn_show.pack(side=tk.LEFT, padx=5)
        self._btn_export = ttk.Button(button_container, text=_("导出结果"),
                   command=self.export_results)
        self._btn_export.pack(side=tk.LEFT, padx=5)

        self._lbl_result = ttk.Label(self.frame, text=_("处理结果:"))
        self._lbl_result.pack(anchor=tk.W, pady=(2, 2))
        text_frame = ttk.Frame(self.frame)
        text_frame.pack(fill=tk.BOTH, expand=True)

        self.result_text = tk.Text(text_frame, wrap=tk.WORD, width=45, height=10)
        scrollbar = ttk.Scrollbar(
            text_frame, orient=tk.VERTICAL, command=self.result_text.yview)
        self.result_text.configure(yscrollcommand=scrollbar.set)
        self.result_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        pfrm = ttk.Frame(self.frame)
        pfrm.pack(fill=tk.X, pady=(5, 2))
        pfrm.columnconfigure(0, weight=1)
        pfrm.columnconfigure(1, weight=2)
        self.progress_var = tk.DoubleVar()
        self.status_var = tk.StringVar(value=_("就绪：请选择文件夹"))
        self.status_label = ttk.Label(pfrm, textvariable=self.status_var)
        self.status_label.grid(row=0, column=0, sticky=tk.W, padx=(0, 5))
        self.progress = ttk.Progressbar(
            pfrm, orient=tk.HORIZONTAL,
            mode='determinate', variable=self.progress_var)
        self.progress.grid(row=0, column=1, sticky=tk.EW)
        self.geo_selected_directory.trace_add('write', self._on_folder_path_changed)

        self.show_usage_instructions()

    def show_usage_instructions(self):
        lines = [
            _("=== 图像及视频地理位置信息处理工具使用说明 ==="),
            "",
            _("第一步：选择文件夹"),
            _("   • 点击\"浏览...\"按钮选择包含图像、视频文件的文件夹"),
            _("   • 程序支持多层及子文件夹，会自动递归扫描所有子目录"),
            _("   • 支持.jpg, .jpeg, .png, .tiff, .nef, .cr2, .arw, .mov, .mp4 等文件"),
            _("   • 可以同时包含 .gpx 轨迹文件用于位置匹配"),
            _("   • 支持复杂的文件夹结构，无需手动整理文件"),
            "",
            _("第二步：设置时间差阈值"),
            _("   • 默认30分钟，用于匹配图像拍摄时间与GPS轨迹点时间"),
            _("   • 可根据需要调整，范围建议10-60分钟"),
            "",
            _("第三步：提取图像信息"),
            _("   • 点击\"提取图像信息\"按钮开始分析文件"),
            _("   • 程序会扫描所有图像和视频文件，提取EXIF信息"),
            _("   • 包括拍摄时间、GPS坐标、文件大小等信息"),
            "",
            _("第四步：处理位置信息"),
            _("   • 点击\"处理位置信息\"，为没有GPS数据的文件智能匹配GPS坐标"),
            _("   • 基于时间匹配算法，自动找到最接近的GPS轨迹点"),
            "",
            _("第五步：查看结果"),
            _("   • 点击\"显示结果\"按钮查看详细处理结果"),
            _("   • 支持搜索、编辑位置信息、编辑拍摄日期等功能"),
            _("   • 手动编辑位置信息后可回到第四步操作，提高效率。"),
            "",
            _("第六步：导出结果"),
            _("   • 点击\"导出结果\"按钮保存处理结果"),
            _("   • 可以导出为CSV格式，包含所有文件信息"),
            "",
            _("注意事项："),
            _("   • 处理前请备份重要文件"),
            _("   • 确保有足够的磁盘空间"),
            _("   • 处理大量文件时请耐心等待"),
            _("   • 建议先在小批量文件上测试"),
        ]
        instructions = "\n".join(lines)
        self.result_text.config(state='normal')
        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(1.0, instructions)
        self.result_text.config(state='disabled')

    def browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.geo_selected_directory.set(folder)

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
                self.geo_selected_directory.set(p)
                return

    def _on_folder_path_changed(self, *_args):
        if self.geo_selected_directory.get():
            self.status_var.set(_("提取图像信息"))
        else:
            self.status_var.set(_("就绪：请选择文件夹"))

    def update_ui_state(self, processing, thread=None):
        state = "disabled" if processing else "normal"
        self.extract_btn.config(state=state)
        self.process_btn.config(state=state)
        self._btn_show.config(state=state)
        self._btn_export.config(state=state)
        if processing:
            return
        # 只清除"自己"的线程状态，避免旧线程的结束回调误清新线程状态。
        # 迟到回调（thread 非 None 且不等于当前线程）一律不清理：
        # 此时锁可能属于日期页/编辑对话框任务（它们不注册 current_thread），
        # 误清 is_processing 会让两个任务并发写同一批文件导致损坏。
        cleared = False
        with self.app.lock:
            if thread is None:
                if self.app.current_thread is None:
                    self.app.is_processing = False
                    cleared = True
            elif self.app.current_thread is thread:
                self.app.current_thread = None
                self.app.is_processing = False
                cleared = True
        if not cleared:
            # 锁仍被其它任务持有（本回调未清理任何状态）：
            # 按钮保持禁用，避免 UI 显示可用而实际被互斥拒绝
            self.extract_btn.config(state="disabled")
            self.process_btn.config(state="disabled")
            self._btn_show.config(state="disabled")
            self._btn_export.config(state="disabled")

    def is_thread_running(self):
        with self.app.lock:
            return (self.app.current_thread is not None and
                    _thread_is_alive(self.app.current_thread) and
                    self.app.is_processing)

    def start_extract_thread(self):
        if self.is_thread_running():
            messagebox.showwarning(_("处理中"), _("请等待当前任务完成"))
            return
        folder = self.geo_selected_directory.get()
        if not folder:
            messagebox.showwarning(_("提示"), _("请先选择一个文件夹"))
            return
        # 全局互斥：日期页等其它任务进行中时禁止启动
        if not self.app.acquire_processing():
            messagebox.showwarning(_("处理中"), _("其他任务正在处理中，请等待完成"))
            return
        self._only_wpd_cache = self.only_process_with_date.get()

        self.progress_var.set(0)
        self.status_var.set(_("迭代进度: 0/0"))
        self.update_ui_state(True)
        self.result_text.config(state='normal')
        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(tk.END, _("正在提取图像信息...") + "\n")
        self.result_text.see(tk.END)
        self.result_text.config(state='disabled')

        # 扫描前不清空 app.a/app.b/gps_data：扫描失败或目录为空时
        # 旧结果仍保留，避免用户已有数据被无提示清空。
        # 新结果由 extract_image_info 在扫描成功后加锁整体替换。
        with self.app.lock:
            t = threading.Thread(target=self._extract_wrapper, args=(folder,), daemon=True)
            self.app.current_thread = t
        self.app.register_thread(t)
        t.start()

    def start_process_thread(self):
        if self.is_thread_running():
            messagebox.showwarning(_("处理中"), _("请等待当前任务完成"))
            return
        if not self.app.acquire_processing():
            messagebox.showwarning(_("处理中"), _("其他任务正在处理中，请等待完成"))
            return
        # 用户可能输入非数字：非法输入时提示并还原为上次有效值，避免静默使用默认值
        try:
            threshold = int(self.time_threshold.get())
        except Exception:
            messagebox.showwarning(
                _("输入错误"),
                _("时间差阈值必须是整数，已还原为上次有效值: ") + str(self._threshold_min_cache),
                parent=self.app.root)
            self.time_threshold.set(self._threshold_min_cache)
            self.app.release_processing()
            return
        if threshold < 0 or threshold == 0:
            # 0 分钟在匹配阶段会被静默替换为默认值，为避免用户
            # 期望"仅精确匹配"却实际按 30 分钟匹配，与负数一样直接拒绝
            messagebox.showwarning(
                _("输入错误"),
                _("时间差阈值必须大于 0，已还原为上次有效值: ") + str(self._threshold_min_cache),
                parent=self.app.root)
            self.time_threshold.set(self._threshold_min_cache)
            self.app.release_processing()
            return
        self._threshold_min_cache = threshold
        with self.app.lock:
            t = threading.Thread(
                target=self._process_wrapper,
                args=(), daemon=True)
            self.app.current_thread = t
        self.update_ui_state(True)
        self.app.register_thread(t)
        t.start()

    def _extract_wrapper(self, folder):
        try:
            self.extract_image_info(folder, only_wpd=getattr(self, '_only_wpd_cache', False))
        except Exception as e:
            self.root_after(lambda e=e: messagebox.showerror(
                _("错误"), _("提取图像信息时出错: ") + str(e)))
        finally:
            self.root_after(lambda t=threading.current_thread(): self.update_ui_state(False, t))

    def _process_wrapper(self):
        try:
            self.process_location_info()
        except Exception as e:
            self.root_after(lambda e=e: messagebox.showerror(
                _("错误"), _("处理位置信息时出错: ") + str(e)))
        finally:
            self.root_after(lambda t=threading.current_thread(): self.update_ui_state(False, t))

    def root_after(self, callback):
        try:
            self.app.post_to_ui(callback)
        except Exception:
            log_exc()

    def show_messagebox(self, msg_type, title, message, parent=None):
        if parent is None:
            parent = self.app.root
        funcs = {
            'info': messagebox.showinfo,
            'warning': messagebox.showwarning,
            'error': messagebox.showerror,
            'askyesno': messagebox.askyesno,
        }
        return funcs.get(msg_type, messagebox.showinfo)(title, message, parent=parent)

    def extract_image_info(self, folder, only_wpd=False):
        self.root_after(lambda: self.result_text.config(state='normal'))
        self.root_after(lambda: self.result_text.insert(tk.END, "\n" + _("提取信息中...") + "\n"))
        self.root_after(lambda: self.result_text.see(tk.END))
        self.root_after(lambda: self.result_text.config(state='disabled'))

        # 节流：进度条按 1% 粒度更新，避免每个文件都排队 Tk 回调导致 UI 卡顿
        _last_pct = [None]
        _last_log_pct = [-1]

        def progress_callback(pct):
            p = int(pct)
            if _last_pct[0] != p or p >= 100:
                _last_pct[0] = p
                self.root_after(lambda: self.progress_var.set(p))

        def log_callback(done, total):
            # 日志按百分比节流（每 5% 或最后一次），万级文件时避免
            # 每个文件都往 UI 队列塞一条 lambda 造成主线程 Tcl 往返积压
            pct = int(done / total * 100)
            if pct > _last_log_pct[0] or done >= total:
                _last_log_pct[0] = pct
                self.root_after(lambda d=done, t=total: self.status_var.set(
                    _("迭代进度: ") + str(d) + "/" + str(t)))

        a_list, b_list, gps_data, total_scanned, skipped_count = scan_folder(
            folder, progress_callback,
            only_process_with_date=only_wpd,
            log_callback=log_callback)

        with self.app.lock:
            self.app.a.clear()
            self.app.a.extend(a_list)
            self.app.b.clear()
            self.app.b.extend(b_list)
            self.app.gps_data.clear()
            self.app.gps_data.extend(gps_data)
            self.app.initial_a_count = len(a_list)
            self.app.initial_b_count = len(b_list)
            self.app.processed_count = total_scanned

        self.root_after(lambda: self.progress_var.set(100))

        if gps_data:
            self.root_after(lambda: (
                self.result_text.config(state='normal'),
                self.result_text.insert(
                    tk.END, _("从GPX文件中解析出 ") + str(len(gps_data)) + _(" 个GPS轨迹点。") + "\n\n"),
                self.result_text.config(state='disabled'),
            ))

        total_parts = []
        if a_list:
            total_parts.append(_("有位置信息的图像示例:"))
            fi = a_list[0]
            time_str = fi.dt.strftime('%Y-%m-%d %H:%M:%S') if fi.dt and fi.dt != datetime.min else _('未知')
            lat_str = f"{fi.latitude:.8f}" if fi.latitude is not None else _('未知')
            lon_str = f"{fi.longitude:.8f}" if fi.longitude is not None else _('未知')
            alt_str = f"{fi.altitude:.2f}m" if fi.altitude is not None else _('未知')
            total_parts.append(f"  {fi.filename}: " + _("时间") + f"={time_str}, " + _("位置") + f"=({lat_str}, {lon_str}), " + _("高度") + f"={alt_str}")
            if len(a_list) > 1:
                total_parts.append("  ... " + _("还有") + str(len(a_list) - 1) + _(" 个文件"))
        if b_list:
            total_parts.append(_("没有位置信息的图像示例:"))
            fi = b_list[0]
            time_str = fi.dt.strftime('%Y-%m-%d %H:%M:%S') if fi.dt and fi.dt != datetime.min else _('未知')
            size_str = f"{fi.file_size / 1024 / 1024:.2f}MB" if fi.file_size else _('未知')
            total_parts.append(f"  {fi.filename}: " + _("时间") + f"={time_str}, " + _("大小") + f"={size_str}")
            if len(b_list) > 1:
                total_parts.append("  ... " + _("还有") + str(len(b_list) - 1) + _(" 个文件"))
        summary = _("===== 提取结果摘要 =====") + "\n"
        if only_wpd:
            summary += _("扫描的图像总数: ") + str(total_scanned) + "\n"
            summary += _("有原始日期的文件: ") + str(total_scanned - skipped_count) + "\n"
            summary += _("跳过的文件(无原始日期): ") + str(skipped_count) + "\n"
        else:
            summary += _("处理的图像总数: ") + str(total_scanned) + "\n"
        summary += _("有位置信息的图像: ") + str(len(a_list)) + "\n"
        summary += _("没有位置信息的图像: ") + str(len(b_list)) + "\n"
        if gps_data:
            summary += _("GPX轨迹点数: ") + str(len(gps_data)) + "\n"
        summary += "\n"
        summary += "\n".join(total_parts)
        if total_parts:
            summary += "\n"
        summary += "\n" + _("提示: 点击\"处理位置信息\"按钮为没有GPS坐标的图像分配位置信息。\n")
        summary += _("      点击\"显示结果\"按钮可查看或手动修改日期及GPS数据。\n")
        self.root_after(lambda: self.result_text.config(state='normal'))
        self.root_after(lambda: self.result_text.insert(tk.END, "\n\n" + summary))
        self.root_after(lambda: self.result_text.see(tk.END))
        self.root_after(lambda: self.result_text.config(state='disabled'))
        self.root_after(lambda: self.progress_var.set(100))
        self.root_after(lambda: self.status_var.set(_("迭代进度: ") + str(total_scanned) + "/" + str(total_scanned)))

    def process_location_info(self):
        # 互斥与按钮状态已由 start_process_thread 在启动时设置
        with self.app.lock:
            has_b = bool(self.app.b)
            has_a = bool(self.app.a)
            has_gps = bool(self.app.gps_data)
        if not has_b and not has_gps:
            self.root_after(lambda: self.show_messagebox("info", _("提示"), _("没有需要处理位置信息的图像和GPS数据")))
            return
        if not has_a and not has_gps:
            self.root_after(lambda: self.show_messagebox("info", _("提示"), _("没有可用于参考的位置信息")))
            return

        threshold = getattr(self, '_threshold_min_cache', DEFAULT_TIME_THRESHOLD_MINUTES)
        if threshold <= 0:
            threshold = DEFAULT_TIME_THRESHOLD_MINUTES

        self.root_after(lambda: self.result_text.config(state='normal'))
        self.root_after(lambda: self.result_text.delete(1.0, tk.END))
        self.root_after(
            lambda: self.result_text.insert(
                tk.END,
                _("正在处理位置信息...") + "\n\n"))
        self.root_after(lambda: self.result_text.see(tk.END))

        # 节流：进度按 1% 粒度、日志每 20 个文件更新一次
        _last_pct = [None]
        _log_count = [0]

        def progress_callback(pct, processed=0, total=0, label=None):
            p = int(pct)
            if _last_pct[0] != p or p >= 100:
                _last_pct[0] = p
                self.root_after(lambda: self.progress_var.set(p))
            if total > 0:
                _log_count[0] += 1
                if _log_count[0] % 20 == 0 or processed >= total:
                    text = (label or _('比对进度')) + _(": ") + str(processed) + "/" + str(total)
                    self.root_after(
                        lambda t=text: self.status_var.set(t))

        def iteration_callback(iteration, max_iter):
            self.root_after(
                lambda i=iteration, m=max_iter: self.status_var.set(
                    _("迭代 ") + str(i) + "/" + str(m)))
            self.root_after(
                lambda: self.result_text.insert(
                    tk.END, _("===== 迭代 ") + str(iteration) + "/" + str(max_iter) + _(" =====\n")))
            self.root_after(lambda: self.result_text.see(tk.END))

        # 处理阶段日志按 20 条合并成一批投递：写盘阶段对每个文件都会
        # 回调一次（万级文件会产生数万条 Tk 回调），逐条插入+see 会
        # 阻塞主线程且 Text 无限增长。合并后 Tk 往返减少 20 倍。
        _log_batch = []
        _LOG_BATCH_SIZE = 20

        def _flush_log_batch():
            if not _log_batch:
                return
            batch = "\n".join(_log_batch)
            del _log_batch[:]
            self.root_after(lambda b=batch: (
                self.result_text.insert(tk.END, b + "\n"),
                self.result_text.see(tk.END)))

        def log_callback(msg):
            _log_batch.append(msg)
            if len(_log_batch) >= _LOG_BATCH_SIZE:
                _flush_log_batch()

        updated, a_list, b_list = process_location_info(
            self.app.a, self.app.b, self.app.gps_data,
            threshold_minutes=threshold,
            progress_callback=progress_callback,
            iteration_callback=iteration_callback,
            log_callback=log_callback,
            lock=self.app.lock,
        )

        with self.app.lock:
            if a_list is not self.app.a:
                self.app.a.clear()
                self.app.a.extend(a_list)
            if b_list is not self.app.b:
                self.app.b.clear()
                self.app.b.extend(b_list)
            self.app.updated_count = updated

        # 收尾：把不足一满批的剩余日志一次性刷入界面
        _flush_log_batch()

        summary = "\n" + _("===== 处理完成 =====") + "\n"
        summary += _("更新文件总数: ") + str(updated) + "\n"
        summary += _("剩余没有位置信息的文件: ") + str(len(b_list)) + "\n"
        self.root_after(lambda: self.result_text.insert(tk.END, summary))
        self.root_after(lambda: self.result_text.see(tk.END))
        self.root_after(lambda: self.result_text.config(state='disabled'))
        self.root_after(lambda: self.progress_var.set(100))

    def show_location_in_map(self, file_info):
        if file_info.latitude is None or file_info.longitude is None:
            return
        import webbrowser
        from config import AMAP_URL, BMAP_URL, TMAP_URL, APPLE_MAPS_URL
        lat, lon = file_info.latitude, file_info.longitude
        system = _platform.system()
        map_label = urllib.parse.quote(_("图像拍摄位置"))

        if system == "Windows":
            urls = [
                APPLE_MAPS_URL.format(lat=lat, lon=lon),
                AMAP_URL.format(lon=lon, lat=lat, name=map_label),
                BMAP_URL.format(lat=lat, lon=lon, name=map_label),
                TMAP_URL.format(lat=lat, lon=lon, name=map_label),
            ]
        elif system == "Darwin":
            urls = [
                APPLE_MAPS_URL.format(lat=lat, lon=lon),
                AMAP_URL.format(lon=lon, lat=lat, name=map_label),
                BMAP_URL.format(lat=lat, lon=lon, name=map_label),
            ]
        else:
            urls = [
                APPLE_MAPS_URL.format(lat=lat, lon=lon),
                AMAP_URL.format(lon=lon, lat=lat, name=map_label),
                BMAP_URL.format(lat=lat, lon=lon, name=map_label),
            ]
        urls.append(f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=15/{lat}/{lon}")

        for url in urls:
            try:
                # webbrowser.open 失败时返回 False 而非抛异常：
                # 必须检查返回值，否则首条 URL 打不开时后续备选永远不会尝试
                if webbrowser.open(url):
                    return
            except Exception:
                log_exc()
                continue
        messagebox.showwarning(_("警告"), _("无法打开地图链接，请检查默认浏览器设置"),
                               parent=self.app.root)

    def show_results(self):
        if self.is_thread_running():
            messagebox.showwarning(_("处理中"), _("请等待当前任务完成"))
            return
        with self.app.lock:
            has_data = bool(self.app.a) or bool(self.app.b)
        if not has_data:
            self.show_messagebox("info", _("提示"), _("请先提取图像信息"))
            return

        if self.result_window:
            try:
                if self.result_window.window.winfo_exists():
                    self.result_window.refresh()
                    self.result_window.window.lift()
                    return
            except Exception:
                log_exc()

        from ui.results_window import ResultsWindow
        self.result_window = ResultsWindow(self)

    def export_results(self):
        if self.is_thread_running():
            messagebox.showwarning(_("处理中"), _("请等待当前任务完成"))
            return
        if getattr(self, '_exporting', False):
            messagebox.showwarning(_("处理中"), _("正在导出，请稍候"))
            return
        with self.app.lock:
            has_data = bool(self.app.a) or bool(self.app.b)
        if not has_data:
            self.show_messagebox("info", _("提示"), _("没有可导出的数据，请先提取图像信息"))
            return

        export_file = filedialog.asksaveasfilename(
            title=_("导出结果"),
            defaultextension=".gpx",
            filetypes=[
                (_("GPX文件"), "*.gpx"),
                (_("文本文件"), "*.txt"), (_("CSV文件"), "*.csv"),
                (_("JSON文件"), "*.json"),
                (_("所有文件"), "*.*"),
            ]
        )
        if not export_file:
            return

        # 主线程只弹保存对话框；写盘移入 worker 线程，避免万级文件时
        # 逐条写盘冻结 UI。快照在全局锁下拷贝，导出期间后台任务
        # 并发修改列表不影响本次导出内容的一致性
        with self.app.lock:
            a_snap = list(self.app.a)
            b_snap = list(self.app.b)
            gps_snap = list(self.app.gps_data)
            init_a = self.app.initial_a_count
            init_b = self.app.initial_b_count
            upd = self.app.updated_count
        ext = os.path.splitext(export_file)[1].lower()

        def worker():
            try:
                gps_data_list = [p.to_dict() if hasattr(p, 'to_dict') else p
                                for p in gps_snap]
                if ext == '.csv':
                    export_to_csv(export_file, a_snap, b_snap)
                elif ext == '.json':
                    export_to_json(export_file, a_snap, b_snap, gps_data_list)
                elif ext == '.gpx':
                    export_to_gpx(export_file, a_snap)
                else:
                    stats = generate_statistics(
                        a_snap, b_snap, gps_data_list,
                        initial_a_count=init_a,
                        initial_b_count=init_b,
                        updated_count=upd)
                    export_to_txt(export_file, a_snap, b_snap,
                                gps_data_list, stats)
                self.app.post_to_ui(lambda: self.show_messagebox(
                    "info", _("导出成功"),
                    _("结果已成功导出到:\n") + export_file))
            except Exception as e:
                log_exc()
                self.app.post_to_ui(lambda e=str(e): self.show_messagebox(
                    "error", _("导出失败"),
                    _("导出时发生错误:\n") + e))
            finally:
                # 导出结束恢复按钮；期间若有后台任务启动，
                # 按钮状态由 update_ui_state 统一管理，不覆盖
                self.app.post_to_ui(lambda: self._btn_export.config(
                    state="disabled" if self.is_thread_running() else "normal"))
                self.app.post_to_ui(lambda: setattr(self, '_exporting', False))

        self._exporting = True
        self._btn_export.config(state="disabled")
        t = threading.Thread(target=worker, daemon=True)
        self.app.register_thread(t)
        t.start()
