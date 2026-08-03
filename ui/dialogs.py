"""编辑对话框集合"""

import os
import platform
import threading
import traceback
import webbrowser
from datetime import datetime

import tkinter as tk
from tkinter import ttk
from ui import custom_msgbox as messagebox
from utils.i18n import _
from utils.media_utils import format_gps_coord

MAP_SELECTOR_URL = "https://maps.apple.com"
MAP_SELECTOR_URL_BACKUP = "https://guihuayun.com/maps/getxy.php?area"


def _is_url_reachable(url):
    try:
        from urllib.parse import urlparse
        import socket
        host = urlparse(url).hostname
        if not host:
            return False
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect((host, 443))
            return True
        finally:
            sock.close()
    except Exception:
        return False


def _open_map_selector_async(app, callback):
    """异步检测首选地图服务可达性后回调，避免同步 socket 连接阻塞 UI 线程"""
    def check():
        try:
            reachable = _is_url_reachable(MAP_SELECTOR_URL)
        except Exception:
            reachable = False
        app.post_to_ui(lambda: callback(reachable))
    t = threading.Thread(target=check, daemon=True)
    app.register_thread(t)
    t.start()

from utils.exif_utils import (
    update_image_gps, update_raw_gps, update_video_gps, update_audio_gps,
    remove_gps_info
)
from config import RAW_EXTENSIONS, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS
from models.media_file import FileStatus, get_val
from services.date_processor import (
    update_file_shooting_date, clear_file_shooting_date
)
from utils.platform_utils import open_file_with_system
from services.geo_processor import (
    find_files_with_same_location
)


def _paste_date_to_entries(window, date_entry, time_entry):
    import re
    try:
        content = window.clipboard_get()
        m = re.search(r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)', content)
        if m:
            date_entry.delete(0, tk.END)
            date_entry.insert(0, m.group(1))
            time_entry.delete(0, tk.END)
            time_entry.insert(0, m.group(2))
            return True
        m = re.search(r'(\d{4}-\d{2}-\d{2})', content)
        if m:
            date_entry.delete(0, tk.END)
            date_entry.insert(0, m.group(1))
            return True
        try:
            messagebox.showwarning(_("粘贴失败"), _("剪贴板内容不包含有效日期格式"),
                                   parent=window)
        except Exception:
            traceback.print_exc()
    except Exception:
        try:
            messagebox.showwarning(_("粘贴失败"), _("无法读取剪贴板"), parent=window)
        except Exception:
            traceback.print_exc()
    return False


def _parse_coordinates(content):
    import re

    m = re.search(r'[?&#]ll=(-?\d+\.?\d*)%2C(-?\d+\.?\d*)', content)
    if m:
        return (m.group(1), m.group(2), None)

    m = re.search(r'[?&#]ll=(-?\d+\.?\d*),(-?\d+\.?\d*)', content)
    if m:
        return (m.group(1), m.group(2), None)

    m = re.search(r'maps\.apple\.com\.cn.*?(-?\d+\.\d+).*?(-?\d+\.\d+)', content)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if -90 <= a <= 90 and -180 <= b <= 180:
            return (m.group(1), m.group(2), None)
        if -90 <= b <= 90 and -180 <= a <= 180:
            return (m.group(2), m.group(1), None)

    # 优先匹配显式的三元组：lat, lon, alt（如 "39.9, 116.4, 50.0"）
    # 这样高度与经纬度来自同一组数据，避免误取无关数字
    m3 = re.search(
        r'(-?\d+\.?\d*)\s*[,，;；\s]\s*(-?\d+\.?\d*)\s*[,，;；\s]\s*(-?\d+\.?\d*)',
        content)
    if m3:
        vals = [float(m3.group(1)), float(m3.group(2)), float(m3.group(3))]
        # 检查是否符合 (lat, lon, alt) 或 (lon, lat, alt) 组合
        if -90 <= vals[0] <= 90 and -180 <= vals[1] <= 180:
            return (m3.group(1), m3.group(2), m3.group(3))
        if -90 <= vals[1] <= 90 and -180 <= vals[0] <= 180:
            return (m3.group(2), m3.group(1), m3.group(3))

    nums = re.findall(r'-?\d+\.\d+', content)
    if len(nums) >= 2:
        floats = [(s, float(s)) for s in nums]
        lat_candidates = [(s, v) for s, v in floats if -90 <= v <= 90]
        lon_candidates = [(s, v) for s, v in floats if -180 <= v <= 180]
        best = None
        best_len = 0
        for lat_str, lat_val in lat_candidates:
            for lon_str, lon_val in lon_candidates:
                if lat_str is lon_str:
                    continue
                pair_len = len(lat_str) + len(lon_str)
                if pair_len > best_len:
                    # 高度仅当存在明确的第三个数且与坐标来自同一片段时才有意义，
                    # 这里不自动猜测高度，避免粘贴错误数据
                    best = (str(lat_val), str(lon_val), None)
                    best_len = pair_len
        if best:
            return best
    return None


