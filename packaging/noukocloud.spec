# -*- mode: python ; coding: utf-8 -*-
"""Сборка НоуКо Cloud в один .exe (PyInstaller)."""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve()
if ROOT.name == "packaging":   # spec-файл лежит в packaging/, проект — уровнем выше
    ROOT = ROOT.parent

# NOUKOCLOUD_ONEFILE=1 — собираем одним файлом (build.ps1 -OneFile)
ONEFILE = os.environ.get("NOUKOCLOUD_ONEFILE", "0") == "1"

block_cipher = None

# Модули Qt, которые программе не нужны — вырезаем, чтобы уменьшить размер.
EXCLUDES = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    # Превью играет QtMultimedia (FFmpeg внутри), а не браузер: Chromium из
    # QtWebEngine собран без H.264/AAC и весит ~150 МБ — исключаем его целиком.
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSerialPort", "PySide6.QtSensors", "PySide6.QtWebSockets", "PySide6.QtWebChannel",
    "tkinter", "unittest", "pydoc_data", "test",
    "numpy", "PIL", "IPython", "notebook",
    "torch", "torchvision", "transformers", "diffusers", "accelerate",
]

datas = []
# Данные PySide6 (переводы, плагины, FFmpeg) собирает сам hook PyInstaller.
# collect_data_files("PySide6") тащил бы ВСЁ — из-за этого сборка весила 650 МБ.
datas += [
    (str(ROOT / "docs"), "docs"),
    # Иконки флажков дерева (галочка/чёрточка) — их читает таблица стилей.
    (str(ROOT / "app" / "ui" / "icons"), "app/ui/icons"),
    # Оформление «НоуКо»: шрифты, логотипы, сетка звёзд.
    (str(ROOT / "app" / "ui" / "assets"), "app/ui/assets"),
]

hiddenimports = [
    "httpx",
    "httpx._models",
    "httpcore",
    "h11",
    "win32crypt",
    # Плеер превью: QtMultimedia тянет плагин ffmpegmediaplugin и avcodec/avformat.
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetwork",
    # Иконки интерфейса рисуются из SVG.
    "PySide6.QtSvg",
]
# Вкладка «Соцсети»: yt-dlp грузит обработчики сайтов лениво — забираем все.
try:
    hiddenimports += collect_submodules("yt_dlp")
except Exception:  # pragma: no cover - yt-dlp не установлен
    print("  ВНИМАНИЕ: yt-dlp не установлен — вкладка «Соцсети» не будет работать")
hiddenimports += collect_submodules("app")

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if ONEFILE:
    # Один файл: бинарники и данные уезжают внутрь exe.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="NoukoCloud",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ROOT / "packaging" / "app.ico") if (ROOT / "packaging" / "app.ico").exists() else None,
    )
else:
    # Папка: exe собирается отдельно, бинарники кладёт COLLECT.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="NoukoCloud",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ROOT / "packaging" / "app.ico") if (ROOT / "packaging" / "app.ico").exists() else None,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="NoukoCloud",
    )


def _trim_languages(target: Path) -> None:
    """Оставляем только русский и английский: -50 МБ к сборке."""
    import re

    removed = 0
    freed = 0
    for item in target.rglob("*"):
        # Только переводы Qt (.qm) в папках translations. Раньше сюда попадали
        # и все .pak — включая qtwebengine_resources.pak, без которого
        # встроенный браузер (прежний плеер превью) вообще не работал.
        if not item.is_file() or item.suffix.lower() != ".qm" or item.parent.name != "translations":
            continue
        tokens = re.split(r"[._-]", item.stem)
        if any(token.lower() in ("ru", "en") for token in tokens):
            continue
        try:
            freed += item.stat().st_size
            item.unlink()
            removed += 1
        except OSError:
            pass
    print(f"  удалено языковых файлов: {removed} ({freed // (1024 * 1024)} МБ)")

    # Пустые каталоги переводов убираем, чтобы не путать при развёртывании.
    for folder in target.rglob("translations"):
        if folder.is_dir() and not any(folder.iterdir()):
            try:
                folder.rmdir()
            except OSError:
                pass


def _bundle_ffmpeg(target: Path) -> None:
    """Кладёт ffmpeg и ffprobe (bin/) рядом с программой.

    Пользователь скачивает один архив — ffmpeg уже внутри, ставить ничего
    не нужно. Если в проекте их ещё нет, скачиваем (tools/fetch_ffmpeg.py).
    NOUKOCLOUD_SKIP_FFMPEG=1 — собрать без них (быстрая проверочная сборка).

    ffprobe нужен не меньше ffmpeg: yt-dlp через него узнаёт кодек звука
    и длительность. Без него склейка видео со звуком (а у YouTube они
    лежат отдельно) падает с «unable to obtain file audio codec with ffprobe».
    """
    import shutil
    import subprocess

    if os.environ.get("NOUKOCLOUD_SKIP_FFMPEG") == "1":
        print("  ffmpeg: пропущен (NOUKOCLOUD_SKIP_FFMPEG=1)")
        return
    source = ROOT / "bin"
    if not (source / "ffmpeg.exe").exists():
        print("  ffmpeg: скачиваем…")
        subprocess.run([sys.executable, str(ROOT / "tools" / "fetch_ffmpeg.py")], check=False)
    if not (source / "ffmpeg.exe").exists():
        print("  ВНИМАНИЕ: ffmpeg не скачался — сборка без него")
        return
    dest = target / "bin"
    dest.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.is_file():
            shutil.copy2(item, dest / item.name)
    print(f"  ffmpeg: {dest}")


if not ONEFILE:
    try:
        _target = Path(DISTPATH) / coll.name
    except NameError:  # pragma: no cover
        _target = Path(SPECPATH).parent / "dist" / coll.name
    _trim_languages(_target)
    _bundle_ffmpeg(_target)
