"""媒体文件扫描器 - 扫描文件夹并提取元数据

负责递归扫描文件夹，查找媒体文件和 GPX 轨迹文件，
使用多线程并行提取文件的 EXIF/GPS 元数据，
并根据是否有 GPS 信息将文件分类到两个列表中。
"""

import os
import threading
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    VIDEO_EXTENSIONS, AUDIO_EXTENSIONS, ALL_MEDIA_EXTENSIONS
)
from utils.exif_utils import (
    extract_exif_metadata, extract_pil_gps, extract_video_gps_with_exiftool
)
from utils.gpx_utils import parse_gpx_file
from utils.media_utils import get_file_creation_datetime
from models.media_file import MediaFileInfo
from models.gps_data import GpsPoint
from utils.logging_utils import log_exc, log_warning
from utils.i18n import _


def _sort_key(x):
    """按拍摄时间排序的键：带时区的值统一转本地 naive 再比较"""
    dt = x.dt if x.dt else datetime.min
    if dt.tzinfo is not None:
        try:
            dt = dt.astimezone().replace(tzinfo=None)
        except Exception:
            dt = datetime.min
    return dt


def scan_folder(folder_path, progress_callback=None, only_process_with_date=False,
                log_callback=None):
    """扫描文件夹中的媒体文件和 GPX 文件

    执行三个步骤：
      1. 查找并解析所有 .gpx 轨迹文件
      2. 查找所有支持的媒体文件
      3. 多线程提取每个文件的元数据并分类

    Args:
        folder_path: 要扫描的文件夹路径
        progress_callback: 进度回调函数(百分比)
        only_process_with_date: 如果为 True，只处理有 EXIF 日期的文件
        log_callback: 日志回调函数(文件名)，每次处理一个文件时调用

    Returns:
        tuple: (a_list, b_list, gps_data, total, skipped_count)
            a_list: 有 GPS 信息的文件列表
            b_list: 没有 GPS 信息的文件列表
            gps_data: GPX 轨迹点列表
            total: 扫描的文件总数
            skipped_count: 跳过的文件数（仅 only_process_with_date 时有效）
    """
    gps_data = []
    files = []

    # 合并遍历：一次 os.walk 同时收集 GPX 和媒体文件
    # onerror 只统计无法访问的目录（跳过继续遍历），最后汇总一条日志，
    # 避免每个无权限子目录都打一条日志刷屏；单个目录不可访问
    # 不应中断整个扫描导致文件/轨迹漏扫
    walk_errors = [0]

    def _on_walk_error(exc):
        walk_errors[0] += 1

    try:
        if not os.path.isdir(folder_path):
            return [], [], [], 0, 0
        for r, _dirs, fs in os.walk(folder_path, onerror=_on_walk_error):
            for f in fs:
                ext = os.path.splitext(f)[1].lower()
                if ext == '.gpx':
                    gpx_path = os.path.join(r, f)
                    points = parse_gpx_file(gpx_path)
                    for p in points:
                        gps_data.append(GpsPoint(
                            datetime_val=p['datetime'],
                            latitude=p['latitude'],
                            longitude=p['longitude'],
                            altitude=p['altitude'],
                            source=p.get('source', 'GPX'),
                            source_file=p.get('source_file'),
                        ))
                elif ext in ALL_MEDIA_EXTENSIONS:
                    files.append(os.path.join(r, f))
        if walk_errors[0]:
            log_warning(
                _("扫描时无法访问 {0} 个目录，已跳过").format(walk_errors[0]))
    except PermissionError:
        pass

    if not files:
        return [], [], gps_data, 0, 0

    # 第三步：使用多线程并行提取文件元数据
    a_list = []
    b_list = []
    total = len(files)
    skipped_count = 0

    # 线程数设为 CPU 核心数的 2 倍，最大 32
    _last_log_pct = -1
    _log_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 4) * 2)) as pool:
        futs = {pool.submit(_extract_file_info, p, only_process_with_date): p for p in files}
        done = 0
        for fut in as_completed(futs):
            try:
                info = fut.result()
            except Exception:
                log_exc()
                done += 1
                continue
            # 如果设置了只处理有日期的文件，跳过无日期的
            if only_process_with_date and info.dt is None:
                skipped_count += 1
                done += 1
                continue
            # 根据是否有 GPS 信息分类
            if info.latitude is not None and info.longitude is not None:
                a_list.append(info)
            else:
                b_list.append(info)
            done += 1
            pct = int(done / total * 100)
            if progress_callback and (done % 10 == 0 or done == total):
                progress_callback(pct)
            if log_callback:
                with _log_lock:
                    if pct > _last_log_pct:
                        _last_log_pct = pct
                        log_callback(done, total)

    # 按拍摄时间排序两个列表（带时区值统一转本地 naive 再比较，
    # 与 geo_processor 的匹配基准一致，避免 aware/naive 混比抛 TypeError）
    a_list.sort(key=_sort_key)
    b_list.sort(key=_sort_key)

    return a_list, b_list, gps_data, total, skipped_count