class EditCoordinatesDialog:
    """编辑 GPS 坐标对话框"""

    def __init__(self, app, file_info, tree, item_id, parent_window,
                 results_window=None):
        self.app = app
        self.file_info = file_info
        self.tree = tree
        self.item_id = item_id
        self.parent_window = parent_window
        self.results_window = results_window

        ww, wh = 420, 400
        x = max(0, parent_window.winfo_rootx() + parent_window.winfo_width() // 2 - ww // 2)
        y = max(0, parent_window.winfo_rooty() + parent_window.winfo_height() // 2 - wh // 2)

        self.window = tk.Toplevel(parent_window)
        self.window.transient(parent_window)
        self.window.grab_set()

        self.window.title(_("编辑位置信息"))
        self.window.geometry(f"{ww}x{wh}+{x}+{y}")
        self.window.resizable(True, True)
        app.edit_windows.append(self.window)

        def on_close():
            if hasattr(self, '_timer'):
                try:
                    self.window.after_cancel(self._timer)
                except Exception:
                    traceback.print_exc()
            if self.window in app.edit_windows:
                app.edit_windows.remove(self.window)
            self.window.destroy()
        self._on_close = on_close
        self.window.protocol("WM_DELETE_WINDOW", on_close)

        self._build_ui()

    def _build_ui(self):
        vcmd = (self.window.register(self._validate_float), '%P')

        ttk.Label(self.window,
                  text=_("编辑文件: ") + self.file_info.filename,
                  font=('', 10, 'bold')).pack(pady=8)

        form = ttk.Frame(self.window)
        form.pack(padx=12, pady=5, fill=tk.X)

        self.lat_var = tk.StringVar(
            value=format_gps_coord(self.file_info.latitude))
        self.lon_var = tk.StringVar(
            value=format_gps_coord(self.file_info.longitude))
        self.alt_var = tk.StringVar(
            value=format_gps_coord(self.file_info.altitude))

        ttk.Label(form, text=_("纬度 (Latitude):")).grid(row=0, column=0, sticky=tk.E, pady=4, padx=(0, 10))
        ttk.Entry(form, textvariable=self.lat_var, validate='key',
                  validatecommand=vcmd, width=20).grid(row=0, column=1, sticky=tk.W, pady=4)

        ttk.Label(form, text=_("经度 (Longitude):")).grid(row=1, column=0, sticky=tk.E, pady=4, padx=(0, 10))
        ttk.Entry(form, textvariable=self.lon_var, validate='key',
                  validatecommand=vcmd, width=20).grid(row=1, column=1, sticky=tk.W, pady=4)

        ttk.Label(form, text=_("高度 (Altitude):")).grid(row=2, column=0, sticky=tk.E, pady=4, padx=(0, 10))
        ttk.Entry(form, textvariable=self.alt_var, validate='key',
                  validatecommand=vcmd, width=20).grid(row=2, column=1, sticky=tk.W, pady=4)

        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=1)

        cp_frame = ttk.Frame(self.window)
        cp_frame.pack(pady=8)
        ttk.Button(cp_frame, text=_("复制位置信息"),
                   command=self._copy_location).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(cp_frame, text=_("粘贴位置信息"),
                   command=self._paste_location).pack(side=tk.LEFT)

        self.clip_status_var = tk.StringVar()
        self.clip_status_label = ttk.Label(self.window, textvariable=self.clip_status_var,
                                           foreground="#666", font=('', 9))
        self.clip_status_label.pack()
        self._update_clip_status()

        info = ttk.Label(self.window,
                         text=_("提示:\n• 纬度范围: -90 到 90\n• 经度范围: -180 到 180\n• 高度单位: 米\n• 留空表示删除该信息\n• 使用复制/粘贴功能可快速应用相同位置"),
                         justify=tk.LEFT, foreground="gray", font=('', 9))
        info.pack(pady=(5, 8))

        btn_frame = ttk.Frame(self.window)
        btn_frame.pack(pady=(12, 0))

        ttk.Button(btn_frame, text=_("保存修改"),
                   command=self._save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text=_("清空GPS"),
                   command=self._clear_gps).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text=_("地图选择"),
                   command=self._map_selector).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text=_("取消"),
                   command=self._on_close).pack(side=tk.LEFT, padx=5)

        self.status_label = ttk.Label(self.window, text="", font=('', 14, 'bold'))
        self.status_label.pack(pady=(0, 0))

        ttk.Label(self.window,
                  text=_("地图选择提示:\nhttps://maps.apple.com 地图上，左键点按选定的地址，显示已标记位置时，复制浏览器地址栏的内容，程序会自动分析GPS数据。"),
                  foreground="gray", font=('', 8), wraplength=360).pack(pady=(0, 3))

        self.window.bind('<Return>', lambda e: self._save())
        self.window.bind('<Escape>', lambda e: self._on_close())
        self.window.bind('<Control-s>', lambda e: self._save())
        self.window.bind('<Command-s>', lambda e: self._save())
        self.window.bind('<Command-w>', lambda e: self._on_close())
        self.window.bind('<Control-v>', lambda e: self._paste_location())
        self.window.bind('<FocusIn>', lambda e: self._update_clip_status())

    def _validate_float(self, value):
        if value == "" or value == "-":
            return True
        try:
            float(value)
            return True
        except ValueError:
            return False

    def _copy_location(self):
        lat = self.file_info.latitude
        lon = self.file_info.longitude
        alt = self.file_info.altitude
        if lat is not None and lon is not None:
            text = f"{format_gps_coord(lon)}, {format_gps_coord(lat)}"
            if alt is not None:
                text += f", {format_gps_coord(alt)}"
            try:
                self.window.clipboard_clear()
                self.window.clipboard_append(text)
            except Exception:
                try:
                    self.app.root.clipboard_clear()
                    self.app.root.clipboard_append(text)
                except Exception:
                    traceback.print_exc()

            self.app.location_clipboard.update({
                'latitude': lat, 'longitude': lon, 'altitude': alt,
                'source_file': self.file_info.filename,
                'timestamp': datetime.now(),
            })
            self._update_clip_status()

            messagebox.showinfo(_("已复制"),
                                _("纬度: ") + format_gps_coord(lat) + "\n" + _("经度: ") + format_gps_coord(lon) + "\n" + _("高度: ") + (format_gps_coord(alt) or _('无')) + "\n\n" + _("(已写入系统剪贴板)"),
                                parent=self.window)

    def _parse_coordinates(self, content):
        return _parse_coordinates(content)

    def _paste_location(self):
        lat_str = lon_str = alt_str = None
        try:
            content = self.window.clipboard_get()
            result = self._parse_coordinates(content)
            if result:
                lat_str, lon_str, alt_str = result
        except Exception:
            traceback.print_exc()

        if lat_str is not None and lon_str is not None:
            self.lat_var.set(lat_str)
            self.lon_var.set(lon_str)
            self.alt_var.set(alt_str if alt_str is not None else "")
            return

        clip = self.app.location_clipboard
        if clip['latitude'] is None and clip['longitude'] is None:
            messagebox.showwarning(_("粘贴失败"), _("系统剪贴板和位置信息剪贴板均为空\n请先复制位置信息"), parent=self.window)
            return

        ts = clip.get('timestamp')
        if ts:
            age = (datetime.now() - ts).total_seconds()
            if age > 86400:
                if not messagebox.askyesno(_("剪贴板已过期"),
                    _("内部剪贴板中的位置信息已超过24小时（") + str(int(age/3600)) + _("小时前复制），\n")
                    + _("来源: ") + clip.get('source_file', _('未知')) + "\n"
                    + _("复制时间: ") + ts.strftime('%Y-%m-%d %H:%M:%S') + "\n\n"
                    + _("是否仍要粘贴？"), parent=self.window):
                    return

        self.lat_var.set(format_gps_coord(clip['latitude']) if clip['latitude'] is not None else "")
        self.lon_var.set(format_gps_coord(clip['longitude']) if clip['longitude'] is not None else "")
        self.alt_var.set(format_gps_coord(clip['altitude']) if clip['altitude'] is not None else "")

    def _update_clip_status(self):
        try:
            content = self.window.clipboard_get()
            result = self._parse_coordinates(content)
            if result:
                lat_str, lon_str, alt_str = result
                lat, lon = float(lat_str), float(lon_str)
                alt = float(alt_str) if alt_str else None
                ic = self.app.location_clipboard
                if (ic['latitude'] is not None
                        and abs(ic['latitude'] - lat) < 1e-6
                        and abs(ic['longitude'] - lon) < 1e-6):
                    text = (_("剪贴板: ") + f"({lat_str}, {lon_str})"
                            + _(" | 来源: ") + ic.get('source_file', _('未知')))
                    if ic.get('timestamp'):
                        age = (datetime.now() - ic['timestamp']).total_seconds()
                        text += _(" | ") + f"{age/3600:.1f}h" + _("前")
                    self.clip_status_var.set(text)
                else:
                    self.clip_status_var.set(
                        _("剪贴板: ") + f"({lat_str}, {lon_str})" + _(" | 来源: 系统剪贴板"))
                return
        except Exception:
            traceback.print_exc()

        ic = self.app.location_clipboard
        if ic['latitude'] is not None:
            text = (_("剪贴板: ") + f"({format_gps_coord(ic['latitude'])}, {format_gps_coord(ic['longitude'])})"
                    + _(" | 来源: ") + ic.get('source_file', _('未知')))
            if ic.get('timestamp'):
                age = (datetime.now() - ic['timestamp']).total_seconds()
                text += _(" | ") + f"{age/3600:.1f}h" + _("前")
            self.clip_status_var.set(text)
            return

        self.clip_status_var.set("")

    def _save(self):
        # 全局互斥：防止与后台 geo 处理并发写同一文件
        if not self.app.acquire_processing():
            messagebox.showwarning(_("警告"), _("其他任务正在处理中，请等待完成"), parent=self.window)
            return
        self._processing_acquired = True
        try:
            new_lat = self.lat_var.get().strip()
            new_lon = self.lon_var.get().strip()
            new_alt = self.alt_var.get().strip()

            lat_val = float(new_lat) if new_lat else None
            lon_val = float(new_lon) if new_lon else None
            alt_val = float(new_alt) if new_alt else None

            if (lat_val is None) != (lon_val is None):
                messagebox.showerror(_("错误"), _("纬度和经度必须同时填写或同时留空"), parent=self.window)
                self._release_if_acquired()
                return
            if lat_val is not None and not -90 <= lat_val <= 90:
                messagebox.showerror(_("错误"), _("纬度必须在-90到90之间"), parent=self.window)
                self._release_if_acquired()
                return
            if lon_val is not None and not -180 <= lon_val <= 180:
                messagebox.showerror(_("错误"), _("经度必须在-180到180之间"), parent=self.window)
                self._release_if_acquired()
                return

            old_lat, old_lon = self.file_info.latitude, self.file_info.longitude
            self._old_lat, self._old_lon = old_lat, old_lon

            needs_batch = False
            same_loc_files = []
            if old_lat is not None and old_lon is not None and lat_val is not None and lon_val is not None:
                if abs(old_lat - lat_val) > 1e-6 or abs(old_lon - lon_val) > 1e-6:
                    same_loc_files = find_files_with_same_location(
                        self.app, old_lat, old_lon,
                        self.file_info.altitude, tolerance=1e-6)
                    same_loc_files = [f for f in same_loc_files
                                      if f is not self.file_info]
                    if same_loc_files:
                        msg = (_("发现 ") + str(len(same_loc_files))
                               + _(" 个文件与 '") + self.file_info.filename + "'\n"
                               + _("具有相同的位置 (") + f"{old_lat:.8f}, {old_lon:.8f}" + _(")。\n\n")
                               + _("是否要将这些文件的位置也一并更新？"))
                        dlg = tk.Toplevel(self.window)
                        dlg.title(_("确认批量修改"))
                        dlg.transient(self.window)
                        dlg.grab_set()
                        ttk.Label(dlg, text=msg, wraplength=380, justify=tk.LEFT,
                                 padding=12).pack(fill=tk.BOTH, expand=True)
                        bf = ttk.Frame(dlg)
                        bf.pack(pady=6)
                        dlg.update_idletasks()
                        x = self.window.winfo_rootx() + self.window.winfo_width() // 2 - dlg.winfo_reqwidth() // 2
                        y = self.window.winfo_rooty() + self.window.winfo_height() // 2 - dlg.winfo_reqheight() // 2
                        dlg.geometry(f"+{max(0,x)}+{max(0,y)}")
                        dlg.resizable(False, False)
                        result = [0]
                        def set_result(v):
                            result[0] = v
                            dlg.destroy()
                        ttk.Button(bf, text=_("修改当前"),
                                  command=lambda: set_result(1)).pack(side=tk.LEFT, padx=5)
                        ttk.Button(bf, text=_("全部修改"),
                                  command=lambda: set_result(2)).pack(side=tk.LEFT, padx=5)
                        ttk.Button(bf, text=_("取消"),
                                  command=lambda: set_result(0)).pack(side=tk.LEFT, padx=5)
                        dlg.protocol("WM_DELETE_WINDOW", lambda: set_result(0))
                        self.window.wait_window(dlg)
                        if result[0] == 0:
                            self._release_if_acquired()
                            return
                        needs_batch = (result[0] == 2)

            if lat_val is None and lon_val is None:
                remove_gps_info(self.file_info.path)
            else:
                loc_info = {'latitude': lat_val, 'longitude': lon_val, 'altitude': alt_val}
                ext = os.path.splitext(self.file_info.path)[1].lower()
                if ext in RAW_EXTENSIONS:
                    update_raw_gps(self.file_info.path, loc_info)
                elif ext in VIDEO_EXTENSIONS:
                    update_video_gps(self.file_info.path, loc_info)
                elif ext in AUDIO_EXTENSIONS:
                    update_audio_gps(self.file_info.path, loc_info)
                else:
                    update_image_gps(self.file_info.path, loc_info)

            self.file_info.latitude = lat_val
            self.file_info.longitude = lon_val
            self.file_info.altitude = alt_val

            if needs_batch and same_loc_files:
                self._start_same_location_batch(lat_val, lon_val, alt_val, same_loc_files)
                return
            self._finalize_save()

        except ValueError:
            try:
                messagebox.showerror(_("错误"), _("请输入有效的数值"), parent=self.window)
            except Exception:
                traceback.print_exc()
            self._release_if_acquired()
        except Exception:
            traceback.print_exc()
            try:
                messagebox.showerror(_("错误"), _("操作失败"), parent=self.window)
            except Exception:
                traceback.print_exc()
            self._release_if_acquired()

    def _release_if_acquired(self):
        if getattr(self, '_processing_acquired', False):
            self.app.release_processing()
            self._processing_acquired = False

    def _start_same_location_batch(self, lat_val, lon_val, alt_val, same_loc_files):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        total = len(same_loc_files)
        rw = self.results_window
        if rw and rw.window.winfo_exists():
            rw.progress_bar['value'] = 0
            rw.progress_bar['maximum'] = total
            rw.progress_label.config(text=_("批量更新中... 0/") + str(total))

        def process_one(f):
            try:
                loc = {'latitude': lat_val, 'longitude': lon_val, 'altitude': alt_val}
                ext = os.path.splitext(f.path)[1].lower()
                if ext in RAW_EXTENSIONS:
                    update_raw_gps(f.path, loc)
                elif ext in VIDEO_EXTENSIONS:
                    update_video_gps(f.path, loc)
                elif ext in AUDIO_EXTENSIONS:
                    update_audio_gps(f.path, loc)
                else:
                    update_image_gps(f.path, loc)
                # 工作线程只写磁盘，不修改 MediaFileInfo 属性，
                # 属性更新统一在主线程 _finish_same_location_batch 中加锁完成，避免撕裂读
                return True, ('set', f, lat_val, lon_val, alt_val)
            except Exception:
                return False, None

        def batch_worker():
            done = 0
            batch_failures = 0
            moves = []
            _lock = threading.Lock()
            try:
                with ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 4) * 2)) as pool:
                    futs = {pool.submit(process_one, f): f for f in same_loc_files}
                    for fut in as_completed(futs):
                        ok, move = fut.result()
                        if ok:
                            with _lock:
                                moves.append(move)
                        else:
                            with _lock:
                                batch_failures += 1
                        with _lock:
                            done += 1
                        if rw and (done % 5 == 0 or done == total):
                            self.app.post_to_ui(
                                lambda d=done, t=total: self._update_batch_progress(rw, d, t))
            except Exception:
                traceback.print_exc()
            self.app.post_to_ui(
                lambda: self._finish_same_location_batch(batch_failures, total, moves))

        t = threading.Thread(target=batch_worker, daemon=True)
        self.app.register_thread(t)
        t.start()

    def _update_batch_progress(self, rw, done, total):
        try:
            if rw.window.winfo_exists():
                rw.progress_bar.config(value=done)
                rw.progress_label.config(
                    text=_("批量更新中... ") + str(done) + "/" + str(total))
        except Exception:
            traceback.print_exc()

    def _finish_same_location_batch(self, batch_failures, total, moves=None):
        rw = self.results_window
        try:
            if rw:
                rw.progress_bar['maximum'] = 100
            if batch_failures > 0:
                if rw:
                    rw.progress_label.config(text=_("部分失败: ") + str(batch_failures) + _(" 个失败"))
                else:
                    messagebox.showwarning(_("部分失败"),
                        _("有 ") + str(batch_failures) + _(" 个相同位置的文件更新失败"),
                        parent=self.window)
        except Exception:
            traceback.print_exc()
        if moves:
            # 主线程加锁统一写入属性，避免 worker 线程直接改对象造成撕裂读
            with self.app.lock:
                for move in moves:
                    try:
                        fi, lat, lon, alt = (move[1], move[2], move[3], move[4])
                        fi.latitude = lat
                        fi.longitude = lon
                        fi.altitude = alt
                    except Exception:
                        traceback.print_exc()
        self._finalize_save()

    def _finalize_save(self):
        try:
            with self.app.lock:
                if (self._old_lat is None or self._old_lon is None) and \
                        (self.file_info.latitude is not None and self.file_info.longitude is not None):
                    if self.file_info in self.app.b:
                        self.app.b.remove(self.file_info)
                        self.app.a.append(self.file_info)
                elif (self._old_lat is not None and self._old_lon is not None) and \
                        (self.file_info.latitude is None or self.file_info.longitude is None):
                    if self.file_info in self.app.a:
                        self.app.a.remove(self.file_info)
                        self.app.b.append(self.file_info)

            try:
                self.tree.delete(self.item_id)
            except Exception:
                traceback.print_exc()

            if self.results_window:
                self.results_window.refresh()

            try:
                rw = self.results_window
                if rw:
                    rw.progress_label.config(text="")
                    rw.progress_bar['value'] = 25
                    rw.window.after(50, lambda: rw.progress_bar.config(value=50))
                    rw.window.after(100, lambda: rw.progress_bar.config(value=75))
                    rw.window.after(150, lambda: (rw.progress_bar.config(value=100),
                                                   rw.progress_label.config(text=_("位置信息已更新"))))
            except Exception:
                traceback.print_exc()
            self._on_close()

        except Exception:
            traceback.print_exc()
            try:
                messagebox.showerror(_("错误"), _("操作失败"), parent=self.window)
            except Exception:
                traceback.print_exc()
        finally:
            self._release_if_acquired()

    def _clear_gps(self):
        if messagebox.askyesno(_("确认清空"),
                                _("确定要清空 '") + self.file_info.filename + _(" 的GPS信息吗？"),
                                parent=self.window):
            # 全局互斥：防止与后台 geo 处理并发写同一文件
            if not self.app.acquire_processing():
                messagebox.showwarning(_("警告"), _("其他任务正在处理中，请等待完成"), parent=self.window)
                return
            self._processing_acquired = True
            try:
                remove_gps_info(self.file_info.path)
                self.file_info.latitude = None
                self.file_info.longitude = None
                self.file_info.altitude = None

                with self.app.lock:
                    if self.file_info in self.app.a:
                        self.app.a.remove(self.file_info)
                        self.app.b.append(self.file_info)

                try:
                    self.tree.delete(self.item_id)
                except Exception:
                    traceback.print_exc()

                if self.results_window:
                    try:
                        self.results_window.refresh()
                    except Exception:
                        traceback.print_exc()

                try:
                    messagebox.showinfo(_("成功"), _("GPS信息已清空"), parent=self.window)
                except Exception:
                    try:
                        messagebox.showinfo(_("成功"), _("GPS信息已清空"))
                    except Exception:
                        traceback.print_exc()
                self._on_close()
            except Exception:
                traceback.print_exc()
                try:
                    messagebox.showerror(_("错误"), _("操作失败"), parent=self.window)
                except Exception:
                    traceback.print_exc()
            finally:
                self._release_if_acquired()

    def _map_selector(self):
        _open_map_selector_async(self.app, self._open_selected_map)

    def _open_selected_map(self, primary_reachable):
        try:
            url = MAP_SELECTOR_URL if primary_reachable else MAP_SELECTOR_URL_BACKUP
            webbrowser.open(url)
        except Exception:
            traceback.print_exc()


