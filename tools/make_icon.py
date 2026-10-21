"""Собирает packaging/app.ico — значок программы (знак-«мишень» из логотипа «НоуКо»).

    python tools/make_icon.py

Рисует тот же значок, что и окно программы (app.ui.theme.draw_app_icon),
в нескольких размерах и складывает их в один .ico (PNG внутри — так
Windows показывает значок чётко на любом масштабе экрана).
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
    from app.ui.theme import draw_app_icon

    images = []
    for size in SIZES:
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        draw_app_icon(size).save(buffer, "PNG")
        images.append((size, bytes(data)))

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    blobs = b""
    for size, png in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    target = ROOT / "packaging" / "app.ico"
    target.write_bytes(header + entries + blobs)
    print(f"Значок: {target} ({len(images)} размеров)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
