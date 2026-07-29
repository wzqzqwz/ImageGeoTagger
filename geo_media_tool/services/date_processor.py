"""日期处理服务 - 修改拍摄日期和重命名文件

提供两个主要功能：
  1. 根据文件名中的日期信息改写文件的 EXIF 拍摄日期
  2. 根据文件现有的拍摄日期重命名文件

支持图像（含 RAW）、视频和音频文件格式。
"""

from datetime import datetime
from pathlib import Path

from geo_media_tool.config import IMAGE_EXTENSIONS, RAW_EXTENSIONS, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS
from geo_media_tool.utils.i18n import _
from geo_media_tool.utils.media_utils import (
    parse_datetime_from_filename, get_existing_datetime
)
from geo_media_tool.utils.exif_utils import (
    read_exif_datetime, update_image_date, update_raw_date,
    update_video_date, update_audio_date, blank_exif_dates,
    clear_video_date, clear_audio_date
)


class MediaDateRenamer:
    """文件日期操作处理类

    负责扫描文件、解析文件名中的日期、将日期写入文件 EXIF。
    支持试运行模式（只预览不实际修改）。
    """

    def __init__(self, dry_run=True):
        # 试运行模式：True 表示只预览不实际修改文件
        self.dry_run = dry_run

    def get_existing_datetime(self, file_path):
        """获取文件现有的最佳日期时间信息

        优先级：EXIF > QuickTime > 文件创建时间 > 文件修改时间
        """
        return get_existing_datetime(file_path)

    def parse_datetime_from_filename(self, filename):
        """从文件名中解析日期时间

        支持多种常见格式：YYYY-MM-DD_HH-MM-SS、YYYYMMDD_HHMMSS 等
        """
        return parse_datetime_from_filename(filename)

    def process_file(self, file_path, skip_existing=False):
        """处理单个文件：根据文件名日期设置拍摄日期

        Args:
            file_path: 文件路径
            skip_existing: 如果为 True，跳过已有 EXIF 日期的文件

        Returns:
            tuple: (是否成功, 消息字符串)
        """
        file_path = Path(file_path)
        if not file_path.exists():
            return False, _("文件不存在: ") + str(file_path)

        # 如果启用了跳过已有日期的选项，检查文件是否已有拍摄日期
        if skip_existing:
            dt = get_existing_datetime(file_path)
            if dt is not None and dt != datetime.min:
                return True, _("跳过已有日期数据的文件")

        # 从文件名解析日期
        parsed_date = self.parse_datetime_from_filename(file_path.name)
        if not parsed_date:
            return False, _("无法从文件名解析日期时间")

        # 试运行模式下不实际修改文件
        if self.dry_run:
            return True, _("试运行: 将设置日期为 ") + parsed_date.strftime('%Y-%m-%d %H:%M:%S')

        # 根据文件类型选择合适的日期写入方法
        try:
            ext = file_path.suffix.lower()
            if ext in IMAGE_EXTENSIONS:
                update_image_date(file_path, parsed_date)
            elif ext in RAW_EXTENSIONS:
                update_raw_date(file_path, parsed_date)
            elif ext in VIDEO_EXTENSIONS:
                update_video_date(file_path, parsed_date)
            elif ext in AUDIO_EXTENSIONS:
                update_audio_date(file_path, parsed_date)
            else:
                raise Exception(_("不支持的文件格式"))
            return True, _("成功更新日期: ") + parsed_date.strftime('%Y:%m:%d %H:%M:%S')
        except Exception as e:
            return False, _("处理文件时出错: ") + str(e)


def update_file_shooting_date(file_path, new_datetime, file_ext):
    """更新媒体文件的拍摄日期

    根据文件扩展名自动选择对应的方法进行处理。

    Args:
        file_path: 文件路径
        new_datetime: 新的日期时间值
        file_ext: 文件扩展名（小写，带点号，如 .jpg）
    """
    ext = file_ext.lower()
    if ext in RAW_EXTENSIONS:
        update_raw_date(file_path, new_datetime)
    elif ext in VIDEO_EXTENSIONS:
        update_video_date(file_path, new_datetime)
    elif ext in AUDIO_EXTENSIONS:
        update_audio_date(file_path, new_datetime)
    else:
        update_image_date(file_path, new_datetime)


def clear_file_shooting_date(file_path, file_ext):
    """清除媒体文件的拍摄日期

    将日期标记设置为 Unix 纪元（1970:01:01 00:00:00），
    而非直接删除标签，以保持文件结构完整。

    Args:
        file_path: 文件路径
        file_ext: 文件扩展名（小写，带点号）
    """
    ext = file_ext.lower()
    if ext in VIDEO_EXTENSIONS:
        clear_video_date(file_path)
    elif ext in AUDIO_EXTENSIONS:
        clear_audio_date(file_path)
    else:
        blank_exif_dates(file_path)