def _extract_file_info(file_path, only_process_with_date=False):
    """提取单个文件的元数据

    根据文件类型选择不同的提取策略：
      - 视频文件：使用 ExifTool 提取 GPS 和时间
      - 图像文件：先用 exifread 库，失败则用 Pillow 库
      - 如果都没有日期信息，回退到文件修改时间

    Args:
        file_path: 文件路径
        only_process_with_date: 是否只处理有日期的文件

    Returns:
        MediaFileInfo 对象
    """
    ext = os.path.splitext(file_path)[1].lower()
    info = MediaFileInfo(file_path)

    if ext not in ALL_MEDIA_EXTENSIONS:
        return info

    # 音频文件：跳过 ExifTool 探测（音频极少含 GPS，且每次调用都要
    # 启动一个子进程，批量扫描千级 MP3 时开销巨大），日期直接回退
    # 文件系统创建/修改时间；视频仍需 ExifTool 提取 GPS 与时间
    if ext in AUDIO_EXTENSIONS:
        if not only_process_with_date:
            dt = get_file_creation_datetime(Path(file_path))
            if not dt:
                try:
                    dt = datetime.fromtimestamp(os.path.getmtime(file_path))
                except Exception:
                    dt = None
            info.dt = dt
        return info

    # 视频文件 - 使用 ExifTool 提取（音频没有标准 EXIF/PIL 可读，
    # 走图像路径会导致 exifread 与 PIL 对每个音频文件抛异常刷屏）
    if ext in VIDEO_EXTENSIONS:
        lat, lon, alt, dt = extract_video_gps_with_exiftool(file_path)
        info.latitude = lat
        info.longitude = lon
        info.altitude = alt
        if dt:
            info.dt = dt
        elif not only_process_with_date:
            # 仅当未启用"只处理有日期的文件"时才回退到文件系统创建时间，
            # 否则该选项对视频无效（创建时间几乎总是存在）
            dt = get_file_creation_datetime(Path(file_path))
            if dt:
                info.dt = dt
        if only_process_with_date and info.dt is None:
            return info
        return info

    # 图像文件 - 一次解析 EXIF 同时提取日期与 GPS（避免对同一文件重复解析）
    dt, lat, lon, alt = extract_exif_metadata(file_path)
    info.dt = dt

    if lat is not None and lon is not None:
        info.latitude = lat
        info.longitude = lon
        info.altitude = alt
    else:
        lat, lon, alt = extract_pil_gps(file_path)
        info.latitude = lat
        info.longitude = lon
        info.altitude = alt

    # 如果仍然没有日期信息，使用文件修改时间作为备选
    # 但仅在只处理有日期的模式关闭时使用
    if info.dt is None and not only_process_with_date:
        try:
            info.dt = datetime.fromtimestamp(os.path.getmtime(file_path))
        except Exception:
            info.dt = None

    return info



