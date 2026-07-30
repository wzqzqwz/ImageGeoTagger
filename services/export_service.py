"""导出服务 - 将处理结果导出为多种格式"""

import os
import csv
import json
from datetime import datetime
from collections.abc import Mapping

from utils.gpx_utils import create_gpx_element, prettify_xml
from utils.i18n import _


def _get_val(item, key, default=None):
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def export_to_txt(filepath, a_list, b_list, gps_data, stats_text):
    with open(filepath, 'w', encoding='utf-8', newline='') as f:
        f.write(_("图像地理位置信息处理结果") + "\n")
        f.write("=" * 50 + "\n\n")
        f.write(stats_text)
        f.write("\n\n")

        f.write(_("有位置信息的文件:") + "\n")
        f.write("-" * 30 + "\n")
        for i, item in enumerate(a_list, 1):
            _write_file_info_txt(f, i, item)

        f.write(_("没有位置信息的文件:") + "\n")
        f.write("-" * 30 + "\n")
        for i, item in enumerate(b_list, 1):
            _write_file_info_txt(f, i, item)

        if gps_data:
            f.write(_("GPX轨迹数据:") + "\n")
            f.write("-" * 30 + "\n")
            for i, point in enumerate(gps_data, 1):
                f.write(f"{i}. " + _("轨迹点") + "\n")
                dt = _get_val(point, 'datetime')
                time_str = dt.strftime('%Y-%m-%d %H:%M:%S') if dt else _('未知时间')
                f.write(_("   时间: ") + time_str + "\n")
                lat, lon = _get_val(point, 'latitude'), _get_val(point, 'longitude')
                if lat is not None and lon is not None:
                    f.write(_("   位置: ") + f"({lat:.6f}, {lon:.6f})")
                    alt = _get_val(point, 'altitude')
                    if alt is not None:
                        f.write(f", " + _("高度: ") + f"{alt:.2f}m")
                    f.write("\n")
                f.write(_("   来源: ") + _get_val(point, 'source_file', _('未知')) + "\n\n")


def _write_file_info_txt(f, i, item):
    filename = _get_val(item, 'filename', '')
    dt = _get_val(item, 'dt')
    lat = _get_val(item, 'latitude')
    lon = _get_val(item, 'longitude')
    alt = _get_val(item, 'altitude')
    fsize = _get_val(item, 'file_size')
    path = _get_val(item, 'path', '')

    f.write(f"{i}. {filename}\n")
    time_str = dt.strftime('%Y-%m-%d %H:%M:%S') if dt and dt != datetime.min else _('未知时间')
    f.write(_("   时间: ") + time_str + "\n")
    if lat is not None and lon is not None:
        f.write(_("   位置: ") + f"({lat:.6f}, {lon:.6f})")
        if alt is not None:
            f.write(f", " + _("高度: ") + f"{alt:.2f}m")
        f.write("\n")
    if fsize is not None:
        f.write(_("   大小: ") + f"{fsize / (1024 * 1024):.2f} MB\n")
    else:
        f.write(_("   大小: 未知\n"))
    f.write(_("   路径: ") + path + "\n\n")


def export_to_csv(filepath, a_list, b_list):
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([_('文件名'), _('文件路径'), _('拍摄时间'), _('纬度'),
                        _('经度'), _('高度(米)'), _('文件大小(MB)'), _('是否有位置信息')])

        for item in a_list + b_list:
            dt = _get_val(item, 'dt')
            lat = _get_val(item, 'latitude')
            lon = _get_val(item, 'longitude')
            alt = _get_val(item, 'altitude')
            fsize = _get_val(item, 'file_size')
            filename = _get_val(item, 'filename', '')
            path = _get_val(item, 'path', '')

            time_str = dt.strftime('%Y-%m-%d %H:%M:%S') if dt and dt != datetime.min else ''
            lat_str = lat if lat is not None else ''
            lon_str = lon if lon is not None else ''
            alt_str = alt if alt is not None else ''
            size_str = f"{fsize / (1024 * 1024):.2f}" if fsize is not None else ''
            has_loc = _('是') if (lat is not None and lon is not None) else _('否')

            writer.writerow([filename, path, time_str,
                           lat_str, lon_str, alt_str, size_str, has_loc])


