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
import math
import threading
import atexit
import time
import functools
from collections import deque
from datetime import datetime, timezone, timedelta

import exifread
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import piexif
from utils.i18n import _
from utils.logging_utils import log_exc

from config import RAW_EXTENSIONS, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS
from utils.platform_utils import get_startupinfo, get_app_dir

# ExifTool 探测结果缓存：避免每个文件都启动一次子进程探测
_exiftool_cache_lock = threading.Lock()
_exiftool_cache = {'available': None, 'path': None}

# 并发 ExifTool 子进程信号量：限制同时运行的 exiftool 数量，
# 防止线程池全开时同时启动数十个 exiftool 进程导致低内存 OOM。
# 4 并发的写入吞吐过低（每个文件需独立启动 perl 进程），
# 按 CPU 核数提升并发以加快批量写入。
EXIFTOOL_MAX_CONCURRENT = max(4, min(12, (os.cpu_count() or 4) * 2))


class _PoolExecutionError(Exception):
    """常驻进程池执行层故障（超时/进程退出等），调用方应回退独立进程模式"""


# per-file 写锁：日期页/地理页/编辑对话框的任务可能并发写同一文件
# （ExifTool 同名 tmp 文件互删、备份互相覆盖），按绝对路径串行化。
_file_locks_guard = threading.Lock()
_file_locks = {}


def _per_file_lock(file_path):
    key = os.path.abspath(file_path)
    with _file_locks_guard:
        lk = _file_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _file_locks[key] = lk
        return lk


def _synchronized_file(func):
    """装饰公开写入口：对同一文件的写入加 per-file 锁"""
    @functools.wraps(func)
    def wrapper(file_path, *args, **kwargs):
        with _per_file_lock(file_path):
            return func(file_path, *args, **kwargs)
    return wrapper


def _piexif_temp_path(file_path, ext):
    """在目标同目录创建临时副本路径

    与目标同卷，之后 os.replace 才是原子替换；
    系统临时目录与目标跨卷时 shutil.move 会退化为
    copy+delete，失败会截断原文件。
    """
    fd, temp_path = tempfile.mkstemp(prefix='.igt_tmp_', suffix=ext,
                                     dir=os.path.dirname(os.path.abspath(file_path)))
    os.close(fd)
    return temp_path


class _StayOpenWorker:
    """单个 ExifTool 常驻进程（-stay_open 模式）

    通过 stdin 逐条发送命令（每行一个参数，以 -execute 结尾），
    从 stdout 读到空行判定命令结束，避免每个文件启动一次 perl 进程。
    同一 worker 串行执行命令，由外部信号量控制整体并发。
    """

    _ENCODING_HINT = 'FileName encoding must be specified'

    def __init__(self, tool_path, code_page):
        self.tool_path = tool_path
        self.code_page = code_page
        self.lock = threading.Lock()
        self.dead = False
        self._stderr_lines = deque()
        # stderr 行由守护线程 append、命令线程 list() 读取，需要同一把锁，
        # 否则迭代/长度读取与 append 并发会抛 RuntimeError（deque mutated）
        self._stderr_lock = threading.Lock()
        # 已从队首丢弃的 stderr 行数（配合 _stderr_lines 还原真实序号）
        self._stderr_trimmed = 0
        self._start()

    # ---------- 进程生命周期 ----------
    def _start(self):
        self.proc = subprocess.Popen(
            [self.tool_path, '-stay_open', 'True', '-@', '-'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, startupinfo=get_startupinfo())
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True, name='exiftool-stderr')
        self._stderr_thread.start()

    def _read_stderr(self):
        enc = f'cp{self.code_page}' if os.name == 'nt' else 'utf-8'
        try:
            while True:
                line = self.proc.stderr.readline()
                if not line:
                    break
                with self._stderr_lock:
                    self._stderr_lines.append(line.decode(enc, errors='replace'))
                    # 只保留最近 2 万行：长会话大量 Warning 时避免内存无界增长
                    while len(self._stderr_lines) > 20000:
                        self._stderr_lines.popleft()
                        self._stderr_trimmed += 1
        except Exception:
            pass

    def _kill(self, proc=None):
        # 超时看门狗必须杀"创建时捕获的那个进程"：若期间进程已被
        # _restart 替换，操作 self.proc 会杀掉新进程导致无谓失败
        target = proc if proc is not None else self.proc
        try:
            target.kill()
        except Exception:
            pass
        try:
            target.wait(timeout=5)
        except Exception:
            pass

    def _restart(self):
        self._kill()
        self.dead = False
        with self._stderr_lock:
            self._stderr_lines.clear()
            self._stderr_trimmed = 0
        self._start()

    # ---------- 命令执行 ----------
    def _run_command(self, args, enc, timeout):
        # 超时看门狗：readline 是阻塞调用，超时只能靠杀进程解除
        watchdog = threading.Timer(timeout, self._kill, args=(self.proc,))
        watchdog.daemon = True
        watchdog.start()
        try:
            def _stderr_count():
                with self._stderr_lock:
                    return self._stderr_trimmed + len(self._stderr_lines)

            stderr_start_total = _stderr_count()
            data = ''.join(a + '\n' for a in args) + '-execute\n'
            self.proc.stdin.write(data.encode(enc, errors='replace'))
            self.proc.stdin.flush()

            out_parts = []
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise _PoolExecutionError(_("ExifTool 常驻进程意外退出"))
                text = line.decode(enc, errors='replace')
                if '{ready}' in text:
                    break
                out_parts.append(text)
            # 等待 stderr 守护线程排空本命令的输出：ExifTool 在打印
            # {ready} 之前已把本命令的 stderr 全部写入管道，滞后只来自
            # 线程调度。短暂等待直到队列长度稳定，可保证错误文本归属
            # 正确，避免把上一条命令的 stderr 算到本条头上（假成功）。
            for _i in range(50):
                cur = _stderr_count()
                time.sleep(0.005)
                if cur == _stderr_count():
                    break
            # 取最近 lines_this_cmd 行：队列可能已被超限清理截断队首，
            # 但本命令自身的 stderr 必然位于队尾，取尾部即可正确还原
            lines_this_cmd = _stderr_count() - stderr_start_total
            with self._stderr_lock:
                texts = list(self._stderr_lines)
            if lines_this_cmd > 0:
                stderr_text = ''.join(
                    texts[max(0, len(texts) - lines_this_cmd):]).rstrip('\n')
            else:
                stderr_text = ''
            return ''.join(out_parts), stderr_text
        finally:
            watchdog.cancel()

    def run(self, args, timeout=60):
        """执行一条命令，返回 (stdout_text, stderr_text)

        执行层故障（超时/进程退出/编码异常）会重启进程后重试一次，
        仍失败则抛 _PoolExecutionError 交由调用方回退独立模式。
        """
        with self.lock:
            enc = f'cp{self.code_page}' if os.name == 'nt' else 'utf-8'
            # stay_open 协议按行传参：参数值含换行/回车会被 ExifTool
            # 解析为额外参数行（POSIX 文件名可合法包含换行，属于注入面）；
            # 系统代码页无法编码的字符若用 errors='replace' 会静默写错路径。
            # 两种情况均拒绝执行，由调用方回退独立进程模式
            # （argv 列表传参，无注入/编码损坏问题）。
            for a in args:
                if any(ch in a for ch in ('\n', '\r')):
                    raise _PoolExecutionError(
                        _("参数包含换行符，已回退独立进程模式"))
                try:
                    a.encode(enc)
                except UnicodeEncodeError:
                    raise _PoolExecutionError(
                        _("文件名包含当前编码无法表示的字符，已回退独立进程模式"))
            for attempt in range(2):
                try:
                    if self.dead or self.proc.poll() is not None:
                        self._restart()
                    return self._run_command(args, enc, timeout)
                except _PoolExecutionError:
                    self.dead = True
                    continue
                except (BrokenPipeError, OSError, UnicodeError) as e:
                    self.dead = True
                    if attempt == 0:
                        continue
                    raise _PoolExecutionError(str(e)) from e
            raise _PoolExecutionError(_("ExifTool 常驻进程连续两次执行失败"))


