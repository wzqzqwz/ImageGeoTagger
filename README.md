# ImageGeoTagger - 鍥惧儚鍦扮悊浣嶇疆淇℃伅澶勭悊宸ュ叿

涓€娆捐法骞冲彴鐨勬闈㈠伐鍏凤紝鐢ㄤ簬鎵归噺澶勭悊濯掍綋鏂囦欢鐨勬媿鎽勬棩鏈熷拰 GPS 鍦扮悊浣嶇疆淇℃伅銆?
## 鍔熻兘

- **鏃ユ湡澶勭悊** 鈥?鎵归噺淇敼/娓呴櫎鐓х墖銆佽棰戙€侀煶棰戞枃浠剁殑鎷嶆憚鏃ユ湡锛圗XIF/QuickTime/XMP锛夛紱鏀寔浠庢枃浠跺悕瑙ｆ瀽鏃ユ湡骞惰嚜鍔ㄩ噸鍛藉悕
- **GPS 澶勭悊** 鈥?涓烘棤 GPS 淇℃伅鐨勬枃浠舵壒閲忔坊鍔犱綅缃俊鎭紱鏀寔 GPX 杞ㄨ抗鍖归厤銆佹墜鍔ㄨ緭鍏ュ潗鏍囥€佸湴鍥鹃€夋嫨
- **鏂囦欢鎵弿** 鈥?閫掑綊鎵弿鏂囦欢澶癸紝鑷姩璇嗗埆鏀寔鐨勫獟浣撴牸寮?- **鎷栨斁鏀寔** 鈥?鐩存帴灏嗘枃浠跺す鎷栧叆璺緞杈撳叆妗?- **瀵煎嚭** 鈥?瀵煎嚭澶勭悊缁撴灉涓?CSV/TXT/GPX/KML 鏍煎紡

## 绯荤粺瑕佹眰

- **Windows** 7+ / **macOS** 10.12+ / **Linux** (X11)
- **Python** 3.9+
- **ExifTool**锛圼涓嬭浇](https://exiftool.org/)锛夆€?鍙€変絾鎺ㄨ崘锛岀敤浜?RAW/瑙嗛/闊抽鏂囦欢澶勭悊

## 蹇€熷紑濮?
```bash
# 鍏嬮殕
git clone https://github.com/yourusername/ImageGeoTagger.git
cd ImageGeoTagger

# 瀹夎渚濊禆
pip install -r requirements.txt

# 杩愯
python -m geo_media_tool
```

## 渚濊禆

| 鍖?| 鐢ㄩ€?|
|------|---------|
| Pillow | 鍥惧儚 EXIF 璇诲彇 |
| piexif | EXIF 鍐欏叆锛圝PEG/TIFF锛?|
| exifread | 鍘熷 EXIF 瑙ｆ瀽 |
| tkinterdnd2 | 鎷栨斁鏀寔 |
| numpy | 鍦扮悊璁＄畻 |
| rawpy | RAW 鏍煎紡鏀寔 |

## 椤圭洰缁撴瀯

```
geo_media_tool/
鈹溾攢鈹€ main.py               # 鍏ュ彛
鈹溾攢鈹€ config.py              # 閰嶇疆甯搁噺
鈹溾攢鈹€ models/                # 鏁版嵁妯″瀷
鈹溾攢鈹€ services/              # 鏍稿績涓氬姟閫昏緫
鈹?  鈹溾攢鈹€ date_processor.py
鈹?  鈹溾攢鈹€ geo_processor.py
鈹?  鈹溾攢鈹€ media_scanner.py
鈹?  鈹斺攢鈹€ export_service.py
鈹溾攢鈹€ ui/                    # 鍥惧舰鐣岄潰
鈹?  鈹溾攢鈹€ main_window.py
鈹?  鈹溾攢鈹€ date_tab.py
鈹?  鈹溾攢鈹€ geo_tab.py
鈹?  鈹溾攢鈹€ dialogs.py
鈹?  鈹斺攢鈹€ custom_msgbox.py
鈹斺攢鈹€ utils/                 # 宸ュ叿鍑芥暟
    鈹溾攢鈹€ exif_utils.py
    鈹溾攢鈹€ platform_utils.py
    鈹斺攢鈹€ recycle_bin.py
```

## 鎵撳寘涓虹嫭绔?exe

```bash
pip install pyinstaller
pyinstaller ImageGeoTagger.spec
```

## 璁稿彲璇?
MIT License
