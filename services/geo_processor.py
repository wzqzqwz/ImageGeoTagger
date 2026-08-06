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
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import RAW_EXTENSIONS, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS
from utils.exif_utils import (
    update_image_gps, update_raw_gps, update_video_gps, update_audio_gps
)
from utils.i18n import _
from utils.logging_utils import log_exc


def _get_dt(obj):
    """从 MediaFileInfo (.dt) 或 GpsPoint (.timestamp) 获取 datetime"""
    return getattr(obj, 'dt', getattr(obj, 'timestamp', None))


def _to_utc_naive(dt):
    """将任意 datetime 转为本地无时区 datetime，确保比较基准一致

    照片 EXIF 日期、GPX 时间等在数据引入时均已按"本地时间"存储，
    因此这里统一把带时区的值先转成本地时间再剥离 tzinfo，
    保证带偏移值（如视频 +08:00）与本地 naive 值在同一条时间线上比较。

    注意：名称中的 "utc" 仅表示"统一基准"（unified），语义是转本地
    时间（astimezone() 使用本机时区），并非转 UTC。在"相机时区 == 本机
    时区"的假设下自洽；跨时区拍摄、回国后处理的场景会系统性偏移，
    需要 UI 层提供时区修正选项（当前未实现）。

    若 dt 已是 naive datetime，直接返回。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def process_location_info(a_list, b_list, gps_data, threshold_minutes=30,
                          max_iterations=10, progress_callback=None,
                          iteration_callback=None, log_callback=None,
                          lock=None, dry_run=False):
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
        lock: 并发访问 a_list/b_list 的互斥锁
        dry_run: 试运行模式。为 True 时只匹配与记录"将写入"日志，
                不写盘、不移动文件；返回值 updated_count 表示
                "将更新的文件数"。

    Returns:
        tuple: (更新文件数, 最终 a_list, 最终 b_list)
    """
    if lock is None:
        lock = threading.Lock()

    # 重置上次运行遗留的写入失败重试计数：一次临时故障不应耗尽
    # 后续运行的重试额度（_write_retries 存于文件对象上会跨运行累积）
    with lock:
        for img in b_list:
            if getattr(img, '_write_retries', 0):
                img._write_retries = 0

    updated_count = 0

    # 构建按时间排序的参考点列表（统一转为 UTC naive 再排序）
    # 参考点来自：已有 GPS 的文件 + GPX 轨迹点
    # 加锁快照：主线程的编辑/删除对话框会并发修改 app.a/app.b
    with lock:
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
        while iteration < max_iterations and updated_in_iteration:
            with lock:
                has_b = bool(b_list)
            if not has_b:
                break
            iteration += 1
            updated_in_iteration = False
            updated_files = []

            if iteration_callback:
                iteration_callback(iteration, max_iterations)

            # 只处理有日期信息的文件（没有日期无法进行时间匹配）
            # datetime.min（清空日期后的占位值）也视为无有效时间
            # 加锁快照迭代，避免与主线程列表修改产生 "list changed size during iteration"
            with lock:
                b_with_time = [img for img in b_list
                               if img.dt and img.dt != datetime.min]
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
                except Exception:
                    # 匹配阶段真实异常：不应静默吞掉；若未提供日志回调则至少在控制台可查
                    log_exc()
                    if log_callback:
                        try:
                            log_callback(_("匹配异常: ") + repr(futs[fut]) + " - " + (traceback.format_exc(limit=1)))
                        except Exception:
                            log_exc()
                    result = None
                processed += 1
                try:
                    if progress_callback:
                        progress_callback(processed / total * 100, processed, total, _("比对进度"))
                except Exception:
                    log_exc()

                if result:
                    # result = (img, loc)；写盘成功前不修改 img 对象属性，
                    # 避免列表渲染读到预置坐标及对象与磁盘不一致
                    updated_files.append(result)
                    updated_in_iteration = True

            if dry_run:
                # 试运行：只记录"将写入"日志，不写盘、不移动文件。
                # 单轮匹配结果即完整（本轮不改变任何状态），直接结束，
                # 避免空转满 max_iterations 轮
                for f, loc in updated_files:
                    updated_count += 1
                    try:
                        if log_callback:
                            log_callback(
                                _("试运行: ") + f"{f.filename} - "
                                + _("位置: ") + f"({loc['latitude']:.8f}, {loc['longitude']:.8f})")
                    except Exception:
                        log_exc()
                break

            # 将本轮匹配到的文件写入实际的 GPS 数据
            if updated_files:
                write_total = len(updated_files)
                write_done = 0
                write_futs = {}
                for f, loc in updated_files:
                    write_futs[write_pool.submit(_update_file_location, f.path, loc)] = (f, loc)
                moved_files = []
                for wfut in as_completed(write_futs):
                    f, loc = write_futs[wfut]
                    succeeded = False
                    err_msg = ""
                    try:
                        wfut.result()
                        write_done += 1
                        moved_files.append((f, loc))
                        succeeded = True
                    except Exception as write_err:
                        write_done += 1
                        err_msg = str(write_err) if str(write_err) else type(write_err).__name__
                        retries = getattr(f, '_write_retries', 0) + 1
                        f._write_retries = retries
                    # 回调无论成功失败都要执行，但回调异常绝不能干扰业务结果判定
                    try:
                        if log_callback:
                            # 写盘成功后才输出"已更新"日志，避免先报成功再报失败
                            if succeeded:
                                log_callback(
                                    _("已更新: ") + f"{f.filename} - "
                                    + _("位置: ") + f"({loc['latitude']:.8f}, {loc['longitude']:.8f})"
                                )
                            else:
                                log_callback(
                                    _("写入失败") + f"({retries}/3): {f.filename} - {err_msg}"
                                )
                    except Exception:
                        log_exc()
                    try:
                        if progress_callback:
                            progress_callback(write_done / write_total * 100, write_done, write_total, _("写入进度"))
                    except Exception:
                        # 与匹配阶段一致：回调异常只记日志，不能中断收尾流程
                        # （否则 moved_files 的列表迁移/统计被跳过，磁盘已写而 UI 状态失真）
                        log_exc()
                if moved_files:
                    # 批量移动分类：O(n) 重建列表，避免锁内逐文件 in/remove 的 O(n²)
                    with lock:
                        ids_in_b = {id(x) for x in b_list}
                        to_move = [(f, loc) for f, loc in moved_files if id(f) in ids_in_b]
                        if to_move:
                            rset = {id(f) for f, _ in to_move}
                            b_list[:] = [x for x in b_list if id(x) not in rset]
                            a_list.extend(f for f, _ in to_move)
                            # 只在文件确实移入 a_list 时计数，与 UI 列表状态一致，
                            # 避免处理期间被编辑对话框移出列表的文件造成统计虚高
                            updated_count += len(to_move)
                        # 写盘成功即回填坐标（含处理期间被移出 b_list 的文件），
                        # 保证对象与磁盘实际状态一致且主线程渲染无撕裂读
                        for f, loc in moved_files:
                            f.latitude = loc['latitude']
                            f.longitude = loc['longitude']
                            f.altitude = loc['altitude']

            # 重新构建排序参考列表（包含本轮新匹配的文件，统一时区后再排序）
            with lock:
                all_reference = list(a_list) + list(gps_data)
            sorted_ref = sorted(
                [x for x in all_reference if _get_dt(x) is not None],
                key=lambda x: _to_utc_naive(_get_dt(x)) or datetime.min
            )
    finally:
        # wait=True 确保所有已提交的写文件任务完成后再返回，
        # 避免窗口关闭时写入被截断导致文件损坏
        try:
            match_pool.shutdown(wait=True)
            write_pool.shutdown(wait=True)
        except Exception:
            log_exc()

    # 最终按时间排序两个列表（统一时区后再排序）
    with lock:
        a_list.sort(key=lambda x: _to_utc_naive(x.dt) if x.dt else datetime.min)
        b_list.sort(key=lambda x: _to_utc_naive(x.dt) if x.dt else datetime.min)

    return updated_count, a_list, b_list


