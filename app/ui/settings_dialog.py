"""Настройки: ключи приложений, папка загрузок, скорость, кэш превью."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.config import AppConfig, preview_cache_path_for


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


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent: QWidget | None = None,
                 cache_ops: CacheOps | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._cache_ops = cache_ops
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        # --- Ключи приложений ---
        keys_box = QGroupBox("Доступ к облакам", self)
        keys_form = QFormLayout(keys_box)
        keys_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self._yandex_id = QLineEdit(config.yandex_client_id, keys_box)
        self._yandex_id.setPlaceholderText("Client ID из oauth.yandex.ru")
        self._yandex_secret = QLineEdit(config.yandex_client_secret, keys_box)
        self._yandex_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._yandex_secret.setPlaceholderText("Client Secret")
        keys_form.addRow("Яндекс.Диск — Client ID", self._yandex_id)
        keys_form.addRow("Яндекс.Диск — Client Secret", self._yandex_secret)

        self._google_id = QLineEdit(config.google_client_id, keys_box)
        self._google_id.setPlaceholderText("Client ID из console.cloud.google.com")
        self._google_secret = QLineEdit(config.google_client_secret, keys_box)
        self._google_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._google_secret.setPlaceholderText("Client Secret")
        self._google_api_key = QLineEdit(config.google_api_key, keys_box)
        self._google_api_key.setPlaceholderText("Необязательно: для публичных папок без входа")
        keys_form.addRow("Google Диск — Client ID", self._google_id)
        keys_form.addRow("Google Диск — Client Secret", self._google_secret)
        keys_form.addRow("Google Диск — API Key", self._google_api_key)

        hint = QLabel(
            "Как получить ключи — пошаговая инструкция в файле docs/OAUTH_SETUP.md "
            "(или папке «Документация» рядом с программой).",
            keys_box,
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        keys_form.addRow(hint)
        layout.addWidget(keys_box)

        # --- Загрузки ---
        downloads_box = QGroupBox("Загрузки", self)
        downloads_form = QFormLayout(downloads_box)
        downloads_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        dir_row = QHBoxLayout()
        self._dir = QLineEdit(config.download_dir, downloads_box)
        browse = QPushButton("Обзор…", downloads_box)
        browse.clicked.connect(self._pick_dir)
        dir_row.addWidget(self._dir)
        dir_row.addWidget(browse)
        downloads_form.addRow("Папка загрузок", dir_row)

        self._parallel = QSpinBox(downloads_box)
        self._parallel.setRange(1, 16)
        self._parallel.setValue(config.max_parallel_files)
        self._parallel.setToolTip("Сколько файлов качать одновременно")
        downloads_form.addRow("Файлов одновременно", self._parallel)

        self._segments = QSpinBox(downloads_box)
        self._segments.setRange(1, 16)
        self._segments.setValue(config.max_parallel_segments)
        self._segments.setToolTip("Сколько частей одного большого файла качать одновременно")
        downloads_form.addRow("Частей одного файла", self._segments)

        self._segment_mb = QSpinBox(downloads_box)
        self._segment_mb.setRange(8, 2048)
        self._segment_mb.setSingleStep(8)
        self._segment_mb.setValue(config.segment_min_mb)
        self._segment_mb.setSuffix(" МБ")
        self._segment_mb.setToolTip("Минимальный размер части. Файл меньше — качается целиком")
        downloads_form.addRow("Размер части от", self._segment_mb)

        self._use_segments = QCheckBox("Докачивать большие файлы по частям", downloads_box)
        self._use_segments.setChecked(config.use_segments)
        downloads_form.addRow(self._use_segments)

        self._speed = QSpinBox(downloads_box)
        self._speed.setRange(0, 10000)
        self._speed.setValue(config.download_speed_limit_mbps)
        self._speed.setSpecialValueText("Без ограничения")
        self._speed.setSuffix(" Мбит/с")
        downloads_form.addRow("Ограничение скорости", self._speed)
        layout.addWidget(downloads_box)

        # --- Превью ---
        preview_box = QGroupBox("Превью", self)
        preview_form = QFormLayout(preview_box)
        preview_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self._ffmpeg = QLineEdit(config.ffmpeg_path, preview_box)
        self._ffmpeg.setPlaceholderText("Оставьте пустым — найдём в системе")
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self._ffmpeg)
        ffmpeg_browse = QPushButton("Обзор…", preview_box)
        ffmpeg_browse.clicked.connect(self._pick_ffmpeg)
        ffmpeg_row.addWidget(ffmpeg_browse)
        preview_form.addRow("Путь к ffmpeg", ffmpeg_row)

        # Кэш превью: где лежит, сколько занимает, очистка.
        cache_row = QHBoxLayout()
        self._cache_dir = QLineEdit(config.preview_cache_dir, preview_box)
        self._cache_dir.setPlaceholderText("По умолчанию — в папке загрузок")
        self._cache_dir.setToolTip(
            "Куда складывать скачанные для превью куски видео. Удобно выбрать "
            "быстрый диск с запасом места. Внутри выбранной папки программа "
            "создаст свою подпапку .svscloud-preview и будет работать только в ней."
        )
        self._cache_dir.textChanged.connect(self._update_cache_info)
        cache_row.addWidget(self._cache_dir)
        cache_browse = QPushButton("Обзор…", preview_box)
        cache_browse.clicked.connect(self._pick_cache_dir)
        cache_row.addWidget(cache_browse)
        cache_default = QPushButton("По умолчанию", preview_box)
        cache_default.setToolTip("Хранить кэш в папке загрузок")
        cache_default.clicked.connect(lambda: self._cache_dir.setText(""))
        cache_row.addWidget(cache_default)
        preview_form.addRow("Папка кэша превью", cache_row)

        self._cache_path_label = QLabel("", preview_box)
        self._cache_path_label.setProperty("muted", True)
        self._cache_path_label.setWordWrap(True)
        self._cache_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        preview_form.addRow("", self._cache_path_label)

        self._cache_limit = QDoubleSpinBox(preview_box)
        self._cache_limit.setRange(0.5, 1000.0)
        self._cache_limit.setDecimals(1)
        self._cache_limit.setSingleStep(1.0)
        self._cache_limit.setSuffix(" ГБ")
        self._cache_limit.setValue(float(config.preview_cache_gb))
        self._cache_limit.setToolTip("Больше этого объёма кэш не займёт: старое удаляется само")
        preview_form.addRow("Предел кэша", self._cache_limit)

        usage_row = QHBoxLayout()
        self._cache_usage = QLabel("", preview_box)
        usage_row.addWidget(self._cache_usage, 1)
        self._cache_open = QPushButton("Открыть папку", preview_box)
        self._cache_open.clicked.connect(self._open_cache_folder)
        usage_row.addWidget(self._cache_open)
        self._cache_clear = QPushButton("Очистить кэш", preview_box)
        self._cache_clear.setToolTip(
            "Удалить всё, что скачано для превью. Скачанные файлы в папке загрузок не трогаются."
        )
        self._cache_clear.clicked.connect(self._clear_cache)
        usage_row.addWidget(self._cache_clear)
        preview_form.addRow("Занято", usage_row)
        layout.addWidget(preview_box)
        self._dir.textChanged.connect(self._update_cache_info)
        self._update_cache_info()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self
        )
        # Переводы Qt в сборку не входят — подписываем кнопки сами.
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

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
        self._cache_usage.setText(
            f"{human_size(size)} · файлов: {files}" if files else "Кэш пуст"
        )
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