class _ExifToolPool:
    """ExifTool 常驻进程池

    池内进程通过 -stay_open 常驻，命令经 stdin 交互式发送，
    省去每文件一次 perl 进程启动的开销。任何执行层故障都会回退
    到独立进程模式（_run_exiftool 内的原逻辑），保证写入安全性。

    生命周期：任务空闲 EXIFTOOL_POOL_IDLE_SECONDS 后自动关闭
    （见 close_exiftool_pool），下次使用时由 _get_pool 重新创建，
    避免任务结束后进程长时间驻留。
    """

    def __init__(self, size, tool_path):
        self.size = size
        self.tool_path = tool_path
        self.closed = False
        self._sem = threading.BoundedSemaphore(size)
        self._lock = threading.Lock()
        # 正在执行的命令数：close 时若 >0 说明有命令在途，推迟关闭，
        # 避免空闲回收计时器与命令执行竞争导致进程被杀/孤儿进程泄漏
        self._in_use = 0
        self._in_use_lock = threading.Lock()
        self.workers = []
        self._next = 0
        try:
            for i in range(size):
                self.workers.append(_StayOpenWorker(tool_path, _system_codepage()))
        except Exception:
            # 部分 worker 启动失败：立即清理已启动的进程，
            # 否则前序 worker 变成无主的孤儿 perl 进程（构造泄漏）
            for w in self.workers:
                try:
                    w.dead = True
                    w._kill()
                except Exception:
                    pass
            self.workers = []
            raise
        atexit.register(self.close)

    def run(self, args, timeout=60):
        with self._sem:
            with self._in_use_lock:
                if self.closed:
                    # close() 已标记关闭并杀掉全部 worker：拒绝新命令，
                    # 由调用方回退独立进程模式。若放行，worker.run 会在
                    # 发现进程已死后 _restart() 复活孤儿 perl 进程
                    # （池已不在缓存中，空闲回收计时器也不会再触发）。
                    raise _PoolExecutionError(_("ExifTool 进程池已关闭"))
                self._in_use += 1
            try:
                with self._lock:
                    worker = None
                    for i in range(self.size):
                        w = self.workers[self._next]
                        self._next = (self._next + 1) % self.size
                        if not w.dead:
                            worker = w
                            break
                    if worker is None:
                        raise _PoolExecutionError(_("ExifTool 常驻进程池全部失效"))
                return worker.run(args, timeout)
            finally:
                with self._in_use_lock:
                    self._in_use -= 1

    def close(self):
        """关闭全部常驻进程；有命令在途时返回 False（由调用方延后重试）"""
        with self._in_use_lock:
            if self._in_use > 0:
                return False
            # 与 run() 的 closed 检查处于同一把锁：closed 置位后任何
            # 新到达的命令都会在进入前被拒绝，不会出现
            # "close 完成后 worker 被 _restart() 复活"的孤儿进程竞态
            self.closed = True
        for w in getattr(self, 'workers', []):
            try:
                w.dead = True
                w._kill()
            except Exception:
                pass
        return True


_pool_lock = threading.Lock()
_pool_cache = {}
_pool_idle_timer = None
# 池创建失败后的重试冷却（秒）：期间直接返回 None，
# 避免 exiftool 不可用时每次写入都重复构造整个进程池（数十次进程启动）
_POOL_RETRY_COOLDOWN = 5.0
# tool_path -> 最近一次池创建失败的时间（monotonic）
_pool_fail_time = {}
# 最后一次 exiftool 调用后空闲多久自动关闭常驻进程池
EXIFTOOL_POOL_IDLE_SECONDS = 30


def _get_pool(tool_path):
    """获取常驻进程池；已关闭/不存在的池自动重建，启动失败返回 None"""
    with _pool_lock:
        pool = _pool_cache.get(tool_path)
        if pool is not None and not pool.closed:
            return pool
        last_fail = _pool_fail_time.get(tool_path, 0)
        if time.monotonic() - last_fail < _POOL_RETRY_COOLDOWN:
            return None
        try:
            pool = _ExifToolPool(EXIFTOOL_MAX_CONCURRENT, tool_path)
            _pool_fail_time.pop(tool_path, None)
        except Exception:
            log_exc('ExifTool 常驻进程池创建失败')
            _pool_fail_time[tool_path] = time.monotonic()
            pool = None
        if pool is not None:
            _pool_cache[tool_path] = pool
        return pool


def close_exiftool_pool():
    """关闭 ExifTool 常驻进程池，释放全部 perl 进程

    任务执行完毕后调用；下次任何写入任务开始时 _get_pool 会自动重建。
    供外部任务收尾调用，也由空闲计时器自动触发。
    若关闭时仍有命令在途（空闲回收与执行竞争的竞态），
    保留该池并推迟到下一个空闲周期再关闭。
    """
    global _pool_cache, _pool_idle_timer
    with _pool_lock:
        if _pool_idle_timer is not None:
            _pool_idle_timer.cancel()
            _pool_idle_timer = None
        still_busy = {}
        for path, pool in _pool_cache.items():
            if pool.close():
                continue
            still_busy[path] = pool
        if still_busy:
            # 有命令在途：池保持可用（closed=False），空闲周期结束后再关
            _pool_cache = still_busy
            t = threading.Timer(EXIFTOOL_POOL_IDLE_SECONDS, close_exiftool_pool)
            t.daemon = True
            _pool_idle_timer = t
            t.start()
        else:
            _pool_cache = {}


def _arm_pool_idle_close():
    """每次成功使用池后重置空闲计时器：空闲超过阈值自动关闭池"""
    global _pool_idle_timer
    with _pool_lock:
        if _pool_idle_timer is not None:
            _pool_idle_timer.cancel()
        t = threading.Timer(EXIFTOOL_POOL_IDLE_SECONDS, close_exiftool_pool)
        t.daemon = True
        _pool_idle_timer = t
        t.start()


