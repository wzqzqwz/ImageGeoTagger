"""地理位置处理服务

核心功能：为没有 GPS 信息的文件自动匹配位置。
使用基于时间的迭代匹配算法，将文件的拍摄时间与已知的 GPS 参考点（
来自其他有 GPS 的文件和 GPX 轨迹数据）进行匹配。

核心算法：
  1. 将所有已知 GPS 位置按时间排序
  2. 对每个无 GPS 的文件，二分查找时间最接近的参考点
  3. 如果时间差在阈值内，分配该位置
  4. 迭代多轮，每轮新获得位置的文件可作为下一轮的参考点
"""

import os
import threading
import traceback
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from ImageGeoTagger.config import RAW_EXTENSIONS, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS
from ImageGeoTagger.utils.exif_utils import (
    update_image_gps, update_raw_gps, update_video_gps, update_audio_gps
)
from ImageGeoTagger.utils.i18n import _


def _get_dt(obj):
    """从 MediaFileInfo (.dt) 或 GpsPoint (.timestamp) 获取 datetime"""
    return getattr(obj, 'dt', getattr(obj, 'timestamp', None))


def _to_utc_naive(dt):
    """将任意 datetime 转为 UTC 无时区 datetime，确保比较基准一致

    若 dt 带有时区信息，先转换为 UTC 再剥离 tzinfo；
    若已是 naive datetime，直接返回。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def process_location_info(a_list, b_list, gps_data, threshold_minutes=30,
                          max_iterations=10, progress_callback=None,
                          iteration_callback=None, log_callback=None,
                          lock=None):
    """处理无 GPS 文件的位置信息

    通过时间匹配算法，为没有 GPS 坐标的文件分配位置。
    算法迭代多轮，每轮新匹配的文件可作为后续轮次的参考点。

    Args:
        a_list: 已有 GPS 数据的 MediaFileInfo 列表
        b_list: 没有 GPS 数据的 MediaFileInfo 列表
        gps_data: 来自 GPX 文件的 GpsPoint 列表
        threshold_minutes: 时间匹配阈值（分钟）
        max_iterations: 最大迭代轮数
        progress_callback: 进度回调函数(百分比, 已处理, 总数, 标签)
        iteration_callback: 迭代回调函数(当前轮次, 总轮次)
        log_callback: 日志回调函数(消息字符串)

    Returns:
        tuple: (更新文件数, 最终 a_list, 最终 b_list)
    """
    if lock is None:
        lock = threading.Lock()
    updated_count = 0

    # 构建按时间排序的参考点列表（统一转为 UTC naive 再排序）
    # 参考点来自：已有 GPS 的文件 + GPX 轨迹点
    all_reference = list(a_list) + list(gps_data)
    sorted_ref = sorted(
        [x for x in all_reference if _get_dt(x) is not None],
        key=lambda x: _to_utc_naive(_get_dt(x)) or datetime.min
    )

    iteration = 0
    updated_in_iteration = True

    # 创建线程池，在整个处理过程中复用
    max_workers = min(32, (os.cpu_count() or 4) * 2)
    match_pool = ThreadPoolExecutor(max_workers=max_workers)
    write_pool = ThreadPoolExecutor(max_workers=max_workers)

    try:
        # 迭代处理，直到没有文件可以匹配或达到最大迭代次数
        while iteration < max_iterations and updated_in_iteration and b_list:
            iteration += 1
            updated_in_iteration = False
            updated_files = []

            if iteration_callback:
                iteration_callback(iteration, max_iterations)

            # 只处理有日期信息的文件（没有日期无法进行时间匹配）
            b_with_time = [img for img in b_list if img.dt]
            total = len(b_with_time)
            processed = 0

            futs = {}
            for img in b_with_time:
                futs[match_pool.submit(
                    _match_single_image, img, sorted_ref, threshold_minutes
                )] = img

            for fut in as_completed(futs):
                try:
                    result = fut.result()
                    processed += 1
                    if progress_callback:
                        progress_callback(processed / total * 100, processed, total, _("比对进度"))

                    if result:
                        updated_files.append(result)
                        updated_in_iteration = True
                        if log_callback:
                            log_callback(
                                _("已更新: ") + f"{result.filename} - "
                                + _("位置: ") + f"({result.latitude:.6f}, {result.longitude:.6f})"
                            )
                except Exception:
                    traceback.print_exc()
                    processed += 1
                    if progress_callback:
                        progress_callback(processed / total * 100, processed, total, _("比对进度"))

            # 将本轮匹配到的文件写入实际的 GPS 数据
            if updated_files:
                write_total = len(updated_files)
                write_done = 0
                write_futs = {}
                for f in updated_files:
                    loc = {'latitude': f.latitude, 'longitude': f.longitude, 'altitude': f.altitude}
                    write_futs[write_pool.submit(_update_file_location, f.path, loc)] = f
                for wfut in as_completed(write_futs):
                    f = write_futs[wfut]
                    try:
                        wfut.result()
                        write_done += 1
                        updated_count += 1
                        with lock:
                            if f in b_list:
                                b_list.remove(f)
                            a_list.append(f)
                    except Exception as write_err:
                        write_done += 1
                        err_msg = str(write_err) if str(write_err) else type(write_err).__name__
                        retries = getattr(f, '_write_retries', 0) + 1
                        f._write_retries = retries
                        if log_callback:
                            log_callback(
                                _("写入失败") + f"({retries}/3): {f.filename} - {err_msg}"
                            )
                    if progress_callback:
                        progress_callback(write_done / write_total * 100, write_done, write_total, _("写入进度"))

            # 重新构建排序参考列表（包含本轮新匹配的文件，统一时区后再排序）
            all_reference = list(a_list) + list(gps_data)
            sorted_ref = sorted(
                [x for x in all_reference if _get_dt(x) is not None],
                key=lambda x: _to_utc_naive(_get_dt(x)) or datetime.min
            )
    finally:
        match_pool.shutdown(wait=False)
        write_pool.shutdown(wait=False)

    # 最终按时间排序两个列表（统一时区后再排序）
    a_list.sort(key=lambda x: _to_utc_naive(x.dt) if x.dt else datetime.min)
    b_list.sort(key=lambda x: _to_utc_naive(x.dt) if x.dt else datetime.min)

    return updated_count, a_list, b_list


def _match_single_image(img_info, sorted_ref, threshold_minutes, max_retries=3):
    """匹配单个文件到最近的 GPS 参考点

    如果找到时间差在阈值内的参考点，直接将位置信息赋值给文件对象。
    写入失败的文件会重试最多 max_retries 次。

    Args:
        img_info: 要匹配的 MediaFileInfo 对象
        sorted_ref: 按时间排序的参考点列表
        threshold_minutes: 时间差阈值（分钟）
        max_retries: 写入失败最大重试次数（默认 3）

    Returns:
        匹配成功返回更新后的 MediaFileInfo，否则返回 None
    """
    if img_info.dt is None:
        return None

    # 写入失败次数已达上限，不再重试
    retries = getattr(img_info, '_write_retries', 0)
    if retries >= max_retries:
        return None

    closest = _find_closest_by_time(img_info.dt, sorted_ref, threshold_minutes)
    if closest:
        img_info.latitude = closest.latitude
        img_info.longitude = closest.longitude
        img_info.altitude = closest.altitude
        return img_info
    return None


def _find_closest_by_time(target_time, sorted_ref, threshold_minutes):
    """通过二分查找找到时间最接近的参考点

    算法：
      1. 先用二分查找定位到最接近的索引位置
      2. 然后向两边扩展，找到真正最接近的点
      3. 如果时间差超过阈值，返回 None

    Args:
        target_time: 目标时间
        sorted_ref: 按时间排序的参考点列表
        threshold_minutes: 最大时间差（分钟）

    Returns:
        最接近的参考点，或 None（如果没有在阈值内的点）
    """
    if not sorted_ref:
        return None

    dt = timedelta(minutes=threshold_minutes)
    left, right = 0, len(sorted_ref) - 1
    best = None
    best_diff = dt
    best_idx = -1

    # 统一转为 UTC naive 再比较，避免时区感知 datetime 与 naive datetime 比较出错
    target_time = _to_utc_naive(target_time)

    # 二分查找：找到时间最接近 target_time 的参考点
    while left <= right:
        mid = (left + right) // 2
        ref_dt = _get_dt(sorted_ref[mid])
        if ref_dt is None:
            left = mid + 1
            continue
        ref_dt = _to_utc_naive(ref_dt)
        diff = abs(ref_dt - target_time)

        if diff < best_diff:
            best_diff = diff
            best = sorted_ref[mid]
            best_idx = mid

        if ref_dt < target_time:
            left = mid + 1
        else:
            right = mid - 1

    if best is None or best_diff > dt:
        return None

    # 从最佳匹配点向左右两边扩展搜索
    # 因为时间排序是连续的，相邻的点可能时间差更小
    l, r = best_idx - 1, best_idx + 1
    while l >= 0:
        ref_dt = _get_dt(sorted_ref[l])
        if ref_dt is None:
            l -= 1
            continue
        ref_dt = _to_utc_naive(ref_dt)
        diff = abs(ref_dt - target_time)
        if diff < best_diff:
            best_diff = diff
            best = sorted_ref[l]
        elif diff > best_diff:
            break
        l -= 1

    while r < len(sorted_ref):
        ref_dt = _get_dt(sorted_ref[r])
        if ref_dt is None:
            r += 1
            continue
        ref_dt = _to_utc_naive(ref_dt)
        diff = abs(ref_dt - target_time)
        if diff < best_diff:
            best_diff = diff
            best = sorted_ref[r]
        elif diff > best_diff:
            break
        r += 1

    return best


def _update_file_location(file_path, location_info):
    """根据文件类型将位置信息写入文件

    根据文件扩展名自动选择对应的 GPS 写入方法。

    Args:
        file_path: 文件路径
        location_info: 包含 latitude, longitude, altitude 的字典
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext in RAW_EXTENSIONS:
        update_raw_gps(file_path, location_info)
    elif ext in VIDEO_EXTENSIONS:
        update_video_gps(file_path, location_info)
    elif ext in AUDIO_EXTENSIONS:
        update_audio_gps(file_path, location_info)
    else:
        update_image_gps(file_path, location_info)


