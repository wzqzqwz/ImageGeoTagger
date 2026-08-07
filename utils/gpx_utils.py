"""GPX 文件解析和生成工具

提供 GPX（GPS Exchange Format）文件的完整处理功能：
  - 解析 GPX 文件提取轨迹点数据
  - 从数据生成 GPX XML 结构
  - GPX 时间的解析和格式化
"""

import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from utils.logging_utils import log_exc

try:
    from defusedxml import ElementTree as SafeET
except ImportError:
    SafeET = None


def _reject_unsafe_xml(gpx_file_path):
    """流式扫描 XML 前导部分，检测 DTD/实体声明（防实体膨胀 DoS）。

    恶意 GPX 可通过内部实体展开（billion laughs）造成内存耗尽。
    DTD 声明只能出现在根元素之前的 prolog 中；与固定读取文件头部不同，
    这里按 XML 词法逐段扫描（跳过注释和处理指令），直到遇到根元素才停止，
    因此声明前即使有大量前导空白/注释也能被检测到。

    Returns:
        bool: True 表示检测到 DTD/实体声明或前导异常，应拒绝解析
    """
    # 单个注释/空白段超过该长度视为异常前导，保守拒绝
    MAX_SCAN = 4 * 1024 * 1024
    try:
        with open(gpx_file_path, 'rb') as f:
            buf = b''
            pos = 0
            while True:
                lt = buf.find(b'<', pos)
                if lt == -1:
                    # 当前位置之后没有 '<'：继续读取更多数据
                    if len(buf) - pos > MAX_SCAN:
                        return True
                    chunk = f.read(65536)
                    if not chunk:
                        # EOF，整个文件都是前导部分，未发现危险声明
                        return False
                    buf += chunk
                    continue
                if lt - pos > MAX_SCAN:
                    # 元素间超长空白/文本，异常
                    return True
                if buf.startswith(b'<!--', lt):
                    # 注释：内部内容不会展开，跳过
                    end = buf.find(b'-->', lt + 4)
                    if end == -1 or end - lt > MAX_SCAN:
                        if end != -1:
                            return True
                        # 未找到终止符：累计长度超限即拒绝（防内存无界增长）
                        if len(buf) - lt > MAX_SCAN:
                            return True
                        chunk = f.read(65536)
                        if not chunk:
                            return False
                        buf += chunk
                        continue
                    pos = end + 3
                    # 截断已消费的前导：buf 无界增长会使单段注释多时
                    # 出现 O(n²) 复制（实测 180MB 前导耗时 49s），
                    # 且内存随文件大小线性膨胀
                    if pos > 1024 * 1024:
                        buf = buf[pos:]
                        pos = 0
                    continue
                if buf.startswith(b'<?', lt):
                    # 处理指令（如 XML 声明）：跳过
                    end = buf.find(b'?>', lt + 2)
                    if end == -1:
                        if len(buf) - lt > MAX_SCAN:
                            return True
                        chunk = f.read(65536)
                        if not chunk:
                            return False
                        buf += chunk
                        continue
                    pos = end + 2
                    if pos > 1024 * 1024:
                        buf = buf[pos:]
                        pos = 0
                    continue
                if len(buf) - lt < 9:
                    # 声明/标签可能被截断在缓冲区边界，补齐后再判断
                    chunk = f.read(65536)
                    if not chunk:
                        return False
                    buf += chunk
                    continue
                low = buf[lt:lt + 9].lower()
                if low.startswith(b'<!doctype') or low.startswith(b'<!entity'):
                    return True
                # 根元素（或其它非 DTD 声明）出现，前导扫描结束
                return False
    except Exception:
        # 读取异常（权限/损坏等）时按"可疑"处理拒绝解析（fail-closed）：
        # ET.parse 也会失败，但返回 True 保证异常文件绝不进入解析路径
        return True
    return False


