"""Мастер первого запуска.

Показывается, когда файл настроек пользователя ещё не создан (или в нём нет
отметки о пройденном мастере). Задача мастера — за пару минут объяснить, как
программа работает, и спросить самое нужное: ключи доступа и папку загрузок.

Ключи можно пропустить: программа и без них открывает публичные ссылки.
Настройка «мастер пройден» сохраняется и после пропуска — второй раз окно
не появляется.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import autostart
from ..core.config import CONFIG_VERSION, AppConfig
from ..core.paths import default_download_dir
from .widgets import OverlayDialog, button, label

log = logging.getLogger(__name__)

STEP_WELCOME = 0
STEP_KEYS = 1
STEP_FOLDER = 2
STEP_DONE = 3

#: Минимальная осмысленная длина ключа — короткая строка почти всегда
#: означает, что скопировали не всё.
MIN_KEY_LENGTH = 8


# ----------------------------------------------------------------------
# Проверки (вынесены отдельно, чтобы их можно было тестировать без окон)
# ----------------------------------------------------------------------
def keys_present(config: AppConfig) -> bool:
    """Есть ли ключи OAuth (из portable-конфига, окружения или настроек)."""
    return bool((config.yandex_client_id or "").strip()
                or (config.google_client_id or "").strip())


def validate_key_pair(client_id: str, secret: str) -> str:
    """Проверка формы ключей — без обращения к сети.

    Пустая строка — всё в порядке: ключи разрешено не вводить.
    Иначе возвращает текст ошибки для показа пользователю.
    """
    client_id = (client_id or "").strip()
    secret = (secret or "").strip()
    if not client_id and not secret:
        return ""
    if not client_id or not secret:
        return "Заполните оба поля: Client ID и Client Secret."
    if len(client_id) < MIN_KEY_LENGTH:
        return "Client ID слишком короткий — похоже, скопировали его не целиком."
    if len(secret) < MIN_KEY_LENGTH:
        return "Client Secret слишком короткий — похоже, скопировали его не целиком."
    return ""


def check_writable(path: str) -> tuple[bool, str]:
    """Можно ли писать в папку: создаём служебный файл и удаляем его.

    Возвращает (можно, текст ошибки). Текст ошибки пустой, если всё хорошо.
    """
    target = Path(path or ".").expanduser()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"Папку не удалось создать: {exc.strerror or exc}"
    try:
        with tempfile.NamedTemporaryFile(dir=target, prefix=".noukocloud-test-",
                                         delete=False) as fh:
            fh.write(b"noukocloud")
            name = fh.name
    except OSError as exc:
        return False, f"В папку нельзя записывать: {exc.strerror or exc}"
    try:
        Path(name).unlink()
    except OSError:
        pass
    return True, ""


def free_space_text(path: str) -> str:
    """Свободное место на диске с папкой — одной строкой."""
    try:
        usage = shutil.disk_usage(Path(path or ".").expanduser())
    except OSError:
        return "Свободное место: неизвестно"
    free = float(usage.free)
    if free >= 1024 ** 3:
        return f"Свободно на диске: {free / 1024 ** 3:.1f} ГБ"
    return f"Свободно на диске: {free / 1024 ** 2:.0f} МБ"


def _error_label(parent: QWidget) -> QLabel:
    lbl = label("", None, parent, wrap=True)
    lbl.setProperty("error", "true")
    lbl.hide()
    return lbl


# ----------------------------------------------------------------------
class FirstRunWizard(OverlayDialog):
    """Окно мастера: приветствие → ключи → папка → готово."""

    def __init__(self, config: AppConfig, parent: QWidget | None = None, *,
                 on_accounts: Callable[[], None] | None = None,
                 on_open_link: Callable[[], None] | None = None) -> None:
        super().__init__("мастер настройки", parent, max_width=780)
        self._config = config
        self._on_accounts = on_accounts
        self._on_open_link = on_open_link
        self.setWindowTitle("Мастер настройки")

        self._pages = QStackedWidget(self.body)
        self.body_layout.addWidget(self._pages, 1)

        self._build_welcome()
        self._build_keys()
        self._build_folder()
        self._build_done()

        self._skip = button("Пропустить", "ghost", self.card, size="m")
        self._skip.setToolTip("Настроить позже: программа работает и с публичными ссылками")
        self._skip.clicked.connect(self.skip)
        self._back = button("Назад", "outline", self.card, size="m")
        self._back.clicked.connect(self.go_back)
        self._next = button("Далее", "primary", self.card, size="m")
        self._next.clicked.connect(self.go_next)
        self.footer.addWidget(self._skip)
        self.footer.addWidget(self._back)
        self.footer.addWidget(self._next)

        self.go_to(STEP_WELCOME)

    # ------------------------------------------------------------------
    # Шаги
    # ------------------------------------------------------------------
    def _build_welcome(self) -> None:
        page = QWidget(self._pages)
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(label("НоуКо Cloud открывает ваши облака", "h3", page))
        layout.addWidget(label(
            "Программа показывает файлы Яндекс.Диска и Google Диска, даёт превью "
            "и качает нужное к себе — с докачкой и очередью.",
            "lead", page, wrap=True))
        for text in (
            "Облако при этом только читается: программа ничего не удаляет, "
            "не переименовывает и не перемещает файлы на серверах.",
            "Скачанное складывается в папку, которую вы выберете дальше.",
            "Войти в свой «Мой диск» можно позже: по публичной ссылке программа "
            "работает и без входа.",
        ):
            layout.addWidget(label("•  " + text, "muted", page, wrap=True))
        layout.addStretch(1)
        self._pages.addWidget(page)

    def _build_keys(self) -> None:
        page = QWidget(self._pages)
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(label("Ключи доступа к облакам", "h3", page))
        self._keys_preset = keys_present(self._config)
        if self._keys_preset:
            layout.addWidget(label("Ключи уже настроены", "lead", page, wrap=True))
            layout.addWidget(label(
                "Секретные ключи мы не показываем. Проверить или заменить их можно "
                "в «Настройках», там же добавляются ключи второго облака.",
                "muted", page, wrap=True))
            self._yandex_id = self._yandex_secret = None
            self._google_id = self._google_secret = None
        else:
            layout.addWidget(label(
                "Ключи нужны только для своего «Моего диска» — чужие ссылки "
                "работают и без них. Проверка ключей идёт без обращения к сети.",
                "muted", page, wrap=True))
            self._yandex_id = self._key_field(layout, "Яндекс.Диск — Client ID",
                                              "Client ID из oauth.yandex.ru")
            self._yandex_secret = self._key_field(layout, "Яндекс.Диск — Client Secret",
                                                  "Client Secret", secret=True)
            self._google_id = self._key_field(layout, "Google Диск — Client ID",
                                              "Client ID из console.cloud.google.com")
            self._google_secret = self._key_field(layout, "Google Диск — Client Secret",
                                                  "Client Secret", secret=True)
            guide = button("Инструкция: как получить ключи", "soft", page)
            guide.clicked.connect(self._show_guide)
            layout.addWidget(guide, 0, Qt.AlignmentFlag.AlignLeft)
        self._keys_error = _error_label(page)
        layout.addWidget(self._keys_error)
        layout.addStretch(1)
        self._pages.addWidget(page)

    def _key_field(self, layout: QVBoxLayout, text: str, placeholder: str,
                   secret: bool = False) -> QLineEdit:
        layout.addWidget(label(text, None, self._pages))
        edit = QLineEdit(self._pages)
        edit.setPlaceholderText(placeholder)
        edit.setAccessibleName(text)
        if secret:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(edit)
        return edit

    def _build_folder(self) -> None:
        page = QWidget(self._pages)
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(label("Папка для загрузок", "h3", page))
        layout.addWidget(label(
            "Файлы будут скачиваться сюда. Папку можно поменять в «Настройках».",
            "muted", page, wrap=True))
        row = QHBoxLayout()
        row.setSpacing(8)
        current = (self._config.download_dir or "").strip()
        if not current:
            current = str(default_download_dir())
        self._dir = QLineEdit(current, page)
        self._dir.setAccessibleName("Папка загрузок")
        self._dir.textChanged.connect(self._update_dir_status)
        browse = button("Обзор…", "soft", page)
        browse.clicked.connect(self._pick_dir)
        row.addWidget(self._dir, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        self._dir_status = label("", "muted", page, wrap=True)
        layout.addWidget(self._dir_status)
        self._dir_error = _error_label(page)
        layout.addWidget(self._dir_error)
        layout.addStretch(1)
        self._pages.addWidget(page)
        self._update_dir_status(current)

    def _build_done(self) -> None:
        page = QWidget(self._pages)
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(label("Готово", "h3", page))
        self._done_summary = label("", "muted", page, wrap=True)
        layout.addWidget(self._done_summary)
        self._autostart_box = QCheckBox("Запускать вместе с Windows (сразу в фоне)", page)
        self._autostart_box.setChecked(autostart.enabled())
        if not autostart.supported():
            self._autostart_box.setEnabled(False)
            self._autostart_box.setToolTip("Доступно в собранной программе (NoukoCloud.exe)")
        layout.addWidget(self._autostart_box)
        self._run_bg_box = QCheckBox("Сворачивать в трей вместо закрытия окна", page)
        self._run_bg_box.setChecked(bool(getattr(self._config, "run_in_background", True)))
        self._run_bg_box.setToolTip("Крестик прячет окно, загрузки идут дальше")
        layout.addWidget(self._run_bg_box)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._btn_accounts = button("Войти в аккаунт", "soft", page, icon_name="user")
        self._btn_accounts.clicked.connect(self._open_accounts)
        self._btn_link = button("Открыть ссылку", "soft", page, icon_name="link")
        self._btn_link.clicked.connect(self._open_link)
        actions.addWidget(self._btn_accounts)
        actions.addWidget(self._btn_link)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)
        self._pages.addWidget(page)

    # ------------------------------------------------------------------
    # Навигация
    # ------------------------------------------------------------------
    @property
    def step(self) -> int:
        return self._pages.currentIndex()

    def go_to(self, index: int) -> None:
        index = max(STEP_WELCOME, min(STEP_DONE, index))
        self._pages.setCurrentIndex(index)
        self._back.setEnabled(index > STEP_WELCOME)
        self._next.setText("Готово" if index == STEP_DONE else "Далее")
        if index == STEP_DONE:
            self._update_summary()

    def go_next(self) -> None:
        if self.step == STEP_KEYS and not self._validate_keys():
            return
        if self.step == STEP_FOLDER and not self._validate_folder():
            return
        if self.step == STEP_DONE:
            self.finish()
            return
        self.go_to(self.step + 1)

    def go_back(self) -> None:
        if self.step > STEP_WELCOME:
            self.go_to(self.step - 1)

    # ------------------------------------------------------------------
    # Проверки шагов
    # ------------------------------------------------------------------
    def _validate_keys(self) -> bool:
        error = ""
        if not self._keys_preset:
            error = validate_key_pair(self._yandex_id.text(), self._yandex_secret.text())
            if not error:
                error = validate_key_pair(self._google_id.text(), self._google_secret.text())
        self._keys_error.setText(error)
        self._keys_error.setVisible(bool(error))
        return not error

    def _validate_folder(self) -> bool:
        path = self._dir.text().strip()
        if not path:
            self._dir_error.setText("Укажите папку для загрузок.")
            self._dir_error.setVisible(True)
            return False
        ok, error = check_writable(path)
        self._dir_error.setText(error)
        self._dir_error.setVisible(not ok)
        return ok

    def _update_dir_status(self, text: str) -> None:
        path = (text or "").strip()
        if not path:
            self._dir_status.setText("Папка не выбрана")
            return
        self._dir_status.setText(f"{path}  ·  {free_space_text(path)}")

    def _pick_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка для загрузок", self._dir.text())
        if path:
            self._dir.setText(path)

    def _show_guide(self) -> None:
        from .help_dialog import HelpDialog

        HelpDialog(self.parentWidget() or self).exec()

    def _update_summary(self) -> None:
        lines = []
        if self._keys_preset:
            lines.append("Ключи доступа: уже настроены.")
        else:
            yandex = bool(self._yandex_id.text().strip())
            google = bool(self._google_id.text().strip())
            if yandex or google:
                parts = []
                if yandex:
                    parts.append("Яндекс.Диск")
                if google:
                    parts.append("Google Диск")
                lines.append("Ключи доступа: " + ", ".join(parts) + ".")
            else:
                lines.append("Ключи доступа: не заданы — войти в «Мой диск» пока нельзя, "
                             "публичные ссылки работают.")
        lines.append(f"Папка загрузок: {self._dir.text().strip()}")
        self._done_summary.setText("\n".join(lines))

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------
    def _open_accounts(self) -> None:
        self._apply()
        if self._on_accounts is not None:
            self._on_accounts()

    def _open_link(self) -> None:
        self._apply()
        if self._on_open_link is not None:
            self._on_open_link()

    def _apply(self) -> None:
        """Переносит ответы мастера в настройки и сохраняет их."""
        cfg = self._config
        if not self._keys_preset:
            cfg.yandex_client_id = self._yandex_id.text().strip()
            cfg.yandex_client_secret = self._yandex_secret.text().strip()
            cfg.google_client_id = self._google_id.text().strip()
            cfg.google_client_secret = self._google_secret.text().strip()
        cfg.download_dir = self._dir.text().strip() or cfg.download_dir
        cfg.run_in_background = self._run_bg_box.isChecked()
        if autostart.supported() and self._autostart_box.isChecked() != autostart.enabled():
            if not autostart.set_enabled(self._autostart_box.isChecked()):
                log.warning("Не удалось изменить автозапуск из мастера")
        self._mark_done(cfg)

    def _mark_done(self, cfg: AppConfig) -> None:
        cfg.first_run_completed = True
        cfg.config_version = max(int(getattr(cfg, "config_version", 0) or 0), CONFIG_VERSION)
        try:
            cfg.save()
        except OSError as exc:
            log.warning("Не удалось сохранить настройки после мастера: %s", exc)

    def finish(self) -> None:
        if not self._validate_folder():
            self.go_to(STEP_FOLDER)
            return
        self._apply()
        self.accept()

    def skip(self) -> None:
        """Пропустить мастер: программа остаётся работоспособной."""
        self._mark_done(self._config)
        self.reject()