class EditShootingDateDialog:
    """编辑拍摄日期对话框"""

    def __init__(self, app, file_info, tree=None, item_id=None,
                 parent_window=None, results_window=None, is_date_tab=False):
        self.app = app
        self.file_info = file_info
        self.tree = tree
        self.item_id = item_id
        self.parent_window = parent_window or app.root
        self.results_window = results_window
        self.is_date_tab = is_date_tab

        file_path = get_val(file_info, 'path', '')
        self.file_path = str(file_path) if file_path else ''
        self.file_ext = os.path.splitext(self.file_path)[1].lower() if self.file_path else ''

        orig_date = get_val(file_info, 'original_date', None)
        if orig_date is not None and orig_date != _('无'):
            try:
                self.current_date = datetime.strptime(orig_date, '%Y-%m-%d %H:%M:%S')
            except Exception:
                # 日期解析失败时保持未知（显示"无"），避免误用当前时间覆盖真实日期
                self.current_date = None
        else:
            dt = get_val(file_info, 'dt', None)
            # 无日期信息时保持空值，绝不预填当前时间，
            # 否则用户随手按回车就会把"今天"误写入文件
            self.current_date = dt if dt and dt != datetime.min else None

        self._create_window()

    def _get_info(self, key, default=''):
        return get_val(self.file_info, key, default)

    def _create_window(self):
        ww, wh = 360, 270
        target = self.parent_window
        try:
            x = target.winfo_rootx() + target.winfo_width() // 2 - ww // 2
            y = target.winfo_rooty() + target.winfo_height() // 2 - wh // 2
        except Exception:
            x, y = 100, 100

        self.window = tk.Toplevel(target)
        self.window.transient(target)
        self.window.grab_set()

        self.window.title(_("编辑拍摄日期 - ") + self._get_info('filename', ''))
        self.window.geometry(f"{ww}x{wh}+{max(0,x)}+{max(0,y)}")
        self.window.resizable(False, False)

        main = ttk.Frame(self.window, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        info_frame = ttk.LabelFrame(main, text=_("文件信息"), padding=6)
        info_frame.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(info_frame,
                 text=_("文件名: ") + self._get_info('filename', ''),
                 font=('', 10)).pack(anchor=tk.W)
        current_date_str = self.current_date.strftime('%Y-%m-%d %H:%M:%S') if self.current_date else _('无')
        ttk.Label(info_frame,
                 text=_("当前拍摄日期: ") + current_date_str,
                 font=('', 10)).pack(anchor=tk.W)

        input_frame = ttk.LabelFrame(main, text=_("新的拍摄日期"), padding=8)
        input_frame.pack(fill=tk.X, pady=(0, 6))

        dt_f = ttk.Frame(input_frame)
        dt_f.pack(expand=True)
        ttk.Label(dt_f, text=_("日期:"), font=('', 11)).pack(side=tk.LEFT, padx=(0, 6))
        self.date_entry = ttk.Entry(dt_f, width=11, font=('', 11))
        self.date_entry.pack(side=tk.LEFT, padx=(0, 15))
        self.date_entry.insert(0, self.current_date.strftime('%Y-%m-%d') if self.current_date else '')
        ttk.Label(dt_f, text=_("时间:"), font=('', 11)).pack(side=tk.LEFT, padx=(0, 6))
        self.time_entry = ttk.Entry(dt_f, width=9, font=('', 11))
        self.time_entry.pack(side=tk.LEFT)
        self.time_entry.insert(0, self.current_date.strftime('%H:%M:%S') if self.current_date else '')

        if self.is_date_tab:
            ttk.Label(input_frame, text=_("留空日期和时间可清除拍摄日期"),
                     font=('', 9), foreground='gray').pack(pady=(4, 0))

        cp_frame = ttk.Frame(main)
        cp_frame.pack(pady=(0, 6))
        ttk.Button(cp_frame, text=_("复制日期"),
                   command=self._copy_date, width=10).pack(side=tk.LEFT, padx=5)
        ttk.Button(cp_frame, text=_("粘贴日期"),
                   command=self._paste_date, width=10).pack(side=tk.LEFT, padx=5)

        btn_frame = ttk.Frame(main)
        btn_frame.pack()
        ttk.Button(btn_frame, text=_("保存"),
                   command=self._save, width=10).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text=_("取消"),
                   command=self.window.destroy, width=10).pack(side=tk.LEFT, padx=6)

        self.window.bind('<Return>', lambda e: self._save())
        self.window.bind('<Escape>', lambda e: self.window.destroy())
        self.window.bind('<Control-s>', lambda e: self._save())
        self.window.bind('<Command-s>', lambda e: self._save())
        self.window.bind('<Control-c>', lambda e: self._copy_date())
        self.window.bind('<Command-c>', lambda e: self._copy_date())
        self.window.bind('<Control-v>', lambda e: self._paste_date())
        self.window.bind('<Command-v>', lambda e: self._paste_date())

    def _copy_date(self):
        try:
            text = f"{self.date_entry.get().strip()} {self.time_entry.get().strip()}"
            if text.strip():
                self.window.clipboard_clear()
                self.window.clipboard_append(text)
                try:
                    messagebox.showinfo(_("已复制"), _("日期已复制到剪贴板:\n") + text,
                                       parent=self.window)
                except Exception:
                    traceback.print_exc()
        except Exception:
            traceback.print_exc()

    def _paste_date(self):
        _paste_date_to_entries(self.window, self.date_entry, self.time_entry)

    def _save(self):
        # 全局互斥：防止与后台 geo 处理并发写同一文件
        if not self.app.acquire_processing():
            messagebox.showwarning(_("警告"), _("其他任务正在处理中，请等待完成"), parent=self.window)
            return
        self._processing_acquired = True
        try:
            date_str = self.date_entry.get().strip()
            time_str = self.time_entry.get().strip()

            if date_str and time_str:
                try:
                    new_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    messagebox.showerror(_("错误"), _("日期时间格式不正确"), parent=self.window)
                    return
            elif date_str or time_str:
                messagebox.showerror(_("错误"), _("请同时填写日期和时间，或都留空"), parent=self.window)
                return
            else:
                new_dt = None

            if new_dt is not None:
                update_file_shooting_date(self.file_path, new_dt, self.file_ext)
            else:
                clear_file_shooting_date(self.file_path, self.file_ext)

            if isinstance(self.file_info, dict):
                if new_dt is not None:
                    self.file_info['original_date'] = new_dt.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    # 存 None 而非翻译串，避免排序/过滤/导出/切换语言后行为漂移
                    self.file_info['original_date'] = None
                self.file_info['manual_edit_date'] = True
                self.file_info['status'] = FileStatus.MANUALLY_EDITED
            else:
                self.file_info.dt = new_dt
                self.file_info.manual_edit_date = True

            if self.tree:
                if self.is_date_tab:
                    try:
                        values = list(self.tree.item(self.item_id, 'values'))
                        if len(values) >= 5:
                            values[2] = self._get_info('original_date', _('无'))
                            if (hasattr(self.results_window, 'operation_mode')
                                    and self.results_window.operation_mode.get() == "rename_file"
                                    and len(values) >= 6):
                                fp = self.file_path
                                if fp and new_dt:
                                    existing = new_dt
                                    new_fn = self.results_window._gen_new_name(
                                        fp, existing, self.file_info)
                                    if new_fn is None:
                                        if self.file_info.get('manual_rename'):
                                            new_fn = _('已手动重命名')
                                        else:
                                            new_fn = _('与原文件名相同')
                                    elif not new_fn:
                                        new_fn = _('无拍摄日期')
                                else:
                                    new_fn = _('无拍摄日期')
                                values[4] = new_fn
                                self.file_info['_new_filename_display'] = new_fn
                            self.tree.item(self.item_id, values=values)
                    except Exception:
                        traceback.print_exc()
                if hasattr(self.tree, 'sync_search'):
                    try:
                        self.tree.sync_search()
                    except Exception:
                        traceback.print_exc()

            if self.results_window and hasattr(self.results_window, 'refresh'):
                try:
                    self.results_window.refresh()
                except Exception:
                    traceback.print_exc()

            try:
                log_date = new_dt.strftime('%Y-%m-%d %H:%M:%S') if new_dt else _('无（已清除）')
                log_msg = _("编辑拍摄日期: ") + os.path.basename(self.file_path) + " → " + log_date + "\n"
                if hasattr(self.results_window, '_append_log'):
                    try:
                        self.results_window._append_log(log_msg)
                    except Exception:
                        traceback.print_exc()
            except Exception:
                traceback.print_exc()
            try:
                if self.results_window and hasattr(self.results_window, '_pulse_progress'):
                    self.results_window._pulse_progress()
                elif self.results_window and hasattr(self.results_window, 'progress_bar'):
                    rw = self.results_window
                    rw.progress_label.config(text="")
                    rw.progress_bar['value'] = 25
                    rw.window.after(50, lambda: rw.progress_bar.config(value=50))
                    rw.window.after(100, lambda: rw.progress_bar.config(value=75))
                    rw.window.after(150, lambda: (rw.progress_bar.config(value=100),
                                                   rw.progress_label.config(text=_("拍摄日期已更新"))))
            except Exception:
                traceback.print_exc()
            try:
                self.window.destroy()
            except Exception:
                traceback.print_exc()
        except Exception:
            traceback.print_exc()
            try:
                messagebox.showerror(_("错误"), _("操作失败"), parent=self.window)
            except Exception:
                traceback.print_exc()
        finally:
            if getattr(self, '_processing_acquired', False):
                self.app.release_processing()
                self._processing_acquired = False