def parse_gpx_time(time_str):
    """解析 GPX 时间字符串（将 UTC 转换为本地时间）

    GPX 时间通常采用 ISO 8601 格式并以 Z 结尾（UTC 时间），
    此函数会将其自动转换为本地时区。

    Args:
        time_str: GPX 时间字符串（如 "2024-01-15T10:30:00Z"）

    Returns:
        datetime: 本地时间，解析失败返回 None
    """
    try:
        # 兼容小写 z 后缀：GPX 1.1 规定用大写 Z，但部分生成器输出小写 z
        if time_str and time_str[-1] in ('Z', 'z'):
            base_str = time_str[:-1]
            if '.' in base_str:
                utc_time = datetime.strptime(base_str, "%Y-%m-%dT%H:%M:%S.%f")
            else:
                utc_time = datetime.strptime(base_str, "%Y-%m-%dT%H:%M:%S")
            utc_time = utc_time.replace(tzinfo=timezone.utc)
            return utc_time.astimezone().replace(tzinfo=None)
        elif '+' in time_str or time_str.count('-') > 2:
            try:
                s = time_str
                if s[-1:] in ('Z', 'z'):
                    s = s[:-1] + '+00:00'
                dt = datetime.fromisoformat(s)
                return dt.astimezone().replace(tzinfo=None)
            except Exception:
                log_exc()
        if '.' in time_str:
            return datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S.%f")
        else:
            return datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        log_exc()
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                    "%Y-%m-%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S.%f"]:
            try:
                return datetime.strptime(time_str, fmt)
            except Exception:
                continue
    return None


def parse_gpx_file(gpx_file_path):
    """解析 GPX 文件，提取所有轨迹点

    解析 GPX 1.1 格式的文件，提取每个轨迹点的经纬度、时间和高度。
    自动处理命名空间（兼容不同 GPX 生成器）。

    Args:
        gpx_file_path: GPX 文件路径

    Returns:
        list[dict]: 轨迹点字典列表，每个字典包含：
            datetime, latitude, longitude, altitude, source, source_file
    """
    points = []
    try:
        # 拒绝含 DTD/实体声明的 GPX，防止实体膨胀 DoS
        if _reject_unsafe_xml(gpx_file_path):
            return points

        if SafeET is not None:
            tree = SafeET.parse(gpx_file_path)
        else:
            tree = ET.parse(gpx_file_path)
        root = tree.getroot()

        # 自动检测并处理 XML 命名空间
        has_ns = '}' in root.tag
        if has_ns:
            ns_uri = root.tag.split('}')[0][1:]
            ns = {'gpx': ns_uri}
            point_paths = ['.//gpx:trkpt', './/gpx:rtept', './/gpx:wpt']
            ele_path = 'gpx:ele'
            time_path = 'gpx:time'
        else:
            ns = {}
            point_paths = ['.//trkpt', './/rtept', './/wpt']
            ele_path = 'ele'
            time_path = 'time'

        # 轨迹点 <trkpt> / 路线点 <rtept> / 航点 <wpt> 都解析：
        # 部分 GPX 生成器只输出 wpt/rtept（无 trkseg），只认 trkpt 会
        # 误报"轨迹点 0 个，无法匹配"
        for path in point_paths:
            for pt in root.findall(path, ns):
                lat = pt.get('lat')
                lon = pt.get('lon')
                if lat is None or lon is None:
                    continue

                # 海拔（可选）
                ele_elem = pt.find(ele_path, ns)
                ele = ele_elem.text if ele_elem is not None else None

                # 时间（必需：无时间的轨迹点无法用于匹配）
                time_elem = pt.find(time_path, ns)
                time_str = time_elem.text if time_elem is not None else None
                if time_str is None:
                    continue

                dt = parse_gpx_time(time_str)
                if dt is None:
                    continue

                # 单个坏点（非法/越界坐标）只跳过该点，不影响同文件其它有效点
                try:
                    lat_v = float(lat)
                    lon_v = float(lon)
                    ele_v = float(ele) if ele is not None else None
                except (ValueError, TypeError):
                    continue
                if not -90 <= lat_v <= 90 or not -180 <= lon_v <= 180:
                    continue
                if ele_v is not None and not (ele_v == ele_v):
                    # NaN 高度视为无高度
                    ele_v = None

                points.append({
                    'datetime': dt,
                    'latitude': lat_v,
                    'longitude': lon_v,
                    'altitude': ele_v,
                    'source': 'GPX',
                    'source_file': os.path.basename(gpx_file_path),
                })
    except Exception:
        log_exc()

    return points


def prettify_xml(elem, level=0):
    """为 XML 元素树添加合适的缩进并返回格式化后的 XML 字符串

    递归处理 XML 元素，添加换行和缩进使其可读。

    Args:
        elem: XML 元素
        level: 当前缩进级别

    Returns:
        str: 格式化后的 XML 字符串
    """
    indent = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
        for child in elem:
            prettify_xml(child, level + 1)
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent
    return ET.tostring(elem, encoding='unicode')


