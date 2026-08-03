"""媒体文件工具函数

提供媒体文件相关的通用功能：
  - 判断文件是否为支持的媒体类型
  - 从文件名中解析日期时间
  - 获取文件的创建日期（跨平台）
  - 获取文件的最佳可用日期时间（多来源优先级）
"""

import os
import platform
import re
import traceback
from datetime import datetime
from pathlib import Path

from config import ALL_MEDIA_EXTENSIONS, DATETIME_PATTERNS
from utils.exif_utils import read_exif_datetime, read_quicktime_datetime


def is_media_file(file_path):
    """检查文件是否为支持的媒体类型

    Args:
        file_path: 文件路径

    Returns:
        bool: 是否支持的媒体文件
    """
    return Path(file_path).suffix.lower() in ALL_MEDIA_EXTENSIONS


def format_gps_coord(value):
    """格式化 GPS 坐标值用于显示/预填

    坐标经度分秒有理数换算回十进制后存在浮点表示噪声
    （如 35.420289999999994），固定 8 位小数（1e-8 度 ≈ 1 毫米）
    并去除尾零，可还原为用户输入的原值（35.42029）。

    Args:
        value: 经纬度浮点值或 None

    Returns:
        str: 格式化后的字符串，None 返回空字符串
    """
    if value is None:
        return ''
    return f"{value:.8f}".rstrip('0').rstrip('.')


def parse_datetime_from_filename(filename):
    """从文件名中解析日期时间

    使用配置文件中定义的正则表达式模式进行匹配。
    支持多种常见命名格式，如 YYYY-MM-DD_HH-MM-SS、YYYYMMDD_HHMMSS 等。

    Args:
        filename: 文件名（带扩展名）

    Returns:
        datetime or None: 解析成功返回 datetime 对象，失败返回 None
    """
    if not filename:
        return None
    # 去掉文件扩展名，只解析文件名的 stem 部分
    name_without_ext = Path(filename).stem
    for pattern in DATETIME_PATTERNS:
        match = re.search(pattern, name_without_ext)
        if match:
            groups = match.groups()
            try:
                if len(groups) == 6:
                    if len(groups[0]) == 2:
                        # 时间在前格式：HH-MM-SS_YYYY-MM-DD / HHMMSS_YYYYMMDD
                        # 前 3 组是时分秒，后 3 组才是年月日
                        hour, minute, second, year, month, day = groups
                    else:
                        # 完整日期时间：YYYY-MM-DD HH:MM:SS
                        year, month, day, hour, minute, second = groups
                    return datetime(int(year), int(month), int(day),
                                    int(hour), int(minute), int(second))
                elif len(groups) == 3:
                    # 仅日期：YYYY-MM-DD
                    year, month, day = groups
                    return datetime(int(year), int(month), int(day))
            except ValueError:
                continue  # 无效日期跳过当前模式
    return None


QUICKTIME_EXTENSIONS = frozenset({'.mov', '.mp4', '.m4v', '.3gp', '.3g2'})


def get_file_creation_datetime(file_path):
    """从操作系统元数据获取文件创建日期

    不同平台的文件创建时间获取方式不同：
      - macOS/BSD: st_birthtime（真正的创建时间）
      - Windows: st_ctime（文件创建时间）
      - Linux: 没有可移植的创建时间 API，返回 None

    Args:
        file_path: 文件路径（Path 对象）

    Returns:
        datetime or None
    """
    try:
        stat_result = file_path.stat()
        if hasattr(stat_result, 'st_birthtime') and stat_result.st_birthtime is not None:
            return datetime.fromtimestamp(stat_result.st_birthtime)
        if platform.system() == 'Windows':
            return datetime.fromtimestamp(stat_result.st_ctime)
    except (OSError, AttributeError, OverflowError, ValueError):
        traceback.print_exc()
    return None


def get_existing_datetime(file_path, fallback_to_fs=True):
    """获取文件的最佳可用日期时间

    优先级（从高到低）：
      1. EXIF 拍摄日期（适用于图像文件）
      2. QuickTime 创建日期（适用于 MOV/MP4 视频）
      3. 文件系统创建时间（fallback_to_fs 为 True 时）
      4. 文件系统修改时间（fallback_to_fs 为 True 时）

    Args:
        file_path: 文件路径
        fallback_to_fs: 是否在无 EXIF 日期时回退到文件系统时间。
            判断"是否有拍摄日期"时应传 False，否则 Windows/Linux 上
            创建/修改时间几乎总是存在，会把无 EXIF 日期的文件误判为已有日期。

    Returns:
        datetime or None
    """
    file_path = Path(file_path)
    if not file_path.exists():
        return None

    # 第一优先级：EXIF 日期
    dt = read_exif_datetime(file_path)
    if dt is not None:
        return dt

    # 第二优先级：QuickTime 日期（仅视频文件）
    if file_path.suffix.lower() in QUICKTIME_EXTENSIONS:
        dt = read_quicktime_datetime(file_path)
        if dt is not None:
            return dt

    if not fallback_to_fs:
        return None

    # 第三优先级：文件创建时间
    dt = get_file_creation_datetime(file_path)
    if dt is not None:
        return dt

    # 第四优先级：文件修改时间
    try:
        return datetime.fromtimestamp(file_path.stat().st_mtime)
    except (OSError, ValueError, OverflowError):
        return None
