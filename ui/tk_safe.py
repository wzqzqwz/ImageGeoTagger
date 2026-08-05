"""Tk 生命周期安全工具

窗口 destroy 后，已注册的 after() 回调仍会被 Tcl 触发，
此时对已销毁 widget 的任何访问都会抛 TclError。
打包版隐藏控制台后，这类异常在 stderr 里变成一堆无意义的报错噪音。
本模块提供带存活检查的 after 调度封装：窗口已销毁则注册/触发时静默跳过。
"""

import tkinter as tk

from utils.logging_utils import log_exc


def safe_after(widget, delay, callback):
    """窗口存活时才注册 after 回调；触发时若窗口已销毁则静默跳过

    注意：回调在 Tk 主线程执行，窗口销毁与回调触发之间的竞态
    由回调内部的 winfo_exists 检查兜底。
    """
    try:
        if not widget.winfo_exists():
            return None
    except tk.TclError:
        return None

    def _guarded():
        try:
            if widget.winfo_exists():
                callback()
        except tk.TclError:
            pass
        except Exception:
            log_exc()

    try:
        return widget.after(delay, _guarded)
    except tk.TclError:
        return None


def pulse_progress(window, bar, label, done_text):
    """进度条三段式脉冲动画（50/100/150ms 各推进一档）

    操作完成后展示"成功"的视觉反馈；窗口已关闭时静默跳过。
    """
    def _set(value, text):
        try:
            bar['value'] = value
            if text is not None:
                label.config(text=text)
        except tk.TclError:
            pass

    safe_after(window, 50, lambda: _set(50, None))
    safe_after(window, 100, lambda: _set(75, None))
    safe_after(window, 150, lambda: _set(100, done_text))