def export_to_json(filepath, a_list, b_list, gps_data):
    export_data = {
        'export_info': {
            'timestamp': datetime.now().isoformat(),
            'total_files': len(a_list) + len(b_list),
            'files_with_location': len(a_list),
            'files_without_location': len(b_list),
            'gps_track_points': len(gps_data) if gps_data else 0,
        },
        'files_with_location': [],
        'files_without_location': [],
        'gps_track_data': [],
    }

    for item in a_list:
        dt = _get_val(item, 'dt')
        export_data['files_with_location'].append({
            'filename': _get_val(item, 'filename', ''),
            'path': _get_val(item, 'path', ''),
            'datetime': dt.isoformat() if dt and dt != datetime.min else None,
            'latitude': _get_val(item, 'latitude'),
            'longitude': _get_val(item, 'longitude'),
            'altitude': _get_val(item, 'altitude'),
            'file_size': _get_val(item, 'file_size'),
        })

    for item in b_list:
        dt = _get_val(item, 'dt')
        export_data['files_without_location'].append({
            'filename': _get_val(item, 'filename', ''),
            'path': _get_val(item, 'path', ''),
            'datetime': dt.isoformat() if dt and dt != datetime.min else None,
            'latitude': _get_val(item, 'latitude'),
            'longitude': _get_val(item, 'longitude'),
            'altitude': _get_val(item, 'altitude'),
            'file_size': _get_val(item, 'file_size'),
        })

    if gps_data:
        for point in gps_data:
            if isinstance(point, dict):
                export_data['gps_track_data'].append({
                    'datetime': point['datetime'].isoformat() if point['datetime'] else None,
                    'latitude': point['latitude'],
                    'longitude': point['longitude'],
                    'altitude': point['altitude'],
                    'source_file': point.get('source_file'),
                })
            else:
                export_data['gps_track_data'].append(point.to_dict())

    with open(filepath, 'w', encoding='utf-8', newline='') as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)


def export_to_gpx(filepath, a_list, gps_data):
    points = []
    for item in a_list:
        points.append({
            'datetime': _get_val(item, 'dt'),
            'latitude': _get_val(item, 'latitude'),
            'longitude': _get_val(item, 'longitude'),
            'altitude': _get_val(item, 'altitude'),
            'filename': _get_val(item, 'filename', ''),
        })

    root = create_gpx_element(points, _("图像位置信息导出"))

    with open(filepath, 'w', encoding='utf-8', newline='') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(prettify_xml(root))