class BatchDateEditDialog:
    """批量编辑拍摄日期对话框"""

    def __init__(self, app, selected_files, tree, is_date_tab=False,
                 refresh_callback=None):
        self.app = app
        self.selected_files = selected_files
        self.tree = tree
        self.is_date_tab = is_date_tab
        self.refresh_callback = refresh_callback

        parent = tree.winfo_toplevel() if tree else app.root

        ww, wh = 360, 250
        try:
            x = parent.winfo_x() + parent.winfo_width() // 2 - ww // 2
            y = parent.winfo_y() + parent.winfo_height() // 2 - wh // 2
            geom = f"{ww}x{wh}+{x}+{y}"
        except Exception:
            geom = f"{ww}x{wh}+100+100"

        self.window = tk.Toplevel(parent)
        self.window.transient(parent)
        self.window.grab_set()

        self.window.title(_("批量编辑拍摄日期"))
        self.window.geometry(geom)
        self.window.resizable(False, False)

        self._build_ui()

    def _build_ui(self):
        main = ttk.Frame(self.window, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        info = ttk.LabelFrame(main, text=_("选中文件"), padding=6)
        info.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(info, text=_("已选择 ") + str(len(self.selected_files)) + _(" 个文件"),
                 font=('', 10)).pack()

        input_f = ttk.LabelFrame(main, text=_("新的拍摄日期"), padding=8)
        input_f.pack(fill=tk.X, pady=(0, 6))

        dt_f = ttk.Frame(input_f)
        dt_f.pack(expand=True)
        ttk.Label(dt_f, text=_("日期:"), font=('', 11)).pack(side=tk.LEFT, padx=(0, 6))
        self.date_entry = ttk.Entry(dt_f, width=11, font=('', 11))
        self.date_entry.pack(side=tk.LEFT, padx=(0, 15))
        self.date_entry.insert(0, datetime.now().strftime('%Y-%m-%d'))
        ttk.Label(dt_f, text=_("时间:"), font=('', 11)).pack(side=tk.LEFT, padx=(0, 6))
        self.time_entry = ttk.Entry(dt_f, width=9, font=('', 11))
        self.time_entry.pack(side=tk.LEFT)
        self.time_entry.insert(0, datetime.now().strftime('%H:%M:%S'))

        cp_frame = ttk.Frame(main)
        cp_frame.pack(pady=(0, 6))
        ttk.Button(cp_frame, text=_("复制日期"),
                   command=self._copy_date, width=10).pack(side=tk.LEFT, padx=5)
        ttk.Button(cp_frame, text=_("粘贴日期"),
                   command=self._paste_date, width=10).pack(side=tk.LEFT, padx=5)

        btn_f = ttk.Frame(main)
        btn_f.pack()
        self.status_lbl = tk.Label(btn_f, text="", font=('', 10, 'bold'))
        self.status_lbl.pack(pady=(0, 6))
        bc = ttk.Frame(btn_f)
        bc.pack()
        self.apply_btn = ttk.Button(bc, text=_("应用"), command=self._apply, width=10)
        self.apply_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(bc, text=_("取消"), command=self.window.destroy, width=10).pack(side=tk.LEFT, padx=6)

        self.window.bind('<Return>', lambda e: self._apply())
        self.window.bind('<Escape>', lambda e: self.window.destroy())
        self.window.bind('<Control-c>', lambda e: self._copy_date())
        self.window.bind('<Command-c>', lambda e: self._copy_date())
        self.window.bind('<Control-v>', lambda e: self._paste_date())
        self.window.bind('<Command-v>', lambda e: self._paste_date())

    def _copy_date(self):
        try:
            text = f"{self.date_entry.get().strip()} {self.time_entry.get().strip()}"
            if text.strip():
                self.window.clipboard_clear()
                self.window.clipboard_append(text)
                try:
                    messagebox.showinfo(_("已复制"), _("日期已复制到剪贴板:\n") + text,
                                       parent=self.window)
                except Exception:
                    traceback.print_exc()
        except Exception:
            traceback.print_exc()

    def _paste_date(self):
        _paste_date_to_entries(self.window, self.date_entry, self.time_entry)

    def _apply(self):
        try:
            date_str = self.date_entry.get().strip()
            time_str = self.time_entry.get().strip()
            if not date_str or not time_str:
                messagebox.showwarning(_("输入错误"), _("请输入完整的日期和时间"), parent=self.window)
                return
            new_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")

            # 全局互斥：防止与后台 geo 处理并发写同一文件
            if not self.app.acquire_processing():
                messagebox.showwarning(_("警告"), _("其他任务正在处理中，请等待完成"), parent=self.window)
                return
            self._processing_acquired = True

            self.window.destroy()

            if self.refresh_callback:
                try:
                    self.refresh_callback(new_dt, self.selected_files)
                except Exception:
                    traceback.print_exc()
                finally:
                    self._release_if_acquired()
                return

            from concurrent.futures import ThreadPoolExecutor, as_completed
            from services.date_processor import update_file_shooting_date

            def process_one(fi):
                try:
                    fp = get_val(fi, 'path', '')
                    ext = os.path.splitext(str(fp))[1].lower()
                    update_file_shooting_date(str(fp), new_dt, ext)
                    if isinstance(fi, dict):
                        fi['original_date'] = new_dt.strftime('%Y-%m-%d %H:%M:%S')
                        fi['manual_edit_date'] = True
                        fi['status'] = FileStatus.DATE_CHANGED
                    else:
                        # 同步更新内存中的日期，避免界面显示旧日期造成"没改成功"错觉
                        fi.dt = new_dt
                        fi.manual_edit_date = True
                    return True, None
                except Exception as e:
                    return False, str(e)

            prog_total = len(self.selected_files)
            prog_done = [0]
            prog_success = [0]
            prog_failed = [0]
            prog_lock = threading.Lock()

            def process():
                try:
                    with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4) * 2)) as pool:
                        futs = {pool.submit(process_one, fi): fi for fi in self.selected_files}
                        for fut in as_completed(futs):
                            ok, _ = fut.result()
                            with prog_lock:
                                if ok:
                                    prog_success[0] += 1
                                else:
                                    prog_failed[0] += 1
                                prog_done[0] += 1
                except Exception:
                    traceback.print_exc()

            prog_thread = threading.Thread(target=process, daemon=True)
            self.app.register_thread(prog_thread)
            self._update_results_progress(0, _("处理中..."))

            def poll():
                try:
                    alive = prog_thread.is_alive()
                    with prog_lock:
                        d = prog_done[0]
                        s = prog_success[0]
                        f = prog_failed[0]
                    if not alive:
                        self._finish_up(s, f)
                        return
                    if prog_total > 0:
                        pct = d / prog_total * 100
                        txt = _("处理中...") if d == 0 else (_("进度: ") + str(d) + "/" + str(prog_total))
                        rw = self.app.geo_tab.result_window
                        if rw is not None and hasattr(rw, 'progress_bar'):
                            # 结果窗口可能已被用户关闭：winfo_exists 抛 TclError 时跳过刷新
                            try:
                                if rw.window.winfo_exists():
                                    rw.progress_bar['value'] = pct
                                    rw.progress_label.config(text=txt)
                                    rw.window.update_idletasks()
                            except Exception:
                                pass
                    self.app.root.after(200, poll)
                except Exception:
                    traceback.print_exc()
                    try:
                        self.app.root.after(200, poll)
                    except Exception:
                        traceback.print_exc()

            prog_thread.start()
            poll()
        except ValueError:
            try:
                messagebox.showerror(_("格式错误"), _("日期时间格式不正确"), parent=self.window)
            except Exception:
                traceback.print_exc()
        except Exception:
            traceback.print_exc()
            try:
                messagebox.showerror(_("错误"), _("操作失败"), parent=self.window)
            except Exception:
                traceback.print_exc()

    def _update_results_progress(self, value, text=""):
        try:
            rw = self.app.geo_tab.result_window
            if rw and hasattr(rw, 'progress_bar'):
                rw.progress_bar['value'] = value
                rw.progress_label.config(text=text)
                rw.window.update()
        except Exception:
            traceback.print_exc()

    def _release_if_acquired(self):
        if getattr(self, '_processing_acquired', False):
            self.app.release_processing()
            self._processing_acquired = False

    def _finish_up(self, success, failed):
        try:
            rw = self.app.geo_tab.result_window
            if rw and hasattr(rw, 'progress_bar'):
                rw.progress_bar['value'] = 100
                rw.progress_label.config(text=_("完成: ") + str(success) + _("成功/") + str(failed) + _("失败"))
                rw.window.update()
        except Exception:
            traceback.print_exc()
        if self.tree:
            try:
                self.tree.sync_search()
            except Exception:
                traceback.print_exc()
        self._release_if_acquired()
