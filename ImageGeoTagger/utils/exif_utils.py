"""EXIF/GPS 读写工具函数

提供了完整的 EXIF 元数据读写功能，支持：
  - 读取图像文件的 EXIF 日期和 GPS 信息（使用 exifread / Pillow）
  - 读取 QuickTime 视频文件的创建日期（通过二进制解析）
  - 写入 GPS 坐标到图像/RAW/视频/音频文件（piexif + ExifTool 双方案）
  - 写入/清除拍摄日期
  - ExifTool 自动检测和调用

技术说明：
  - 图像文件优先使用 piexif（纯 Python EXIF 库）写入
  - RAW/视频/音频文件必须依赖 ExifTool 外部工具
  - piexif 失败时自动回退到 ExifTool
"""

import os
import platform
import struct
import shutil
import tempfile
import subprocess
import json
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import exifread
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import piexif
from ImageGeoTagger.utils.i18n import _

from ImageGeoTagger.config import RAW_EXTENSIONS, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS, PIE_SUPPORTED_EXTENSIONS
from ImageGeoTagger.utils.platform_utils import get_startupinfo, get_app_dir


def _get_stat(file_path):
    s = os.stat(file_path)
    return s.st_atime, s.st_mtime


def read_exif_datetime(file_path):
    """读取图像文件的 EXIF 拍摄日期

    按优先级尝试三种 EXIF 标签：
      - DateTimeOriginal（原始拍摄日期）
      - Image DateTime（图像修改日期）
      - DateTimeDigitized（数字化日期）

    Args:
        file_path: 文件路径

    Returns:
        datetime or None: 解析成功返回 datetime 对象，失败返回 None
        如果 EXIF 中存在日期标签但解析失败，返回 datetime.min
    """
    try:
        with open(file_path, 'rb') as f:
            tags = exifread.process_file(f, details=False)
            for tag in ['EXIF DateTimeOriginal', 'Image DateTime', 'EXIF DateTimeDigitized']:
                if tag in tags:
                    dt_str = str(tags[tag])
                    if dt_str.strip():
                        for fmt in ['%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S']:
                            try:
                                return datetime.strptime(dt_str, fmt)
                            except ValueError:
                                continue
            # 如果存在日期标签但格式无法解析，返回 None
            return None
    except (OSError, ValueError, KeyError, struct.error):
        traceback.print_exc()
        return None


def read_quicktime_datetime(file_path):
    """从 QuickTime 视频文件中读取创建日期

    通过解析 QuickTime 的二进制容器格式（atom 结构）来提取创建时间。
    QuickTime 时间基准是 1904-01-01（Mac 标准纪元）。
    使用增量读取方式避免将整个大视频文件加载到内存。

    Args:
        file_path: 视频文件路径

    Returns:
        datetime or None: 解析成功返回本地时间，失败返回 None
    """
    ATOM_HEADER_SIZE = 8
    try:
        with open(file_path, 'rb') as f:
            # 逐层查找 moov → mvhd，只读取 atom header 和必要的 payload
            def _find_moov(fh, file_size):
                pos = 0
                while pos + ATOM_HEADER_SIZE <= file_size:
                    fh.seek(pos)
                    header = fh.read(ATOM_HEADER_SIZE)
                    if len(header) < ATOM_HEADER_SIZE:
                        break
                    atom_size, atom_type = struct.unpack('>I4s', header)
                    if atom_size == 0:
                        break
                    if atom_type == b'moov':
                        return pos, atom_size
                    pos += atom_size
                return None, None

            f.seek(0, 2)
            file_size = f.tell()
            if file_size < ATOM_HEADER_SIZE:
                return None

            moov_pos, moov_size = _find_moov(f, file_size)
            if moov_pos is None or moov_size is None:
                return None

            # 在 moov atom 中查找 'mvhd'
            moov_end = min(moov_pos + moov_size, file_size)
            search_pos = moov_pos + ATOM_HEADER_SIZE
            while search_pos + ATOM_HEADER_SIZE <= moov_end:
                f.seek(search_pos)
                sub_header = f.read(ATOM_HEADER_SIZE)
                if len(sub_header) < ATOM_HEADER_SIZE:
                    break
                sub_size, sub_type = struct.unpack('>I4s', sub_header)
                if sub_size == 0:
                    break
                if sub_type == b'mvhd':
                    # mvhd: 读取版本字节和 creation time
                    read_len = min(sub_size, ATOM_HEADER_SIZE + 28) if sub_size > 0 else ATOM_HEADER_SIZE + 28
                    f.seek(search_pos)
                    mvhd_data = f.read(read_len)
                    if len(mvhd_data) < ATOM_HEADER_SIZE + 5:
                        break
                    ver = mvhd_data[ATOM_HEADER_SIZE]
                    if ver == 0:
                        qt_time = struct.unpack('>I', mvhd_data[ATOM_HEADER_SIZE + 4:ATOM_HEADER_SIZE + 8])[0]
                    else:
                        if len(mvhd_data) < ATOM_HEADER_SIZE + 28:
                            break
                        qt_time = struct.unpack('>Q', mvhd_data[ATOM_HEADER_SIZE + 12:ATOM_HEADER_SIZE + 20])[0]
                    utc_dt = (datetime(1904, 1, 1) + timedelta(seconds=qt_time)).replace(tzinfo=timezone.utc)
                    return utc_dt.astimezone().replace(tzinfo=None)
                search_pos += sub_size
    except (OSError, ValueError, KeyError, struct.error, MemoryError) as e:
        traceback.print_exc()
    return None


