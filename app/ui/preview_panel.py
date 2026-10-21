"""Панель превью: плеер, сведения о файле, кнопка «Скачать»."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.models import CloudFile
from ..preview.player import PreviewPlayer
from .widgets import button


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


def file_meta(file: CloudFile) -> str:
    """«MP4 · 4.1 ГБ · изменён 14.10.2026 12:31»."""
    parts = []
    ext = file.name.rsplit(".", 1)[-1].upper() if "." in file.name else ""
    if ext and len(ext) <= 5:
        parts.append(ext)
    elif file.mime:
        parts.append(file.mime)
    parts.append(human_size(file.size))
    if file.modified:
        parts.append(f"изменён {file.modified.strftime('%d.%m.%Y %H:%M')}")
    return " · ".join(parts)


class PreviewPanel(QWidget):
    download_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        self.player = PreviewPlayer(self)
        layout.addWidget(self.player, 1)

        info = QVBoxLayout()
        info.setSpacing(4)
        self._title = QLabel("", self)
        self._title.setProperty("role", "name")
        self._title.setWordWrap(True)
        info.addWidget(self._title)
        self._info = QLabel("", self)
        self._info.setProperty("muted", True)
        self._info.setWordWrap(True)
        info.addWidget(self._info)
        layout.addLayout(info)

        self._hint = QLabel(
            "Пробел — пауза, ← / → — перемотка на 5 секунд. Файл целиком не скачивается.", self)
        self._hint.setProperty("role", "hint")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        actions = QHBoxLayout()
        self._btn_download = button("Скачать только этот файл", "outline", self, icon_name="download")
        self._btn_download.setToolTip(
            "Скачает один файл из предпросмотра. "
            "Чтобы забрать много файлов — отметьте их в дереве слева "
            "и нажмите «Скачать выбранное»."
        )
        self._btn_download.clicked.connect(self._emit_download)
        self._btn_download.setEnabled(False)
        actions.addWidget(self._btn_download)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._file: CloudFile | None = None

    # ------------------------------------------------------------------
    @property
    def current_file(self) -> CloudFile | None:
        return self._file

    def show_file(self, file: CloudFile, url: str, kind: str, autoplay: bool = True,
                  message: str = "", buffer_probe=None) -> None:
        """Показывает файл: kind — video | audio | image | unknown.

        message — если превью показать нельзя, текст вместо плеера.
        buffer_probe — состояние кэша потока (для буферизации, см. PreviewPlayer).
        """
        self._file = file
        self._title.setText(file.name)
        self._info.setText(file_meta(file))
        self._btn_download.setEnabled(True)

        if message:
            self.player.show_message(message)
        elif kind in ("video", "audio"):
            self.player.play(url, kind, file.name, autoplay=autoplay, buffer_probe=buffer_probe)
        elif kind == "image":
            self.player.show_image(url)
        else:
            self.player.show_message(
                "Превью этого формата недоступно. Скачайте файл, чтобы посмотреть его."
            )

    def show_message(self, text: str) -> None:
        self._file = None
        self._title.setText("")
        self._info.setText("")
        self.player.show_message(text)
        self._btn_download.setEnabled(False)

    def stop(self) -> None:
        self.player.stop()

    def _emit_download(self) -> None:
        if self._file is not None:
            self.download_requested.emit(self._file)
