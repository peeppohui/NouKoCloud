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


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


class PreviewPanel(QWidget):
    download_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self._title = QLabel("", self)
        self._title.setProperty("heading", True)
        self._title.setWordWrap(True)
        layout.addWidget(self._title)

        self._info = QLabel("", self)
        self._info.setProperty("muted", True)
        self._info.setWordWrap(True)
        layout.addWidget(self._info)

        self.player = PreviewPlayer(self)
        layout.addWidget(self.player, 1)

        actions = QHBoxLayout()
        self._btn_download = QPushButton("Скачать только этот файл", self)
        self._btn_download.setToolTip(
            "Скачает один файл из предпросмотра. "
            "Чтобы забрать много файлов — отметьте их в дереве слева "
            "и нажмите «Скачать выбранное»."
        )
        self._btn_download.setProperty("accent", True)
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
        details = [f"Размер: {human_size(file.size)}"]
        if file.mime:
            details.append(f"Формат: {file.mime}")
        if file.modified:
            details.append(f"Изменён: {file.modified.strftime('%d.%m.%Y %H:%M')}")
        self._info.setText("  ·  ".join(details))
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
