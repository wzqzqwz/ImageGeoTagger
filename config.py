"""配置常量和文件类型定义

本模块定义了所有支持的媒体文件扩展名、日期解析模式、
默认参数以及各种服务的 URL 等全局常量。
"""

# 支持的图像文件扩展名（用于 EXIF 读取/写入）
# 包含常见格式和各大相机厂商的 RAW 格式
IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp', '.gif', '.webp',
    '.heic', '.heif', '.nef', '.cr2', '.cr3', '.arw', '.dng', '.orf',
    '.rw2', '.pef', '.srw', '.raf', '.x3f', '.mrw', '.erf', '.kdc',
    '.dcr', '.raw', '.srf', '.sr2', '.mef', '.mos', '.fff', '.iiq',
    '.nrw', '.cap', '.tga', '.exr', '.hdr',
}

# RAW 文件扩展名（需要 ExifTool 第三方工具处理）
# RAW 文件通常不包含标准 EXIF，需要通过 ExifTool 读写元数据
RAW_EXTENSIONS = {
    '.nef', '.cr2', '.cr3', '.arw', '.orf', '.rw2', '.dng', '.pef',
    '.srw', '.raf', '.x3f', '.sr2', '.mef', '.mrw', '.kdc', '.dcr',
    '.erf', '.3fr', '.fff', '.mos', '.iiq', '.nrw', '.r3d', '.raw',
    '.bay', '.srf', '.cap',
}

# 支持的视频文件扩展名
# 视频文件同样通过 ExifTool 进行元数据读写
VIDEO_EXTENSIONS = {
    '.mov', '.mp4', '.avi', '.mkv', '.wmv', '.flv', '.webm', '.m4v',
    '.3gp', '.3g2', '.asf', '.rm', '.rmvb', '.vob', '.ogv', '.mts',
    '.m2ts', '.ts',
}

# 支持的音频文件扩展名
# 通过 ExifTool 写入 XMP 标签来存储元数据
AUDIO_EXTENSIONS = {
    '.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a', '.aiff',
    '.au', '.ra',
}

# 所有媒体文件扩展名的合集（用于文件扫描过滤）
ALL_MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

# 标准图像扩展名（支持 piexif 库直接读写 EXIF）
# piexif 是纯 Python 的 EXIF 读写库，不需要外部依赖
PIE_SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.tiff', '.tif', '.png'}

# Date-time patterns for filename parsing
# WARNING: Patterns without 4-digit year (e.g. YYMMDD) are ambiguous with DDMMYY.
# Year-first patterns are placed first to take priority. If you encounter date
# misparses, consider reordering or constraining with separators.
DATETIME_PATTERNS = [
    # 年在前（优先级高，避免歧义）
    # 使用 (?<!\d) 和 (?!\d) 单词边界防止嵌入长数字串中误匹配
    r'(?<!\d)(\d{4})-(\d{2})-(\d{2})[_\s](\d{2})-(\d{2})-(\d{2})(?!\d)',
    r'(?<!\d)(\d{4})-(\d{2})-(\d{2})[_\s](\d{2}):(\d{2}):(\d{2})(?!\d)',
    r'(?<!\d)(\d{4})(\d{2})(\d{2})[_\s](\d{2})(\d{2})(\d{2})(?!\d)',
    r'(?<!\d)(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?!\d)',
    r'(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)',
    r'(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)',
    # 时间在前（无分隔符时无法与年优先区分，要求显式分隔符）
    r'(?<!\d)(\d{2})-(\d{2})-(\d{2})[_\s](\d{4})-(\d{2})-(\d{2})(?!\d)',
    r'(?<!\d)(\d{2})(\d{2})(\d{2})[_\s](\d{4})(\d{2})(\d{2})(?!\d)',
]

# 默认设置
DEFAULT_TIME_THRESHOLD_MINUTES = 30  # 时间匹配阈值（分钟），用于 GPS 轨迹点与文件拍摄时间的匹配
MAX_ITERATIONS = 10  # 位置信息处理的最大迭代次数
DEFAULT_DRY_RUN = True  # 默认启用试运行模式，只预览不实际修改
DEFAULT_RECURSIVE = True  # 默认递归扫描子目录
DEFAULT_SKIP_EXISTING = True  # 默认跳过已有 EXIF 日期的文件

# ExifTool 搜索路径（Windows 系统）
# ExifTool 是一个功能强大的元数据读写工具，用于处理 RAW/视频等文件
# 如果用户在自定义位置安装了 ExifTool，可在此处添加搜索路径
WINDOWS_EXIFTOOL_PATHS = [
    r"C:\Program Files\ExifTool",
    r"C:\Program Files (x86)\ExifTool",
    r"C:\Tools\ExifTool",
    r"C:\exiftool",
]

# ExifTool 搜索路径（macOS/Linux 系统）
UNIX_EXIFTOOL_PATHS = [
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/usr/bin",
    "/opt/exiftool",
    "/Applications/ExifTool",
]

# 地图服务 URL（用于在地图中显示拍摄位置）
# 支持高德地图、百度地图、腾讯地图和苹果地图
# {lat} 和 {lon} 在调用时会被实际的经纬度替换
AMAP_URL = "https://uri.amap.com/marker?position={lon},{lat}&name={name}&src=myapp&coordinate=gaode&callnative=0"
BMAP_URL = "https://api.map.baidu.com/marker?location={lat},{lon}&title={name}&content={name}&output=html"
TMAP_URL = "https://apis.map.qq.com/uri/v1/marker?marker=coord:{lat},{lon};title:{name};addr:{name}"
APPLE_MAPS_URL = "https://maps.apple.com/?q={lat},{lon}"