def to_degrees(value):
    """将十进制度数转换为 (度, 分, 秒) 格式（带进位处理）

    EXIF 标准使用度/分/秒的分数形式存储 GPS 坐标。
    例如：116.39747° 转换为 ((116,1), (23,1), (5099,100))

    Args:
        value: 十进制度数值

    Returns:
        tuple: ((度, 分母), (分, 分母), (秒, 分母))
    """
    d = int(value)
    rest = (value - d) * 60
    m = int(rest)
    s = round((rest - m) * 60 * 100)
    if s >= 6000:
        m += s // 6000
        s = s % 6000
    if m >= 60:
        d += m // 60
        m = m % 60
    return ((d, 1), (m, 1), (s, 100))


def parse_video_time(time_str):
    """解析视频文件的时间字符串（自动处理 UTC 到本地时间的转换）

    视频文件通常使用 UTC 时间存储（带 Z 后缀），
    此函数会自动将其转换为本地时区。

    Args:
        time_str: 时间字符串（支持多种格式，可能带 Z 后缀）

    Returns:
        datetime or None: 本地时间
    """
    try:
        common_formats = [
            "%Y:%m:%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%fZ",
        ]
        is_utc = False
        if time_str.endswith('Z') or 'UTC' in time_str.upper():
            is_utc = True
            time_str = time_str.replace('Z', '').replace('UTC', '').strip()

        for fmt in common_formats:
            try:
                parsed = datetime.strptime(time_str, fmt.replace('Z', ''))
                if is_utc:
                    utc_time = parsed.replace(tzinfo=timezone.utc)
                    return utc_time.astimezone().replace(tzinfo=None)
                return parsed
            except ValueError:
                continue
    except (OSError, ValueError, KeyError, struct.error):
        traceback.print_exc()
    return None


def get_exiftool_path():
    """查找 ExifTool 可执行文件的路径

    搜索顺序：
      1. 系统 PATH 环境变量
      2. 配置文件中定义的常见安装路径
      3. 应用程序目录

    Returns:
        str or None: ExifTool 的完整路径，未找到返回 None
    """
    system = platform.system()

    if system == "Windows":
        names = ['exiftool.exe', 'exiftool(-k).exe']
        sysdirs = os.environ.get('PATH', '').split(os.pathsep)
        from ImageGeoTagger.config import WINDOWS_EXIFTOOL_PATHS
        sysdirs += WINDOWS_EXIFTOOL_PATHS
    else:
        names = ['exiftool']
        sysdirs = os.environ.get('PATH', '').split(os.pathsep)
        from ImageGeoTagger.config import UNIX_EXIFTOOL_PATHS
        sysdirs += UNIX_EXIFTOOL_PATHS

    app_dir = get_app_dir()
    project_root = os.path.dirname(app_dir)
    bundled_dir = os.path.join(app_dir, 'exiftool')
    bundled_dir_root = os.path.join(project_root, 'exiftool')
    sysdirs = [bundled_dir, bundled_dir_root, app_dir, project_root] + sysdirs

    for path in sysdirs:
        for n in names:
            fp = os.path.join(path, n)
            if os.path.isfile(fp):
                if system == "Windows" or os.access(fp, os.X_OK):
                    return fp
    return None


