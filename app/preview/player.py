"""Виджет плеера превью на базе QtWebEngine.

Chromium внутри умеет играть mp4/webm/m4a с перемоткой (Range),
поэтому монтажёр смотрит исходник, не скачивая его целиком.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QLabel, QStackedWidget, QVBoxLayout, QWidget

from ..core.paths import app_data_dir

log = logging.getLogger(__name__)

try:
    from PySide6.QtWebEngineCore import QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
except Exception as exc:  # pragma: no cover
    log.warning("QtWebEngine недоступен: %s", exc)
    WEBENGINE_AVAILABLE = False


class PreviewPlayer(QWidget):
    """Показывает страницу плеера с локального сервера превью."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)

        self._placeholder = QLabel("Выберите файл, чтобы посмотреть превью", self)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._stack.addWidget(self._placeholder)

        self._view = None
        if WEBENGINE_AVAILABLE:
            self._view = QWebEngineView(self)
            profile = QWebEngineProfile("svscloud", self)
            profile.setPersistentStoragePath(str(app_data_dir() / "webprofile"))
            profile.setCachePath(str(app_data_dir() / "webcache"))
            self._view.setPage(self._view.page())
            self._stack.addWidget(self._view)

    # ------------------------------------------------------------------
    def show_url(self, url: str) -> None:
        if self._view is None:
            self._placeholder.setText(
                "Для превью нужен компонент QtWebEngine.\n"
                "Пересоберите программу или посмотрите файл после скачивания."
            )
            self._stack.setCurrentWidget(self._placeholder)
            return
        self._view.setUrl(QUrl(url))
        self._stack.setCurrentWidget(self._view)

    def show_message(self, text: str) -> None:
        self._placeholder.setText(text)
        self._stack.setCurrentWidget(self._placeholder)

    def clear(self) -> None:
        if self._view is not None:
            self._view.setUrl(QUrl("about:blank"))
        self.show_message("Выберите файл, чтобы посмотреть превью")
