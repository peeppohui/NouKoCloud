"""Главное окно приложения."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..context import AppContext
from ..core.errors import friendly_error
from ..core.models import CloudFile, ProviderId, SourceKind
from ..core.paths import logs_dir
from ..providers.base import BrowseContext
from ..preview.server import browser_can_play
from .auth_dialog import AuthDialog
from .file_tree import FileTree
from .preview_panel import PreviewPanel
from .queue_view import QueueView
from .settings_dialog import SettingsDialog
from .theme import STYLESHEET


def _human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._preview_token: str | None = None
        self.setWindowTitle("svsCloud — облачные исходники для монтажа")
        self.resize(1280, 820)
        self.setStyleSheet(STYLESHEET)

        self._build_ui()
        self._connect_bus()
        self._on_checked_changed(0)

        QTimer.singleShot(0, self._after_start)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 8)
        layout.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Horizontal, central)

        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        self.tree = FileTree(left)
        self.tree._loader = self._load_children
        self.tree.children_loaded.connect(self.tree.handle_children_loaded)
        self.tree.file_activated.connect(self._show_preview)
        self.tree.checked_changed.connect(self._on_checked_changed)
        left_layout.addWidget(self.tree, 1)

        # Кнопка массового скачивания живёт РЯДОМ с деревом: так её
        # невозможно перепутать с кнопкой «Скачать этот файл» в превью,
        # а в подписи видно, сколько файлов уедет в очередь.
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self._btn_download = QPushButton("Скачать выбранное", left)
        self._btn_download.setProperty("accent", True)
        self._btn_download.setEnabled(False)
        self._btn_download.clicked.connect(self._download_selected)
        action_row.addWidget(self._btn_download)
        self._selection_label = QLabel("Отмечено: 0", left)
        self._selection_label.setProperty("muted", True)
        action_row.addWidget(self._selection_label)
        action_row.addStretch(1)
        left_layout.addLayout(action_row)

        splitter.addWidget(left)

        self.tabs = QTabWidget(splitter)
        self.preview_panel = PreviewPanel(self.tabs)
        self.preview_panel.download_requested.connect(self._download_one)
        self.tabs.addTab(self.preview_panel, "Превью")
        self.queue_view = QueueView(self.tabs)
        self.queue_view.pause_requested.connect(self._engine_call(self.ctx.engine.pause))
        self.queue_view.resume_requested.connect(self._engine_call(self.ctx.engine.resume))
        self.queue_view.cancel_requested.connect(self._engine_call(self.ctx.engine.cancel))
        self.queue_view.remove_requested.connect(self._engine_call(self.ctx.engine.remove))
        self.queue_view.open_folder_requested.connect(self._open_download_dir)
        self.tabs.addTab(self.queue_view, "Очередь")
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([560, 680])

        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

        self._build_toolbar()
        self._build_statusbar()

    def _build_toolbar(self) -> None:
        bar = QToolBar("Основное", self)
        bar.setMovable(False)
        self.addToolBar(bar)

        self._act_accounts = QAction("Аккаунты", self)
        self._act_accounts.triggered.connect(self._open_accounts)
        bar.addAction(self._act_accounts)

        self._act_link = QAction("Открыть ссылку", self)
        self._act_link.setShortcut(QKeySequence("Ctrl+O"))
        self._act_link.triggered.connect(self._open_link)
        bar.addAction(self._act_link)

        self._act_mydrive = QAction("Мой диск", self)
        self._act_mydrive.triggered.connect(self._open_my_drive)
        bar.addAction(self._act_mydrive)

        spacer = QWidget(bar)
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding, spacer.sizePolicy().verticalPolicy())
        bar.addWidget(spacer)

        # Действие «скачать выбранное» без кнопки в тулбаре: сама кнопка
        # живёт под деревом, где видно отметки.
        self._act_download = QAction("Скачать выбранное", self)
        self._act_download.setShortcut(QKeySequence("Ctrl+D"))
        self._act_download.triggered.connect(self._download_selected)
        self._act_download.setEnabled(False)
        self.addAction(self._act_download)

        self._act_settings = QAction("Настройки", self)
        self._act_settings.triggered.connect(self._open_settings)
        bar.addAction(self._act_settings)

    def _build_statusbar(self) -> None:
        self._status_label = QLabel("Готово", self)
        self.statusBar().addWidget(self._status_label, 1)
        action = QAction("Журнал", self)
        action.triggered.connect(self._open_logs)
        self.statusBar().addAction(action)

    # ------------------------------------------------------------------
    def _engine_call(self, func):
        """Вызывает метод движка в его потоке (asyncio.Event не потокобезопасен)."""
        def wrapper(*args) -> None:
            self.ctx.runtime.submit(func(*args))

        return wrapper

    def _connect_bus(self) -> None:
        bus = self.ctx.bus
        bus.notice.connect(self._on_notice)
        bus.jobs_snapshot.connect(self._on_snapshot)
        bus.job_progress.connect(self._on_progress)
        bus.job_finished.connect(self._on_finished)
        bus.logged_in.connect(self._on_logged_in)
        bus.listing_started.connect(lambda pid: self._status_label.setText("Загружаем список файлов…"))

    def _after_start(self) -> None:
        """Действия после запуска фонового цикла."""
        future = self.ctx.runtime.submit(self.ctx.startup())
        future.add_done_callback(self._on_startup_done)
        self._recover_downloads()

    def _on_startup_done(self, future) -> None:
        try:
            future.result()
        except Exception as exc:
            log.error("Ошибка запуска: %s", exc, exc_info=True)
            self._on_notice("error", "Не удалось запустить фоновые задачи. Перезапустите программу.")

    def _recover_downloads(self) -> None:
        dest = self.ctx.config.download_dir_path
        future = self.ctx.runtime.submit(self.ctx.engine.recover(dest))
        future.add_done_callback(lambda fut: self._after_recover(fut))

    def _after_recover(self, future) -> None:
        try:
            restored = future.result()
        except Exception as exc:
            log.warning("Не удалось восстановить загрузки: %s", exc)
            return
        if restored:
            self._on_notice(
                "info",
                f"Найдено незаконченных загрузок: {len(restored)}. Продолжаем с места обрыва.",
            )
            self.tabs.setCurrentIndex(1)

    # ------------------------------------------------------------------
    def _on_notice(self, level: str, text: str) -> None:
        self._status_label.setText(text)
        if level == "error":
            self._status_label.setProperty("error", True)
        else:
            self._status_label.setProperty("error", False)
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)

    def _on_snapshot(self, jobs) -> None:
        self.queue_view.sync(list(jobs))
        total, done, active = self.ctx.engine.stats()
        self.queue_view.set_summary(total, done, active)

    def _on_progress(self, job) -> None:
        self.queue_view.update_job(job)

    def _on_finished(self, job) -> None:
        if job.state.name == "DONE":
            self._on_notice("info", f"Готово: {job.file.name}")
        elif job.state.name == "FAILED":
            self._on_notice("error", f"Файл не скачан: {job.error}")
        self._on_snapshot(self.ctx.engine.snapshot())

    def _on_logged_in(self, pid: str, name: str) -> None:
        self._on_notice("info", f"Вход выполнен: {name}")

    # ------------------------------------------------------------------
    def _load_children(self, file: CloudFile, ctx: BrowseContext, done) -> None:
        """Асинхронная загрузка содержимого папки."""
        provider = self.ctx.registry.get(file.provider)
        future = self.ctx.runtime.submit(provider.list_children(ctx, file))
        future.add_done_callback(lambda fut: self._deliver(fut, done))

    def _deliver(self, future, done) -> None:
        try:
            files = future.result()
        except Exception as exc:
            log.warning("Не удалось загрузить папку: %s", exc)
            self._on_notice("error", friendly_error(exc, provider="yandex"))
            done([])
        else:
            done(files)

    # ------------------------------------------------------------------
    def _open_accounts(self) -> None:
        dialog = AuthDialog(self.ctx.oauth, self.ctx.runtime, self)
        dialog.login_succeeded.connect(lambda pid, name: self._on_notice("info", f"Вход выполнен: {name}"))
        dialog.login_failed.connect(lambda pid, msg: self._on_notice("error", msg))
        dialog.exec()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.ctx.config, self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            self.ctx.oauth.reload_config()
            self._on_notice("info", "Настройки сохранены")

    def open_link_at_startup(self, url: str) -> None:
        """Открывает ссылку сразу после запуска (ключ --open)."""
        QTimer.singleShot(1500, lambda: self._open_url(url))

    def _open_link(self) -> None:
        url, ok = QInputDialog.getText(
            self,
            "Открыть ссылку",
            "Вставьте ссылку на папку или файл:\n"
            "(Яндекс.Диск, Google Диск)",
        )
        if not ok or not url.strip():
            return
        self._open_url(url.strip())

    def _open_url(self, url: str) -> None:
        self._on_notice("info", "Открываем ссылку…")
        future = self.ctx.runtime.submit(self.ctx.registry.open_link(url))
        future.add_done_callback(self._after_open)

    def _after_open(self, future) -> None:
        try:
            provider, ctx, items = future.result()
        except Exception as exc:
            log.warning("Ссылка не открылась: %s", exc)
            self._on_notice("error", friendly_error(exc))
            return
        title = ctx.display_name or provider.display_name
        root = self.tree.add_root(title, ctx, provider.display_name)
        self.tree.set_children(root, items)
        self.tree.expand_item(root)
        if items:
            self._on_notice(
                "info",
                f"Открыто: {title}. Отметьте нужные файлы и нажмите «Скачать выбранное».",
            )
        else:
            self._on_notice("warn", f"«{title}» открыта, но внутри ничего нет.")

    def _open_my_drive(self) -> None:
        choices = [("Яндекс.Диск", ProviderId.YANDEX), ("Google Диск", ProviderId.GOOGLE)]
        names = [name for name, _ in choices]
        name, ok = QInputDialog.getItem(self, "Мой диск", "Какой диск открыть?", names, 0, False)
        if not ok:
            return
        pid = dict(choices)[name]
        if not self.ctx.oauth.is_logged_in(pid):
            self._on_notice("warn", "Сначала войдите в аккаунт (кнопка «Аккаунты»).")
            self._open_accounts()
            if not self.ctx.oauth.is_logged_in(pid):
                return
        ctx = BrowseContext(source=SourceKind.OAUTH, root_path="/", display_name="Мой диск")
        provider = self.ctx.registry.get(pid)
        future = self.ctx.runtime.submit(provider.list_children(ctx, None))
        future.add_done_callback(lambda fut: self._after_my_drive(pid, fut))

    def _after_my_drive(self, pid: ProviderId, future) -> None:
        try:
            items = future.result()
        except Exception as exc:
            log.warning("Не удалось открыть «Мой диск»: %s", exc)
            self._on_notice("error", friendly_error(exc, provider=pid.value))
            return
        ctx = BrowseContext(source=SourceKind.OAUTH, root_path="/", display_name="Мой диск")
        root = self.tree.add_root("Мой диск", ctx)
        self.tree.set_children(root, items)
        self.tree.expand_item(root)
        self._on_notice("info", f"«Мой диск» открыт: {len(items)} объектов")

    # ------------------------------------------------------------------
    def _on_checked_changed(self, count: int) -> None:
        """Обновляет подпись кнопки: видно, сколько файлов уедет в очередь."""
        self._act_download.setEnabled(count > 0)
        self._btn_download.setEnabled(count > 0)
        files = self.tree.checked_files()
        total = sum(f.size for f in files)
        self._btn_download.setText(f"Скачать выбранное ({count})")
        self._btn_download.setToolTip(
            f"В очередь встанет {count} файл(ов), общий объём {_human_size(total)}"
        )
        self._selection_label.setText(
            f"Отмечено: {count}" + (f" · {_human_size(total)}" if count else "")
        )
        if count:
            self._status_label.setText(f"Отмечено файлов: {count}")

    def _download_selected(self) -> None:
        files = self.tree.checked_files()
        log.info("Скачать выбранное: отмечено файлов — %d", len(files))
        if not files:
            self._on_notice("warn", "Отметьте хотя бы один файл.")
            return
        self._download_files(files)

    def _download_one(self, file: CloudFile) -> None:
        log.info("Скачать один файл: %s", file.name)
        self._download_files([file])

    def _download_files(self, files: list[CloudFile]) -> None:
        dest = self.ctx.config.download_dir_path
        future = self.ctx.runtime.submit(self.ctx.engine.enqueue(files, dest))
        future.add_done_callback(self._after_enqueue)

    def _after_enqueue(self, future) -> None:
        try:
            jobs = future.result()
        except Exception as exc:
            log.warning("Не удалось поставить в очередь: %s", exc)
            self._on_notice("error", "Не удалось начать загрузку. Подробности в журнале.")
            return
        self.tabs.setCurrentIndex(1)
        self._on_notice("info", f"В очереди: {len(jobs)} файл(ов). Куда: {self.ctx.config.download_dir}")
    # ------------------------------------------------------------------
    def _show_preview(self, file: CloudFile) -> None:
        if self._preview_token:
            self.ctx.preview.drop_session(self._preview_token)
        session = self.ctx.preview.add_session(file)
        self._preview_token = session.token
        note = ""
        if not browser_can_play(file):
            if self.ctx.ffmpeg.available:
                note = "Формат не показывается в браузере — готовим облегчённую копию."
                self._build_proxy(file, session)
            else:
                note = "Этот формат нельзя показать без ffmpeg. Скачайте файл, чтобы посмотреть его."
        self.preview_panel.show_file(file, self.ctx.preview.player_url(session.token), note)

    def _build_proxy(self, file: CloudFile, session) -> None:
        async def job() -> bool:
            provider = self.ctx.registry.get(file.provider)
            link = await provider.resolve_download(file)
            out = self.ctx.config.download_dir_path / ".svscloud-preview" / f"{session.token}.mp4"
            ok = await self.ctx.ffmpeg.build_proxy(
                link.url, link.headers, out,
                max_height=self.ctx.config.preview_max_height,
                seconds=self.ctx.config.preview_proxy_seconds,
            )
            if ok:
                session.proxy_path = out
            return ok

        future = self.ctx.runtime.submit(job())
        future.add_done_callback(self._after_proxy)

    def _after_proxy(self, future) -> None:
        try:
            ok = future.result()
        except Exception as exc:
            log.info("Облегчённая копия не удалась: %s", exc)
            return
        if ok:
            self._on_notice("info", "Превью готово.")

    # ------------------------------------------------------------------
    def _open_download_dir(self) -> None:
        path = str(self.ctx.config.download_dir_path)
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_logs(self) -> None:
        path = str(logs_dir() / "svscloud.log")
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", path])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self.ctx.config.window_geometry = self.saveGeometry().toBase64().data().decode()
            self.ctx.config.save()
        except Exception:
            pass
        future = self.ctx.runtime.submit(self.ctx.shutdown())
        try:
            future.result(timeout=8)
        except Exception:
            log.debug("Завершение не до конца", exc_info=True)
        self.ctx.runtime.stop()
        super().closeEvent(event)