def check_exiftool():
    """检查 ExifTool 是否可用

    先检查配置路径中的 ExifTool，再尝试直接在命令行中调用。
    通过运行 'exiftool -ver' 验证是否可用。

    Returns:
        tuple: (是否可用, 工具路径或可执行文件名)
    """
    exiftool_path = get_exiftool_path()
    si = get_startupinfo()
    if exiftool_path:
        try:
            r = subprocess.run([exiftool_path, '-ver'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=5, startupinfo=si, errors='replace')
            if r.returncode == 0:
                return True, exiftool_path
        except Exception:
            traceback.print_exc()
    try:
        r = subprocess.run(['exiftool', '-ver'],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=5, startupinfo=si, errors='replace')
        if r.returncode == 0:
            return True, 'exiftool'
    except Exception:
        traceback.print_exc()
    return False, None


def _build_gps_exiftool_args(location_info):
    args = [
        f'-GPSLatitude={location_info["latitude"]}',
        f'-GPSLongitude={location_info["longitude"]}',
        f'-GPSLatitudeRef={"N" if location_info["latitude"] >= 0 else "S"}',
        f'-GPSLongitudeRef={"E" if location_info["longitude"] >= 0 else "W"}',
    ]
    if location_info.get('altitude') is not None:
        args += [
            f'-GPSAltitude={abs(location_info["altitude"])}',
            f'-GPSAltitudeRef={"0" if location_info["altitude"] >= 0 else "1"}'
        ]
    return args

def _try_write_with_methods(file_path, methods):
    for method in methods:
        try:
            _run_exiftool(method, file_path)
            return True
        except Exception:
            traceback.print_exc()
            continue
    return False


def update_image_gps(file_path, location_info):
    """将 GPS 信息写入图像文件

    优先使用 piexif 库（纯 Python，速度快），
    如果 piexif 失败则自动回退到 ExifTool。
    处理过程中使用临时文件以确保数据安全。

    Args:
        file_path: 图像文件路径
        location_info: 包含 latitude, longitude, altitude 的字典
    """
    orig_atime, orig_mtime = _get_stat(file_path)
    ext = os.path.splitext(file_path)[1].lower()

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
        temp_path = temp_file.name
    try:
        shutil.copy2(file_path, temp_path)
        exif_dict = piexif.load(temp_path)

        gps_ifd = {
            piexif.GPSIFD.GPSVersionID: (2, 2, 0, 0),
            piexif.GPSIFD.GPSLatitudeRef: b'N' if location_info['latitude'] >= 0 else b'S',
            piexif.GPSIFD.GPSLatitude: to_degrees(abs(location_info['latitude'])),
            piexif.GPSIFD.GPSLongitudeRef: b'E' if location_info['longitude'] >= 0 else b'W',
            piexif.GPSIFD.GPSLongitude: to_degrees(abs(location_info['longitude']))
        }

        if location_info.get('altitude') is not None:
            gps_ifd[piexif.GPSIFD.GPSAltitudeRef] = 0 if location_info['altitude'] >= 0 else 1
            abs_alt = abs(location_info['altitude'])
            if abs(abs_alt - round(abs_alt)) < 1e-9:
                gps_ifd[piexif.GPSIFD.GPSAltitude] = (round(abs_alt), 1)
            else:
                numerator = round(abs_alt * 100)
                gps_ifd[piexif.GPSIFD.GPSAltitude] = (numerator, 100)

        exif_dict["GPS"] = gps_ifd
        piexif.insert(piexif.dump(exif_dict), temp_path)
        if os.path.getsize(temp_path) == 0:
            raise Exception(_("写入结果为空文件"))
        try:
            piexif.load(temp_path)
        except Exception:
            raise Exception(_("写入结果EXIF结构无效"))
        shutil.move(temp_path, file_path)
        os.utime(file_path, (orig_atime, orig_mtime))
    except Exception:
        traceback.print_exc()
        _write_gps_with_exiftool(file_path, location_info, orig_atime, orig_mtime)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _run_exiftool(args, file_path):
    """运行 ExifTool 命令行工具

    使用固定的参数组合：
      -overwrite_original: 直接覆盖原文件（不创建备份）
      -P: 保留原始文件时间戳

    Args:
        args: ExifTool 参数列表
        file_path: 要处理的文件路径

    Returns:
        subprocess.CompletedProcess

    Raises:
        Exception: ExifTool 不可用或运行失败
    """
    available, tool_path = check_exiftool()
    if not available:
        raise Exception(_("ExifTool 不可用，无法处理此文件"))

    cmd = [tool_path, '-overwrite_original', '-P', *args, file_path]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=60, startupinfo=get_startupinfo(),
                            errors='replace')
    if result.returncode != 0:
        msg = result.stderr or result.stdout or ""
        ext = os.path.splitext(file_path)[1].lower()
        if "not yet supported" in msg:
            raise Exception(_("暂不支持写入") + f" {ext.upper()} " + _("文件格式"))
        elif "FileName encoding must be specified" in msg:
            # 重试时加上 UTF-8 文件名编码参数
            retry_cmd = [tool_path, '-overwrite_original', '-P',
                         '-charset', 'Filename=UTF8', *args, file_path]
            result = subprocess.run(retry_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=60, startupinfo=get_startupinfo(),
                                    errors='replace')
            if result.returncode == 0:
                return result
            raise Exception(_("文件名包含非ASCII字符，处理失败"))
        else:
            raise Exception(msg)
    return result


