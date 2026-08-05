"""GPS 数据模型

定义了 GPS 轨迹点的数据结构，用于从 GPX 文件中解析出的轨迹点，
以及在地理位置处理过程中作为参考点。
"""


class GpsPoint:
    """GPS 轨迹点数据模型

    表示 GPS 轨迹中的一个采样点，包含时间、经纬度、高度等信息。
    通常从 GPX 文件中解析得到，用于与媒体文件的拍摄时间进行匹配。
    """

    def __init__(self, datetime_val, latitude, longitude, altitude=None,
                 source='GPX', source_file=None):
        if latitude is not None and not -90 <= latitude <= 90:
            raise ValueError(f"latitude out of range: {latitude}")
        if longitude is not None and not -180 <= longitude <= 180:
            raise ValueError(f"longitude out of range: {longitude}")
        # 轨迹点的时间戳（已转换到本地时区）
        self.timestamp = datetime_val
        # 纬度（十进制度数，北正南负）
        self.latitude = latitude
        # 经度（十进制度数，东正西负）
        self.longitude = longitude
        # 海拔高度（米），可选
        self.altitude = altitude
        # 数据来源，默认为 'GPX'
        self.source = source
        # 来源 GPX 文件名，用于追踪数据来源
        self.source_file = source_file

    def has_coordinates(self):
        """检查该点是否有有效的经纬度坐标"""
        return self.latitude is not None and self.longitude is not None

    def to_dict(self):
        """将对象转换为字典，便于序列化（JSON/CSV 导出）"""
        return {
            'datetime': self.timestamp,
            'latitude': self.latitude,
            'longitude': self.longitude,
            'altitude': self.altitude,
            'source': self.source,
            'source_file': self.source_file,
        }