def generate_statistics(a_list, b_list, gps_data,
                        initial_a_count=None, initial_b_count=None,
                        updated_count=None):
    INDENT = "  "
    stats = _("===== 图像地理位置处理统计报告 =====") + "\n\n"
    total = len(a_list) + len(b_list)

    stats += _("文件处理统计:") + "\n"
    stats += INDENT + _("处理文件总数: ") + str(total) + "\n"
    if total > 0:
        stats += INDENT + _("有位置信息的文件: ") + str(len(a_list)) + f" ({len(a_list)/total*100:.1f}%)\n"
        stats += INDENT + _("没有位置信息的文件: ") + str(len(b_list)) + f" ({len(b_list)/total*100:.1f}%)\n"

    if initial_a_count is not None and updated_count is not None:
        new_loc = len(a_list) - initial_a_count
        stats += "\n" + _("处理前后对比:") + "\n"
        stats += INDENT + _("处理前有位置信息: ") + str(initial_a_count) + "\n"
        stats += INDENT + _("处理后有位置信息: ") + str(len(a_list)) + "\n"
        stats += INDENT + _("新增位置信息: ") + str(new_loc) + "\n"
        stats += INDENT + _("更新位置信息的文件数: ") + str(updated_count) + "\n"

    if gps_data:
        stats += "\n" + _("GPX轨迹数据统计:") + "\n"
        stats += INDENT + _("轨迹点总数: ") + str(len(gps_data)) + "\n"
        times = [_get_val(p, 'datetime') for p in gps_data if _get_val(p, 'datetime')]
        if times:
            min_t, max_t = min(times), max(times)
            stats += INDENT + _("时间跨度: ") + min_t.strftime('%Y-%m-%d %H:%M:%S') + _(" 到 ") + max_t.strftime('%Y-%m-%d %H:%M:%S') + "\n"
            duration = max_t - min_t
            stats += INDENT + _("时间跨度: ") + str(duration.days) + _(" 天 ") + str(duration.seconds // 3600) + _(" 小时 ") + str((duration.seconds % 3600) // 60) + _(" 分钟") + "\n"

    all_items = a_list + b_list

    stats += "\n" + _("文件类型统计:") + "\n"
    file_types = {}
    lats, lons, alts = [], [], []
    with_time = []
    total_size = 0
    for item in all_items:
        filename = _get_val(item, 'filename', '')
        ext = os.path.splitext(filename)[1].lower()
        file_types[ext] = file_types.get(ext, 0) + 1
        lat = _get_val(item, 'latitude')
        lon = _get_val(item, 'longitude')
        alt = _get_val(item, 'altitude')
        if lat is not None and lon is not None:
            lats.append(lat)
            lons.append(lon)
        if alt is not None:
            alts.append(alt)
        dt = _get_val(item, 'dt')
        if dt:
            with_time.append(dt)
        total_size += _get_val(item, 'file_size') or 0

    for ext, count in sorted(file_types.items()):
        stats += INDENT + (ext or _('无扩展名')) + _(": ") + str(count) + _(" 个文件\n")

    if lats and lons:
        stats += "\n" + _("位置信息分析:") + "\n"
        stats += INDENT + _("纬度范围: ") + f"{min(lats):.6f}" + _(" 到 ") + f"{max(lats):.6f}\n"
        stats += INDENT + _("经度范围: ") + f"{min(lons):.6f}" + _(" 到 ") + f"{max(lons):.6f}\n"
        if alts:
            stats += INDENT + _("高度范围: ") + f"{min(alts):.2f}" + _(" 到 ") + f"{max(alts):.2f}\n"
            stats += INDENT + _("平均高度: ") + f"{sum(alts) / len(alts):.2f}\n"

    stats += "\n" + _("时间信息统计:") + "\n"
    if all_items:
        stats += INDENT + _("有时间信息的文件: ") + str(len(with_time)) + f" ({len(with_time)/len(all_items)*100:.1f}%)\n"
    if with_time:
        stats += INDENT + _("最早时间: ") + min(with_time).strftime('%Y-%m-%d %H:%M:%S') + "\n"
        stats += INDENT + _("最晚时间: ") + max(with_time).strftime('%Y-%m-%d %H:%M:%S') + "\n"

    stats += "\n" + _("文件大小统计:") + "\n"
    stats += INDENT + _("总大小: ") + f"{total_size/(1024*1024*1024):.2f}" + _(" GB") + "\n"
    if all_items:
        stats += INDENT + _("平均文件大小: ") + f"{total_size/len(all_items)/(1024*1024):.2f}" + _(" MB") + "\n"

    stats += "\n" + _("处理建议:") + "\n"
    if len(b_list) > 0:
        stats += _("  • 还有 ") + str(len(b_list)) + _(" 个文件没有位置信息") + "\n"
        if not gps_data:
            stats += _("  • 建议添加GPX轨迹文件来为这些文件分配位置信息") + "\n"
        else:
            stats += _("  • 可能需要调整时间阈值或检查文件时间戳是否正确") + "\n"
    stats += _("  • 使用\"显示结果\"标签页可以查看和编辑具体文件的位置信息") + "\n"
    stats += _("  • 使用\"导出结果\"功能可以将处理结果保存为文件") + "\n"

    stats += _("\n===== 报告生成时间: {} =====\n").format(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    return stats
