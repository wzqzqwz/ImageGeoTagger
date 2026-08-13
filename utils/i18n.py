"""多语言支持引擎"""

import json
import locale
import os
import sys

_TRANSLATIONS = {}
_CURRENT_LANG = 'zh'

_LANG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'locales')

_SUPPORTED = {
    'zh': '中文',
    'en': 'English',
    'fr': 'Français',
    'ru': 'Русский',
    'es': 'Español',
    'ar': 'العربية',
}

def _detect_system_lang():
    """检测系统语言，返回语言代码

    优先使用 locale 检测；Windows 上 locale 返回值是
    'Chinese (Simplified)_China' 这类格式，无法直接匹配语言码，
    改用 GetUserDefaultUILanguage 获取真实的系统 UI 语言。
    """
    try:
        code = locale.getlocale()[0]
        if code:
            code = code.split('_')[0].lower()
            if code in _SUPPORTED:
                return code
    except Exception:
        pass
    if sys.platform == 'win32':
        try:
            import ctypes
            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            name = locale.windows_locale.get(lcid)
            if name:
                code = name.split('_')[0].lower()
                if code in _SUPPORTED:
                    return code
        except Exception:
            pass
    elif sys.platform == 'darwin':
        # macOS：GUI 应用通常无 LANG 环境变量、locale 未初始化，
        # 用 defaults read -g AppleLanguages 读取系统首选语言
        # （输出形如 ( "zh-Hans-CN", "en-CN" )，取首个的 BCP-47 前缀）。
        try:
            import subprocess
            import re
            out = subprocess.run(
                ['defaults', 'read', '-g', 'AppleLanguages'],
                capture_output=True, text=True, timeout=3, errors='replace')
            if out.returncode == 0:
                m = re.search(r'"([^"]+)"', out.stdout)
                if m:
                    lang = m.group(1).split('-')[0].lower()
                    if lang in _SUPPORTED:
                        return lang
        except Exception:
            pass
    # Linux/macOS 兜底：LANG 环境变量（如 zh_CN.UTF-8 → zh）
    try:
        env = os.environ.get('LANG', '')
        code = env.split('.')[0].split('_')[0].lower()
        if code in _SUPPORTED:
            return code
    except Exception:
        pass
    return 'en'

def load_lang(lang_code=None):
    """加载指定语言包，None 则自动检测"""
    global _TRANSLATIONS, _CURRENT_LANG
    if lang_code is None:
        lang_code = _detect_system_lang()
    if lang_code not in _SUPPORTED:
        lang_code = 'zh'
    _CURRENT_LANG = lang_code
    filepath = os.path.join(_LANG_DIR, f'{lang_code}.json')
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            _TRANSLATIONS = json.load(f)
    except Exception:
        import sys
        print(f"Warning: Failed to load language file: {filepath}", file=sys.stderr)
        # 加载失败时保留上一份可用翻译（若存在），避免界面退化为显示原文键
        if not _TRANSLATIONS:
            _TRANSLATIONS = {}

def set_language(lang_code):
    """手动切换语言"""
    load_lang(lang_code)

def get_language():
    """返回当前语言代码"""
    return _CURRENT_LANG

def get_language_name():
    """返回当前语言显示名"""
    return _SUPPORTED.get(_CURRENT_LANG, _CURRENT_LANG)

def get_supported_languages():
    """返回 {code: name} 字典"""
    return dict(_SUPPORTED)

def _(text):
    """翻译函数，找不到翻译则返回原文"""
    return _TRANSLATIONS.get(text, text)