# 独立进程模式（常驻池故障回退路径）的信号量：限制并发 exiftool 子进程数
_exiftool_semaphore = threading.BoundedSemaphore(EXIFTOOL_MAX_CONCURRENT)


def _system_codepage():
    """Windows 活动代码页（中文系统=936/GBK，英文=1252）。

    subprocess 经 CreateProcessW 传参后 ExifTool 侧得到的是系统代码页字节串；
    常驻池 stdin 通道也必须用该编码写文件名参数。
    """
    if os.name == 'nt':
        try:
            import ctypes
            return int(ctypes.windll.kernel32.GetACP())
        except Exception:
            pass
    return 65001


def _get_stat(file_path):
    s = os.stat(file_path)
    return s.st_atime, s.st_mtime


def _now_str():
    # 毫秒精度：同一秒内多次备份保留时不会被互相覆盖
    return datetime.now().strftime('%Y%m%d-%H%M%S%f')[:-3]


def _exif_datetime_from_tags(tags):
    """从 exifread 解析出的 tags 中提取拍摄日期

    Returns:
        datetime or None: 解析成功返回 datetime 对象，失败返回 None
    """
    for tag in ['EXIF DateTimeOriginal', 'Image DateTime', 'EXIF DateTimeDigitized']:
        if tag in tags:
            dt_str = str(tags[tag])
            if dt_str.strip():
                for fmt in ['%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S']:
                    try:
                        return datetime.strptime(dt_str, fmt)
                    except ValueError:
                        continue
    return None


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
        如果 EXIF 中存在日期标签但解析失败，返回 None
    """
    try:
        with open(file_path, 'rb') as f:
            tags = exifread.process_file(f, details=False)
        return _exif_datetime_from_tags(tags)
    except Exception:
        # exifread 对畸形文件可能抛 TypeError/IndexError/AttributeError/
        # UnicodeDecodeError 等非标准异常，统一兜底避免打穿调用线程
        log_exc()
        return None


def extract_exif_metadata(file_path):
    """一次解析 EXIF 同时提取拍摄日期与 GPS 坐标

    扫描阶段对同一图像文件只解析一次 EXIF，
    避免 read_exif_datetime + extract_exif_gps 各自打开文件重复解析。

    Args:
        file_path: 文件路径

    Returns:
        tuple: (拍摄日期, 纬度, 经度, 高度)，失败返回 (None, None, None, None)
    """
    try:
        with open(file_path, 'rb') as f:
            tags = exifread.process_file(f, details=False)
    except Exception:
        # exifread 对畸形文件可能抛非标准异常（见 read_exif_datetime 说明）
        log_exc()
        return None, None, None, None
    return _exif_datetime_from_tags(tags), *_gps_from_tags(tags)


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
                    if atom_size == 1:
                        # 64 位扩展尺寸（QuickTime 合法值），否则逐字节扫描大文件会卡死
                        ext = fh.read(8)
                        if len(ext) < 8:
                            break
                        atom_size = struct.unpack('>Q', ext)[0]
                    if atom_size == 0:
                        # 0 表示延伸至文件尾（规范允许），按文件尾处理
                        atom_size = file_size - pos
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
                    # 布局：8字节atom头 + 1字节版本 + 3字节flags，
                    # v0: creation_time 4字节（偏移12）；v1: creation_time 8字节（偏移12）
                    read_len = min(sub_size, ATOM_HEADER_SIZE + 28) if sub_size > 0 else ATOM_HEADER_SIZE + 28
                    f.seek(search_pos)
                    mvhd_data = f.read(read_len)
                    if len(mvhd_data) < ATOM_HEADER_SIZE + 5:
                        break
                    ver = mvhd_data[ATOM_HEADER_SIZE]
                    if ver == 0:
                        qt_time = struct.unpack('>I', mvhd_data[ATOM_HEADER_SIZE + 4:ATOM_HEADER_SIZE + 8])[0]
                    else:
                        if len(mvhd_data) < ATOM_HEADER_SIZE + 12:
                            break
                        qt_time = struct.unpack('>Q', mvhd_data[ATOM_HEADER_SIZE + 4:ATOM_HEADER_SIZE + 12])[0]
                    utc_dt = (datetime(1904, 1, 1) + timedelta(seconds=qt_time)).replace(tzinfo=timezone.utc)
                    return utc_dt.astimezone().replace(tzinfo=None)
                search_pos += sub_size
    except (OSError, ValueError, KeyError, struct.error, MemoryError, OverflowError):
        log_exc()
    return None


def _exif_ratio_value(v):
    """将 exifread 的 Ratio/数值安全转换为 float（0 分母/异常返回 None）"""
    try:
        if hasattr(v, 'num'):
            den = float(getattr(v, 'den', 1))
            if den == 0:
                return None
            return float(v.num) / den
        return float(v)
    except (AttributeError, ValueError, TypeError, ZeroDivisionError):
        return None


def _ratio_deg_to_float(values):
    """将度/分/秒三元组安全转换为十进制度数，失败返回 None"""
    try:
        if len(values) != 3:
            return None
        parts = [_exif_ratio_value(v) for v in values]
        if any(p is None for p in parts):
            return None
        return parts[0] + parts[1] / 60 + parts[2] / 3600
    except Exception:
        return None


def to_degrees(value):
    """将十进制度数转换为 (度, 分, 秒) 格式（带进位处理）

    EXIF 标准使用度/分/秒的分数形式存储 GPS 坐标。
    例如：116.39747° 转换为 ((116,1), (23,1), (1430892,1e6))

    采用整数运算（秒分辨率 1/1e6 ≈ 2.8e-10 度 ≈ 0.003 厘米）：
      - 避免浮点乘除产生长尾小数（如 0.0399999999999992）
      - 与 ExifTool 写入路径（RAW/视频/音频）精度一致，
        保证粘贴的坐标写入不同文件后数值位数不丢失
      - 分母固定 1e6 且 3600 含因子 9：输入为 6 位小数（手机照片
        标准）时组合值整除 9，约分后分母仅含 2/5 因子，Windows
        资源管理器属性栏等十进制回读显示为有限小数，无 0.5887888889
        类长尾
    分子上限 < 60 * DEN = 6e7，低于 EXIF Rational 的 32 位上限
    （4.29e9），可被 piexif 正常序列化。

    Args:
        value: 十进制度数值

    Returns:
        tuple: ((度, 分母), (分, 分母), (秒, 分母))
    """
    DEN = 1_000_000  # 秒的表示精度：1/1e6 秒 ≈ 2.8e-10 度
    total = round(value * 3600 * DEN)  # 总"单位秒"，整数
    d, rem = divmod(total, 3600 * DEN)
    m, s = divmod(rem, 60 * DEN)
    return ((d, 1), (m, 1), (s, DEN))


def _parse_video_time_detailed(time_str):
    """解析视频时间字符串，返回 (datetime 或 None, 是否含时区信息)

    Returns:
        tuple: (datetime 或 None, bool)
            bool 为 True 表示输入带时区信息（Z 后缀/UTC/±HH:MM），
            返回值已是本地时区 naive 时间；False 表示输入为无时区
            裸时间，未经任何转换。
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
        # 兼容小写 z 后缀（部分非标准生成器输出），统一按 UTC 处理
        if time_str[-1:] in ('Z', 'z') or 'UTC' in time_str.upper():
            is_utc = True
            time_str = time_str.rstrip('Zz').replace('UTC', '').strip()

        # 显式时区偏移（ExifTool 常输出 ...+08:00 / ...-05:00）：
        # 用带 %z 的格式解析，再转成本地时间，保证与照片/GPX 的
        # 本地 naive 时间在同一时间线上比较。
        offset_formats = [
            "%Y:%m:%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y/%m/%d %H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
        ]
        for fmt in offset_formats:
            try:
                aware = datetime.strptime(time_str.strip(), fmt)
                return aware.astimezone().replace(tzinfo=None), True
            except ValueError:
                continue

        for fmt in common_formats:
            try:
                parsed = datetime.strptime(time_str, fmt.replace('Z', ''))
                if is_utc:
                    utc_time = parsed.replace(tzinfo=timezone.utc)
                    return utc_time.astimezone().replace(tzinfo=None), True
                return parsed, False
            except ValueError:
                continue
    except Exception:
        log_exc()
    return None, False