def _local_to_utc_str(dt):
    """将本地 naive datetime 转为 UTC ISO 字符串（带 Z 后缀）

    使用 time.mktime 将本地时间转换为 UTC 时间戳，
    自动处理 DST（夏令时）转换。

    注意：time.mktime 在 32 位时间戳环境中只能处理约 1970-2038 年的日期。
    超出范围的日期（如清空日期后写入的 0001-01-01）会导致 OverflowError，
    这里捕获异常返回 None，由调用方跳过该时间标签。
    """
    if dt is None:
        return None
    try:
        if dt.tzinfo is not None:
            utc_dt = dt.astimezone(timezone.utc)
        else:
            import time as _time
            # 快速过滤超出 mktime 支持范围的年份，避免不必要的异常开销。
            # 注意 UTC+x 时区下本地 1970 年初可能对应负时间戳，故下界取 1971。
            if dt.year < 1971 or dt.year > 2037:
                return None
            timestamp = _time.mktime(dt.timetuple())
            utc_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return utc_dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    except (OverflowError, ValueError, OSError):
        # mktime 范围限制或系统时区问题，返回 None 由调用方处理
        return None


def create_gpx_element(points, name="Image Geo Data"):
    """从数据点列表创建 GPX XML 元素树

    生成符合 GPX 1.1 规范的元素树，包含：
      - metadata（元数据：名称、时间）
      - trk/trkseg/trkpt（轨迹段和轨迹点）
      - wpt（航点，包含文件名信息）

    Args:
        points: 数据点字典列表（含 datetime, latitude, longitude, altitude, filename）
        name: GPX 轨道名称

    Returns:
        xml.etree.ElementTree.Element: GPX 根元素
    """
    root = ET.Element("gpx")
    root.set("version", "1.1")
    root.set("creator", "Image Geo Tagger")
    root.set("xmlns", "http://www.topografix.com/GPX/1/1")

    # 元数据
    metadata = ET.SubElement(root, "metadata")
    name_elem = ET.SubElement(metadata, "name")
    name_elem.text = name
    time_elem = ET.SubElement(metadata, "time")
    time_elem.text = _local_to_utc_str(datetime.now())

    # 过滤有效数据点并按时间排序
    # 只保留时间在 mktime 支持范围内的点，避免 OverflowError
    # 注意：边界要留余量——在 UTC+x 时区，本地 1970-01-01 00:00 可能对应
    # UTC 1969-12-31（负时间戳），Windows mktime 不支持，故用 1971-2037 保险
    def _valid_dt(dt_val):
        return (dt_val is not None
                and 1971 <= dt_val.year <= 2037)

    def _naive_dt(dt_val):
        # 统一转本地 naive 再排序，避免混合 aware/naive 抛 TypeError
        # （与 export_service._naive_dt 语义一致）
        if dt_val is not None and dt_val.tzinfo is not None:
            try:
                return dt_val.astimezone().replace(tzinfo=None)
            except Exception:
                return dt_val
        return dt_val

    sorted_items = sorted(
        [dict(p, datetime=_naive_dt(p.get('datetime'))) for p in points
         if _valid_dt(p.get('datetime'))
         and p.get('latitude') is not None
         and p.get('longitude') is not None],
        key=lambda x: x['datetime']
    )

    # 生成轨迹段（trkseg）
    if sorted_items:
        trk = ET.SubElement(root, "trk")
        trk_name = ET.SubElement(trk, "name")
        trk_name.text = name
        trkseg = ET.SubElement(trk, "trkseg")

        for item in sorted_items:
            trkpt = ET.SubElement(trkseg, "trkpt")
            # 6 位小数与手机照片 EXIF 精度一致（1e-6 度 ≈ 11 厘米）
            trkpt.set("lat", f"{item['latitude']:.8f}")
            trkpt.set("lon", f"{item['longitude']:.8f}")
            if item.get('altitude') is not None:
                ele = ET.SubElement(trkpt, "ele")
                ele.text = f"{item['altitude']:.2f}"
            t = ET.SubElement(trkpt, "time")
            t.text = _local_to_utc_str(item['datetime'])

    # 生成航点（wpt），包含文件名
    for item in sorted_items:
        wpt = ET.SubElement(root, "wpt")
        wpt.set("lat", f"{item['latitude']:.8f}")
        wpt.set("lon", f"{item['longitude']:.8f}")
        if item.get('altitude') is not None:
            ele = ET.SubElement(wpt, "ele")
            ele.text = f"{item['altitude']:.2f}"
        n = ET.SubElement(wpt, "name")
        n.text = item.get('filename', "Point")
        if item.get('datetime'):
            t = ET.SubElement(wpt, "time")
            t.text = _local_to_utc_str(item['datetime'])

    return root
