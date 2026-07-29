# ImageGeoTagger - 图像地理位置信息处理工具

一款跨平台的桌面工具，用于批量处理媒体文件的拍摄日期和 GPS 地理位置信息。

## 功能

- **日期处理** — 批量修改/清除照片、视频、音频文件的拍摄日期（EXIF/QuickTime/XMP）；支持从文件名解析日期并自动重命名
- **GPS 处理** — 为无 GPS 信息的文件批量添加位置信息；支持 GPX 轨迹匹配、手动输入坐标、地图选择
- **文件扫描** — 递归扫描文件夹，自动识别支持的媒体格式
- **拖放支持** — 直接将文件夹拖入路径输入框
- **导出** — 导出处理结果为 CSV/TXT/GPX/KML 格式

## 系统要求

- **Windows** 7+ / **macOS** 10.12+ / **Linux** (X11)
- **Python** 3.9+
- **ExifTool**（[下载](https://exiftool.org/)）— 可选但推荐，用于 RAW/视频/音频文件处理

## 快速开始

```bash
# 克隆
git clone https://github.com/yourusername/ImageGeoTagger.git
cd ImageGeoTagger

# 安装依赖
pip install -r requirements.txt

# 运行
python -m geo_media_tool
```

## 依赖

| 包 | 用途 |
|------|---------|
| Pillow | 图像 EXIF 读取 |
| piexif | EXIF 写入（JPEG/TIFF） |
| exifread | 原始 EXIF 解析 |
| tkinterdnd2 | 拖放支持 |
| numpy | 地理计算 |
| rawpy | RAW 格式支持 |

## 项目结构

```
geo_media_tool/
├── main.py               # 入口
├── config.py              # 配置常量
├── models/                # 数据模型
├── services/              # 核心业务逻辑
│   ├── date_processor.py
│   ├── geo_processor.py
│   ├── media_scanner.py
│   └── export_service.py
├── ui/                    # 图形界面
│   ├── main_window.py
│   ├── date_tab.py
│   ├── geo_tab.py
│   ├── dialogs.py
│   └── custom_msgbox.py
└── utils/                 # 工具函数
    ├── exif_utils.py
    ├── platform_utils.py
    └── recycle_bin.py
```

## 打包为独立 exe

```bash
pip install pyinstaller
pyinstaller ImageGeoTagger.spec
```

## 许可证

MIT License