def find_files_with_same_location(app, target_lat, target_lon, target_alt,
                                   tolerance=1e-6):
    """查找所有与目标位置相同的文件

    用于批量编辑时，找出所有与当前编辑文件位置相同的文件。

    Args:
        app: 主应用对象（包含 a 和 b 列表）
        target_lat: 目标纬度
        target_lon: 目标经度
        target_alt: 目标高度（可选）
        tolerance: 比较容差

    Returns:
        list: 位置匹配的文件列表
    """
    matches = []
    with app.lock:
        all_files = list(app.a) + list(app.b)
    for f in all_files:
        lat = f.latitude if hasattr(f, 'latitude') else None
        lon = f.longitude if hasattr(f, 'longitude') else None
        alt = f.altitude if hasattr(f, 'altitude') else None
        if lat is not None and lon is not None:
            if abs(lat - target_lat) < tolerance and abs(lon - target_lon) < tolerance:
                if target_alt is None or alt is None or abs((alt or 0) - target_alt) < tolerance:
                    matches.append(f)
    return matches


def batch_update_same_location_files(app, target_lat, target_lon, target_alt,
                                      new_lat, new_lon, new_alt,
                                      tolerance=1e-6):
    """批量更新所有具有相同位置的文件

    将所有与目标位置相同的文件更新为新的位置信息。

    Args:
        app: 主应用对象
        target_lat/lon/alt: 要匹配的目标位置
        new_lat/lon/alt: 新的位置信息
        tolerance: 位置比较容差

    Returns:
        tuple: (成功数, 失败数)
    """
    matches = find_files_with_same_location(app, target_lat, target_lon,
                                             target_alt, tolerance)
    success = 0
    failed = 0
    for f in matches:
        try:
            loc = {'latitude': new_lat, 'longitude': new_lon, 'altitude': new_alt}
            _update_file_location(f.path if hasattr(f, 'path') else '', loc)
            f.latitude = new_lat
            f.longitude = new_lon
            f.altitude = new_alt
            success += 1
        except Exception:
            traceback.print_exc()
            failed += 1
    return success, failed