class BatchLocationEditDialog:
    """批量编辑位置信息对话框"""

    def __init__(self, app, selected_files, tree):
        self.app = app
        self.selected_files = selected_files
        self.tree = tree
        parent = tree.winfo_toplevel() if tree else app.root

        ww, wh = 400, 340
        try:
            x = parent.winfo_x() + parent.winfo_width() // 2 - ww // 2
            y = parent.winfo_y() + parent.winfo_height() // 2 - wh // 2
            geom = f"{ww}x{wh}+{x}+{y}"
        except Exception:
            geom = f"{ww}x{wh}+100+100"

        self.window = tk.Toplevel(parent)
        self.window.transient(parent)
        self.window.grab_set()

        self.window.title(_("批量编辑位置信息"))
        self.window.geometry(geom)
        self.window.resizable(False, False)

        self._build_ui()

    def _build_ui(self):
        main = ttk.Frame(self.window, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        info = ttk.LabelFrame(main, text=_("选中文件"), padding=6)
        info.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(info, text=_("已选择 ") + str(len(self.selected_files)) + _(" 个文件"),
                 font=('', 11)).pack()

        input_f = ttk.LabelFrame(main, text=_("新的位置信息"), padding=8)
        input_f.pack(fill=tk.X, pady=(0, 4))

        for label, var_name in [(_("纬度:"), "lat"), (_("经度:"), "lon"), (_("高度(米):"), "alt")]:
            f = ttk.Frame(input_f)
            f.pack(expand=True, pady=(0, 4))
            ttk.Label(f, text=label, font=('', 11)).pack(side=tk.LEFT, padx=(0, 8))
            entry = ttk.Entry(f, width=18, font=('', 11))
            entry.pack(side=tk.LEFT)
            setattr(self, f"{var_name}_entry", entry)

        paste_f = ttk.Frame(main)
        paste_f.pack(pady=(0, 1))
        ttk.Button(paste_f, text=_("地图选择"),
                   command=lambda: self._open_map_selector()).pack(side=tk.LEFT, padx=4)
        ttk.Button(paste_f, text=_("粘贴位置信息"),
                   command=self._paste_location).pack(side=tk.LEFT, padx=4)

        btn_f = ttk.Frame(main)
        btn_f.pack(pady=(0, 0))
        self.status_lbl = tk.Label(btn_f, text="", font=('', 11, 'bold'))
        self.status_lbl.pack(pady=(0, 1))
        bc = ttk.Frame(btn_f)
        bc.pack()
        self.apply_btn = ttk.Button(bc, text=_("应用"), command=self._apply, width=12)
        self.apply_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(bc, text=_("取消"), command=self.window.destroy, width=12).pack(side=tk.LEFT, padx=6)

        ttk.Label(self.window,
                  text=_("地图选择提示:\nhttps://maps.apple.com 地图上，左键点按选定的地址，显示已标记位置时，复制浏览器地址栏的内容，程序会自动分析GPS数据。"),
                  foreground="gray", font=('', 8), wraplength=360).pack(pady=(0, 2))

        self.window.bind('<Return>', lambda e: self._apply())
        self.window.bind('<Escape>', lambda e: self.window.destroy())
        self.window.bind('<Control-v>', lambda e: self._paste_location())

    def _update_results_progress(self, value, text=""):
        try:
            rw = self.app.geo_tab.result_window
            if rw and hasattr(rw, 'progress_bar'):
                rw.progress_bar['value'] = value
                rw.progress_label.config(text=text)
                rw.window.update_idletasks()
        except Exception:
            traceback.print_exc()

    def _release_if_acquired(self):
        if getattr(self, '_processing_acquired', False):
            self.app.release_processing()
            self._processing_acquired = False

    def _parse_coordinates(self, content):
        return _parse_coordinates(content)

    def _open_map_selector(self):
        _open_map_selector_async(self.app, self._open_selected_map)

    def _open_selected_map(self, primary_reachable):
        try:
            url = MAP_SELECTOR_URL if primary_reachable else MAP_SELECTOR_URL_BACKUP
            webbrowser.open(url)
        except Exception:
            traceback.print_exc()

    def _paste_location(self):
        try:
            content = self.window.clipboard_get()

            if content:
                result = self._parse_coordinates(content)
                if result:
                    lat_str, lon_str, alt_str = result
                    self.lat_entry.delete(0, tk.END)
                    self.lat_entry.insert(0, lat_str)
                    self.lon_entry.delete(0, tk.END)
                    self.lon_entry.insert(0, lon_str)
                    if alt_str:
                        self.alt_entry.delete(0, tk.END)
                        self.alt_entry.insert(0, alt_str)
                    return
                else:
                    try:
                        messagebox.showwarning(_("粘贴失败"),
                            _("系统剪贴板内容不包含有效坐标\n请复制形如 \"39.90882, 116.39747\" 的坐标信息"),
                            parent=self.window)
                    except Exception:
                        traceback.print_exc()
                    return
        except Exception:
            traceback.print_exc()

        clip = self.app.location_clipboard
        if clip['latitude'] is not None and clip['longitude'] is not None:
            self.lat_entry.delete(0, tk.END)
            self.lat_entry.insert(0, format_gps_coord(clip['latitude']))
            self.lon_entry.delete(0, tk.END)
            self.lon_entry.insert(0, format_gps_coord(clip['longitude']))
            if clip['altitude'] is not None:
                self.alt_entry.delete(0, tk.END)
                self.alt_entry.insert(0, format_gps_coord(clip['altitude']))
            return

        try:
            messagebox.showwarning(_("粘贴失败"), _("系统剪贴板和位置信息剪贴板均为空"), parent=self.window)
        except Exception:
            traceback.print_exc()

    def _apply(self):
        try:
            lat_str = self.lat_entry.get().strip()
            lon_str = self.lon_entry.get().strip()
            alt_str = self.alt_entry.get().strip()

            lat = float(lat_str) if lat_str else None
            lon = float(lon_str) if lon_str else None
            alt = float(alt_str) if alt_str else None
        except ValueError:
            messagebox.showerror(_("格式错误"), _("请输入有效的数值"), parent=self.window)
            return

        if (lat is not None and lon is None) or (lat is None and lon is not None):
            messagebox.showwarning(_("输入错误"), _("纬度和经度必须同时提供或同时留空"), parent=self.window)
            return

        if lat is not None and (lat < -90 or lat > 90):
            messagebox.showerror(_("输入错误"), _("纬度必须在 -90 到 90 之间"), parent=self.window)
            return
        if lon is not None and (lon < -180 or lon > 180):
            messagebox.showerror(_("输入错误"), _("经度必须在 -180 到 180 之间"), parent=self.window)
            return

        # 全局互斥：防止与后台 geo 处理并发写同一文件
        if not self.app.acquire_processing():
            messagebox.showwarning(_("警告"), _("其他任务正在处理中，请等待完成"), parent=self.window)
            return
        self._processing_acquired = True

        self.window.destroy()

        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def process_one(fi):
            try:
                ext = os.path.splitext(str(fi.path))[1].lower()
                # 工作线程只写磁盘，不修改 MediaFileInfo 属性，
                # 属性更新统一在主线程 _on_loc_batch_done 中加锁完成，避免撕裂读
                if lat is not None and lon is not None:
                    loc = {'latitude': lat, 'longitude': lon, 'altitude': alt}
                    if ext in RAW_EXTENSIONS:
                        update_raw_gps(fi.path, loc)
                    elif ext in VIDEO_EXTENSIONS:
                        update_video_gps(fi.path, loc)
                    elif ext in AUDIO_EXTENSIONS:
                        update_audio_gps(fi.path, loc)
                    else:
                        update_image_gps(fi.path, loc)
                    return True, ('set', fi, lat, lon, alt)
                else:
                    remove_gps_info(fi.path)
                    return True, ('clear', fi, None, None, None)
            except Exception as e:
                return False, str(e)

        def process():
            total = len(self.selected_files)
            success = 0
            failed = 0
            moves = []
            _lock = threading.Lock()
            self.app.post_to_ui(lambda: self._update_results_progress(0, _("处理中...")))
            with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4) * 2)) as pool:
                futs = {pool.submit(process_one, fi): fi for fi in self.selected_files}
                done = 0
                for fut in as_completed(futs):
                    ok, move_info = fut.result()
                    if ok:
                        with _lock:
                            success += 1
                            if move_info:
                                moves.append(move_info)
                    else:
                        with _lock:
                            failed += 1
                    with _lock:
                        done += 1
                    if done % 5 == 0 or done == total:
                        self.app.post_to_ui(lambda d=done, t=total: (
                            self._update_results_progress(d / t * 100,
                                                          _("进度: ") + str(d) + "/" + str(total))
                        ))
            self.app.post_to_ui(lambda s=success, f=failed, mv=moves:
                                self._on_loc_batch_done(s, f, mv))

        t = threading.Thread(target=process, daemon=True)
        self.app.register_thread(t)
        t.start()

    def _on_loc_batch_done(self, success, failed, moves=None):
        self._update_results_progress(100, _("完成: ") + str(success) + _("成功/") + str(failed) + _("失败"))
        if moves:
            with self.app.lock:
                for action, fi, lat, lon, alt in moves:
                    try:
                        if action == 'set':
                            old_has = fi.latitude is not None and fi.longitude is not None
                            fi.latitude = lat
                            fi.longitude = lon
                            fi.altitude = alt
                            if not old_has and fi in self.app.b:
                                self.app.b.remove(fi)
                                self.app.a.append(fi)
                        else:
                            old_has = fi.latitude is not None
                            fi.latitude = None
                            fi.longitude = None
                            fi.altitude = None
                            if old_has and fi in self.app.a:
                                self.app.a.remove(fi)
                                self.app.b.append(fi)
                    except Exception:
                        traceback.print_exc()
        try:
            self.app.geo_tab.show_results()
        except Exception:
            traceback.print_exc()
        try:
            rw = self.app.geo_tab.result_window
            if rw:
                rw.progress_label.config(text=_("完成: ") + str(success) + _("成功/") + str(failed) + _("失败"))
        except Exception:
            traceback.print_exc()
        self._release_if_acquired()


