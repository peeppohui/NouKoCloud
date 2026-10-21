"""Пути приложения: где хранятся настройки, токены, логи."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "NoukoCloud"
APP_TITLE = "НоуКо Cloud"

#: Каталоги с данными прежних версий (svsCloud, ещё раньше YaGoDownloader) —
#: из них переносим настройки и токены, чтобы после переименования
#: не пришлось вводить ключи и входить заново. Первый найденный — главный.
LEGACY_APP_NAMES = ("svsCloud", "YaGoDownloader")


def is_frozen() -> bool:
    """True, если запущено из собранного .exe (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def _base_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base)


def _migrate_legacy(base: Path) -> None:
    """Копирует настройки и токены из каталога прежней версии.

    Старый каталог не трогаем: можно вернуться на старую версию.
    """
    current = base / APP_NAME
    for name in ("config.json", "tokens.dat"):
        target = current / name
        if target.exists():
            continue
        for legacy_name in LEGACY_APP_NAMES:
            source = base / legacy_name / name
            if source.exists() and source != target:
                try:
                    shutil.copy2(source, target)
                except OSError:
                    continue
                break


def app_data_dir() -> Path:
    """Каталог с настройками/токенами/логами."""
    base = _base_data_dir()
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    _migrate_legacy(base)
    return path


def resource_dir() -> Path:
    """Каталог с ресурсами (в .exe — временная распаковка PyInstaller)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def logs_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def hide_path(path: Path) -> None:
    """Делает папку скрытой в проводнике Windows (служебные папки программы)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        FILE_ATTRIBUTE_HIDDEN = 0x2
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs != -1 and not attrs & FILE_ATTRIBUTE_HIDDEN:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs | FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass


def default_download_dir() -> Path:
    downloads = Path.home() / "Downloads" / "NoukoCloud"
    downloads.mkdir(parents=True, exist_ok=True)
    return downloads


def bundled_ffmpeg() -> Path | None:
    """ffmpeg, положенный рядом с .exe (bin/ffmpeg.exe)."""
    base = Path(sys.executable).parent if is_frozen() else Path(__file__).resolve().parent.parent.parent
    for candidate in (base / "bin" / "ffmpeg.exe", base / "ffmpeg.exe"):
        if candidate.exists():
            return candidate
    return None


def portable_config_path() -> Path | None:
    """Конфиг рядом с программой — «портативные» настройки сборки.

    Нужен, чтобы релизная сборка могла сразу содержать ключи OAuth:
    пользователю не нужно ничего вводить, достаточно нажать «Войти».
    Свои настройки в %APPDATA% всегда имеют приоритет.
    """
    base = Path(sys.executable).parent if is_frozen() else Path(__file__).resolve().parent.parent.parent
    candidate = base / "config.json"
    return candidate if candidate.exists() else None
