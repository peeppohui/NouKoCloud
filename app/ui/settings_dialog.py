"""Настройки: ключи приложений, папка загрузок, скорость, кэш превью."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QBoxLayout,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core import autostart
from ..core.config import AppConfig, preview_cache_path_for
from . import theme
from .widgets import OverlayDialog, button, label, responsive_box


class CacheOps(Protocol):
    """Операции с кэшем превью (их выполняет главное окно: оно знает о плеере)."""

    def usage(self, path: Path) -> tuple[int, int]: ...          # (байт, файлов)
    def clear(self, path: Path) -> tuple[int, int]: ...          # (освобождено, не удалено)
    def open_folder(self, path: Path) -> None: ...


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


class SettingsDialog(OverlayDialog):
    def __init__(self, config: AppConfig, parent: QWidget | None = None,
                 cache_ops: CacheOps | None = None) -> None:
        # В Soyuz Grotesk нет буквы «й» — её набираем шрифтом Actay Wide (как в макете).
        super().__init__(
            f'настро<span style="font-family:\'{theme.WIDE}\'">й</span>ки', parent, max_width=1000)
        self._config = config
        self._cache_ops = cache_ops
        self.setWindowTitle("Настройки")

        columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        columns.setSpacing(16)
        self.body_layout.addLayout(columns)
        self._columns_bp = responsive_box(columns, self, 900)

        # --- Ключи приложений ---
        keys_box = QGroupBox("ДОСТУП К ОБЛАКАМ", self.body)
        keys = QVBoxLayout(keys_box)
        keys.setSpacing(12)

        def key_field(text: str, value: str, placeholder: str, secret: bool = False) -> QLineEdit:
            keys.addWidget(label(text, None, keys_box))
            edit = QLineEdit(value, keys_box)
            edit.setPlaceholderText(placeholder)
            edit.setAccessibleName(text)
            if secret:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            keys.addWidget(edit)
            keys.addSpacing(4)
            return edit

        self._yandex_id = key_field("Яндекс.Диск — Client ID", config.yandex_client_id,
                                    "Client ID из oauth.yandex.ru")
        self._yandex_secret = key_field("Яндекс.Диск — Client Secret", config.yandex_client_secret,
                                        "Client Secret", secret=True)
        self._google_id = key_field("Google Диск — Client ID", config.google_client_id,
                                    "Client ID из console.cloud.google.com")
        self._google_secret = key_field("Google Диск — Client Secret", config.google_client_secret,
                                        "Client Secret", secret=True)
        self._google_api_key = key_field("Google Диск — API Key", config.google_api_key,
                                         "Необязательно: для публичных папок без входа")
        keys.addWidget(label(
            "Ключи нужны только для своего «Моего диска» — чужие ссылки работают без них.",
            "hint", keys_box, wrap=True))
        guide = button("Инструкция: как получить ключи", "soft", keys_box)
        guide.clicked.connect(self._show_guide)
        keys.addWidget(guide, 0, Qt.AlignmentFlag.AlignLeft)
        keys.addStretch(1)
        left = QVBoxLayout()
        left.setSpacing(16)
        left.addWidget(keys_box, 1)
        ui_box = QGroupBox("ИНТЕРФЕЙС", self.body)
        ui = QVBoxLayout(ui_box)
        ui.setSpacing(12)
        self._ui_motion = QCheckBox("Анимации кнопок (плавная подсветка)", ui_box)
        self._ui_motion.setChecked(bool(getattr(config, "ui_motion", True)))
        self._ui_sounds = QCheckBox("Звуки интерфейса", ui_box)
        self._ui_sounds.setChecked(bool(getattr(config, "ui_sounds", True)))
        ui.addWidget(self._ui_motion)
        ui.addWidget(self._ui_sounds)
        volume_row = QHBoxLayout()
        volume_row.setSpacing(12)
        volume_row.addWidget(label("Громкость звуков", None, ui_box))
        self._ui_volume = QSlider(Qt.Orientation.Horizontal, ui_box)
        self._ui_volume.setRange(0, 100)
        self._ui_volume.setValue(int(getattr(config, "ui_volume", 60)))
        self._ui_volume.setAccessibleName("Громкость звуков интерфейса")
        volume_row.addWidget(self._ui_volume, 1)
        self._ui_volume_label = label("", "muted", ui_box)
        self._ui_volume_label.setMinimumWidth(44)
        volume_row.addWidget(self._ui_volume_label)
        ui.addLayout(volume_row)
        self._ui_volume.valueChanged.connect(self._on_volume_changed)
        # Отпустили ползунок — проигрываем образец, чтобы было слышно громкость.
        self._ui_volume.sliderReleased.connect(self._preview_volume)
        self._ui_sounds.toggled.connect(self._ui_volume.setEnabled)
        self._ui_volume.setEnabled(self._ui_sounds.isChecked())
        self._on_volume_changed(self._ui_volume.value())
        left.addWidget(ui_box)

        bg_box = QGroupBox("РАБОТА В ФОНЕ", self.body)
        bg = QVBoxLayout(bg_box)
        bg.setSpacing(12)
        self._run_bg = QCheckBox("Не закрываться, а работать в фоне (значок у часов)", bg_box)
        self._run_bg.setChecked(bool(getattr(config, "run_in_background", True)))
        self._run_bg.setToolTip("Крестик прячет окно, загрузки идут дальше. "
                                "Выйти полностью — «Выход» в меню значка у часов")
        bg.addWidget(self._run_bg)
        self._autostart = QCheckBox("Запускать вместе с Windows (сразу в фоне)", bg_box)
        self._autostart.setChecked(autostart.enabled())
        if not autostart.supported():
            self._autostart.setEnabled(False)
            self._autostart.setToolTip("Доступно в собранной программе (NoukoCloud.exe)")
        bg.addWidget(self._autostart)
        left.addWidget(bg_box)
        columns.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(16)
        columns.addLayout(right, 1)

        # --- Загрузки ---
        downloads_box = QGroupBox("ЗАГРУЗКИ", self.body)
        downloads = QVBoxLayout(downloads_box)
        downloads.setSpacing(12)
        downloads.addWidget(label("Папка загрузок", None, downloads_box))
        dir_row = QHBoxLayout()
        dir_row.setSpacing(8)
        self._dir = QLineEdit(config.download_dir, downloads_box)
        self._dir.setAccessibleName("Папка загрузок")
        browse = button("Обзор…", "soft", downloads_box)
        browse.clicked.connect(self._pick_dir)
        dir_row.addWidget(self._dir, 1)
        dir_row.addWidget(browse)
        downloads.addLayout(dir_row)

        def number_row(text: str, spin, unit: str = "") -> None:
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(label(text, None, downloads_box), 1)
            spin.setFixedWidth(96 if unit != "Мбит/с" else 110)
            spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
            spin.setAccessibleName(text)
            row.addWidget(spin)
            unit_label = label(unit, "muted", downloads_box)
            unit_label.setMinimumWidth(48)
            row.addWidget(unit_label)
            downloads.addLayout(row)

        self._parallel = QSpinBox(downloads_box)
        self._parallel.setRange(1, 16)
        self._parallel.setValue(config.max_parallel_files)
        self._parallel.setToolTip("Сколько файлов качать одновременно")
        number_row("Файлов одновременно", self._parallel)

        self._segments = QSpinBox(downloads_box)
        self._segments.setRange(1, 32)
        self._segments.setValue(config.max_parallel_segments)
        self._segments.setToolTip("Сколько частей одного файла качать одновременно. Облако режет скорость "
                                  "каждого соединения, поэтому больше частей — быстрее (обычно 8–16)")
        number_row("Частей одного файла", self._segments)

        self._segment_mb = QSpinBox(downloads_box)
        self._segment_mb.setRange(1, 2048)
        self._segment_mb.setSingleStep(1)
        self._segment_mb.setValue(config.segment_min_mb)
        self._segment_mb.setToolTip("Размер одной части. Меньше — больше частей и соединений "
                                    "(быстрее на медленном облаке); файл не больше части качается целиком")
        number_row("Размер части", self._segment_mb, "МБ")

        self._speed = QSpinBox(downloads_box)
        self._speed.setRange(0, 10000)
        self._speed.setValue(config.download_speed_limit_mbps)
        self._speed.setSpecialValueText("нет")
        self._speed.setToolTip("0 — без ограничения")
        number_row("Ограничение скорости", self._speed, "Мбит/с")

        self._use_segments = QCheckBox("Докачивать большие файлы по частям", downloads_box)
        self._use_segments.setChecked(config.use_segments)
        downloads.addWidget(self._use_segments)
        right.addWidget(downloads_box)

        # --- Превью ---
        preview_box = QGroupBox("ПРЕВЬЮ", self.body)
        preview = QVBoxLayout(preview_box)
        preview.setSpacing(12)
        preview.addWidget(label("Путь к ffmpeg", None, preview_box))
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.setSpacing(8)
        self._ffmpeg = QLineEdit(config.ffmpeg_path, preview_box)
        self._ffmpeg.setPlaceholderText("Оставьте пустым — найдём в системе")
        self._ffmpeg.setAccessibleName("Путь к ffmpeg")
        ffmpeg_row.addWidget(self._ffmpeg, 1)
        ffmpeg_browse = button("Обзор…", "soft", preview_box)
        ffmpeg_browse.clicked.connect(self._pick_ffmpeg)
        ffmpeg_row.addWidget(ffmpeg_browse)
        preview.addLayout(ffmpeg_row)

        # Кэш превью: где лежит, сколько занимает, очистка.
        preview.addWidget(label("Папка кэша превью", None, preview_box))
        cache_row = QHBoxLayout()
        cache_row.setSpacing(8)
        self._cache_dir = QLineEdit(config.preview_cache_dir, preview_box)
        self._cache_dir.setPlaceholderText("По умолчанию — в папке загрузок")
        self._cache_dir.setAccessibleName("Папка кэша превью")
        self._cache_dir.setToolTip(
            "Куда складывать скачанные для превью куски видео. Удобно выбрать "
            "быстрый диск с запасом места. Внутри выбранной папки программа "
            "создаст свою подпапку .noukocloud-preview и будет работать только в ней."
        )
        self._cache_dir.textChanged.connect(self._update_cache_info)
        cache_row.addWidget(self._cache_dir, 1)
        cache_browse = button("Обзор…", "soft", preview_box)
        cache_browse.clicked.connect(self._pick_cache_dir)
        cache_row.addWidget(cache_browse)
        cache_default = button("По умолчанию", "link", preview_box)
        cache_default.setToolTip("Хранить кэш в папке загрузок")
        cache_default.clicked.connect(lambda: self._cache_dir.setText(""))
        cache_row.addWidget(cache_default)
        preview.addLayout(cache_row)

        self._cache_path_label = label("", "hint", preview_box, wrap=True)
        self._cache_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        preview.addWidget(self._cache_path_label)

        limit_row = QHBoxLayout()
        limit_row.setSpacing(8)
        limit_row.addWidget(label("Предел кэша", None, preview_box), 1)
        self._cache_limit = QDoubleSpinBox(preview_box)
        self._cache_limit.setRange(0.5, 1000.0)
        self._cache_limit.setDecimals(1)
        self._cache_limit.setSingleStep(1.0)
        self._cache_limit.setValue(float(config.preview_cache_gb))
        self._cache_limit.setFixedWidth(96)
        self._cache_limit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cache_limit.setAccessibleName("Предел кэша, ГБ")
        self._cache_limit.setToolTip("Больше этого объёма кэш не займёт: старое удаляется само")
        self._cache_limit.valueChanged.connect(self._update_cache_info)
        limit_row.addWidget(self._cache_limit)
        unit = label("ГБ", "muted", preview_box)
        unit.setMinimumWidth(48)
        limit_row.addWidget(unit)
        preview.addLayout(limit_row)

        usage_head = QHBoxLayout()
        usage_head.addWidget(label("Занято", None, preview_box))
        usage_head.addStretch(1)
        self._cache_usage = QLabel("", preview_box)
        self._cache_usage.setWordWrap(True)
        self._cache_usage.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        usage_head.addWidget(self._cache_usage, 3)
        preview.addLayout(usage_head)
        self._cache_bar = QProgressBar(preview_box)
        self._cache_bar.setRange(0, 1000)
        self._cache_bar.setTextVisible(False)
        self._cache_bar.setFixedHeight(6)
        preview.addWidget(self._cache_bar)

        usage_row = QHBoxLayout()
        usage_row.setSpacing(8)
        self._cache_open = button("Открыть папку", "soft", preview_box)
        self._cache_open.clicked.connect(self._open_cache_folder)
        usage_row.addWidget(self._cache_open)
        self._cache_clear = button("Очистить кэш", "soft", preview_box)
        self._cache_clear.setToolTip(
            "Удалить всё, что скачано для превью. Скачанные файлы в папке загрузок не трогаются."
        )
        self._cache_clear.clicked.connect(self._clear_cache)
        usage_row.addWidget(self._cache_clear)
        usage_row.addStretch(1)
        preview.addLayout(usage_row)
        right.addWidget(preview_box)
        right.addStretch(1)

        self._dir.textChanged.connect(self._update_cache_info)
        self._update_cache_info()

        cancel = button("Отмена", "outline", self.card, size="xl")
        cancel.clicked.connect(self.reject)
        save = button("Сохранить", "primary", self.card)
        save.setDefault(False)
        save.setAutoDefault(False)
        save.clicked.connect(self._save)
        self.footer.addWidget(cancel)
        self.footer.addWidget(save)

    # ------------------------------------------------------------------
    def _pick_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка загрузок", self._dir.text())
        if path:
            self._dir.setText(path)

    # --- кэш превью ----------------------------------------------------
    def _current_cache_path(self) -> Path:
        """Папка кэша, которая используется СЕЙЧАС (сохранённая настройка)."""
        return self._config.preview_cache_path

    def _edited_cache_path(self) -> Path:
        """Папка кэша с учётом ещё не сохранённых правок в окне."""
        download_dir = self._dir.text().strip() or self._config.download_dir
        return preview_cache_path_for(self._cache_dir.text(), download_dir)

    def _update_cache_info(self) -> None:
        current, edited = self._current_cache_path(), self._edited_cache_path()
        text = f"Файлы кэша: {edited}"
        if _norm(edited) != _norm(current):
            text += "  (после сохранения; сейчас — " + str(current) + ")"
        self._cache_path_label.setText(text)
        if self._cache_ops is None:
            self._cache_usage.setText("—")
            self._cache_clear.setEnabled(False)
            self._cache_open.setEnabled(False)
            return
        size, files = self._cache_ops.usage(current)
        limit = float(self._cache_limit.value()) * 1024 ** 3
        self._cache_usage.setText(
            f"{human_size(size)} из {self._cache_limit.value():g} ГБ · файлов: {files}"
            if files else "Кэш пуст"
        )
        self._cache_bar.setValue(int(1000 * min(1.0, size / limit)) if limit else 0)
        self._cache_clear.setEnabled(files > 0)

    def _pick_cache_dir(self) -> None:
        start = self._cache_dir.text().strip() or self._dir.text().strip()
        path = QFileDialog.getExistingDirectory(self, "Папка для кэша превью", start)
        if path:
            self._cache_dir.setText(path)

    def _open_cache_folder(self) -> None:
        if self._cache_ops is not None:
            self._cache_ops.open_folder(self._current_cache_path())

    def _clear_cache(self) -> None:
        if self._cache_ops is None:
            return
        freed, failed = self._cache_ops.clear(self._current_cache_path())
        self._update_cache_info()
        message = f"Освобождено: {human_size(freed)}."
        if failed:
            message += f" Не удалось удалить файлов: {failed} — они ещё используются, попробуйте позже."
        self._cache_usage.setToolTip(message)
        self._cache_usage.setText(self._cache_usage.text() + "  ·  " + message)

    def _ask_clear_old_cache(self, old: Path, size: int) -> bool:
        """Спрашиваем, удалить ли кэш из прежней папки (вынесено — для тестов)."""
        answer = QMessageBox.question(
            self,
            "Кэш превью",
            f"Папка кэша меняется. В прежней папке осталось {human_size(size)} кэша:\n{old}\n\n"
            "Удалить его?",
        )
        return answer == QMessageBox.StandardButton.Yes

    def _show_guide(self) -> None:
        from .help_dialog import HelpDialog

        HelpDialog(self.parentWidget() or self).exec()

    def _on_volume_changed(self, value: int) -> None:
        self._ui_volume_label.setText(f"{value}%")

    def _preview_volume(self) -> None:
        from . import sound

        bank = sound.bank()
        previous = bank.enabled
        bank.enabled = True
        bank.set_volume(self._ui_volume.value())
        bank.play("open", force=True)
        bank.enabled = previous

    def _pick_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Найти ffmpeg", self._ffmpeg.text())
        if path:
            self._ffmpeg.setText(path)

    def _save(self) -> None:
        config = self._config
        config.yandex_client_id = self._yandex_id.text().strip()
        config.yandex_client_secret = self._yandex_secret.text().strip()
        config.google_client_id = self._google_id.text().strip()
        config.google_client_secret = self._google_secret.text().strip()
        config.google_api_key = self._google_api_key.text().strip()
        config.download_dir = self._dir.text().strip() or config.download_dir
        config.max_parallel_files = self._parallel.value()
        config.max_parallel_segments = self._segments.value()
        config.segment_min_mb = self._segment_mb.value()
        config.use_segments = self._use_segments.isChecked()
        config.download_speed_limit_mbps = self._speed.value()
        config.ffmpeg_path = self._ffmpeg.text().strip()
        config.ui_motion = self._ui_motion.isChecked()
        config.ui_sounds = self._ui_sounds.isChecked()
        config.ui_volume = self._ui_volume.value()
        config.run_in_background = self._run_bg.isChecked()
        if self._autostart.isEnabled() and self._autostart.isChecked() != autostart.enabled():
            autostart.set_enabled(self._autostart.isChecked())

        old_cache = self._current_cache_path()
        config.preview_cache_dir = self._cache_dir.text().strip()
        config.preview_cache_gb = float(self._cache_limit.value())
        new_cache = config.preview_cache_path
        if self._cache_ops is not None and _norm(old_cache) != _norm(new_cache):
            size, files = self._cache_ops.usage(old_cache)
            if files and self._ask_clear_old_cache(old_cache, size):
                self._cache_ops.clear(old_cache)
        config.save()
        self.accept()


def _norm(path: Path) -> str:
    try:
        return str(path.expanduser().resolve()).lower()
    except OSError:
        return str(path).lower()
