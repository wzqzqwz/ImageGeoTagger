"""媒体文件数据模型"""

import enum
import os
from datetime import datetime

from ImageGeoTagger.utils.i18n import _


class FileStatus(enum.Enum):
    PENDING = "pending"
    NO_DATE_NEEDED = "no_date_needed"
    PENDING_DATE_CHANGE = "pending_date_change"
    PARSE_FAILED = "parse_failed"
    DATE_CHANGED = "date_changed"
    DRY_RUN_DATE_CHANGED = "dry_run_date_changed"
    DRY_RUN = "dry_run"
    FAILED = "failed"
    PENDING_RENAME = "pending_rename"
    SKIPPED = "skipped"
    RENAMED = "renamed"
    HAS_DATE_IN_FILENAME = "has_date_in_filename"
    MANUALLY_RENAMED = "manually_renamed"
    SAME_AS_ORIGINAL = "same_as_original"
    NO_DATE_TAKEN = "no_date_taken"
    NO_RENAME_NEEDED = "no_rename_needed"
    MANUALLY_EDITED = "manually_edited"


def _status_text_direct(status):
    if status == FileStatus.PENDING:
        return _('待处理')
    if status == FileStatus.NO_DATE_NEEDED:
        return _('无需更改日期')
    if status == FileStatus.PENDING_DATE_CHANGE:
        return _('待更改日期')
    if status == FileStatus.PARSE_FAILED:
        return _('无法解析日期')
    if status == FileStatus.DATE_CHANGED:
        return _('日期更改成功')
    if status == FileStatus.DRY_RUN_DATE_CHANGED:
        return _('试运行（日期更改成功）')
    if status == FileStatus.PENDING_RENAME:
        return _('待重命名')
    if status == FileStatus.SKIPPED:
        return _('跳过')
    if status == FileStatus.RENAMED:
        return _('重命名成功')
    if status == FileStatus.HAS_DATE_IN_FILENAME:
        return _('原文件名有日期')
    if status == FileStatus.MANUALLY_RENAMED:
        return _('已手动重命名')
    if status == FileStatus.SAME_AS_ORIGINAL:
        return _('与原文件名相同')
    if status == FileStatus.NO_DATE_TAKEN:
        return _('无拍摄日期')
    if status == FileStatus.NO_RENAME_NEEDED:
        return _('无需重命名（文件名相同）')
    if status == FileStatus.MANUALLY_EDITED:
        return _('已手动编辑（跳过自动修改）')
    return str(status)


def status_text(status, detail=""):
    if status is None:
        return ""
    if status == FileStatus.DRY_RUN:
        return _('试运行: ') + detail
    if status == FileStatus.FAILED:
        return _('失败: ') + detail
    return _status_text_direct(status)


_STATUS_SORT_ORDER = {
    FileStatus.PENDING_DATE_CHANGE: 1,
    FileStatus.PENDING_RENAME: 2,
    FileStatus.NO_DATE_NEEDED: 3,
    FileStatus.NO_RENAME_NEEDED: 4,
    FileStatus.SKIPPED: 5,
    FileStatus.PARSE_FAILED: 6,
    FileStatus.RENAMED: 7,
    FileStatus.DATE_CHANGED: 8,
    FileStatus.MANUALLY_RENAMED: 9,
    FileStatus.DRY_RUN: 10,
    FileStatus.DRY_RUN_DATE_CHANGED: 11,
    FileStatus.FAILED: 12,
    FileStatus.MANUALLY_EDITED: 13,
    FileStatus.HAS_DATE_IN_FILENAME: 14,
    FileStatus.NO_DATE_TAKEN: 15,
    FileStatus.SAME_AS_ORIGINAL: 16,
    FileStatus.PENDING: 17,
}


def status_sort_key(status):
    return _STATUS_SORT_ORDER.get(status, 999)


def get_val(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class MediaFileInfo:
    def __init__(self, file_path, filename=None, datetime_val=None,
                 latitude=None, longitude=None, altitude=None,
                 file_size=None):
        self.path = file_path
        self.filename = filename or os.path.basename(file_path)
        self.dt = datetime_val
        self.latitude = latitude
        self.longitude = longitude
        self.altitude = altitude
        self.file_size = file_size if file_size is not None else self._safe_getsize(file_path)

    @staticmethod
    def _safe_getsize(file_path):
        try:
            return os.path.getsize(file_path)
        except OSError:
            return 0

    def has_gps(self):
        return self.latitude is not None and self.longitude is not None

    def has_datetime(self):
        return self.dt is not None and self.dt != datetime.min

    def get(self, key, default=None):
        mapping = {
            'path': self.path,
            'filename': self.filename,
            'datetime': self.dt,
            'latitude': self.latitude,
            'longitude': self.longitude,
            'altitude': self.altitude,
            'file_size': self.file_size,
            'dt': self.dt,
        }
        return mapping.get(key, default)

    def __contains__(self, key):
        return key in ('path', 'filename', 'datetime', 'latitude', 'longitude', 'altitude', 'file_size', 'dt')

    def to_dict(self):
        return {
            'path': self.path,
            'filename': self.filename,
            'datetime': self.dt,
            'latitude': self.latitude,
            'longitude': self.longitude,
            'altitude': self.altitude,
            'file_size': self.file_size,
        }


class FileRecord:
    def __init__(self, path, filename=None, dt=None, latitude=None, longitude=None,
                 altitude=None, file_size=None, status=None):
        self.path = path
        self.filename = filename or os.path.basename(path)
        self.dt = dt
        self.latitude = latitude
        self.longitude = longitude
        self.altitude = altitude
        self.file_size = file_size
        self.status = status

    def get(self, key, default=None):
        mapping = {
            'path': self.path,
            'filename': self.filename,
            'datetime': self.dt,
            'dt': self.dt,
            'latitude': self.latitude,
            'longitude': self.longitude,
            'altitude': self.altitude,
            'file_size': self.file_size,
            'status': self.status,
        }
        return mapping.get(key, default)

    def __contains__(self, key):
        return key in ('path', 'filename', 'datetime', 'dt', 'latitude', 'longitude', 'altitude', 'file_size', 'status')