def parse_video_time(time_str):
    """解析视频文件的时间字符串（自动处理 UTC 到本地时间的转换）

    视频文件通常使用 UTC 时间存储（带 Z 后缀），
    此函数会自动将其转换为本地时区。

    Args:
        time_str: 时间字符串（支持多种格式，可能带 Z 后缀）

    Returns:
        datetime or None: 本地时间
    """
    dt, _ = _parse_video_time_detailed(time_str)
    return dt


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
        from config import WINDOWS_EXIFTOOL_PATHS
        sysdirs += WINDOWS_EXIFTOOL_PATHS
    else:
        names = ['exiftool']
        sysdirs = os.environ.get('PATH', '').split(os.pathsep)
        from config import UNIX_EXIFTOOL_PATHS
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
            log_exc()
    try:
        r = subprocess.run(['exiftool', '-ver'],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=5, startupinfo=si, errors='replace')
        if r.returncode == 0:
            return True, 'exiftool'
    except Exception:
        log_exc()
    return False, None


def get_exiftool_cached():
    """获取 ExifTool 路径（带缓存，避免每个文件重复探测子进程）"""
    with _exiftool_cache_lock:
        if _exiftool_cache['available'] is None:
            _exiftool_cache['available'], _exiftool_cache['path'] = check_exiftool()
        return _exiftool_cache['available'], _exiftool_cache['path']


def _validate_location(location_info):
    """校验 GPS 写入坐标：必须为有限数值且在合法范围内

    NaN/Inf/越界/非数字值会写入损坏的 EXIF 或产生难以理解的报错，
    统一在写入前拦截，抛出带上下文信息的 ValueError。

    Returns:
        tuple: (纬度, 经度, 高度或 None)
    """
    try:
        lat = float(location_info['latitude'])
        lon = float(location_info['longitude'])
    except (KeyError, TypeError, ValueError):
        raise ValueError(_("无效的 GPS 坐标: ") + repr(location_info))
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError(_("GPS 坐标必须为有限数值"))
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError(_("GPS 坐标超出有效范围: ") + f"({lat}, {lon})")
    alt = location_info.get('altitude')
    if alt is not None:
        try:
            alt = float(alt)
        except (TypeError, ValueError):
            raise ValueError(_("无效的 GPS 高度: ") + repr(alt))
        if not math.isfinite(alt):
            raise ValueError(_("GPS 高度必须为有限数值"))
    return lat, lon, alt


def _build_gps_exiftool_args(location_info):
    lat, lon, alt = _validate_location(location_info)
    args = [
        f'-GPSLatitude={lat}',
        f'-GPSLongitude={lon}',
        f'-GPSLatitudeRef={"N" if lat >= 0 else "S"}',
        f'-GPSLongitudeRef={"E" if lon >= 0 else "W"}',
    ]
    if alt is not None:
        args += [
            f'-GPSAltitude={abs(alt)}',
            f'-GPSAltitudeRef={"0" if alt >= 0 else "1"}'
        ]
    else:
        # 高度为空 = 清除文件中原有的 GPS 高度标签，
        # 与 piexif 路径（整块替换 GPS IFD）行为保持一致
        args += ['-GPSAltitude=', '-GPSAltitudeRef=']
    return args


