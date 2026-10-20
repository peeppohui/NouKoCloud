# -*- mode: python ; coding: utf-8 -*-
"""Сборка svsCloud в один .exe (PyInstaller)."""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve()
if ROOT.name == "packaging":   # spec-файл лежит в packaging/, проект — уровнем выше
    ROOT = ROOT.parent

# SVSCLOUD_ONEFILE=1 — собираем одним файлом (build.ps1 -OneFile)
ONEFILE = os.environ.get("SVSCLOUD_ONEFILE", "0") == "1"

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
]
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
        name="svsCloud",
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
        name="svsCloud",
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
        name="svsCloud",
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


if not ONEFILE:
    try:
        _target = Path(DISTPATH) / coll.name
    except NameError:  # pragma: no cover
        _target = Path(SPECPATH).parent / "dist" / coll.name
    _trim_languages(_target)
