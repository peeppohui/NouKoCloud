"""Запуск вместе с Windows (в фоне, значком у часов).

Запись в HKCU\\...\\Run — только для текущего пользователя, без прав
администратора. Работает только у собранной программы (.exe): при запуске
из исходников запускать нечего.
"""

from __future__ import annotations

import logging
import sys

from .paths import APP_NAME, is_frozen

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: Ключ командной строки: запуститься сразу в фоне, без окна.
BACKGROUND_ARG = "--background"


def supported() -> bool:
    return sys.platform == "win32" and is_frozen()


def command() -> str:
    return f'"{sys.executable}" {BACKGROUND_ARG}'


def enabled() -> bool:
    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
    except OSError:
        return False
    # Программу перенесли в другую папку — старая запись уже не наша.
    return str(value).strip().lower() == command().lower()


def set_enabled(on: bool) -> bool:
    """Включает/выключает автозапуск. Возвращает, удалось ли."""
    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        log.warning("Не удалось изменить автозапуск", exc_info=True)
        return False
    log.info("Автозапуск с Windows: %s", "включён" if on else "выключен")
    return True