@_synchronized_file
def update_image_gps(file_path, location_info):
    """将 GPS 信息写入图像文件

    优先使用 piexif 库（纯 Python，速度快），
    如果 piexif 失败则自动回退到 ExifTool。
    处理过程中使用临时文件以确保数据安全。

    Args:
        file_path: 图像文件路径
        location_info: 包含 latitude, longitude, altitude 的字典
    """
    lat, lon, alt = _validate_location(location_info)
    orig_atime, orig_mtime = _get_stat(file_path)
    ext = os.path.splitext(file_path)[1].lower()

    # 同目录临时副本 + os.replace 原子替换；同目录不可写时抛错，
    # 由 except 分支回退 ExifTool（其写前备份与恢复路径更安全）
    temp_path = _piexif_temp_path(file_path, ext)
    try:
        shutil.copy2(file_path, temp_path)
        exif_dict = piexif.load(temp_path)

        gps_ifd = {
            piexif.GPSIFD.GPSVersionID: (2, 2, 0, 0),
            piexif.GPSIFD.GPSLatitudeRef: b'N' if lat >= 0 else b'S',
            piexif.GPSIFD.GPSLatitude: to_degrees(abs(lat)),
            piexif.GPSIFD.GPSLongitudeRef: b'E' if lon >= 0 else b'W',
            piexif.GPSIFD.GPSLongitude: to_degrees(abs(lon))
        }

        if alt is not None:
            gps_ifd[piexif.GPSIFD.GPSAltitudeRef] = 0 if alt >= 0 else 1
            abs_alt = abs(alt)
            if abs(abs_alt - round(abs_alt)) < 1e-9:
                gps_ifd[piexif.GPSIFD.GPSAltitude] = (round(abs_alt), 1)
            else:
                # 厘米级精度（分母 100 = 0.01 米），避免截断原始数值位数
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
        os.replace(temp_path, file_path)
        os.utime(file_path, (orig_atime, orig_mtime))
    except Exception:
        log_exc()
        _write_gps_with_exiftool(file_path, location_info, orig_atime, orig_mtime)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _run_exiftool(args, file_path, strict=False, external_backup=None):
    """运行 ExifTool 命令行工具

    使用固定的参数组合：
      -overwrite_original: 直接覆盖原文件（不创建备份）
      -P: 保留原始文件时间戳

    执行路径：优先使用常驻进程池（-stay_open，省去每文件 perl 进程启动），
    池执行层故障时自动回退到独立进程模式；两条路径的写入安全性一致。

    写前备份：覆盖写入前先把原文件完整复制到同目录（同盘，速度快，
    目录不可写时回退系统临时目录），写入成功后删除备份，任何异常路径
    （ExifTool 失败/被中断/严格校验不通过）都会自动用备份恢复原文件。
    多方法串行写入（视频/音频多个容器逐个尝试）时传入 external_backup
    只备份一次，避免每个方法都对大文件重复整文件复制。

    Args:
        args: ExifTool 参数列表
        file_path: 要处理的文件路径
        strict: 为 True 时，ExifTool 输出含警告
                （如"标签不支持"）也视为失败，避免误报写入成功
        external_backup: 由调用方创建并拥有的备份路径；传入时本函数
                不再创建/删除备份，失败恢复仍使用该备份（所有权归调用方）

    Raises:
        Exception: ExifTool 不可用或运行失败
    """
    available, tool_path = get_exiftool_cached()
    if not available:
        raise Exception(_("ExifTool 不可用，无法处理此文件"))

    # ---- 写前备份：保证失败时能恢复原始文件 ----
    owns_backup = external_backup is None
    backup_path = external_backup if not owns_backup else (
        _make_backup(file_path) if os.path.isfile(file_path) else None)

    # 清理上次异常退出残留的 ExifTool 临时文件（<name>_exiftool_tmp）。
    # ExifTool 使用 -overwrite_original 写盘时若检测到同名 tmp 已存在会拒绝写入
    # 并报 "Temporary file already exists"；程序内同一文件写入是串行的，
    # 残留只能来自被强杀/崩溃的旧进程，删除后可继续正常写入。
    if os.path.isfile(file_path):
        try:
            stale_tmp = file_path + '_exiftool_tmp'
            if os.path.exists(stale_tmp):
                os.remove(stale_tmp)
        except OSError:
            pass

    restore_ok = False
    try:
        stdout_text, stderr_text = _execute_exiftool(tool_path, args, file_path)
        _validate_exiftool_result(stdout_text, stderr_text, strict, file_path, tool_path, args)
        restore_ok = True  # 写入成功，稍后清理备份
    except Exception:
        # 任何失败路径：用备份恢复原文件，然后重新抛出
        restore_ok = False
        if backup_path and os.path.exists(backup_path):
            try:
                shutil.copy2(backup_path, file_path)
                restore_ok = True
            except Exception:
                log_exc()
        # 恢复失败时绝不删除备份：备份是此时唯一的原始副本，删了会永久丢失数据。
        # 将其复制为 原文件名.igt_backup_<时间戳> 保留在原文件同目录，
        # 并在异常信息中附上备份路径，便于用户手动找回。
        if backup_path and os.path.exists(backup_path) and not restore_ok:
            keep_name = file_path + '.igt_backup_' + _now_str()
            try:
                shutil.copy2(backup_path, keep_name)
                raise Exception(
                    _("写入失败且原文件恢复失败，已保留备份: ") + keep_name)
            except Exception:
                log_exc()
                raise
        raise
    finally:
        # 仅在成功或备份已被原样恢复时才删除备份；
        # 恢复失败时备份已复制为 .igt_backup_*，此处不应再删。
        # 外部传入的备份由调用方统一管理，此处不删除。
        if owns_backup and backup_path and os.path.exists(backup_path) and restore_ok:
            try:
                os.remove(backup_path)
            except OSError:
                pass


def _make_backup(file_path):
    """写前备份：优先原文件同目录（同盘复制快），目录不可写时回退系统临时目录"""
    ext = os.path.splitext(file_path)[1] or '.bin'
    base_dir = os.path.dirname(os.path.abspath(file_path))
    tried = []
    for d in (base_dir, None):  # None = 系统临时目录
        backup_path = None
        try:
            fd, backup_path = tempfile.mkstemp(prefix='.igt_bak_', suffix=ext, dir=d)
            os.close(fd)
            shutil.copy2(file_path, backup_path)
            return backup_path
        except Exception:
            if backup_path:
                try:
                    os.remove(backup_path)
                except OSError:
                    pass
            tried.append(str(d or _("系统临时目录")))
            continue
    # 备份失败（如磁盘满/权限）时中止写入，避免无备份覆盖原文件
    raise Exception(_("无法创建原文件备份，已中止写入: ") + str(file_path))


