"""多语言支持引擎"""

import json
import locale
import os

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
    """检测系统语言，返回语言代码"""
    try:
        code = locale.getlocale()[0]
        if code:
            code = code.split('_')[0]
            if code in _SUPPORTED:
                return code
    except Exception:
        pass
    return 'zh'

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