def _match_single_image(img_info, sorted_ref, threshold_minutes, max_retries=3):
    """匹配单个文件到最近的 GPS 参考点

    如果找到时间差在阈值内的参考点，返回 (img_info, 位置字典)；
    匹配成功后不修改 img_info 对象属性，由调用方在写盘成功后再写入，
    避免对象与磁盘不一致及主线程渲染的撕裂读。
    写入失败的文件会重试最多 max_retries 次。

    Args:
        img_info: 要匹配的 MediaFileInfo 对象
        sorted_ref: 按时间排序的参考点列表
        threshold_minutes: 时间差阈值（分钟）
        max_retries: 写入失败最大重试次数（默认 3）

    Returns:
        匹配成功返回 (MediaFileInfo, dict)，否则返回 None
    """
    if img_info.dt is None or img_info.dt == datetime.min:
        return None

    # 写入失败次数已达上限，不再重试
    retries = getattr(img_info, '_write_retries', 0)
    if retries >= max_retries:
        return None

    closest = _find_closest_by_time(img_info.dt, sorted_ref, threshold_minutes)
    if closest:
        return img_info, {
            'latitude': closest.latitude,
            'longitude': closest.longitude,
            'altitude': closest.altitude,
        }
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

        # 用 <= 确保时间差恰好等于阈值的点也能被匹配（边界情况）
        if diff <= best_diff:
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
        if diff <= best_diff:
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
        if diff <= best_diff:
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
                # 高度语义：目标无高度=不参与比较；目标有高度时
                # 无高度文件（None）视为不相等，避免误匹配
                if target_alt is None:
                    matches.append(f)
                elif alt is not None and abs(alt - target_alt) < tolerance:
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
            # 属性回填加锁，避免与主线程列表渲染/结果窗口刷新并发读到半更新状态
            with app.lock:
                f.latitude = new_lat
                f.longitude = new_lon
                f.altitude = new_alt
            success += 1
        except Exception:
            log_exc()
            failed += 1
    return success, failed
