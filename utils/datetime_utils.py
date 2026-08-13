"""日期时间归一化工具

提供 datetime 时区归一化的公共函数，供各模块统一使用，
避免多处各自实现相同的 aware→本地 naive 转换导致语义漂移。
"""


def to_local_naive(dt):
    """将任意 datetime 转为本地无时区（naive）datetime

    带时区（aware）的值先转为本地时间再剥离 tzinfo；naive 值原样返回；
    None 返回 None。用于统一 aware/naive 的比较基准（如 min/max、排序、
    时间差计算），避免混合时区值相互比较时抛出 TypeError。

    语义说明：此处"本地"指本机时区（astimezone() 使用本机时区），
    在"数据时间基准 == 本机时区"的假设下自洽；跨时区场景需在数据
    引入侧先行修正时区。

    Args:
        dt: datetime 对象或 None

    Returns:
        datetime 或 None：本地无时区 datetime
    """
    if dt is not None and dt.tzinfo is not None:
        try:
            return dt.astimezone().replace(tzinfo=None)
        except Exception:
            # tzinfo 损坏等极端情况：退回原值，避免因归一化本身抛错
            return dt
    return dt
