"""结果显示窗口（带标签页界面）"""

import tkinter as tk
from tkinter import ttk
from geo_media_tool.ui import custom_msgbox as messagebox
import platform
import os
import traceback
from datetime import datetime

from geo_media_tool.ui.dialogs import (
    EditCoordinatesDialog, BatchDateEditDialog,
    BatchLocationEditDialog, EditShootingDateDialog, GpxPointDetails
)
from geo_media_tool.utils.recycle_bin import send_to_recycle_bin
from geo_media_tool.utils.platform_utils import open_file_with_system, show_file_in_explorer
from geo_media_tool.services.export_service import (
    generate_statistics
)
from geo_media_tool.utils.i18n import _


class ResultsWindow:
    """处理结果展示窗口"""

    def __init__(self, geo_tab):
        self.geo_tab = geo_tab
        self.app = geo_tab.app
        self.window = tk.Toplevel(geo_tab.app.root)
        self.window.transient(geo_tab.app.root)
        self.window.title(_("处理结果详情"))
        ww, wh = 650, 600
        pw, ph = geo_tab.app.root.winfo_width(), geo_tab.app.root.winfo_height()
        px, py = geo_tab.app.root.winfo_rootx(), geo_tab.app.root.winfo_rooty()
        x = px + (pw - ww) // 2
        y = py + (ph - wh) // 2
        self.window.geometry(f"{ww}x{wh}+{x}+{y}")

        style = ttk.Style(self.window)
        from tkinter import font as _tkfont
        _default_font = _tkfont.nametofont("TkDefaultFont")
        style.configure("Treeview", rowheight=_default_font.metrics("linespace") + 4)

        self.notebook = ttk.Notebook(self.window)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 2))

        self._progress_tab_types = ('with_location', 'without_location')

        progress_frame = ttk.Frame(self.window)
        progress_frame.pack(fill=tk.X, padx=8, pady=(0, 8))

        self.progress_label = ttk.Label(progress_frame, text=_("就绪"))
        self.progress_label.pack(side=tk.LEFT, padx=(0, 5))

        self.progress_bar = ttk.Progressbar(
            progress_frame, orient=tk.HORIZONTAL, length=400,
            mode='determinate')
        self.progress_bar.pack(side=tk.RIGHT)

        def _resize_bar(event):
            if event.widget is self.window:
                bw = int(self.window.winfo_width() * 0.6)
                self.progress_bar.configure(length=max(bw, 100))
        self.window.bind('<Configure>', _resize_bar, add='+')

        self._tab_trees = {}
        self._tab_frames = {}
        self._tab_indices = {}

        self._tab_indices['with_location'] = len(self.notebook.tabs())
        self._setup_tab(self.notebook, _("有位置信息"), self.app.a, 'with_location')
        self._tab_indices['without_location'] = len(self.notebook.tabs())
        self._setup_tab(self.notebook, _("无位置信息"), self.app.b, 'without_location')
        self._tab_indices['gpx'] = len(self.notebook.tabs())
        self._setup_gpx_tab(self.notebook)
        self._tab_indices['stats'] = len(self.notebook.tabs())
        self._setup_stats_tab(self.notebook)

        self.notebook.bind('<<NotebookTabChanged>>', self._on_tab_changed)
        self._update_progress_visibility()

        def on_close():
            self.geo_tab.result_window = None
            try:
                self.window.destroy()
            except Exception:
                traceback.print_exc()
        self.window.protocol("WM_DELETE_WINDOW", on_close)

        self.window.update_idletasks()
        max_req = 0
        for tab_frame in self._tab_frames.values():
            try:
                max_req = max(max_req, tab_frame.winfo_reqwidth())
            except Exception:
                pass
        new_w = min(max(max_req + 40, 650), 1200)
        pw, ph = geo_tab.app.root.winfo_width(), geo_tab.app.root.winfo_height()
        px, py = geo_tab.app.root.winfo_rootx(), geo_tab.app.root.winfo_rooty()
        x = px + (pw - new_w) // 2
        y = py + (ph - wh) // 2
        self.window.geometry(f"{new_w}x{wh}+{x}+{y}")

    def _get_gpx_data_list(self):
        return [p.to_dict() if hasattr(p, 'to_dict') else p
                for p in self.app.gps_data]

    def _on_tab_changed(self, event=None):
        self._update_progress_visibility()

    def _update_progress_visibility(self):
        sel = self.notebook.select()
        if not sel:
            return
        frame = self.notebook.nametowidget(sel)
        tab_type = getattr(frame, '_tab_type', None)
        show = tab_type in self._progress_tab_types
        self.progress_label.master.pack_forget()
        if show:
            self.progress_label.master.pack(fill=tk.X, padx=8, pady=(0, 8))

    def _setup_tab(self, notebook, title, data_list, tab_type):
        frame = ttk.Frame(notebook)
        frame._tab_type = tab_type
        notebook.add(frame, text="{} ({})".format(title, len(data_list)))

        search_frame = ttk.Frame(frame)
        search_frame.pack(fill=tk.X, padx=5, pady=(0, 3))

        ttk.Label(search_frame, text=_("搜索:")).pack(side=tk.LEFT)
        search_var = tk.StringVar()
        ttk.Entry(search_frame, textvariable=search_var,
                  width=30).pack(side=tk.LEFT, padx=(5, 10))

        opts_frame = ttk.Frame(search_frame)
        opts_frame.pack(side=tk.LEFT)
        search_fn = tk.BooleanVar(value=True)
        search_tm = tk.BooleanVar(value=True)
        search_loc = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_frame, text=_("文件名"),
                        variable=search_fn).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Checkbutton(opts_frame, text=_("时间"),
                        variable=search_tm).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Checkbutton(opts_frame, text=_("位置"),
                        variable=search_loc).pack(side=tk.LEFT, padx=(0, 5))

        sort_column = None
        sort_reverse = False

        columns = ('seq', 'filename', 'time', 'loc', 'size')
        tree = ttk.Treeview(frame, columns=columns, show='headings', height=14,
                            selectmode='extended')

        headers = {'seq': _('序号'), 'filename': _('文件名'), 'time': _('拍摄时间'),
                   'loc': _('GPS坐标'), 'size': _('文件大小')}
        col_widths = {'seq': 45, 'filename': 130, 'time': 140, 'loc': 180, 'size': 65}

        def _get_sort_key(item, col):
            if col == 'seq':
                return 0
            if col == 'filename':
                return item.filename.lower() if item.filename else ''
            if col == 'time':
                return item.dt if item.dt else datetime.min
            if col == 'loc':
                if item.latitude is not None:
                    return (item.latitude, item.longitude)
                return (999, 999)
            if col == 'size':
                return item.file_size if item.file_size else 0
            return ''

        def update_sort_indicators():
            for col in columns:
                txt = headers[col]
                if col == sort_column:
                    ind = " ↓" if sort_reverse else " ↑"
                    tree.heading(col, text=txt + ind)
                else:
                    tree.heading(col, text=txt)

        def apply_current_sort():
            nonlocal filtered_data
            if sort_column:
                filtered_data.sort(key=lambda x: _get_sort_key(x, sort_column),
                                   reverse=sort_reverse)
            update_display()

        def on_sort_column(col):
            nonlocal sort_column, sort_reverse
            if sort_column == col:
                sort_reverse = not sort_reverse
            else:
                sort_column = col
                sort_reverse = False
            apply_current_sort()
            update_sort_indicators()

        for col in columns:
            tree.heading(col, text=headers[col],
                         command=lambda c=col: on_sort_column(c))
            tree.column(col, width=col_widths[col],
                        anchor=tk.CENTER if col in ('seq', 'time', 'size') else tk.W,
                        stretch=(col in ('filename', 'loc')))

        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)

        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0), pady=(0, 3))
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=(0, 3))

        original_data = sorted(data_list.copy(),
                               key=lambda x: x.dt if x.dt else datetime.min)
        filtered_data = original_data.copy()

        def update_display():
            for item in tree.get_children():
                tree.delete(item)
            for i, item in enumerate(filtered_data):
                time_str = item.dt.strftime('%Y-%m-%d %H:%M:%S') if item.dt and item.dt != datetime.min else _('未知时间')
                loc_str = _("无位置信息")
                if item.latitude is not None and item.longitude is not None:
                    loc_str = f"({item.latitude:.6f}, {item.longitude:.6f})"
                    if item.altitude is not None:
                        loc_str += f", {item.altitude:.2f}m"
                size_str = f"{item.file_size / 1024 / 1024:.2f} MB" if item.file_size else _("未知")
                tree.insert('', 'end', values=(i + 1, item.filename, time_str, loc_str, size_str))

        def perform_search(*args):
            text = search_var.get().lower().strip()
            filtered_data.clear()
            if not text:
                filtered_data.extend(original_data)
            else:
                for item in original_data:
                    match = False
                    if search_fn.get() and text in item.filename.lower():
                        match = True
                    if search_tm.get() and item.dt:
                        if text in item.dt.strftime('%Y-%m-%d %H:%M:%S').lower():
                            match = True
                    if search_loc.get() and item.latitude is not None:
                        if text in f"{item.latitude:.6f},{item.longitude:.6f}":
                            match = True
                    if match:
                        filtered_data.append(item)
            apply_current_sort()

        search_var.trace_add('write', perform_search)
        search_fn.trace_add('write', perform_search)
        search_tm.trace_add('write', perform_search)
        search_loc.trace_add('write', perform_search)
        update_display()

        def on_double_click(event):
            item_id = tree.selection()[0] if tree.selection() else None
            if item_id:
                idx = tree.index(item_id)
                if 0 <= idx < len(filtered_data):
                    EditCoordinatesDialog(
                        self.app, filtered_data[idx], tree, item_id,
                        self.window, self)

        tree.bind('<Double-1>', on_double_click)

        def show_context_menu(event):
            item_id = tree.identify_row(event.y)
            if not item_id:
                return
            sel = tree.selection()
            if item_id not in sel:
                tree.selection_set(item_id)
                sel = [item_id]

            menu = tk.Menu(tree, tearoff=0)

            if len(sel) == 1:
                idx = tree.index(sel[0])
                if 0 <= idx < len(filtered_data):
                    fi = filtered_data[idx]
                    menu.add_command(label=_("打开"),
                                     command=lambda: open_file_with_system(fi.path))
                    if fi.latitude is not None and fi.longitude is not None:
                        menu.add_command(
                            label=_("在地图中显示位置"),
                            command=lambda: self.geo_tab.show_location_in_map(fi))
                    else:
                        menu.add_command(
                            label=_("在地图上选择位置"),
                            command=lambda fi=fi: self._open_edit_with_map(
                                fi, tree, sel[0]))
                    menu.add_separator()
                    menu.add_command(
                        label=_("显示文件位置"),
                        command=lambda: show_file_in_explorer(fi.path))
                    menu.add_command(
                        label=_("编辑位置信息"),
                        command=lambda fi=fi: EditCoordinatesDialog(
                            self.app, fi, tree, sel[0], self.window, self))
                    menu.add_command(
                        label=_("编辑拍摄日期"),
                        command=lambda: EditShootingDateDialog(
                            self.app, fi, tree, sel[0], self.window, self))
                    menu.add_separator()
                    menu.add_command(
                        label=_("从序列中删除"),
                        command=lambda: self._remove_items(tree, sel, filtered_data))
                    menu.add_command(
                        label=_("从磁盘中删除"),
                        command=lambda: self._delete_items(tree, sel, filtered_data))
            else:
                menu.add_command(label=_("从序列中删除"),
                                 command=lambda: self._remove_items(tree, sel, filtered_data))
                menu.add_command(label=_("从磁盘中删除"),
                                 command=lambda: self._delete_items(tree, sel, filtered_data))
                menu.add_separator()
                menu.add_command(
                    label=_("批量修改拍摄日期"),
                    command=lambda: BatchDateEditDialog(
                        self.app, [filtered_data[tree.index(s)] for s in sel], tree))
                menu.add_command(
                    label=_("批量修改位置信息"),
                    command=lambda: BatchLocationEditDialog(
                        self.app, [filtered_data[tree.index(s)] for s in sel], tree))

            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        if platform.system() == "Darwin":
            tree.bind("<Button-2>", show_context_menu)
            tree.bind("<Control-Button-1>", show_context_menu)
            tree.bind("<Button-3>", show_context_menu)
        else:
            tree.bind("<Button-3>", show_context_menu)

        def sync_search(data_snapshot=None):
            nonlocal original_data
            if data_snapshot is not None:
                original_data = sorted(data_snapshot,
                                       key=lambda x: x.dt if x.dt else datetime.min)
            elif tab_type == 'with_location':
                with self.app.lock:
                    original_data = sorted(list(self.app.a),
                                           key=lambda x: x.dt if x.dt else datetime.min)
            else:
                with self.app.lock:
                    original_data = sorted(list(self.app.b),
                                           key=lambda x: x.dt if x.dt else datetime.min)
            perform_search()

        tree.sync_search = sync_search
        self._tab_trees[tab_type] = tree

    def _remove_items(self, tree, selected_items, filtered_data):
        items = []
        for item_id in selected_items:
            idx = tree.index(item_id)
            if 0 <= idx < len(filtered_data):
                items.append(filtered_data[idx])

        if not items:
            return

        if not messagebox.askyesno(_("确认删除"),
                                    _("确定要从序列中删除 ") + str(len(items)) + _(" 个文件吗？")):
            return

        with self.app.lock:
            for item in items:
                if item in self.app.a:
                    self.app.a.remove(item)
                if item in self.app.b:
                    self.app.b.remove(item)

        self.refresh()
        self.progress_label.config(text="")
        self.progress_bar['value'] = 25
        self.window.after(50, lambda: self.progress_bar.config(value=50))
        self.window.after(100, lambda: self.progress_bar.config(value=75))
        self.window.after(150, lambda: (self.progress_bar.config(value=100),
                                         self.progress_label.config(text=_("已从序列中删除 ") + str(len(items)) + _(" 个文件"))))

    def _delete_items(self, tree, selected_items, filtered_data):
        items = []
        for item_id in selected_items:
            idx = tree.index(item_id)
            if 0 <= idx < len(filtered_data):
                items.append(filtered_data[idx])

        if not items:
            return

        count = len(items)
        if count > 1:
            msg = _("确定要将这 ") + str(count) + _(" 个文件移至回收站吗？")
        else:
            msg = _("确定要将这个文件移至回收站吗？")
        if not messagebox.askyesno(_("确认移至回收站"), msg):
            return

        paths = [i.path for i in items if hasattr(i, 'path')]
        success, failed = send_to_recycle_bin(paths)

        with self.app.lock:
            for item in items:
                if item in self.app.a:
                    self.app.a.remove(item)
                if item in self.app.b:
                    self.app.b.remove(item)

        self.refresh()
        self.progress_label.config(text="")
        self.progress_bar['value'] = 25
        self.window.after(50, lambda: self.progress_bar.config(value=50))
        self.window.after(100, lambda: self.progress_bar.config(value=75))
        self.window.after(150, lambda s=success: (self.progress_bar.config(value=100),
                                                    self.progress_label.config(text=_("已将 ") + str(s) + _(" 个文件移至回收站"))))

    def _open_edit_with_map(self, fi, tree, item_id):
        edit = EditCoordinatesDialog(self.app, fi, tree, item_id, self.window, self)
        edit._map_selector()

    def refresh(self):
        try:
            with self.app.lock:
                a_list = list(self.app.a)
                b_list = list(self.app.b)
                gps_data = list(self.app.gps_data) if hasattr(self.app, 'gps_data') else []

            idx_a = self._tab_indices.get('with_location', 0)
            idx_b = self._tab_indices.get('without_location', 1)
            idx_gpx = self._tab_indices.get('gpx', 2)
            if idx_a < len(self.notebook.tabs()):
                self.notebook.tab(idx_a, text=_("有位置信息 (") + str(len(a_list)) + _(")"))
            if idx_b < len(self.notebook.tabs()):
                self.notebook.tab(idx_b, text=_("无位置信息 (") + str(len(b_list)) + _(")"))
            if idx_gpx < len(self.notebook.tabs()):
                self.notebook.tab(idx_gpx, text=_("GPX轨迹数据 (") + str(len(gps_data)) + _(")"))

            for tab_type in ('with_location', 'without_location'):
                tree = self._tab_trees.get(tab_type)
                if tree and hasattr(tree, 'sync_search'):
                    try:
                        snapshot = a_list if tab_type == 'with_location' else b_list
                        tree.sync_search(data_snapshot=snapshot)
                    except Exception:
                        traceback.print_exc()

            gpx_tree = self._tab_trees.get('gpx')
            if gpx_tree:
                try:
                    for item in gpx_tree.get_children():
                        gpx_tree.delete(item)
                    gpx_data_list = [p.to_dict() if hasattr(p, 'to_dict') else p
                                    for p in gps_data]
                    for i, point in enumerate(gpx_data_list):
                        time_str = point['datetime'].strftime('%Y-%m-%d %H:%M:%S') if point.get('datetime') else _('未知')
                        lat_str = f"{point['latitude']:.6f}" if point.get('latitude') is not None else _('未知')
                        lon_str = f"{point['longitude']:.6f}" if point.get('longitude') is not None else _('未知')
                        alt_str = f"{point['altitude']:.2f}" if point.get('altitude') is not None else _('未知')
                        src = point.get('source_file', _('未知'))
                        gpx_tree.insert('', 'end', values=(i + 1, src, time_str, lat_str, lon_str, alt_str))
                except Exception:
                    traceback.print_exc()

            stats_text = self._tab_trees.get('stats')
            if stats_text:
                try:
                    stats_text.config(state=tk.NORMAL)
                    stats_text.delete(1.0, tk.END)
                    gpx_data_list = [p.to_dict() if hasattr(p, 'to_dict') else p
                                    for p in gps_data]
                    stats = generate_statistics(
                        a_list, b_list, gpx_data_list,
                        initial_a_count=self.app.initial_a_count,
                        initial_b_count=self.app.initial_b_count,
                        updated_count=self.app.updated_count)
                    stats_text.insert(tk.END, stats)
                    stats_text.config(state=tk.DISABLED)
                except Exception:
                    traceback.print_exc()
        except Exception:
            traceback.print_exc()
            try:
                self.window.destroy()
            except Exception:
                traceback.print_exc()
            self.geo_tab.result_window = None
            self.geo_tab.show_results()

    def _setup_gpx_tab(self, notebook):
        frame = ttk.Frame(notebook)
        frame._tab_type = 'gpx'
        notebook.add(frame, text=_("GPX轨迹数据 (") + str(len(self.app.gps_data)) + _(")"))

        if not self.app.gps_data:
            ttk.Label(frame, text=_("没有GPX轨迹数据\n\n请确保所选文件夹中包含.gpx格式的轨迹文件"),
                     font=('', 12), justify=tk.CENTER).pack(expand=True)
            return

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=tk.X, padx=5, pady=(3, 2))
        self._gpx_count_label = ttk.Label(toolbar, text=_("共 ") + str(len(self.app.gps_data)) + _(" 个轨迹点"))
        self._gpx_count_label.pack(side=tk.LEFT)

        gpx_data_list = self._get_gpx_data_list()

        columns = ('seq', 'gpx_file', 'time', 'lat', 'lon', 'alt')
        tree = ttk.Treeview(frame, columns=columns, show='headings', height=14,
                            selectmode='extended')
        tree.heading('seq', text=_('序号'))
        tree.column('seq', width=55, anchor=tk.CENTER, stretch=False)
        tree.heading('gpx_file', text=_('来源文件'))
        tree.column('gpx_file', width=120)
        tree.heading('time', text=_('记录时间'))
        tree.column('time', width=130, anchor=tk.CENTER, stretch=False)
        tree.heading('lat', text=_('纬度'))
        tree.column('lat', width=90, anchor=tk.CENTER, stretch=False)
        tree.heading('lon', text=_('经度'))
        tree.column('lon', width=90, anchor=tk.CENTER, stretch=False)
        tree.heading('alt', text=_('高度(米)'))
        tree.column('alt', width=70, anchor=tk.CENTER, stretch=False)

        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0), pady=(0, 3))
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=(0, 3))

        for i, point in enumerate(gpx_data_list):
            time_str = point['datetime'].strftime('%Y-%m-%d %H:%M:%S') if point.get('datetime') else _('未知')
            lat_str = f"{point['latitude']:.6f}" if point.get('latitude') is not None else _('未知')
            lon_str = f"{point['longitude']:.6f}" if point.get('longitude') is not None else _('未知')
            alt_str = f"{point['altitude']:.2f}" if point.get('altitude') is not None else _('未知')
            src = point.get('source_file', _('未知'))
            tree.insert('', 'end', values=(i + 1, src, time_str, lat_str, lon_str, alt_str))

        self._tab_trees['gpx'] = tree
        self._tab_frames['gpx'] = frame

        def show_gpx_context_menu(event):
            item_id = tree.identify_row(event.y)
            if not item_id:
                return
            sel = tree.selection()
            if item_id not in sel:
                tree.selection_set(item_id)
                sel = [item_id]

            menu = tk.Menu(tree, tearoff=0)

            if len(sel) == 1:
                idx = tree.index(sel[0])
                if 0 <= idx < len(gpx_data_list):
                    point = gpx_data_list[idx]
                    menu.add_command(label=_("查看详情"),
                                     command=lambda: GpxPointDetails(self.app, point, self.geo_tab))
                    if point.get('latitude') is not None and point.get('longitude') is not None:
                        menu.add_command(label=_("在地图中显示"),
                                         command=lambda: self.geo_tab.show_location_in_map(
                                             type('obj', (object,), {
                                                 'latitude': point['latitude'],
                                                 'longitude': point['longitude'],
                                                 'altitude': point.get('altitude'),
                                             })()))
                        menu.add_separator()
                        menu.add_command(label=_("复制坐标"),
                                         command=lambda: self._copy_gpx_coords(point))
                    menu.add_separator()
                    menu.add_command(label=_("导出此点"),
                                     command=lambda: self._export_gpx_point(point))
                    menu.add_command(label=_("删除此点"),
                                     command=lambda: self._remove_gpx_points(
                                         tree, sel, gpx_data_list))
            else:
                selected_points = []
                for s in sel:
                    idx = tree.index(s)
                    if 0 <= idx < len(gpx_data_list):
                        selected_points.append(gpx_data_list[idx])
                menu.add_command(
                    label=_("轨迹点统计"),
                    command=lambda pts=selected_points: self._show_gpx_stats(pts))
                menu.add_command(
                    label=_("导出轨迹点"),
                    command=lambda pts=selected_points: self._export_gpx_point(pts))
                menu.add_separator()
                menu.add_command(
                    label=_("删除轨迹点"),
                    command=lambda pts=selected_points: self._remove_gpx_points(
                        tree, sel, gpx_data_list))

            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        def on_gpx_double_click(event):
            item_id = tree.identify_row(event.y)
            if not item_id:
                return
            idx = tree.index(item_id)
            if 0 <= idx < len(gpx_data_list):
                GpxPointDetails(self.app, gpx_data_list[idx], self.geo_tab)

        tree.bind('<Double-1>', on_gpx_double_click)

        if platform.system() == "Darwin":
            tree.bind("<Button-2>", show_gpx_context_menu)
            tree.bind("<Control-Button-1>", show_gpx_context_menu)
            tree.bind("<Button-3>", show_gpx_context_menu)
        else:
            tree.bind("<Button-3>", show_gpx_context_menu)

    def _copy_gpx_coords(self, point):
        lat, lon = point.get('latitude'), point.get('longitude')
        alt = point.get('altitude')
        text = f"{lon}, {lat}" + (f", {alt}" if alt is not None else "")
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(text)
        messagebox.showinfo(_("已复制"), _("坐标已复制到剪贴板:\n") + text)

    def _export_gpx_point(self, point):
        from tkinter import filedialog
        fp = filedialog.asksaveasfilename(
            title=_("导出轨迹点为GPX航点"),
            defaultextension=".gpx",
            filetypes=[(_("GPX文件"), "*.gpx"), (_("所有文件"), "*.*")])
        if not fp:
            return
        try:
            from geo_media_tool.utils.gpx_utils import create_gpx_element, prettify_xml
            pts = [point] if not isinstance(point, list) else point
            root = create_gpx_element(pts, _("图像位置信息导出"))
            with open(fp, 'w', encoding='utf-8', newline='') as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                f.write(prettify_xml(root))
            messagebox.showinfo(_("导出成功"), _("已导出到:\n") + fp)
        except Exception:
            traceback.print_exc()
            messagebox.showerror(_("导出失败"), _("导出过程中发生错误，请检查文件路径和权限。"))

    def _show_gpx_stats(self, points):
        count = len(points)
        times = [p['datetime'] for p in points if p.get('datetime')]
        lats = [p['latitude'] for p in points if p.get('latitude') is not None]
        lons = [p['longitude'] for p in points if p.get('longitude') is not None]
        alts = [p['altitude'] for p in points if p.get('altitude') is not None]

        lines = [_("选中轨迹点: ") + str(count)]
        if times:
            lines.append(_("时间范围: ") + min(times).strftime('%Y-%m-%d %H:%M:%S') + _(" 到 ") + max(times).strftime('%Y-%m-%d %H:%M:%S'))
        if lats and lons:
            lines.append(_("纬度范围: ") + f"{min(lats):.6f}" + _(" 到 ") + f"{max(lats):.6f}")
            lines.append(_("经度范围: ") + f"{min(lons):.6f}" + _(" 到 ") + f"{max(lons):.6f}")
        if alts:
            lines.append(_("高度范围: ") + f"{min(alts):.2f}" + _(" 到 ") + f"{max(alts):.2f}" + _(" 米"))
        messagebox.showinfo(_("轨迹点统计"), "\n".join(lines))

    def _remove_gpx_points(self, tree, selected_items, gpx_data_list):
        count = len(selected_items)
        if not messagebox.askyesno(_("确认删除"), _("确定要从列表中删除这 ") + str(count) + _(" 个轨迹点吗？\n（不会删除原始 GPX 文件）")):
            return
        selected_keys = set()
        for s in selected_items:
            if not tree.exists(s):
                continue
            vals = tree.item(s, 'values')
            if vals and len(vals) >= 6:
                selected_keys.add((vals[2], vals[3], vals[4]))

        def _matches(d):
            t = d.get('datetime', '')
            if hasattr(t, 'strftime'):
                t = t.strftime('%Y-%m-%d %H:%M:%S')
            lat = f"{d.get('latitude', ''):.6f}" if d.get('latitude') is not None else ''
            lon = f"{d.get('longitude', ''):.6f}" if d.get('longitude') is not None else ''
            return (t, lat, lon) in selected_keys

        indices_to_del = [i for i, d in enumerate(gpx_data_list) if _matches(d)]
        for idx in reversed(indices_to_del):
            del gpx_data_list[idx]
        with self.app.lock:
            for idx in reversed(indices_to_del):
                if 0 <= idx < len(self.app.gps_data):
                    del self.app.gps_data[idx]
        for item in selected_items:
            try:
                tree.delete(item)
            except Exception:
                traceback.print_exc()
        for i, item in enumerate(tree.get_children()):
            vals = list(tree.item(item, 'values'))
            if vals:
                vals[0] = i + 1
                tree.item(item, values=tuple(vals))
        self._gpx_count_label.config(text=_("共 ") + str(len(gpx_data_list)) + _(" 个轨迹点"))
        tab_text = _("GPX轨迹数据 (") + str(len(gpx_data_list)) + _(")")
        gpx_idx = self._tab_indices.get('gpx', 2)
        if gpx_idx < len(self.notebook.tabs()):
            self.notebook.tab(gpx_idx, text=tab_text)

    def _setup_stats_tab(self, notebook):
        from tkinter import scrolledtext
        frame = ttk.Frame(notebook)
        frame._tab_type = 'stats'
        notebook.add(frame, text=_("统计信息"))

        text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, width=70, height=24)
        text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._tab_trees['stats'] = text

        def refresh_stats():
            gpx_data_list = self._get_gpx_data_list()
            stats = generate_statistics(self.app.a, self.app.b, gpx_data_list,
                                        initial_a_count=self.app.initial_a_count,
                                        initial_b_count=self.app.initial_b_count,
                                        updated_count=self.app.updated_count)
            text.config(state=tk.NORMAL)
            text.delete(1.0, tk.END)
            text.insert(tk.END, stats)
            text.config(state=tk.DISABLED)

        def on_tab_change(event):
            stats_idx = self._tab_indices.get('stats', 3)
            if notebook.index(notebook.select()) == stats_idx:
                refresh_stats()

        notebook.bind('<<NotebookTabChanged>>', on_tab_change)
        refresh_stats()