class GpxPointDetails:
    """GPX 轨迹点详情对话框"""

    def __init__(self, app, gpx_point, geo_tab=None):
        self.app = app
        self.gpx_point = gpx_point

        ww, wh = 380, 320

        try:
            x = app.root.winfo_rootx() + app.root.winfo_width() // 2 - ww // 2
            y = app.root.winfo_rooty() + app.root.winfo_height() // 2 - wh // 2
        except Exception:
            x, y = 100, 100

        self.window = tk.Toplevel(app.root)
        self.window.transient(app.root)

        self.window.title(_("GPX轨迹点详情"))
        self.window.geometry(f"{ww}x{wh}+{max(0,x)}+{max(0,y)}")
        self.window.resizable(False, False)

        ttk.Label(self.window, text=_("GPX轨迹点详细信息"),
                 font=('', 12, 'bold')).pack(pady=12)

        data = [
            (_("来源文件"), gpx_point.get('source_file', _('未知'))),
            (_("记录时间"), gpx_point['datetime'].strftime('%Y-%m-%d %H:%M:%S') if gpx_point.get('datetime') else _('未知')),
            (_("纬度"), f"{gpx_point['latitude']:.8f}°" if gpx_point.get('latitude') is not None else _('未知')),
            (_("经度"), f"{gpx_point['longitude']:.8f}°" if gpx_point.get('longitude') is not None else _('未知')),
            (_("高度"), f"{gpx_point['altitude']:.2f}" + _(" 米") if gpx_point.get('altitude') is not None else _('未知')),
            (_("数据来源"), gpx_point.get('source', 'GPX')),
        ]

        frame = ttk.Frame(self.window, padding="15")
        frame.pack(fill=tk.BOTH, expand=True)
        for i, (label, value) in enumerate(data):
            ttk.Label(frame, text=label + ":", font=('', 10, 'bold')).grid(
                row=i, column=0, sticky=tk.W, pady=6, padx=(0, 10))
            ttk.Label(frame, text=str(value), font=('', 10)).grid(
                row=i, column=1, sticky=tk.W, pady=6)

        btn_frame = ttk.Frame(self.window)
        btn_frame.pack(pady=15)

        def copy_coords():
            lat, lon = gpx_point.get('latitude'), gpx_point.get('longitude')
            if lat is not None and lon is not None:
                coord_text = f"{lon}, {lat}"
                alt = gpx_point.get('altitude')
                if alt is not None:
                    coord_text += f", {alt}"
                try:
                    self.window.clipboard_clear()
                    self.window.clipboard_append(coord_text)
                except Exception:
                    try:
                        self.app.root.clipboard_clear()
                        self.app.root.clipboard_append(coord_text)
                    except Exception:
                        traceback.print_exc()
                try:
                    messagebox.showinfo(_("已复制"), _("坐标已复制到剪贴板:\n") + coord_text,
                                       parent=self.window)
                except Exception:
                    traceback.print_exc()

        geo_tab_ref = geo_tab or getattr(self.app, 'geo_tab', None)

        def show_in_map():
            if geo_tab_ref and hasattr(geo_tab_ref, 'show_location_in_map'):
                obj = type('obj', (object,), {
                    'latitude': gpx_point.get('latitude'),
                    'longitude': gpx_point.get('longitude'),
                    'altitude': gpx_point.get('altitude'),
                })()
                geo_tab_ref.show_location_in_map(obj)

        ttk.Button(btn_frame, text=_("复制坐标"), command=copy_coords).pack(side=tk.LEFT, padx=5)
        if geo_tab_ref:
            ttk.Button(btn_frame, text=_("在地图中显示"), command=show_in_map).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text=_("关闭"),
                   command=self.window.destroy).pack(side=tk.LEFT, padx=5)


