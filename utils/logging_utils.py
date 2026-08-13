"""应用日志工具 - 文件日志 + 控制台双重输出

打包版（console=False 隐藏控制台）下 traceback.print_exc() 对用户不可见，
异常只进 stderr 无处可查。本模块将日志写入
~/.imagegeotagger/imagegeotagger.log（2MB 滚动 × 3 备份），
同时保留 stderr 输出便于开发调试。
"""

import logging
import os
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.join(os.path.expanduser('~'), '.imagegeotagger')
_LOG_FILE = os.path.join(_LOG_DIR, 'imagegeotagger.log')
_LOGGER_NAME = 'igt'

_initialized = False
# 两线程同时首次调用时防止重复添加 handler（每行日志双写）
_init_lock = threading.Lock()


def setup_logging():
    """初始化日志（幂等）。程序启动时调用一次。"""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        _initialized = True

        logger = logging.getLogger(_LOGGER_NAME)
        if logger.handlers:
            return
        logger.setLevel(logging.DEBUG)
        fmt = logging.Formatter(
            '%(asctime)s %(levelname)s [%(threadName)s] %(message)s')

        try:
            os.makedirs(_LOG_DIR, exist_ok=True)
            fh = RotatingFileHandler(_LOG_FILE, maxBytes=2 * 1024 * 1024,
                                     backupCount=3, encoding='utf-8')
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            pass

        try:
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(fmt)
            logger.addHandler(sh)
        except Exception:
            pass


def get_logger():
    """返回应用根 logger（懒初始化）"""
    setup_logging()
    return logging.getLogger(_LOGGER_NAME)


def log_exc(context=''):
    """记录当前异常（含堆栈）到文件和控制台

    替代 traceback.print_exc()：打包版隐藏控制台后异常不再丢失。
    只能在 except 块内调用；非异常上下文调用会记录空堆栈噪音，
    直接返回空串。

    Args:
        context: 可选的上文说明，便于在长日志中定位

    Returns:
        str: 完整堆栈文本（调用方如需透传原始信息可复用）
    """
    if sys.exc_info()[0] is None:
        # 非异常上下文：format_exc() 只会产生 "NoneType: None" 垃圾日志
        return ''
    tb = traceback.format_exc()
    # setup_logging 幂等（已初始化或有 handler 时直接返回），
    # 此处统一确保 handler 就绪后再写日志
    setup_logging()
    logger = logging.getLogger(_LOGGER_NAME)
    if context:
        logger.error('%s | %s', context, tb.rstrip())
    else:
        logger.error('%s', tb.rstrip())
    return tb


def log_info(msg):
    get_logger().info('%s', msg)


def log_warning(msg):
    get_logger().warning('%s', msg)