def _write_timeout_for(file_path):
    """按文件大小估算 ExifTool 重写超时（秒）

    固定 60s 对数百 MB 以上的视频/RAW 不够：ExifTool 写元数据要整文件
    重写，慢盘上可能超时被误杀（池模式看门狗会杀进程触发备份恢复）。
    按 ~20MB/s 保守估算，256MB 以下保持 60s，封顶 15 分钟。
    """
    try:
        size = os.path.getsize(file_path)
    except OSError:
        return 60
    if size <= 256 * 1024 * 1024:
        return 60
    return min(900, 60 + size // (20 * 1024 * 1024))


def _execute_exiftool(tool_path, args, file_path):
    """执行 ExifTool 写入命令，返回 (stdout_text, stderr_text)

    优先常驻进程池；池执行层故障（进程超时/退出等）时回退到独立进程模式。
    """
    cmd_args = ['-overwrite_original', '-P', *args, file_path]
    timeout = _write_timeout_for(file_path)

    def run_independent():
        with _exiftool_semaphore:
            r = subprocess.run([tool_path, *cmd_args],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=timeout, startupinfo=get_startupinfo(),
                               errors='replace')
            # 非 0 返回码视为执行层故障（崩溃/被强杀），由调用方回退/抛错，
            # 避免文本校验把"进程异常退出"误判为写入成功
            if r.returncode != 0:
                raise _PoolExecutionError(
                    _("ExifTool 执行失败（退出码 ") + str(r.returncode) + "): "
                    + (r.stderr or "").strip()[:200])
            return r.stdout, r.stderr

    pool = None
    try:
        pool = _get_pool(tool_path)
    except Exception:
        log_exc()
    if pool is not None:
        try:
            result = pool.run(cmd_args, timeout=timeout)
            # 每次成功使用后重置空闲计时器，任务结束自动回收进程
            _arm_pool_idle_close()
            return result
        except _PoolExecutionError:
            pass  # 池故障，回退独立模式
    return run_independent()


def _validate_exiftool_result(stdout_text, stderr_text, strict, file_path, tool_path, args):
    """基于输出文本校验 ExifTool 写入结果

    常驻池模式没有进程级返回码，用输出文本等价判断：
    "Error" 出现在 stderr 视同原逻辑的 returncode != 0。

    Raises:
        Exception: 写入失败（含严格模式下的警告）
    """
    stderr = stderr_text or ""
    msg = stderr.strip() or (stdout_text or "").strip()

    if "Error" in stderr or "not yet supported" in stderr:
        # 仅老版本 ExifTool（10.x）把文件名编码问题作为 Error 抛出时才重试；
        # ExifTool 13.x 经 Unicode API 传参，对编码只打 Warning（可正常写入），
        # 且部分 charset 名（cp936/GB2312）会被拒绝，此时不应进入重试。
        if "Error: FileName encoding" in stderr:
            # 老版本 ExifTool（10.x）无 Unicode 传参支持，需指定系统代码页
            # 字符集重试。重试失败时透传原始错误，避免误报为编码问题。
            charset_name = {936: 'GB2312', 932: 'ShiftJIS', 949: 'KSC',
                            950: 'Big5'}.get(_system_codepage(), 'Latin1')
            try:
                out2, err2 = _execute_exiftool(
                    tool_path, ['-charset', 'filename=' + charset_name] + list(args),
                    file_path)
            except Exception:
                log_exc()
                raise Exception((stderr or "").strip()[:500])
            if "Error" in (err2 or "") or "not yet supported" in (err2 or ""):
                # 重试后仍有真实错误（如格式不支持）：透传实际错误信息
                raise Exception((err2 or "").strip()[:500])
            if "Error: FileName encoding" in (err2 or ""):
                # 字符集名不被当前版本接受（或仍无法编码）：透传原始错误
                raise Exception((stderr or "").strip()[:500])
            return
        if "not yet supported" in stderr:
            ext = os.path.splitext(file_path)[1].lower()
            raise Exception(_("暂不支持写入") + f" {ext.upper()} " + _("文件格式"))
        raise Exception(msg[:500])
    if strict and stderr and ('Warning' in stderr or 'Error' in stderr):
        if "FileName encoding must be specified" not in stderr:
            # ExifTool 对不支持的标签仅打 warning 且 exit 0，实际并未写入。
            # 但 "not defined" 类警告（如向 QuickTime 写无组前缀的 GPSAltitude）
            # 只代表该标签不被容器支持，其它标签（lat/lon）已成功写入，
            # 此时不应把整个方法判定失败回滚。
            if "not defined" not in stderr:
                raise Exception(_("ExifTool 未成功写入: ") + stderr.strip()[:200])


def _write_gps_with_exiftool(file_path, location_info, orig_atime, orig_mtime):
    """使用 ExifTool 写入 GPS 坐标的内部函数（piexif 失败时的后备方案）"""
    gpscmd = _build_gps_exiftool_args(location_info)
    _run_exiftool(gpscmd, file_path, strict=True)
    os.utime(file_path, (orig_atime, orig_mtime))


@_synchronized_file
def update_raw_gps(file_path, location_info):
    orig_atime, orig_mtime = _get_stat(file_path)
    gpscmd = _build_gps_exiftool_args(location_info)
    _run_exiftool(gpscmd, file_path, strict=True)
    os.utime(file_path, (orig_atime, orig_mtime))


@_synchronized_file
def update_video_gps(file_path, location_info):
    """使用 ExifTool 将 GPS 信息写入视频文件

    尝试多种写入方法（Keys、QuickTime、XMP、UserData、标准 GPS 标签），
    因为不同视频格式支持的元数据容器不同。

    Args:
        file_path: 视频文件路径
        location_info: 包含 latitude, longitude, altitude 的字典
    """
    _validate_location(location_info)
    orig_atime, orig_mtime = _get_stat(file_path)

    # 尝试五种不同的写入方法，支持不同视频格式的元数据容器。
    # 只有 lat/lon 参与容器探测；高度单独用 XMP 组补写（见下），
    # 因为 Keys/QuickTime/UserData 组大多不支持 GPSAltitude。
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

    # 多方法共用一份备份：避免每个方法都对大文件整文件复制，
    # 方法失败由 _run_exiftool 用同一备份恢复原文件
    backup_path = _make_backup(file_path)
    try:
        success = False
        for method in methods:
            try:
                _run_exiftool(method, file_path, strict=True,
                              external_backup=backup_path)
                success = True
                break
            except Exception:
                log_exc()
                continue

        # 高度独立补写：只有 XMP 组（及标准 GPS 组）支持 GPSAltitude，
        # 覆盖 Keys/QuickTime/UserData 容器。高度写入失败不回滚已成功的 lat/lon。
        if success and location_info.get('altitude') is not None:
            try:
                alt_method = [
                    f'-XMP:GPSAltitude={abs(location_info["altitude"])}',
                    f'-XMP:GPSAltitudeRef={"0" if location_info["altitude"] >= 0 else "1"}',
                    f'-GPSAltitude={abs(location_info["altitude"])}',
                    f'-GPSAltitudeRef={"0" if location_info["altitude"] >= 0 else "1"}',
                ]
                _run_exiftool(alt_method, file_path, strict=True,
                              external_backup=backup_path)
            except Exception:
                log_exc()

        if success:
            os.utime(file_path, (orig_atime, orig_mtime))
        else:
            raise Exception(_("所有GPS写入方法均失败，不支持的视频文件格式"))
    finally:
        if backup_path and os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError:
                pass


@_synchronized_file
def update_audio_gps(file_path, location_info):
    """使用 ExifTool 将 GPS 信息写入音频文件（通过 XMP 标签）

    音频文件主要使用 XMP 元数据容器来存储 GPS 信息。

    Args:
        file_path: 音频文件路径
        location_info: 包含 latitude, longitude, altitude 的字典
    """
    _validate_location(location_info)
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

    # 多方法共用一份备份（见 update_video_gps 说明）
    backup_path = _make_backup(file_path)
    try:
        success = False
        for method in methods:
            try:
                _run_exiftool(method, file_path, strict=True,
                              external_backup=backup_path)
                success = True
                break
            except Exception:
                log_exc()
                continue

    # 高度独立补写（音频同视频：XMP 组支持 GPSAltitude）
        if success and location_info.get('altitude') is not None:
            try:
                _run_exiftool([
                    f'-XMP:GPSAltitude={abs(location_info["altitude"])}',
                    f'-XMP:GPSAltitudeRef={"0" if location_info["altitude"] >= 0 else "1"}',
                ], file_path, strict=True, external_backup=backup_path)
            except Exception:
                log_exc()

        if success:
            os.utime(file_path, (orig_atime, orig_mtime))
        else:
            raise Exception(_("所有GPS写入方法均失败，不支持的音频文件格式"))
    finally:
        if backup_path and os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError:
                pass



@_synchronized_file
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
        try:
            temp_path = _piexif_temp_path(file_path, ext)
        except Exception:
            log_exc()
            _run_exiftool(['-GPS*=', '-XMP:GPS*='], file_path)
            os.utime(file_path, (orig_atime, orig_mtime))
            return
        try:
            shutil.copy2(file_path, temp_path)
            exif_dict = piexif.load(temp_path)
            exif_dict["GPS"] = {}
            piexif.insert(piexif.dump(exif_dict), temp_path)
            os.replace(temp_path, file_path)
        except Exception:
            log_exc()
            _run_exiftool(['-GPS*=', '-XMP:GPS*='], file_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        # piexif 只清 EXIF GPS IFD；部分文件（视频转存、相机直写 XMP）
        # 同时在 XMP 组存有 GPS 坐标，补一次 XMP 清理避免删除不彻底。
        # ExifTool 不可用时忽略（EXIF GPS 已清除，XMP 残留不影响读取优先级）
        try:
            _run_exiftool(['-XMP:GPS*='], file_path)
        except Exception:
            log_exc()

    os.utime(file_path, (orig_atime, orig_mtime))


@_synchronized_file
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

    # 同目录临时副本 + os.replace 原子替换（见 update_image_gps 说明）
    temp_path = _piexif_temp_path(file_path, ext)
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
        os.replace(temp_path, file_path)
        os.utime(file_path, (orig_atime, orig_mtime))
    except Exception:
        log_exc()
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
    _run_exiftool(args, file_path, strict=True)
    os.utime(file_path, (orig_atime, orig_mtime))


@_synchronized_file
def update_raw_date(file_path, new_datetime):
    """使用 ExifTool 将日期写入 RAW 文件"""
    orig_atime, orig_mtime = _get_stat(file_path)
    d = new_datetime.strftime("%Y:%m:%d %H:%M:%S")
    args = [f'-DateTimeOriginal={d}', f'-DateTime={d}', f'-CreateDate={d}']
    _run_exiftool(args, file_path, strict=True)
    os.utime(file_path, (orig_atime, orig_mtime))


@_synchronized_file
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

    # 多方法共用一份备份（见 update_video_gps 说明）
    backup_path = _make_backup(file_path)
    try:
        success = False
        for method in methods:
            try:
                _run_exiftool(method, file_path, strict=True,
                              external_backup=backup_path)
                success = True
                break
            except Exception:
                log_exc()
                continue

        if success:
            os.utime(file_path, (orig_atime, orig_mtime))
        else:
            raise Exception(_("所有日期写入方法均失败，不支持的视频文件格式"))
    finally:
        if backup_path and os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError:
                pass


@_synchronized_file
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

    # 多方法共用一份备份（见 update_video_gps 说明）
    backup_path = _make_backup(file_path)
    try:
        success = False
        for method in methods:
            try:
                _run_exiftool(method, file_path, strict=True,
                              external_backup=backup_path)
                success = True
                break
            except Exception:
                log_exc()
                continue

        if success:
            os.utime(file_path, (orig_atime, orig_mtime))
        else:
            raise Exception(_("所有日期写入方法均失败，不支持的音频文件格式"))
    finally:
        if backup_path and os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError:
                pass


@_synchronized_file
def clear_audio_date(file_path):
    """清除音频文件中的所有日期标签"""
    orig_atime, orig_mtime = _get_stat(file_path)
    _run_exiftool(['-time:all='], file_path)
    os.utime(file_path, (orig_atime, orig_mtime))


@_synchronized_file
def blank_exif_dates(file_path):
    """使用 ExifTool 清空 EXIF 日期标签

    将 ALLDates 设置为 0001:01:01 00:00:00，
    而非删除标签，以保持 EXIF 结构完整。
    """
    orig_atime, orig_mtime = _get_stat(file_path)

    _run_exiftool(['-AllDates=0001:01:01 00:00:00'], file_path)

    os.utime(file_path, (orig_atime, orig_mtime))


@_synchronized_file
def clear_video_date(file_path):
    """清除视频文件中的所有日期标签"""
    orig_atime, orig_mtime = _get_stat(file_path)
    _run_exiftool(['-time:all='], file_path)
    os.utime(file_path, (orig_atime, orig_mtime))


def _gps_from_tags(tags):
    """从 exifread 解析出的 tags 中提取 GPS 坐标（度/分/秒转十进制度数）"""
    lat = lon = alt = None
    if 'GPS GPSLatitude' in tags and 'GPS GPSLongitude' in tags:
        lat = _ratio_deg_to_float(tags['GPS GPSLatitude'].values)
        lon = _ratio_deg_to_float(tags['GPS GPSLongitude'].values)
        if lat is not None and 'GPS GPSLatitudeRef' in tags and str(tags['GPS GPSLatitudeRef']) == 'S':
            lat = -lat
        if lon is not None and 'GPS GPSLongitudeRef' in tags and str(tags['GPS GPSLongitudeRef']) == 'W':
            lon = -lon

    if 'GPS GPSAltitude' in tags:
        try:
            vals = tags['GPS GPSAltitude'].values
            v = vals[0] if vals else None
            if v is not None:
                if hasattr(v, "num"):
                    # exifread 的 Ratio 继承 Fraction，0/0 时 num/den 均为 0，
                    # float(v) 会抛 ZeroDivisionError，必须显式防护
                    den = float(getattr(v, 'den', 1))
                    alt = float(v.num) / den if den != 0 else None
                else:
                    alt = float(v)
                if alt is not None and 'GPS GPSAltitudeRef' in tags:
                    refs = tags['GPS GPSAltitudeRef'].values
                    if refs and refs[0] == 1:
                        alt = -alt
        except (AttributeError, IndexError, ValueError, ZeroDivisionError, TypeError):
            alt = None

    return lat, lon, alt


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
        return _gps_from_tags(tags)
    except Exception:
        # exifread 对畸形文件可能抛非标准异常（见 read_exif_datetime 说明）
        log_exc()
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
                    if isinstance(val, int):
                        # Pillow 10+ 的 getexif() 对 GPSInfo 只返回 IFD 偏移
                        # （int），必须经 get_ifd 展开成实际标签字典，
                        # 否则 val.items() 抛 AttributeError 被跳过、GPS 恒为 None
                        try:
                            val = exif_data.get_ifd(tag)
                        except Exception:
                            log_exc()
                            return None, None, None
                    gps_data = {}
                    try:
                        items = val.items()
                    except AttributeError:
                        # 某些文件的 GPSInfo 值不是可迭代字典（如 int），跳过
                        items = []
                    for gt, gv in items:
                        gps_data[GPSTAGS.get(gt, gt)] = gv
                    lat = lon = alt = None
                    if 'GPSLatitude' in gps_data and 'GPSLongitude' in gps_data:
                        latv, lonv = gps_data['GPSLatitude'], gps_data['GPSLongitude']
                        lat = float(latv[0]) + float(latv[1]) / 60 + float(latv[2]) / 3600
                        lon = float(lonv[0]) + float(lonv[1]) / 60 + float(lonv[2]) / 3600
                        # Pillow 对部分文件返回 bytes（如 b'S'），统一转 str 再比较，
                        # 否则南纬/西经会因 b'S' != 'S' 被误判为北纬/东经
                        if str(gps_data.get('GPSLatitudeRef', 'N')).upper() == 'S':
                            lat = -lat
                        if str(gps_data.get('GPSLongitudeRef', 'E')).upper() == 'W':
                            lon = -lon
                    if 'GPSAltitude' in gps_data:
                        v = gps_data['GPSAltitude']
                        if isinstance(v, tuple) and len(v) >= 2:
                            den = float(v[1])
                            alt = float(v[0]) / den if den != 0 else None
                        else:
                            alt = float(v)
                        if alt is not None and gps_data.get('GPSAltitudeRef', 0) == 1:
                            alt = -alt
                    return lat, lon, alt
    except Exception:
        log_exc()
    return None, None, None


_DATE_TAG_KEYS = (
    'EXIF:DateTimeOriginal', 'Composite:DateTimeOriginal',
    'EXIF:CreateDate', 'EXIF:DateTimeDigitized',
    'QuickTime:MediaCreateDate', 'QuickTime:CreateDate',
    'QuickTime:CreationDate', 'Keys:CreationDate',
    'XMP:CreateDate', 'XMP:DateCreated',
)


def extract_media_datetime_exiftool(file_path):
    """使用 ExifTool 读取媒体文件的拍摄日期（RAW/HEIC/WebP/视频/音频等）

    exifread/QuickTime 二进制解析覆盖不了 RAW、HEIC、WebP、MKV/AVI 等
    容器；「跳过已有日期」等需要准确判断文件是否已有拍摄日期的场景
    必须依赖 ExifTool 读取，否则会把已有日期的文件误判为无日期而覆盖。

    Args:
        file_path: 文件路径

    Returns:
        datetime or None: 读取失败或 ExifTool 不可用时返回 None
    """
    available, tool_path = get_exiftool_cached()
    if not available:
        return None
    try:
        with _exiftool_semaphore:
            r = subprocess.run([tool_path, '-j', '-G', '-n', file_path],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=30, startupinfo=get_startupinfo(),
                               errors='replace')
        if r.returncode != 0:
            return None
        md_list = json.loads(r.stdout)
        if not isinstance(md_list, list) or not md_list or not isinstance(md_list[0], dict):
            return None
        md = md_list[0]
        for key in _DATE_TAG_KEYS:
            if key in md and md[key]:
                parsed, had_tz = _parse_video_time_detailed(str(md[key]))
                if parsed:
                    # QuickTime 时间始终为 UTC（即使输出不带 Z 后缀），
                    # 与 extract_video_gps_with_exiftool 的语义保持一致
                    if key.startswith('QuickTime:') and not had_tz:
                        utc_dt = parsed.replace(tzinfo=timezone.utc)
                        parsed = utc_dt.astimezone().replace(tzinfo=None)
                    return parsed
    except (ValueError, TypeError, OSError, subprocess.SubprocessError):
        log_exc()
    return None


def extract_video_gps_with_exiftool(file_path):
    """使用 ExifTool 从视频文件中提取 GPS 和时间数据

    ExifTool 以 JSON 格式输出所有元数据，从中提取 GPS 坐标和创建时间。
    尝试多种可能的标签路径（QuickTime、XMP、Keys、UserData 等）。

    Args:
        file_path: 视频文件路径

    Returns:
        tuple: (纬度, 经度, 高度, 时间)，失败返回 (None, None, None, None)
    """
    available, tool_path = get_exiftool_cached()
    if not available:
        return None, None, None, None

    tool = tool_path
    cmd = [tool, '-j', '-G', '-n', file_path]

    try:
        with _exiftool_semaphore:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=30, startupinfo=get_startupinfo(),
                              errors='replace')
        if r.returncode != 0:
            return None, None, None, None

        try:
            md_list = json.loads(r.stdout)
        except (ValueError, TypeError):
            # 空输出/非 JSON 输出（如损坏视频）→ 无法提取，返回 None 而非崩溃
            return None, None, None, None
        if not isinstance(md_list, list) or not md_list or not isinstance(md_list[0], dict):
            return None, None, None, None
        md = md_list[0]
        lat = lon = alt = None
        video_time = None

        for dtkey in ('QuickTime:MediaCreateDate', 'QuickTime:MediaModifyDate',
                      'QuickTime:CreationDate', 'QuickTime:CreateDate',
                      'QuickTime:ModifyDate', 'File:FileModifyDate', 'System:FileModifyDate'):
            if dtkey in md and video_time is None:
                try:
                    raw = md[dtkey]
                    parsed, had_tz = _parse_video_time_detailed(raw)
                    if parsed:
                        # QuickTime 时间始终为 UTC（即使 ExifTool 输出不带 Z 后缀）。
                        # 仅当原始字符串不含任何时区信息时按 UTC 转换；
                        # 若已带 Z/±HH:MM，_parse_video_time_detailed 已转成本地时间，
                        # 再转一次会导致偏移重复
                        if dtkey.startswith('QuickTime:') and not had_tz:
                            utc_dt = parsed.replace(tzinfo=timezone.utc)
                            parsed = utc_dt.astimezone().replace(tzinfo=None)
                        video_time = parsed
                        break
                except Exception:
                    log_exc()
                    continue

        if 'Composite:GPSLatitude' in md and 'Composite:GPSLongitude' in md:
            try:
                lat = float(md['Composite:GPSLatitude'])
                lon = float(md['Composite:GPSLongitude'])
            except Exception:
                log_exc()
        elif 'QuickTime:GPSCoordinates' in md:
            try:
                c = md['QuickTime:GPSCoordinates'].split(',')
                if len(c) >= 2:
                    lat, lon = float(c[0]), float(c[1])
            except Exception:
                log_exc()
        elif 'XMP:GPSLatitude' in md and 'XMP:GPSLongitude' in md:
            try:
                lat, lon = float(md['XMP:GPSLatitude']), float(md['XMP:GPSLongitude'])
            except Exception:
                log_exc()
        elif 'Keys:GPSCoordinates' in md:
            try:
                c = md['Keys:GPSCoordinates'].split(',')
                if len(c) >= 2:
                    lat, lon = float(c[0]), float(c[1])
            except Exception:
                log_exc()
        elif 'UserData:GPSCoordinates' in md:
            try:
                c = md['UserData:GPSCoordinates'].split(',')
                if len(c) >= 2:
                    lat, lon = float(c[0]), float(c[1])
            except Exception:
                log_exc()

        if 'Composite:GPSAltitude' in md:
            try:
                alt = float(md['Composite:GPSAltitude'])
            except Exception:
                log_exc()
        elif 'XMP:GPSAltitude' in md:
            try:
                alt = float(md['XMP:GPSAltitude'])
            except Exception:
                log_exc()
        elif 'QuickTime:GPSAltitude' in md:
            try:
                alt = float(md['QuickTime:GPSAltitude'])
            except Exception:
                log_exc()

        return lat, lon, alt, video_time
    except Exception:
        log_exc()
        return None, None, None, None
