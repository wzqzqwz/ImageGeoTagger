"""应用日志工具 - 文件日志 + 控制台双重输出

打包版（console=False 隐藏控制台）下 traceback.print_exc() 对用户不可见，
异常只进 stderr 无处可查。本模块将日志写入
~/.imagegeotagger/imagegeotagger.log（2MB 滚动 × 3 备份），
同时保留 stderr 输出便于开发调试。
"""

import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.join(os.path.expanduser('~'), '.imagegeotagger')
_LOG_FILE = os.path.join(_LOG_DIR, 'imagegeotagger.log')
_LOGGER_NAME = 'igt'

_initialized = False


def setup_logging():
    """初始化日志（幂等）。程序启动时调用一次。"""
    global _initialized
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
    可在 except 块内或任意位置调用（不在异常上下文时记录空堆栈，无副作用）。

    Args:
        context: 可选的上文说明，便于在长日志中定位

    Returns:
        str: 完整堆栈文本（调用方如需透传原始信息可复用）
    """
    tb = traceback.format_exc()
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        pass
    else:
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
