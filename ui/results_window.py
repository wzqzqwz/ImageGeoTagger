"""结果显示窗口（带标签页界面）"""

import tkinter as tk
from tkinter import ttk
from ui import custom_msgbox as messagebox
import platform
import threading
from datetime import datetime

from ui.dialogs import (
    EditCoordinatesDialog, BatchDateEditDialog,
    BatchLocationEditDialog, EditShootingDateDialog, GpxPointDetails
)
from utils.recycle_bin import send_to_recycle_bin
from utils.platform_utils import open_file_with_system, show_file_in_explorer
from services.export_service import (
    generate_statistics
)
from ui.tk_safe import pulse_progress, safe_after
from utils.i18n import _
from utils.logging_utils import log_exc
from utils.media_utils import format_gps_coord


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
                log_exc()
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
        # 切换到统计页时刷新统计（数据在编辑/删除后可能已变化）
        sel = self.notebook.select()
        if sel:
            try:
                frame = self.notebook.nametowidget(sel)
                refresh = getattr(frame, '_refresh_stats', None)
                if refresh:
                    refresh()
            except Exception:
                log_exc()

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
            if sort_column:
                filtered_data.sort(key=lambda x: _get_sort_key(x, sort_column),
                                   reverse=sort_reverse)
            update_display()

        def on_sort_column(col):
            nonlocal sort_column, sort_reverse
            # 序号列没有稳定排序键（_get_sort_key 恒为 0），
            # 点击只会抖动行序，直接忽略
            if col == 'seq':
                return
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

        def _format_row(i, item):
            time_str = item.dt.strftime('%Y-%m-%d %H:%M:%S') if item.dt and item.dt != datetime.min else _('未知时间')
            loc_str = _("无位置信息")
            if item.latitude is not None and item.longitude is not None:
                loc_str = f"({format_gps_coord(item.latitude)}, {format_gps_coord(item.longitude)})"
                if item.altitude is not None:
                    loc_str += f", {format_gps_coord(item.altitude)}m"
            size_str = f"{item.file_size / 1024 / 1024:.2f} MB" if item.file_size else _("未知")
            return (i + 1, item.filename, time_str, loc_str, size_str)

        def update_display():
            # 行级增量更新：仅更新值/新增/删除尾部行，避免大列表时整树重建
            children = tree.get_children()
            n_old = len(children)
            n_new = len(filtered_data)
            common = min(n_old, n_new)
            for i in range(common):
                tree.item(children[i], values=_format_row(i, filtered_data[i]))
            for i in range(common, n_new):
                tree.insert('', 'end', values=_format_row(i, filtered_data[i]))
            for i in range(common, n_old):
                tree.delete(children[i])

        search_job = [None]

        def do_search():
            search_job[0] = None
            # 防抖期间窗口可能已被关闭：先确认 tree 存活，
            # 避免在已销毁的 Treeview 上执行 Tcl 调用抛 TclError
            try:
                if not tree.winfo_exists():
                    return
            except Exception:
                return
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
                        if text in f"{format_gps_coord(item.latitude)},{format_gps_coord(item.longitude)}":
                            match = True
                    if match:
                        filtered_data.append(item)
            apply_current_sort()

        def perform_search(*args):
            # 防抖：连续输入时延迟 150ms 再执行过滤，避免大列表每次按键全量扫描
            if search_job[0] is not None:
                try:
                    tree.after_cancel(search_job[0])
                except Exception:
                    pass
            # safe_after：结果窗口已关闭时静默跳过注册与触发；
            # 不复用同步回退路径（窗口销毁时同步执行同样会抛 TclError）
            search_job[0] = safe_after(tree, 150, do_search)

        search_var.trace_add('write', perform_search)
        search_fn.trace_add('write', perform_search)
        search_tm.trace_add('write', perform_search)
        search_loc.trace_add('write', perform_search)
        update_display()

        def on_double_click(event):
            item_id = tree.selection()[0] if tree.selection() else None
            if not item_id:
                return
            try:
                idx = tree.index(item_id)
            except tk.TclError:
                # 行已随刷新失效：跳过，避免双击时崩溃
                return
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
                try:
                    idx = tree.index(sel[0])
                except tk.TclError:
                    # 行已随刷新失效：菜单无可用目标，直接返回
                    return
                if 0 <= idx < len(filtered_data):
                    fi = filtered_data[idx]
                    # 菜单创建时解析为文件对象：点击菜单时若列表已刷新，
                    # 旧 iid 可能指向另一个文件，删除/编辑会误伤
                    sel_objs = [fi]
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
                        command=lambda objs=sel_objs: self._remove_items(tree, objs, filtered_data))
                    menu.add_command(
                        label=_("从磁盘中删除"),
                        command=lambda objs=sel_objs: self._delete_items(tree, objs, filtered_data))
            else:
                # 菜单创建时一次性解析文件对象：sel 里的 iid 可能在后续
                # 增删/刷新后失效（tree.index 抛 TclError），延迟到点击时
                # 再解析会导致对象列表为空或错位
                sel_objs = self._resolve_items(tree, sel, filtered_data)
                if not sel_objs:
                    return
                menu.add_command(label=_("从序列中删除"),
                                 command=lambda objs=sel_objs: self._remove_items(tree, objs, filtered_data))
                menu.add_command(label=_("从磁盘中删除"),
                                 command=lambda objs=sel_objs: self._delete_items(tree, objs, filtered_data))
                menu.add_separator()
                menu.add_command(
                    label=_("批量修改拍摄日期"),
                    command=lambda objs=sel_objs: BatchDateEditDialog(
                        self.app, objs, tree))
                menu.add_command(
                    label=_("批量修改位置信息"),
                    command=lambda objs=sel_objs: BatchLocationEditDialog(
                        self.app, objs, tree))

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

    def _resolve_items(self, tree, selected_items, filtered_data):
        items = []
        for item_id in selected_items:
            try:
                idx = tree.index(item_id)
            except tk.TclError:
                # 行已随上次刷新失效：跳过，不再让整个操作静默失败
                continue
            if 0 <= idx < len(filtered_data):
                items.append(filtered_data[idx])
        return items

    def _remove_items(self, tree, selected_items, filtered_data):
        # selected_items 已由右键菜单在创建时解析为文件对象
        # （菜单点击时 iid 可能已随刷新失效，不能再按 iid 解析）
        items = selected_items

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
        pulse_progress(self.window, self.progress_bar, self.progress_label,
                       _("已从序列中删除 ") + str(len(items)) + _(" 个文件"))

    def _delete_items(self, tree, selected_items, filtered_data):
        # selected_items 已由右键菜单在创建时解析为文件对象，
        # 不再按 iid 二次解析（菜单点击时 iid 可能已失效）
        items = selected_items

        if not items:
            return

        count = len(items)
        if count > 1:
            msg = _("确定要将这 ") + str(count) + _(" 个文件移至回收站吗？")
        else:
            msg = _("确定要将这个文件移至回收站吗？")
        if not messagebox.askyesno(_("确认移至回收站"), msg):
            return

        # 全局互斥：防止与后台 GEO/日期处理正写盘时移动文件导致数据损坏
        if not self.app.acquire_processing():
            messagebox.showwarning(_("警告"), _("其他任务正在处理中，请等待完成"), parent=self.window)
            return
        # 去重：同一路径被选中多次时只删除一次，避免重复删除误报失败
        seen = set()
        paths = []
        for i in items:
            p = i.path if hasattr(i, 'path') else None
            if p is not None and p not in seen:
                seen.add(p)
                paths.append(p)

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
                lambda s=success, f=failed: self._apply_delete_results(items, s, f))

        thread = threading.Thread(target=worker, daemon=True)
        self.app.register_thread(thread)
        thread.start()

    def _apply_delete_results(self, items, success, failed):
        try:
            failed_paths = set(p for p, _ in failed) if failed else set()

            with self.app.lock:
                for item in items:
                    # 只有删除成功的文件才从列表中移除，失败的文件保留以便重试
                    if getattr(item, 'path', None) in failed_paths:
                        continue
                    if item in self.app.a:
                        self.app.a.remove(item)
                    if item in self.app.b:
                        self.app.b.remove(item)

            self.refresh()
            self.progress_label.config(text="")
            self.progress_bar['value'] = 25
            pulse_progress(self.window, self.progress_bar, self.progress_label,
                           _("已将 ") + str(success) + _(" 个文件移至回收站"))
        finally:
            self.app.release_processing()

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
                        log_exc()

            gpx_tree = self._tab_trees.get('gpx')
            if gpx_tree:
                try:
                    for item in gpx_tree.get_children():
                        gpx_tree.delete(item)
                    gpx_data_list = [p.to_dict() if hasattr(p, 'to_dict') else p
                                    for p in gps_data]
                    state = getattr(gpx_tree, '_gpx_state', None)
                    if state is not None:
                        # data/indices 均从同一 gps_data 快照重建，保持平行
                        state['data'] = gpx_data_list
                        state['indices'] = list(range(len(gps_data)))
                    for i, point in enumerate(gpx_data_list):
                        time_str = point['datetime'].strftime('%Y-%m-%d %H:%M:%S') if point.get('datetime') else _('未知')
                        lat_str = format_gps_coord(point['latitude']) if point.get('latitude') is not None else _('未知')
                        lon_str = format_gps_coord(point['longitude']) if point.get('longitude') is not None else _('未知')
                        alt_str = format_gps_coord(point['altitude']) if point.get('altitude') is not None else _('未知')
                        src = point.get('source_file', _('未知'))
                        gpx_tree.insert('', 'end', values=(i + 1, src, time_str, lat_str, lon_str, alt_str))
                except Exception:
                    log_exc()

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
                    log_exc()
        except Exception:
            log_exc()
            # 刷新失败时保留窗口，避免丢失用户当前视图；
            # 不销毁窗口也不清空 result_window，后续刷新或用户操作仍可继续

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

        # 用可变容器保存当前轨迹点快照，refresh() 重建列表后更新它，
        # 使右键菜单/双击等闭包始终引用最新数据。
        # indices 与 data 平行，记录 data[i] 对应 self.app.gps_data 的下标，
        # 删除时按行号精确定位，避免同时间同坐标的多个点按键值匹配误删
        gpx_state = {'data': self._get_gpx_data_list(),
                     'indices': list(range(len(self.app.gps_data)))}

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

        for i, point in enumerate(gpx_state['data']):
            time_str = point['datetime'].strftime('%Y-%m-%d %H:%M:%S') if point.get('datetime') else _('未知')
            lat_str = format_gps_coord(point['latitude']) if point.get('latitude') is not None else _('未知')
            lon_str = format_gps_coord(point['longitude']) if point.get('longitude') is not None else _('未知')
            alt_str = format_gps_coord(point['altitude']) if point.get('altitude') is not None else _('未知')
            src = point.get('source_file', _('未知'))
            tree.insert('', 'end', values=(i + 1, src, time_str, lat_str, lon_str, alt_str))

        self._tab_trees['gpx'] = tree
        self._tab_frames['gpx'] = frame
        tree._gpx_state = gpx_state

        def show_gpx_context_menu(event):
            item_id = tree.identify_row(event.y)
            if not item_id:
                return
            sel = tree.selection()
            if item_id not in sel:
                tree.selection_set(item_id)
                sel = [item_id]

            gpx_data_list = gpx_state['data']
            menu = tk.Menu(tree, tearoff=0)

            if len(sel) == 1:
                try:
                    idx = tree.index(sel[0])
                except tk.TclError:
                    # 行已随刷新失效：菜单无可用目标，直接返回
                    return
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
                                         tree, sel, gpx_state))
            else:
                selected_points = []
                for s in sel:
                    try:
                        idx = tree.index(s)
                    except tk.TclError:
                        continue
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
                        tree, sel, gpx_state))

            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        def on_gpx_double_click(event):
            item_id = tree.identify_row(event.y)
            if not item_id:
                return
            idx = tree.index(item_id)
            gpx_data_list = gpx_state['data']
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
            from utils.gpx_utils import create_gpx_element, prettify_xml
            pts = [point] if not isinstance(point, list) else point
            root = create_gpx_element(pts, _("图像位置信息导出"))
            with open(fp, 'w', encoding='utf-8', newline='') as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                f.write(prettify_xml(root))
            messagebox.showinfo(_("导出成功"), _("已导出到:\n") + fp)
        except Exception:
            log_exc()
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
            lines.append(_("纬度范围: ") + f"{min(lats):.8f}" + _(" 到 ") + f"{max(lats):.8f}")
            lines.append(_("经度范围: ") + f"{min(lons):.8f}" + _(" 到 ") + f"{max(lons):.8f}")
        if alts:
            lines.append(_("高度范围: ") + f"{min(alts):.2f}" + _(" 到 ") + f"{max(alts):.2f}" + _(" 米"))
        messagebox.showinfo(_("轨迹点统计"), "\n".join(lines))

    def _remove_gpx_points(self, tree, selected_items, gpx_state):
        gpx_data_list = gpx_state['data']
        indices = gpx_state['indices']
        count = len(selected_items)
        if not messagebox.askyesno(_("确认删除"), _("确定要从列表中删除这 ") + str(count) + _(" 个轨迹点吗？\n（不会删除原始 GPX 文件）")):
            return
        # 按行号定位选中项：树项存在即行号有效，行号对应 gpx_state 中的
        # data/indices。同时间同坐标的多个点（如停车记录）按键值匹配会
        # 删掉第一个而非用户选中的那一行，行号定位无此歧义。
        selected_idx = []
        for s in selected_items:
            if not tree.exists(s):
                continue
            idx = tree.index(s)
            if 0 <= idx < len(gpx_data_list):
                selected_idx.append(idx)

        # 降序删除：先删大下标，避免删除后下标前移错位
        with self.app.lock:
            for idx in sorted(set(selected_idx), reverse=True):
                if idx < len(indices):
                    live = indices[idx]
                    del gpx_data_list[idx]
                    del indices[idx]
                    if 0 <= live < len(self.app.gps_data):
                        del self.app.gps_data[live]
        for item in selected_items:
            try:
                tree.delete(item)
            except Exception:
                log_exc()
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
            # 与 refresh() 一致，在锁内快照列表，避免处理期间列表被修改
            with self.app.lock:
                a_list = list(self.app.a)
                b_list = list(self.app.b)
                gps_snapshot = list(self.app.gps_data)
            gpx_data_list = [p.to_dict() if hasattr(p, 'to_dict') else p
                             for p in gps_snapshot]
            stats = generate_statistics(a_list, b_list, gpx_data_list,
                                        initial_a_count=self.app.initial_a_count,
                                        initial_b_count=self.app.initial_b_count,
                                        updated_count=self.app.updated_count)
            text.config(state=tk.NORMAL)
            text.delete(1.0, tk.END)
            text.insert(tk.END, stats)
            text.config(state=tk.DISABLED)

        # 供 _on_tab_changed 在切换到统计页时刷新（该绑定不被 80 行覆盖）
        frame._refresh_stats = refresh_stats
        refresh_stats()
