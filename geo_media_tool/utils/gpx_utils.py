"""GPX 文件解析和生成工具

提供 GPX（GPS Exchange Format）文件的完整处理功能：
  - 解析 GPX 文件提取轨迹点数据
  - 从数据生成 GPX XML 结构
  - GPX 时间的解析和格式化
"""

import os
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta


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
        if time_str.endswith('Z'):
            base_str = time_str[:-1]
            if '.' in base_str:
                utc_time = datetime.strptime(base_str, "%Y-%m-%dT%H:%M:%S.%f")
            else:
                utc_time = datetime.strptime(base_str, "%Y-%m-%dT%H:%M:%S")
            utc_time = utc_time.replace(tzinfo=timezone.utc)
            return utc_time.astimezone().replace(tzinfo=None)
        elif '+' in time_str or time_str.count('-') > 2:
            try:
                dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                return dt.astimezone().replace(tzinfo=None)
            except Exception:
                traceback.print_exc()
        if '.' in time_str:
            return datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S.%f")
        else:
            return datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        traceback.print_exc()
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
        tree = ET.parse(gpx_file_path)
        root = tree.getroot()

        # 自动检测并处理 XML 命名空间
        has_ns = '}' in root.tag
        if has_ns:
            ns_uri = root.tag.split('}')[0][1:]
            ns = {'gpx': ns_uri}
            trkpt_path = './/gpx:trkpt'
            ele_path = 'gpx:ele'
            time_path = 'gpx:time'
        else:
            ns = {}
            trkpt_path = './/trkpt'
            ele_path = 'ele'
            time_path = 'time'

        # 查找所有轨迹点 <trkpt>
        for trkpt in root.findall(trkpt_path, ns):
            lat = trkpt.get('lat')
            lon = trkpt.get('lon')
            if lat is None or lon is None:
                continue

            # 海拔（可选）
            ele_elem = trkpt.find(ele_path, ns)
            ele = ele_elem.text if ele_elem is not None else None

            # 时间（必需：无时间的轨迹点无法用于匹配）
            time_elem = trkpt.find(time_path, ns)
            time_str = time_elem.text if time_elem is not None else None
            if time_str is None:
                continue

            dt = parse_gpx_time(time_str)
            if dt is None:
                continue

            points.append({
                'datetime': dt,
                'latitude': float(lat),
                'longitude': float(lon),
                'altitude': float(ele) if ele is not None else None,
                'source': 'GPX',
                'source_file': os.path.basename(gpx_file_path),
            })
    except Exception:
        traceback.print_exc()

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
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        utc_dt = dt.astimezone(timezone.utc)
    else:
        import time as _time
        timestamp = _time.mktime(dt.timetuple())
        utc_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return utc_dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


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
    sorted_items = sorted(
        [p for p in points if p.get('datetime') and p.get('latitude') is not None and p.get('longitude') is not None],
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
            trkpt.set("lat", str(item['latitude']))
            trkpt.set("lon", str(item['longitude']))
            if item.get('altitude') is not None:
                ele = ET.SubElement(trkpt, "ele")
                ele.text = str(item['altitude'])
            t = ET.SubElement(trkpt, "time")
            t.text = _local_to_utc_str(item['datetime'])

    # 生成航点（wpt），包含文件名
    for item in sorted_items:
        wpt = ET.SubElement(root, "wpt")
        wpt.set("lat", str(item['latitude']))
        wpt.set("lon", str(item['longitude']))
        if item.get('altitude') is not None:
            ele = ET.SubElement(wpt, "ele")
            ele.text = str(item['altitude'])
        n = ET.SubElement(wpt, "name")
        n.text = item.get('filename', "Point")
        if item.get('datetime'):
            t = ET.SubElement(wpt, "time")
            t.text = _local_to_utc_str(item['datetime'])

    return root