def _write_gps_with_exiftool(file_path, location_info, orig_atime, orig_mtime):
    """使用 ExifTool 写入 GPS 坐标的内部函数（piexif 失败时的后备方案）"""
    gpscmd = _build_gps_exiftool_args(location_info)
    _run_exiftool(gpscmd, file_path)
    os.utime(file_path, (orig_atime, orig_mtime))


def update_raw_gps(file_path, location_info):
    orig_atime, orig_mtime = _get_stat(file_path)
    gpscmd = _build_gps_exiftool_args(location_info)
    _run_exiftool(gpscmd, file_path)
    os.utime(file_path, (orig_atime, orig_mtime))


def update_video_gps(file_path, location_info):
    """使用 ExifTool 将 GPS 信息写入视频文件

    尝试多种写入方法（Keys、QuickTime、XMP、UserData、标准 GPS 标签），
    因为不同视频格式支持的元数据容器不同。

    Args:
        file_path: 视频文件路径
        location_info: 包含 latitude, longitude, altitude 的字典
    """
    orig_atime, orig_mtime = _get_stat(file_path)
    ext = os.path.splitext(file_path)[1].lower()

    # 尝试五种不同的写入方法，支持不同视频格式的元数据容器
    methods = [
        [f'-Keys:GPSCoordinates={location_info["latitude"]},{location_info["longitude"]}',
         f'-Keys:GPSLatitude={location_info["latitude"]}',
         f'-Keys:GPSLongitude={location_info["longitude"]}'],
        [f'-QuickTime:GPSCoordinates={location_info["latitude"]},{location_info["longitude"]}',
         f'-QuickTime:GPSLatitude={location_info["latitude"]}',
         f'-QuickTime:GPSLongitude={location_info["longitude"]}'],
        [f'-XMP:GPSLatitude={location_info["latitude"]}',
         f'-XMP:GPSLongitude={location_info["longitude"]}',
         f'-XMP:GPSLatitudeRef={"N" if location_info["latitude"] >= 0 else "S"}',
         f'-XMP:GPSLongitudeRef={"E" if location_info["longitude"] >= 0 else "W"}'],
        [f'-UserData:GPSCoordinates={location_info["latitude"]},{location_info["longitude"]}',
         f'-UserData:GPSLatitude={location_info["latitude"]}',
         f'-UserData:GPSLongitude={location_info["longitude"]}'],
        [f'-GPSLatitude={location_info["latitude"]}',
         f'-GPSLongitude={location_info["longitude"]}',
         f'-GPSLatitudeRef={"N" if location_info["latitude"] >= 0 else "S"}',
         f'-GPSLongitudeRef={"E" if location_info["longitude"] >= 0 else "W"}']
    ]

    if location_info.get('altitude') is not None:
        for method in methods:
            method.append(f'-GPSAltitude={abs(location_info["altitude"])}')
            method.append(f'-GPSAltitudeRef={"0" if location_info["altitude"] >= 0 else "1"}')

    success = False
    for method in methods:
        try:
            _run_exiftool(method, file_path)
            success = True
            break
        except Exception:
            traceback.print_exc()
            continue

    if success:
        os.utime(file_path, (orig_atime, orig_mtime))
    else:
        raise Exception(_("所有GPS写入方法均失败，不支持的视频文件格式"))


