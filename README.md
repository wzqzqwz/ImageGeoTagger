# ImageGeoTagger - 图像地理信息处理工具

一款跨平台的桌面工具，用于批量处理媒体文件的拍摄日期和 GPS 地理信息。

## 功能

- **日期处理** - 批量修改/清除照片、视频、音频文件的拍摄日期（EXIF/QuickTime/XMP）；支持从文件名解析日期并自动重命名
- **GPS 处理** - 为无 GPS 信息的文件批量添加位置信息；支持 GPX 轨迹匹配、手动输入坐标、地图选择
- **文件扫描** - 递归扫描文件夹，自动识别支持的媒体格式
- **拖放支持** - 直接将文件夹拖入路径输入框
- **导出** - 导出处理结果为 CSV/TXT/GPX/JSON 格式

## 系统要求

- Windows 7+ / macOS 10.12+ / Linux (X11 桌面环境)
- Python 3.9+
- ExifTool（可选但推荐，用于 RAW/视频/音频文件处理）

## 快速开始

```bash
# 克隆
git clone https://github.com/wzqzqwz/ImageGeoTagger.git
cd ImageGeoTagger

# 安装依赖
pip install -r requirements.txt

# 运行
python -m main
```

## 各平台环境准备

### Windows

- 自带 `exiftool/exiftool.exe`（项目内已内置，无需额外安装）
- Python 从 [python.org](https://www.python.org) 安装（勾选 tcl/tk 组件）

### Linux (Debian/Ubuntu 示例)

```bash
sudo apt install python3 python3-tk
pip install -r requirements.txt
# 可选依赖：
#   ExifTool（RAW/视频/音频处理）：sudo apt install libimage-exiftool-perl
#   xdg-utils（打开文件/文件夹）：sudo apt install xdg-utils
#   gio 或 trash-cli（移至回收站）：sudo apt install trash-cli
```

### macOS

```bash
brew install python-tk
pip install -r requirements.txt
# 可选：brew install exiftool
```

## 项目结构

```
__init__.py               # 包定义
__main__.py               # 入口
main.py                   # 主启动
config.py                 # 配置常量
models/                   # 数据模型
services/                 # 核心业务逻辑
  date_processor.py
  geo_processor.py
  media_scanner.py
  export_service.py
ui/                       # 图形界面
  main_window.py
  date_tab.py
  geo_tab.py
  dialogs.py
  custom_msgbox.py
  results_window.py
utils/                    # 工具函数
  exif_utils.py
  platform_utils.py
  i18n.py
  media_utils.py
  gpx_utils.py
  recycle_bin.py
locales/                  # 多语言文件
icons/                    # 图标
ImageGeoTagger.spec       # PyInstaller 打包配置
```

## 打包为独立可执行文件

```bash
pip install pyinstaller
pyinstaller ImageGeoTagger.spec
```

- 必须在**目标操作系统**上打包（PyInstaller 不支持跨平台交叉编译）
- 生成产物：`dist/ImageGeoTagger`（macOS/Linux）或 `dist/ImageGeoTagger.exe`（Windows）
- 打包前请将 `exiftool/` 目录替换为当前平台对应的 ExifTool：
  - Windows：`exiftool.exe`
  - macOS/Linux：`exiftool` 脚本（macOS 可用 `brew install exiftool` 后复制，Linux 同理）
- spec 已按平台自动选择图标（Windows `.ico` / macOS `.icns` / Linux `.png`）与 UPX 压缩

## 多语言支持

内置国际化支持，当前支持以下语言：

| 语言 | 代码 |
|------|------|
| 中文 (简体) | `zh` |
| English | `en` |
| Español | `es` |
| Français | `fr` |
| Русский | `ru` |
| العربية | `ar` |

可在设置菜单中切换语言。

## 许可证

MIT License
