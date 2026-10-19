"""Диалог входа в облачные аккаунты."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.errors import friendly_error
from ..core.models import ProviderId

log = logging.getLogger(__name__)


class AuthDialog(QDialog):
    """Два понятных действия: «Войти через Яндекс» и «Войти через Google»."""

    login_succeeded = Signal(str, str)   # provider id, имя аккаунта
    login_failed = Signal(str, str)      # provider id, сообщение

    def __init__(self, oauth, runtime, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._oauth = oauth
        self._runtime = runtime
        self.setWindowTitle("Вход в облачные аккаунты")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        intro = QLabel(
            "Войдите в свой Яндекс.Диск и Google Диск, чтобы видеть «Мой диск».\n"
            "Публичные ссылки работают и без входа — их можно вставить сразу.",
            self,
        )
        intro.setProperty("muted", True)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._yandex_status = self._make_account_block(
            layout, "Яндекс.Диск", ProviderId.YANDEX,
            "После входа браузер покажет код — вставьте его в поле ниже.",
        )
        self._google_status = self._make_account_block(
            layout, "Google Диск", ProviderId.GOOGLE,
            "Вход откроется в браузере и вернётся обратно в программу.",
        )

        self._yandex_code = QLineEdit(self)
        self._yandex_code.setPlaceholderText("Вставьте код (7 цифр) и нажмите Enter")
        self._yandex_code.setVisible(False)
        layout.addWidget(self._yandex_code)

        self._yandex_link = QLabel("", self)
        self._yandex_link.setOpenExternalLinks(True)
        self._yandex_link.setTextFormat(Qt.TextFormat.RichText)
        self._yandex_link.setVisible(False)
        layout.addWidget(self._yandex_link)

        buttons = QHBoxLayout()
        self._yandex_btn = QPushButton("Войти через Яндекс.Диск", self)
        self._yandex_btn.clicked.connect(self._login_yandex)
        self._google_btn = QPushButton("Войти через Google Диск", self)
        self._google_btn.clicked.connect(self._login_google)
        buttons.addWidget(self._yandex_btn)
        buttons.addWidget(self._google_btn)
        buttons.addStretch(1)
        close_btn = QPushButton("Закрыть", self)
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self.login_succeeded.connect(lambda *_: self._refresh())
        self._refresh()

    # ------------------------------------------------------------------
    def _make_account_block(self, layout, title: str, pid: ProviderId, hint: str) -> QLabel:
        box = QLabel(self)
        box.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(box)
        box.setProperty("hint", hint)
        box.setProperty("pid", pid.value)
        return box

    def _refresh(self) -> None:
        for block, pid in ((self._yandex_status, ProviderId.YANDEX), (self._google_status, ProviderId.GOOGLE)):
            name = self._oauth.account_name(pid)
            if self._oauth.is_logged_in(pid):
                block.setText(f"<b>{self._oauth.provider(pid).display_name}</b>: <span style='color:#1e8e5a'>вы вошли как {name or 'аккаунт'}</span>")
            elif not self._oauth.configured(pid):
                block.setText(
                    f"<b>{self._oauth.provider(pid).display_name}</b>: "
                    "<span style='color:#c0392b'>не задан ключ приложения — см. «Настройки»</span>"
                )
            else:
                block.setText(f"<b>{self._oauth.provider(pid).display_name}</b>: <span style='color:#6b7480'>не выполнен вход</span>")

    # ------------------------------------------------------------------
    def _login_yandex(self) -> None:
        if not self._oauth.configured(ProviderId.YANDEX):
            self._status.setText("Сначала укажите ключ приложения в настройках.")
            return
        self._yandex_code.setVisible(True)
        self._yandex_code.setFocus()
        self._yandex_code.returnPressed.connect(self._finish_yandex)

        # Открываем страницу авторизации: Яндекс покажет код подтверждения.
        url = self._oauth.auth_url(ProviderId.YANDEX)
        self._yandex_link.setText(f'<a href="{url}">Открыть страницу авторизации Яндекса</a>')
        self._yandex_link.setVisible(True)
        QDesktopServices.openUrl(QUrl(url))

        pasted = self._clipboard_code()
        if pasted:
            self._yandex_code.setText(pasted)
            self._status.setText(
                "Код из буфера обмена подставлен. Нажмите Enter, чтобы войти."
            )
        else:
            self._status.setText(
                "Войдите в Яндекс в браузере и скопируйте код (7 цифр) — "
                "он подставится сам."
            )

    @staticmethod
    def _clipboard_code() -> str:
        """Код подтверждения Яндекса — 7 цифр."""
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return ""
        text = (clipboard.text() or "").strip()
        if text.isdigit() and 6 <= len(text) <= 10:
            return text
        return ""

    def _finish_yandex(self) -> None:
        code = self._yandex_code.text().strip()
        if not code:
            return
        self._status.setText("Проверяем код…")
        self._yandex_btn.setEnabled(False)
        future = self._runtime.submit(self._oauth.complete_code(ProviderId.YANDEX, code))
        future.add_done_callback(lambda fut: self._on_done(ProviderId.YANDEX, fut))

    def _login_google(self) -> None:
        if not self._oauth.configured(ProviderId.GOOGLE):
            self._status.setText("Сначала укажите ключ приложения в настройках.")
            return
        self._status.setText("Открываем браузер…")
        self._google_btn.setEnabled(False)
        future = self._runtime.submit(self._oauth.login_with_browser(ProviderId.GOOGLE))
        future.add_done_callback(lambda fut: self._on_done(ProviderId.GOOGLE, fut))

    def _on_done(self, pid: ProviderId, future) -> None:
        try:
            name = future.result()
        except Exception as exc:
            log.warning("Вход не удался: %s", exc)
            self.login_failed.emit(pid.value, friendly_error(exc))
        else:
            self.login_succeeded.emit(pid.value, name)
