"""Работа в фоне: значок у часов (системный трей).

Закрытие окна не завершает программу: загрузки (облака и соцсети) идут
дальше, а значок показывает, что качается. Полностью выйти — «Выход» в
меню значка.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..core.paths import APP_TITLE
from . import theme


class Tray(QObject):
    open_requested = Signal()
    downloads_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._icon = QSystemTrayIcon(theme.app_icon(), self)
        self._icon.setToolTip(APP_TITLE)
        menu = QMenu()
        self._menu = menu                # держим ссылку: у QSystemTrayIcon нет владельца меню
        self._add(menu, f"Открыть {APP_TITLE}", self.open_requested.emit)
        self._add(menu, "Папка загрузок", self.downloads_requested.emit)
        menu.addSeparator()
        self._add(menu, "Выход", self.quit_requested.emit)
        self._icon.setContextMenu(menu)
        self._icon.activated.connect(self._on_activated)
        self._icon.messageClicked.connect(self.open_requested.emit)

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    @staticmethod
    def _add(menu: QMenu, text: str, slot: Callable[[], None]) -> None:
        action = menu.addAction(text)
        action.triggered.connect(lambda _checked=False: slot())

    def show(self) -> None:
        self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    def set_activity(self, text: str) -> None:
        self._icon.setToolTip(f"{APP_TITLE} — {text}" if text else APP_TITLE)

    def notify(self, title: str, text: str, error: bool = False) -> None:
        kind = QSystemTrayIcon.MessageIcon.Warning if error else QSystemTrayIcon.MessageIcon.Information
        self._icon.showMessage(title, text, kind, 5000)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_requested.emit()
