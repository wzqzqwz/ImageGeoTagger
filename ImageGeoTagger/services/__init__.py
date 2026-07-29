"""业务逻辑服务包

本包实现了应用程序的核心业务逻辑：
  - media_scanner: 扫描文件夹中的媒体文件和 GPX 轨迹文件
  - geo_processor: 根据时间匹配算法为文件分配 GPS 位置
  - date_processor: 修改文件的拍摄日期和重命名文件
  - export_service: 将处理结果导出为 TXT/CSV/JSON/GPX 格式
"""
