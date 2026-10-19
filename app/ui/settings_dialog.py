"""Настройки: ключи приложений, папка загрузок, скорость."""

from __future__ import annotations


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.config import AppConfig


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
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
        layout.addWidget(preview_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    def _pick_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка загрузок", self._dir.text())
        if path:
            self._dir.setText(path)

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
        config.save()
        self.accept()
