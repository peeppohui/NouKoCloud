"""Диалог входа в облачные аккаунты."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.errors import friendly_error
from ..core.models import ProviderId
from ..oauth.yandex import normalize_code
from . import theme
from .widgets import IconLabel, OverlayDialog, button, label, set_prop

log = logging.getLogger(__name__)


class AuthDialog(OverlayDialog):
    """Два понятных действия: «Войти через Яндекс» и «Войти через Google»."""

    login_succeeded = Signal(str, str)   # provider id, имя аккаунта
    login_failed = Signal(str, str)      # provider id, сообщение
    settings_requested = Signal()        # не настроены ключи — открыть «Настройки»
    logout_requested = Signal(str)       # provider id — выход подтверждён

    def __init__(self, oauth, runtime, parent: QWidget | None = None, engine=None) -> None:
        super().__init__("вход в аккаунты", parent, max_width=580)
        self._oauth = oauth
        self._runtime = runtime
        self._engine = engine
        # После выхода «Сменить аккаунт» сразу открывает вход заново.
        self._switch_after_logout: set[ProviderId] = set()
        self._logout_buttons: dict[ProviderId, tuple[QPushButton, QPushButton]] = {}
        self.setWindowTitle("Вход в облачные аккаунты")
        body = self.body_layout
        body.setSpacing(18)

        body.addWidget(label(
            "Войдите в свой Яндекс.Диск и Google Диск, чтобы видеть «Мой диск». "
            "Публичные ссылки работают и без входа — их можно вставить сразу.",
            "muted", self.body, wrap=True))

        # --- Яндекс ---
        yandex_card, yandex_lay, self._yandex_status = self._account_card(
            "Яндекс.Диск", "После входа браузер покажет код — вставьте его в поле ниже.")
        self._yandex_link = QLabel("", yandex_card)
        self._yandex_link.setOpenExternalLinks(True)
        self._yandex_link.setTextFormat(Qt.TextFormat.RichText)
        self._yandex_link.setVisible(False)
        yandex_lay.addWidget(self._yandex_link)

        self._yandex_code = QLineEdit(yandex_card)
        self._yandex_code.setProperty("variant", "l")
        self._yandex_code.setPlaceholderText("Вставьте код со страницы Яндекса и нажмите Enter")
        self._yandex_code.setAccessibleName("Код со страницы Яндекса")
        self._yandex_code.setVisible(False)
        # Подключаем один раз: иначе каждое нажатие «Войти» добавляло бы
        # ещё один обработчик, и код уходил бы в Яндекс несколько раз.
        self._yandex_code.returnPressed.connect(self._finish_yandex)
        yandex_lay.addWidget(self._yandex_code)

        yandex_row = QHBoxLayout()
        yandex_row.setSpacing(12)
        self._yandex_btn = button("Войти через Яндекс.Диск", "primary", yandex_card, size="m")
        self._yandex_btn.clicked.connect(self._login_yandex)
        yandex_row.addWidget(self._yandex_btn)
        yandex_row.addStretch(1)
        yandex_lay.addLayout(yandex_row)

        yandex_actions = QHBoxLayout()
        yandex_actions.setSpacing(10)
        yandex_logout = button("Выйти", "outline", yandex_card)
        yandex_logout.clicked.connect(
            lambda: self._request_logout(ProviderId.YANDEX, switch=False))
        yandex_switch = button("Сменить аккаунт", "link", yandex_card)
        yandex_switch.clicked.connect(
            lambda: self._request_logout(ProviderId.YANDEX, switch=True))
        yandex_actions.addWidget(yandex_logout)
        yandex_actions.addWidget(yandex_switch)
        yandex_actions.addStretch(1)
        yandex_lay.addLayout(yandex_actions)
        self._logout_buttons[ProviderId.YANDEX] = (yandex_logout, yandex_switch)
        body.addWidget(yandex_card)

        # --- Google ---
        google_card, google_lay, self._google_status = self._account_card(
            "Google Диск", "Вход откроется в браузере и вернётся обратно в программу.")
        google_row = QHBoxLayout()
        self._google_btn = button("Войти через Google Диск", "outline", google_card)
        self._google_btn.clicked.connect(self._login_google)
        google_row.addWidget(self._google_btn)
        google_row.addStretch(1)
        google_lay.addLayout(google_row)

        google_actions = QHBoxLayout()
        google_actions.setSpacing(10)
        google_logout = button("Выйти", "outline", google_card)
        google_logout.clicked.connect(
            lambda: self._request_logout(ProviderId.GOOGLE, switch=False))
        google_switch = button("Сменить аккаунт", "link", google_card)
        google_switch.clicked.connect(
            lambda: self._request_logout(ProviderId.GOOGLE, switch=True))
        google_actions.addWidget(google_logout)
        google_actions.addWidget(google_switch)
        google_actions.addStretch(1)
        google_lay.addLayout(google_actions)
        self._logout_buttons[ProviderId.GOOGLE] = (google_logout, google_switch)
        body.addWidget(google_card)

        self._status = QLabel("", self.body)
        self._status.setWordWrap(True)
        self._status.setProperty("role", "muted")
        body.addWidget(self._status)

        guide_btn = button("Инструкция: где взять ключи", "link", self.card)
        guide_btn.clicked.connect(self._show_guide)
        self.footer.insertWidget(0, guide_btn)
        close_btn = button("Закрыть", "outline", self.card, size="plain")
        close_btn.clicked.connect(self.accept)
        self.footer.addWidget(close_btn)

        # Сигналы испускаются из фонового потока — Qt доставит их в GUI-поток.
        self.login_succeeded.connect(self._on_login_succeeded)
        self.login_failed.connect(self._on_login_failed)
        self._refresh()

    # ------------------------------------------------------------------
    def _account_card(self, title: str, hint: str):
        card = QFrame(self.body)
        card.setProperty("card", True)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(14)
        head = QHBoxLayout()
        head.setSpacing(12)
        avatar = QLabel(card)
        avatar.setProperty("avatar", True)
        avatar.setFixedSize(40, 40)
        av = QHBoxLayout(avatar)
        av.setContentsMargins(0, 0, 0, 0)
        av.addWidget(IconLabel("cloud", "text", 20, avatar), 0, Qt.AlignmentFlag.AlignCenter)
        head.addWidget(avatar)
        name = label(title, "wide", card)
        name.setStyleSheet("font-size: 15px;")
        head.addWidget(name)
        head.addStretch(1)
        status = QLabel("", card)
        status.setProperty("chip", "muted")
        head.addWidget(status)
        lay.addLayout(head)
        lay.addWidget(label(hint, "muted", card, wrap=True))
        return card, lay, status

    def _show_guide(self) -> None:
        from .help_dialog import HelpDialog

        HelpDialog(self.parentWidget() or self).exec()

    def refresh(self) -> None:
        """Публичное обновление после входа/выхода (для MainWindow)."""
        self._refresh()

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def logout_finished(self, pid: ProviderId) -> None:
        """Вызывается MainWindow после успешного выхода."""
        self._set_logout_busy(pid, False)
        self._refresh()
        self._status.setText("Вы вышли из аккаунта. Публичные ссылки остались.")
        if pid in self._switch_after_logout:
            self._switch_after_logout.discard(pid)
            # Не открываем браузер прямо из обработчика сигнала: даём
            # диалогу перерисоваться и только потом начинаем новый вход.
            QTimer.singleShot(0, lambda: self._login(pid))

    def logout_failed(self, pid: ProviderId, message: str) -> None:
        self._set_logout_busy(pid, False)
        self._switch_after_logout.discard(pid)
        self._status.setText(message)

    def _set_logout_busy(self, pid: ProviderId, busy: bool) -> None:
        for widget in self._logout_buttons.get(pid, ()):
            widget.setEnabled(not busy)

    def _refresh(self) -> None:
        for chip, pid in ((self._yandex_status, ProviderId.YANDEX), (self._google_status, ProviderId.GOOGLE)):
            name = self._oauth.account_name(pid)
            logged = self._oauth.is_logged_in(pid)
            if logged:
                chip.setText(f"✓  вы вошли как {name or 'аккаунт'}")
                set_prop(chip, "chip", "ok")
            elif not self._oauth.configured(pid):
                chip.setText("нужен ключ — см. «Настройки»")
                chip.setToolTip("Впишите Client ID и Client Secret в «Настройках»")
                set_prop(chip, "chip", "err")
            else:
                chip.setText("не выполнен вход")
                set_prop(chip, "chip", "muted")
            for widget in self._logout_buttons.get(pid, ()):
                widget.setVisible(logged)
            login_btn = self._yandex_btn if pid is ProviderId.YANDEX else self._google_btn
            login_btn.setVisible(not logged)
            if logged:
                if pid is ProviderId.YANDEX:
                    self._yandex_code.setVisible(False)
                    self._yandex_link.setVisible(False)

    def _login(self, pid: ProviderId) -> None:
        if pid is ProviderId.YANDEX:
            self._login_yandex()
        else:
            self._login_google()

    def _active_jobs(self, pid: ProviderId) -> int:
        if self._engine is None:
            return 0
        try:
            jobs = self._engine.snapshot()
        except Exception:
            return 0
        return sum(
            1 for job in jobs
            if job.file.provider is pid
            and job.state.name in ("QUEUED", "RUNNING", "PAUSED")
        )

    def _confirm_logout(self, pid: ProviderId) -> bool:
        active = self._active_jobs(pid)
        box = QMessageBox(self)
        box.setWindowTitle("Выход из аккаунта")
        box.setIcon(QMessageBox.Icon.Warning if active else QMessageBox.Icon.Question)
        if active:
            box.setText(
                f"Есть незавершённые загрузки: {active}. Они встанут на паузу.\n"
                "Продолжить их можно после повторного входа."
            )
            confirm_text = "Остановить задания и выйти"
        else:
            box.setText(
                "Выйти из аккаунта?\n"
                "Токен будет удалён с этого компьютера. Публичные ссылки останутся."
            )
            confirm_text = "Выйти"
        confirm = box.addButton(confirm_text, QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Не выходить", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is confirm

    def _request_logout(self, pid: ProviderId, switch: bool) -> None:
        if not self._oauth.is_logged_in(pid):
            return
        if not self._confirm_logout(pid):
            return
        if switch:
            self._switch_after_logout.add(pid)
        else:
            self._switch_after_logout.discard(pid)
        self._set_logout_busy(pid, True)
        self._status.setText(
            "Останавливаем задания и выходим…" if self._active_jobs(pid)
            else "Выходим из аккаунта…"
        )
        self.logout_requested.emit(pid.value)

    # ------------------------------------------------------------------
    def _login_yandex(self) -> None:
        if not self._oauth.configured(ProviderId.YANDEX):
            self._status.setText("Сначала укажите ключ приложения в настройках.")
            self.settings_requested.emit()
            return
        self._yandex_code.setVisible(True)
        self._yandex_code.setFocus()

        # Открываем страницу авторизации: Яндекс покажет код подтверждения.
        url = self._oauth.auth_url(ProviderId.YANDEX)
        color = theme.current().text
        self._yandex_link.setText(
            f'<a href="{url}" style="color:{color}">Открыть страницу авторизации Яндекса</a>')
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
                "Войдите в Яндекс в браузере и скопируйте код подтверждения — "
                "он подставится сам."
            )

    @staticmethod
    def _clipboard_code() -> str:
        """Код подтверждения Яндекса из буфера обмена (цифры или буквы с цифрами)."""
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return ""
        text = clipboard.text() or ""
        if len(text) > 300:
            return ""
        code = normalize_code(text)
        # Буквенный код без единой цифры почти наверняка просто слово из буфера.
        if 6 <= len(code) <= 32 and code.isalnum() and code.isascii() \
                and any(ch.isdigit() for ch in code):
            return code
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
            self.settings_requested.emit()
            return
        self._status.setText("Открываем браузер…")
        self._google_btn.setEnabled(False)
        future = self._runtime.submit(self._oauth.login_with_browser(ProviderId.GOOGLE))
        future.add_done_callback(lambda fut: self._on_done(ProviderId.GOOGLE, fut))

    def _on_login_succeeded(self, _pid: str, name: str) -> None:
        self._yandex_btn.setEnabled(True)
        self._google_btn.setEnabled(True)
        self._yandex_code.clear()
        self._status.setText(f"Вход выполнен: {name}")
        self._refresh()

    def _on_login_failed(self, _pid: str, message: str) -> None:
        # Без этого диалог навсегда оставался в «Проверяем код…» с выключенной кнопкой.
        self._yandex_btn.setEnabled(True)
        self._google_btn.setEnabled(True)
        self._status.setText(message)

    def _on_done(self, pid: ProviderId, future) -> None:
        try:
            name = future.result()
        except Exception as exc:
            log.warning("Вход не удался: %s", exc, exc_info=True)
            self.login_failed.emit(pid.value, friendly_error(exc))
        else:
            self.login_succeeded.emit(pid.value, name)