class CompletionDialog:
    """处理完成摘要对话框"""

    def __init__(self, parent, success, skipped, failed):
        self.parent = parent
        try:
            x = parent.winfo_rootx() + parent.winfo_width() // 2 - 140
            y = parent.winfo_rooty() + parent.winfo_height() // 2 - 60
        except Exception:
            x, y = 100, 100

        self.window = tk.Toplevel(parent)
        self.window.title(_("处理完成"))
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.grab_set()

        main = ttk.Frame(self.window, padding=20)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text=_("处理完成"), font=('', 12, 'bold')).pack(pady=(0, 12))

        grid = ttk.Frame(main)
        grid.pack()

        def add_row(label, value, color):
            row = ttk.Frame(grid)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label, font=('', 10), width=8, anchor=tk.E).pack(side=tk.LEFT, padx=(0, 10))
            ttk.Label(row, text=str(value), font=('', 10, 'bold'),
                      foreground=color).pack(side=tk.LEFT)

        add_row(_("成功:"), success, '#2E7D32')
        add_row(_("跳过:"), skipped, '#666666')
        add_row(_("失败:"), failed, '#C62828')

        btn_frame = ttk.Frame(main)
        btn_frame.pack(pady=(12, 0))
        ttk.Button(btn_frame, text=_("确定"), width=10,
                   command=self.window.destroy).pack()

        self.window.update_idletasks()
        w = max(260, self.window.winfo_reqwidth())
        h = self.window.winfo_reqheight()
        self.window.geometry(f"{w}x{h}+{max(0,x)}+{max(0,y)}")