def update_audio_gps(file_path, location_info):
    """使用 ExifTool 将 GPS 信息写入音频文件（通过 XMP 标签）

    音频文件主要使用 XMP 元数据容器来存储 GPS 信息。

    Args:
        file_path: 音频文件路径
        location_info: 包含 latitude, longitude, altitude 的字典
    """
    orig_atime, orig_mtime = _get_stat(file_path)
    methods = [
        [f'-XMP:GPSLatitude={location_info["latitude"]}',
         f'-XMP:GPSLongitude={location_info["longitude"]}',
         f'-XMP:GPSLatitudeRef={"N" if location_info["latitude"] >= 0 else "S"}',
         f'-XMP:GPSLongitudeRef={"E" if location_info["longitude"] >= 0 else "W"}'],
        [f'-GPSLatitude={location_info["latitude"]}',
         f'-GPSLongitude={location_info["longitude"]}',
         f'-GPSLatitudeRef={"N" if location_info["latitude"] >= 0 else "S"}',
         f'-GPSLongitudeRef={"E" if location_info["longitude"] >= 0 else "W"}']
    ]
    if location_info.get('altitude') is not None:
        for method in methods:
            method += [f'-GPSAltitude={abs(location_info["altitude"])}',
                       f'-GPSAltitudeRef={"0" if location_info["altitude"] >= 0 else "1"}']

    success = False
    for method in methods:
        try:
            _run_exiftool(method, file_path)
            success = True
            break
        except Exception:
            traceback.print_exc()
            continue

    if success:
        os.utime(file_path, (orig_atime, orig_mtime))
    else:
        raise Exception(_("所有GPS写入方法均失败，不支持的音频文件格式"))


def remove_gps_info(file_path):
    """从文件中移除所有 GPS 信息

    根据文件类型选择移除方法：
      - RAW/视频/音频：使用 ExifTool 清除所有 GPS 相关标签
      - 图像：使用 piexif 清空 GPS IFD 数据

    Args:
        file_path: 文件路径
    """
    orig_atime, orig_mtime = _get_stat(file_path)
    ext = os.path.splitext(file_path)[1].lower()

    if ext in RAW_EXTENSIONS or ext in VIDEO_EXTENSIONS or ext in AUDIO_EXTENSIONS:
        _run_exiftool(['-GPS*=', '-XMP:GPS*='], file_path)
    else:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
            temp_path = temp_file.name
        try:
            shutil.copy2(file_path, temp_path)
            exif_dict = piexif.load(temp_path)
            exif_dict["GPS"] = {}
            piexif.insert(piexif.dump(exif_dict), temp_path)
            shutil.move(temp_path, file_path)
        except Exception:
            _run_exiftool(['-GPS*=', '-XMP:GPS*='], file_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    os.utime(file_path, (orig_atime, orig_mtime))


def update_image_date(file_path, new_datetime):
    """将日期信息写入图像文件

    优先使用 piexif 库写入三个日期标签（DateTimeOriginal、DateTime、DateTimeDigitized），
    失败时回退到 ExifTool。

    Args:
        file_path: 图像文件路径
        new_datetime: 新的日期时间值
    """
    orig_atime, orig_mtime = _get_stat(file_path)
    ext = os.path.splitext(file_path)[1].lower()

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
        temp_path = temp_file.name
    try:
        shutil.copy2(file_path, temp_path)
        exif_dict = piexif.load(temp_path)

        if 'Exif' not in exif_dict:
            exif_dict['Exif'] = {}
        date_str = new_datetime.strftime('%Y:%m:%d %H:%M:%S').encode('utf-8')
        exif_dict['Exif'][piexif.ExifIFD.DateTimeOriginal] = date_str
        exif_dict['Exif'][piexif.ExifIFD.DateTimeDigitized] = date_str
        exif_dict['0th'][piexif.ImageIFD.DateTime] = date_str

        piexif.insert(piexif.dump(exif_dict), temp_path)
        if os.path.getsize(temp_path) == 0:
            raise Exception(_("写入结果为空文件"))
        shutil.move(temp_path, file_path)
        os.utime(file_path, (orig_atime, orig_mtime))
    except Exception:
        traceback.print_exc()
        _write_date_with_exiftool(file_path, new_datetime, orig_atime, orig_mtime)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _write_date_with_exiftool(file_path, new_datetime, orig_atime, orig_mtime):
    """使用 ExifTool 写入日期的内部函数（piexif 失败时的后备方案）"""
    date_str = new_datetime.strftime('%Y:%m:%d %H:%M:%S')
    args = [f'-DateTimeOriginal={date_str}',
            f'-DateTime={date_str}',
            f'-DateTimeDigitized={date_str}']
    _run_exiftool(args, file_path)
    os.utime(file_path, (orig_atime, orig_mtime))


def update_raw_date(file_path, new_datetime):
    """使用 ExifTool 将日期写入 RAW 文件"""
    orig_atime, orig_mtime = _get_stat(file_path)
    d = new_datetime.strftime("%Y:%m:%d %H:%M:%S")
    args = [f'-DateTimeOriginal={d}', f'-DateTime={d}', f'-CreateDate={d}']
    _run_exiftool(args, file_path)
    os.utime(file_path, (orig_atime, orig_mtime))


def update_video_date(file_path, new_datetime):
    """使用 ExifTool 将日期写入视频文件（尝试多种标签路径）"""
    orig_atime, orig_mtime = _get_stat(file_path)
    d = new_datetime.strftime("%Y:%m:%d %H:%M:%S")

    methods = [
        [f'-Keys:CreateDate={d}', f'-Keys:ModifyDate={d}'],
        [f'-QuickTime:CreateDate={d}', f'-QuickTime:MediaCreateDate={d}'],
        [f'-XMP:CreateDate={d}', f'-XMP:ModifyDate={d}'],
        [f'-UserData:CreateDate={d}', f'-UserData:ModifyDate={d}'],
        [f'-CreateDate={d}', f'-MediaCreateDate={d}'],
    ]

    success = False
    for method in methods:
        try:
            _run_exiftool(method, file_path)
            success = True
            break
        except Exception:
            traceback.print_exc()
            continue

    if success:
        os.utime(file_path, (orig_atime, orig_mtime))
    else:
        raise Exception(_("所有日期写入方法均失败，不支持的视频文件格式"))


def update_audio_date(file_path, new_datetime):
    """使用 ExifTool 将日期写入音频文件（通过 XMP 标签）"""
    orig_atime, orig_mtime = _get_stat(file_path)
    d = new_datetime.strftime("%Y:%m:%d %H:%M:%S")

    methods = [
        [f'-XMP:DateCreated={d}', f'-XMP:ModifyDate={d}'],
        [f'-Keys:CreateDate={d}', f'-Keys:ModifyDate={d}'],
        [f'-QuickTime:CreateDate={d}', f'-QuickTime:MediaCreateDate={d}'],
        [f'-CreateDate={d}', f'-ModifyDate={d}'],
    ]

    success = False
    for method in methods:
        try:
            _run_exiftool(method, file_path)
            success = True
            break
        except Exception:
            traceback.print_exc()
            continue

    if success:
        os.utime(file_path, (orig_atime, orig_mtime))
    else:
        raise Exception(_("所有日期写入方法均失败，不支持的音频文件格式"))


def clear_audio_date(file_path):
    """清除音频文件中的所有日期标签"""
    orig_atime, orig_mtime = _get_stat(file_path)
    _run_exiftool(['-time:all='], file_path)
    os.utime(file_path, (orig_atime, orig_mtime))


def blank_exif_dates(file_path):
    """使用 ExifTool 清空 EXIF 日期标签

    将 ALLDates 设置为 0001:01:01 00:00:00，
    而非删除标签，以保持 EXIF 结构完整。
    """
    orig_atime, orig_mtime = _get_stat(file_path)

    _run_exiftool(['-AllDates=0001:01:01 00:00:00'], file_path)

    os.utime(file_path, (orig_atime, orig_mtime))


def clear_video_date(file_path):
    """清除视频文件中的所有日期标签"""
    orig_atime, orig_mtime = _get_stat(file_path)
    _run_exiftool(['-time:all='], file_path)
    os.utime(file_path, (orig_atime, orig_mtime))


def extract_exif_gps(file_path):
    """使用 exifread 库从图像文件中提取 GPS 数据

    exifread 库可以处理更广泛的 EXIF 格式。
    坐标从度/分/秒格式转换为十进制度数。

    Args:
        file_path: 文件路径

    Returns:
        tuple: (纬度, 经度, 高度)，失败返回 (None, None, None)
    """
    try:
        with open(file_path, 'rb') as f:
            tags = exifread.process_file(f, details=False)

        lat = lon = alt = None
        if 'GPS GPSLatitude' in tags and 'GPS GPSLongitude' in tags:
            latv, lonv = tags['GPS GPSLatitude'].values, tags['GPS GPSLongitude'].values
            lat = float(latv[0]) + float(latv[1]) / 60 + float(latv[2]) / 3600
            lon = float(lonv[0]) + float(lonv[1]) / 60 + float(lonv[2]) / 3600
            if 'GPS GPSLatitudeRef' in tags and str(tags['GPS GPSLatitudeRef']) == 'S':
                lat = -lat
            if 'GPS GPSLongitudeRef' in tags and str(tags['GPS GPSLongitudeRef']) == 'W':
                lon = -lon

        if 'GPS GPSAltitude' in tags:
            try:
                vals = tags['GPS GPSAltitude'].values
                v = vals[0] if vals else None
                if v is not None:
                    if hasattr(v, "num"):
                        den = float(getattr(v, 'den', 1))
                        alt = float(v.num) / den if den != 0 else float(v)
                    else:
                        alt = float(v)
                    if 'GPS GPSAltitudeRef' in tags:
                        refs = tags['GPS GPSAltitudeRef'].values
                        if refs and refs[0] == 1:
                            alt = -alt
            except (AttributeError, IndexError, ValueError):
                alt = None

        return lat, lon, alt
    except (OSError, ValueError, KeyError, struct.error) as e:
        traceback.print_exc()
        return None, None, None


def extract_pil_gps(file_path):
    """使用 Pillow (PIL) 库从图像文件中提取 GPS 数据

    作为 exifread 的备选方案。Pillow 的 EXIF 处理相对有限，
    但在某些 exifread 无法解析的文件上可能成功。

    Args:
        file_path: 文件路径

    Returns:
        tuple: (纬度, 经度, 高度)，失败返回 (None, None, None)
    """
    try:
        with Image.open(file_path) as img:
            exif_data = img.getexif()
            if not exif_data:
                return None, None, None
            for tag, val in exif_data.items():
                tname = TAGS.get(tag, tag)
                if tname == 'GPSInfo':
                    gps_data = {}
                    for gt, gv in val.items():
                        gps_data[GPSTAGS.get(gt, gt)] = gv
                    lat = lon = alt = None
                    if 'GPSLatitude' in gps_data and 'GPSLongitude' in gps_data:
                        latv, lonv = gps_data['GPSLatitude'], gps_data['GPSLongitude']
                        lat = float(latv[0]) + float(latv[1]) / 60 + float(latv[2]) / 3600
                        lon = float(lonv[0]) + float(lonv[1]) / 60 + float(lonv[2]) / 3600
                        if gps_data.get('GPSLatitudeRef', 'N') == 'S':
                            lat = -lat
                        if gps_data.get('GPSLongitudeRef', 'E') == 'W':
                            lon = -lon
                    if 'GPSAltitude' in gps_data:
                        v = gps_data['GPSAltitude']
                        alt = float(v[0]) / float(v[1]) if isinstance(v, tuple) and len(v) >= 2 and float(v[1]) != 0 else float(v)
                        if gps_data.get('GPSAltitudeRef', 0) == 1:
                            alt = -alt
                    return lat, lon, alt
    except (OSError, ValueError, KeyError, struct.error):
        traceback.print_exc()
    return None, None, None


def extract_video_gps_with_exiftool(file_path):
    """使用 ExifTool 从视频文件中提取 GPS 和时间数据

    ExifTool 以 JSON 格式输出所有元数据，从中提取 GPS 坐标和创建时间。
    尝试多种可能的标签路径（QuickTime、XMP、Keys、UserData 等）。

    Args:
        file_path: 视频文件路径

    Returns:
        tuple: (纬度, 经度, 高度, 时间)，失败返回 (None, None, None, None)
    """
    available, tool_path = check_exiftool()
    if not available:
        return None, None, None, None

    tool = tool_path
    cmd = [tool, '-j', '-G', '-n', file_path]

    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=30, startupinfo=get_startupinfo(),
                          errors='replace')
        if r.returncode != 0:
            return None, None, None, None

        md = json.loads(r.stdout)[0]
        lat = lon = alt = None
        video_time = None

        for dtkey in ('QuickTime:MediaCreateDate', 'QuickTime:MediaModifyDate',
                      'QuickTime:CreationDate', 'QuickTime:CreateDate',
                      'QuickTime:ModifyDate', 'File:FileModifyDate', 'System:FileModifyDate'):
            if dtkey in md and video_time is None:
                try:
                    raw = md[dtkey]
                    parsed = parse_video_time(raw)
                    if parsed:
                        # QuickTime 时间始终为 UTC（即使 ExifTool 输出不带 Z 后缀）
                        if dtkey.startswith('QuickTime:') and parsed.tzinfo is None:
                            utc_dt = parsed.replace(tzinfo=timezone.utc)
                            parsed = utc_dt.astimezone().replace(tzinfo=None)
                        video_time = parsed
                        break
                except (OSError, ValueError, KeyError, struct.error) as e:
                    traceback.print_exc()
                    continue

        if 'Composite:GPSLatitude' in md and 'Composite:GPSLongitude' in md:
            try:
                lat = float(md['Composite:GPSLatitude'])
                lon = float(md['Composite:GPSLongitude'])
            except (OSError, ValueError, KeyError, struct.error) as e:
                traceback.print_exc()
        elif 'QuickTime:GPSCoordinates' in md:
            try:
                c = md['QuickTime:GPSCoordinates'].split(',')
                if len(c) >= 2:
                    lat, lon = float(c[0]), float(c[1])
            except (OSError, ValueError, KeyError, struct.error) as e:
                traceback.print_exc()
        elif 'XMP:GPSLatitude' in md and 'XMP:GPSLongitude' in md:
            try:
                lat, lon = float(md['XMP:GPSLatitude']), float(md['XMP:GPSLongitude'])
            except (OSError, ValueError, KeyError, struct.error) as e:
                traceback.print_exc()
        elif 'Keys:GPSCoordinates' in md:
            try:
                c = md['Keys:GPSCoordinates'].split(',')
                if len(c) >= 2:
                    lat, lon = float(c[0]), float(c[1])
            except (OSError, ValueError, KeyError, struct.error) as e:
                traceback.print_exc()
        elif 'UserData:GPSCoordinates' in md:
            try:
                c = md['UserData:GPSCoordinates'].split(',')
                if len(c) >= 2:
                    lat, lon = float(c[0]), float(c[1])
            except (OSError, ValueError, KeyError, struct.error) as e:
                traceback.print_exc()

        if 'Composite:GPSAltitude' in md:
            try:
                alt = float(md['Composite:GPSAltitude'])
            except (OSError, ValueError, KeyError, struct.error) as e:
                traceback.print_exc()
        elif 'XMP:GPSAltitude' in md:
            try:
                alt = float(md['XMP:GPSAltitude'])
            except (OSError, ValueError, KeyError, struct.error) as e:
                traceback.print_exc()
        elif 'QuickTime:GPSAltitude' in md:
            try:
                alt = float(md['QuickTime:GPSAltitude'])
            except (OSError, ValueError, KeyError, struct.error) as e:
                traceback.print_exc()

        return lat, lon, alt, video_time
    except (OSError, ValueError, KeyError, struct.error) as e:
        traceback.print_exc()
        return None, None, None, None
