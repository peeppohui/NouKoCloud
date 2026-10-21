"""Главное окно приложения."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..context import AppContext
from ..core.errors import friendly_error
from ..core.models import CloudFile, ProviderId, SourceKind
from ..downloader.engine import _safe_name
from ..core.logging_setup import errors_log_path
from ..core.paths import logs_dir
from ..providers.base import BrowseContext
from ..preview.stream_cache import cache_usage, clear_directory
from .auth_dialog import AuthDialog
from .file_tree import LOADED_ROLE, FileTree
from .preview_panel import PreviewPanel
from .queue_view import QueueView
from . import theme
from . import sound
from .frameless import FramelessWindow
from .motion import install as install_motion
from .motion import motion
from .settings_dialog import SettingsDialog
from .social_page import SocialPage
from .start_page import StartPage
from .tray import Tray
from .widgets import Breakpoint, IconLabel, SegmentedTabs, StarsBackground, TitleBar, button, glass, label


def _human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"

log = logging.getLogger(__name__)

# Содержимое папки, загруженное заранее, годно столько секунд.
PREFETCH_TTL = 60.0

# Картинки скачиваются для превью целиком — огромные TIFF не тянем.
IMAGE_PREVIEW_LIMIT = 200 * 1024 * 1024


class _GuiInvoker(QObject):
    """Выполняет функцию в GUI-потоке.

    Колбэки concurrent.Future вызываются в фоновом asyncio-потоке, а виджеты
    Qt можно трогать только из главного. Сигнал с QueuedConnection
    переносит вызов в очередь событий GUI.
    """

    call = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @staticmethod
    def _run(fn) -> None:
        fn()


class _PreviewCacheOps:
    """Операции с кэшем превью для окна «Настройки».

    Очистку делает главное окно: перед удалением файлов нужно остановить
    плеер и закрыть открытые кэши — Windows не даёт удалять открытые файлы.
    """

    def __init__(self, window: "MainWindow") -> None:
        self._window = window

    def usage(self, path: Path) -> tuple[int, int]:
        return cache_usage(path)

    def clear(self, path: Path) -> tuple[int, int]:
        win = self._window
        win.preview_panel.stop()
        if win._preview_token:
            win.ctx.preview.drop_session(win._preview_token)
            win._preview_token = None
            win._preview_key = None
        try:
            win.ctx.runtime.submit(win.ctx.preview.close_caches()).result(timeout=10)
        except Exception:
            log.warning("Кэши превью не закрылись перед очисткой", exc_info=True)
        freed, failed = clear_directory(path)
        log.info("Кэш превью очищен: %s (освобождено %d байт, не удалено %d)", path, freed, failed)
        return freed, failed

    def open_folder(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class MainWindow(FramelessWindow, QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._last_url = ""                      # ссылка, которую открываем сейчас
        self._preview_token: str | None = None
        self._preview_key: tuple | None = None   # какой файл сейчас в превью
        self._proxy_tried: set[str] = set()      # токены, для которых уже делали копию ffmpeg
        # Выбор файла стрелками/кликом: превью с небольшой задержкой, чтобы
        # быстрый проход по списку не дёргал облако на каждом файле.
        self._pending_preview: CloudFile | None = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(150)
        # Предзагрузка файла под мышью — если мышь задержалась, а не пролетела.
        self._hover_file: CloudFile | None = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(200)
        self._hover_timer.timeout.connect(self._warm_hovered)
        # Содержимое папок, загруженное заранее (мышь задержалась над папкой):
        # ключ -> (время, future). Раскрыли папку — берём готовое.
        self._prefetched: dict[tuple, tuple[float, object]] = {}
        self._hover_folder: tuple | None = None
        self._preview_timer.timeout.connect(self._preview_pending)
        self._invoker = _GuiInvoker(self)
        self.setWindowTitle("НоуКо Cloud — облачные исходники для монтажа")
        self.setWindowIcon(theme.app_icon())
        self.resize(1280, 820)
        self.setMinimumSize(720, 520)
        self.init_frameless()
        motion().set_enabled(bool(ctx.config.ui_motion))
        theme.manager().motion = bool(ctx.config.ui_motion)
        theme.manager().set_theme(ctx.config.ui_theme)
        sound.bank().enabled = bool(ctx.config.ui_sounds)
        sound.bank().set_volume(int(getattr(ctx.config, "ui_volume", 60)))
        app = QApplication.instance()
        if app is not None:
            install_motion(app)

        self._build_ui()
        self._connect_bus()
        self._quitting = False
        self.tray: Tray | None = None
        self._setup_background()
        self._on_checked_changed(0)

        QTimer.singleShot(0, self._after_start)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("windowRoot")
        root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.title_bar = TitleBar(root)
        self._title_bar = self.title_bar        # для окна без рамки (frameless.py)
        self.title_bar.open_link.connect(self._open_url)
        self.title_bar.my_drive.connect(self._open_my_drive)
        self.title_bar.social.connect(self.show_social)
        self.title_bar.accounts.connect(self._open_accounts)
        self.title_bar.settings.connect(self._open_settings)
        self.title_bar.toggle_theme.connect(self._toggle_theme)
        self.title_bar.home.connect(self.show_start)
        layout.addWidget(self.title_bar)

        self.pages = QStackedWidget(root)
        layout.addWidget(self.pages, 1)

        # --- Стартовый экран ---
        self.start_page = StartPage(self.pages)
        self.start_page.open_link.connect(self._open_url)
        self.start_page.my_drive.connect(self._open_my_drive)
        self.start_page.social.connect(self.show_social)
        self.start_page.accounts.connect(self._open_accounts)
        self.start_page.forget_link.connect(self._forget_link)
        self.start_page.set_recent(self.ctx.config.recent_links)
        self.pages.addWidget(self.start_page)

        # --- Рабочий экран: файлы слева, превью и очередь справа ---
        workspace = StarsBackground(self.pages)
        work = QHBoxLayout(workspace)
        work.setContentsMargins(16, 16, 16, 16)
        splitter = QSplitter(Qt.Orientation.Horizontal, workspace)
        splitter.setHandleWidth(16)
        splitter.setChildrenCollapsible(False)

        left = glass(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        self.tree = FileTree(left)
        self.tree._loader = self._load_children
        self.tree.children_loaded.connect(self.tree.handle_children_loaded)
        self.tree.file_activated.connect(self._activate_preview)
        self.tree.file_selected.connect(self._on_file_selected)
        self.tree.file_hovered.connect(self._on_file_hovered)
        self.tree.folder_hovered.connect(self._on_folder_hovered)
        self.tree.open_drive_requested.connect(self._open_my_drive_for)
        self.tree.checked_changed.connect(self._on_checked_changed)
        left_layout.addWidget(self.tree, 1)

        # Кнопка массового скачивания живёт РЯДОМ с деревом: так её
        # невозможно перепутать с кнопкой «Скачать этот файл» в превью,
        # а в подписи видно, сколько файлов уедет в очередь.
        action_bar = QWidget(left)
        action_bar.setObjectName("footerBar")
        action_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        action_row = QHBoxLayout(action_bar)
        action_row.setContentsMargins(20, 16, 20, 16)
        action_row.setSpacing(16)
        self._btn_download = button("Скачать выбранное", "primary", action_bar, icon_name="download")
        self._btn_download.setEnabled(False)
        self._btn_download.clicked.connect(self._download_selected)
        action_row.addWidget(self._btn_download)
        self._selection_label = label("Отмечено: 0", "muted", action_bar)
        action_row.addWidget(self._selection_label)
        action_row.addStretch(1)
        left_layout.addWidget(action_bar)
        splitter.addWidget(left)

        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)
        self._seg = SegmentedTabs(["Превью", "Очередь"], right)
        self._queue_badge = self._seg.add_badge(1)
        self._queue_badge.setVisible(False)
        self._seg.refresh_badge(1)
        right_layout.addWidget(self._seg, 0, Qt.AlignmentFlag.AlignLeft)

        # tabs — стопка «Превью / Очередь» (API как у QTabWidget: currentWidget,
        # setCurrentIndex, currentChanged).
        self.tabs = QStackedWidget(right)
        self.preview_panel = PreviewPanel(self.tabs)
        self._make_glass(self.preview_panel)
        self.preview_panel.download_requested.connect(self._download_one)
        self.preview_panel.player.failed.connect(self._on_preview_failed)
        self.tabs.addWidget(self.preview_panel)
        self.queue_view = QueueView(self.tabs)
        self._make_glass(self.queue_view)
        self.queue_view.pause_requested.connect(self._engine_call(self.ctx.engine.pause))
        self.queue_view.resume_requested.connect(self._engine_call(self.ctx.engine.resume))
        self.queue_view.cancel_requested.connect(self._engine_call(self.ctx.engine.cancel))
        self.queue_view.remove_requested.connect(self._engine_call(self.ctx.engine.remove))
        self.queue_view.open_folder_requested.connect(self._open_download_dir)
        self.tabs.addWidget(self.queue_view)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.currentChanged.connect(self._seg.set_current)
        self._seg.changed.connect(self.tabs.setCurrentIndex)
        right_layout.addWidget(self.tabs, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)
        # Узкое и высокое окно (половина экрана): список сверху, превью снизу.
        # Узкое и низкое — оставляем рядом: столбцом обоим не хватит высоты.
        self._work_bp = Breakpoint(workspace, lambda w, h: w < 900 and h >= 640,
                                   lambda narrow: splitter.setOrientation(
                                       Qt.Orientation.Vertical if narrow else Qt.Orientation.Horizontal))
        splitter.setSizes([700, 560])
        work.addWidget(splitter)
        self.pages.addWidget(workspace)
        self._workspace = workspace

        # --- Соцсети: видео и звук по ссылке ---
        self.social_page = SocialPage(lambda: self.ctx.config.download_dir_path, self.pages)
        self.social_page.notice.connect(self._on_notice)
        self.social_page.notice.connect(self._notify_hidden)
        self.pages.addWidget(self.social_page)

        self._build_actions()
        self._build_statusbar(layout, root)
        self.setCentralWidget(root)
        self.show_start()

    @staticmethod
    def _make_glass(widget: QWidget) -> None:
        widget.setProperty("glass", True)
        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def _build_actions(self) -> None:
        self._act_link = QAction("Открыть ссылку", self)
        self._act_link.setShortcut(QKeySequence("Ctrl+O"))
        self._act_link.triggered.connect(self._open_link)
        self.addAction(self._act_link)

        # Действие «скачать выбранное» без кнопки в шапке: сама кнопка
        # живёт под деревом, где видно отметки.
        self._act_download = QAction("Скачать выбранное", self)
        self._act_download.setShortcut(QKeySequence("Ctrl+D"))
        self._act_download.triggered.connect(self._download_selected)
        self._act_download.setEnabled(False)
        self.addAction(self._act_download)

    def _build_statusbar(self, layout: QVBoxLayout, parent: QWidget) -> None:
        bar = QWidget(parent)
        bar.setObjectName("footerBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 6, 12, 6)
        row.setSpacing(16)
        self._status_label = QLabel("Готово", bar)
        self._status_label.setMinimumWidth(100)
        row.addWidget(self._status_label, 1)
        self._activity_icon = IconLabel("target", "text", 12, bar, stroke=2.4)
        self._activity_icon.setVisible(False)
        row.addWidget(self._activity_icon)
        self._activity = QLabel("", bar)
        row.addWidget(self._activity)
        log_btn = button("Журнал", "ghost", bar, icon_name="log", size="s")
        log_btn.setToolTip("Открыть папку журналов: errors.log — ошибки с подробностями, "
                           "noukocloud.log — всё, что делала программа")
        log_btn.clicked.connect(self._open_logs)
        row.addWidget(log_btn)
        layout.addWidget(bar)

    # --- экраны -----------------------------------------------------------
    def show_start(self) -> None:
        self.start_page.set_recent(self.ctx.config.recent_links)
        self.pages.setCurrentWidget(self.start_page)
        self.title_bar.set_field_visible(False)
        self.title_bar.set_active(None)

    def show_workspace(self) -> None:
        self.pages.setCurrentWidget(self._workspace)
        self.title_bar.set_field_visible(True)
        if self.title_bar.active() == "social":
            self.title_bar.set_active(None)

    def show_social(self) -> None:
        self.pages.setCurrentWidget(self.social_page)
        self.title_bar.set_field_visible(False)
        self.title_bar.set_active("social")
        self.social_page.link_edit.setFocus()

    def _forget_link(self, url: str) -> None:
        self.ctx.config.forget_link(url)
        self._save_config()
        self.start_page.set_recent(self.ctx.config.recent_links)

    def _save_config(self) -> None:
        try:
            self.ctx.config.save()
        except Exception:
            log.warning("Не удалось сохранить настройки", exc_info=True)

    def _apply_ui_prefs(self) -> None:
        """Анимации и звуки из «Настроек» — сразу, без перезапуска."""
        cfg = self.ctx.config
        sound.bank().enabled = bool(cfg.ui_sounds)
        sound.bank().set_volume(int(cfg.ui_volume))
        if theme.manager().motion != bool(cfg.ui_motion):
            theme.manager().motion = bool(cfg.ui_motion)
            motion().set_enabled(bool(cfg.ui_motion))
            theme.manager().set_theme(cfg.ui_theme)

    def _toggle_theme(self) -> None:
        sound.play("toggle")
        self.ctx.config.ui_theme = theme.manager().toggle()
        self._save_config()

    # --- окно без рамки ---------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        self._apply_native_styles()
        super().showEvent(event)

    def nativeEvent(self, event_type, message):  # noqa: N802
        result = self.frameless_native_event(event_type, message)
        if result is not None:
            return result
        return super().nativeEvent(event_type, message)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.WindowStateChange:
            self.title_bar.sync_max_button(self.isMaximized())
        super().changeEvent(event)

    # ------------------------------------------------------------------
    def _in_gui(self, handler):
        """Оборачивает колбэк Future так, чтобы он выполнился в GUI-потоке."""
        def callback(future) -> None:
            self._invoker.call.emit(lambda: handler(future))

        return callback

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
        future.add_done_callback(self._in_gui(self._on_startup_done))
        self._recover_downloads()

    def _on_startup_done(self, future) -> None:
        try:
            future.result()
        except Exception as exc:
            log.error("Ошибка запуска: %s", exc, exc_info=True)
            self._on_notice("error", "Не удалось запустить фоновые задачи. Перезапустите программу.")
            return
        # «Мой диск» каждого облака, где выполнен вход, открыт сразу.
        for pid in (ProviderId.YANDEX, ProviderId.GOOGLE):
            if self.ctx.oauth.is_logged_in(pid):
                self._open_my_drive_for(pid, interactive=False)

    def _recover_downloads(self) -> None:
        dest = self.ctx.config.download_dir_path
        future = self.ctx.runtime.submit(self.ctx.engine.recover(dest))
        future.add_done_callback(self._in_gui(self._after_recover))

    def _after_recover(self, future) -> None:
        try:
            restored = future.result()
        except Exception as exc:
            log.warning("Не удалось восстановить загрузки: %s", exc, exc_info=True)
            return
        if restored:
            self._on_notice(
                "info",
                f"Найдено незаконченных загрузок: {len(restored)}. Продолжаем с места обрыва.",
            )
            self.show_workspace()
            self.tabs.setCurrentIndex(1)

    # ------------------------------------------------------------------
    def _on_notice(self, level: str, text: str) -> None:
        if level == "error":
            # Что увидел пользователь — в журнал ошибок (подробности — строкой выше).
            log.error("Показано пользователю: %s", text)
            sound.play("error")
        self._status_label.setText(text)
        if level == "error":
            self._status_label.setProperty("error", True)
        else:
            self._status_label.setProperty("error", False)
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)

    def _on_snapshot(self, jobs) -> None:
        jobs = list(jobs)
        self.queue_view.sync(jobs)
        total, done, active = self.ctx.engine.stats()
        self.queue_view.set_summary(total, done, active)
        waiting = sum(1 for j in jobs if j.state.name in ("QUEUED", "RUNNING", "PAUSED"))
        self._queue_badge.setText(str(waiting))
        self._queue_badge.setVisible(waiting > 0)
        self._seg.refresh_badge(1)
        self._update_activity()

    def _on_progress(self, job) -> None:
        self.queue_view.update_job(job)
        self._update_activity()

    def _update_activity(self) -> None:
        """Справа внизу: «качается 2 · 18.4 МБ/с»."""
        try:
            jobs = self.ctx.engine.snapshot()
        except Exception:
            return
        running = [j for j in jobs if j.state.name == "RUNNING"]
        social = self.social_page.active_count()
        if self.tray is not None:
            parts = []
            if running:
                parts.append(f"облако: {len(running)}")
            if social:
                parts.append(f"соцсети: {social}")
            self.tray.set_activity("качается " + ", ".join(parts) if parts else "")
        if not running:
            self._activity.setText("")
            self._activity_icon.setVisible(False)
            return
        speed = sum(j.speed_bps for j in running)
        text = f"качается {len(running)}"
        if speed > 0:
            text += f" · {_human_size(int(speed))}/с"
        self._activity.setText(text)
        self._activity_icon.setVisible(True)

    def _on_finished(self, job) -> None:
        if job.state.name == "DONE":
            self._on_notice("info", f"Готово: {job.file.name}")
            # «Готово» звучит один раз — когда скачана вся очередь, а не на каждый файл.
            if not any(j.state.name in ("QUEUED", "RUNNING") for j in self.ctx.engine.snapshot()):
                sound.play("success")
                self._notify_hidden("info", "Все загрузки из облака готовы")
        elif job.state.name == "FAILED":
            self._on_notice("error", f"Файл не скачан: {job.error}")
            self._notify_hidden("error", f"Не скачан {job.file.name}: {job.error}")
        self._on_snapshot(self.ctx.engine.snapshot())

    def _on_logged_in(self, pid: str, name: str) -> None:
        self._on_notice("info", f"Вход выполнен: {name}")
        try:
            provider = ProviderId(pid)
        except ValueError:
            return
        self._open_my_drive_for(provider, interactive=False)

    # ------------------------------------------------------------------
    @staticmethod
    def _folder_key(file: CloudFile, ctx: BrowseContext) -> tuple:
        return (file.provider.value, file.source.value, ctx.public_key, file.file_id, file.path)

    def _load_children(self, file: CloudFile, ctx: BrowseContext, done) -> None:
        """Асинхронная загрузка содержимого папки (или готовое — из предзагрузки)."""
        cached = self._prefetched.pop(self._folder_key(file, ctx), None)
        if cached is not None and time.monotonic() - cached[0] < PREFETCH_TTL:
            future = cached[1]
        else:
            provider = self.ctx.registry.get(file.provider)
            future = self.ctx.runtime.submit(provider.list_children(ctx, file))
        future.add_done_callback(self._in_gui(lambda fut: self._deliver(fut, done, file.provider.value)))

    def _on_folder_hovered(self, file: CloudFile, ctx: BrowseContext) -> None:
        self._hover_folder = (file, ctx)
        self._hover_file = None
        self._hover_timer.start()

    def _prefetch_folder(self, file: CloudFile, ctx: BrowseContext) -> None:
        key = self._folder_key(file, ctx)
        old = self._prefetched.get(key)
        if old is not None and time.monotonic() - old[0] < PREFETCH_TTL:
            return
        # Старые и лишние предзагрузки забываем (память и свежесть списка).
        now = time.monotonic()
        for k in [k for k, (ts, _f) in self._prefetched.items() if now - ts >= PREFETCH_TTL]:
            del self._prefetched[k]
        while len(self._prefetched) >= 16:
            self._prefetched.pop(next(iter(self._prefetched)))
        provider = self.ctx.registry.get(file.provider)
        future = self.ctx.runtime.submit(provider.list_children(ctx, file))
        # Ошибку предзагрузки не показываем: при раскрытии папку спросят заново.
        future.add_done_callback(self._in_gui(
            lambda fut: (fut.cancelled() or fut.exception() is not None) and self._prefetched.pop(key, None)))
        self._prefetched[key] = (now, future)

    def _deliver(self, future, done, provider: str) -> None:
        try:
            files = future.result()
        except Exception as exc:
            log.warning("Не удалось загрузить папку: %s", exc, exc_info=True)
            self._on_notice("error", friendly_error(exc, provider=provider))
            done(None)   # None — ошибка: дерево даст раскрыть папку повторно
        else:
            done(files)

    # ------------------------------------------------------------------
    def _open_accounts(self) -> None:
        self.title_bar.set_active("accounts")
        dialog = AuthDialog(self.ctx.oauth, self.ctx.runtime, self)
        dialog.login_succeeded.connect(lambda pid, name: self._on_notice("info", f"Вход выполнен: {name}"))
        dialog.login_failed.connect(lambda pid, msg: self._on_notice("error", msg))
        dialog.exec()
        self._restore_nav()

    def _open_settings(self) -> None:
        self.title_bar.set_active("settings")
        dialog = SettingsDialog(self.ctx.config, self, cache_ops=_PreviewCacheOps(self))
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            self.ctx.oauth.reload_config()
            self._apply_ui_prefs()
            self._sync_close_tooltip()
            self._on_notice("info", "Настройки сохранены")
        self._restore_nav()

    def _restore_nav(self) -> None:
        self.title_bar.set_active("social" if self.pages.currentWidget() is self.social_page else None)

    def open_link_at_startup(self, url: str) -> None:
        """Открывает ссылку сразу после запуска (ключ --open)."""
        QTimer.singleShot(1500, lambda: self._open_url(url))

    def _open_link(self) -> None:
        """Ctrl+O — курсор в поле ссылки."""
        if self.pages.currentWidget() is self.start_page:
            edit = self.start_page.link_edit
        else:
            edit = self.title_bar.link_edit
        edit.setFocus()
        edit.selectAll()

    def _open_url(self, url: str) -> None:
        url = url.strip()
        if not url:
            return
        log.info("Открываем ссылку (как вставлена): %r", url)
        self._last_url = url
        self.title_bar.set_link(url)
        self.show_workspace()
        self._on_notice("info", "Открываем ссылку…")
        future = self.ctx.runtime.submit(self.ctx.registry.open_link(url))
        future.add_done_callback(self._in_gui(self._after_open))

    def _after_open(self, future) -> None:
        try:
            provider, ctx, items = future.result()
        except Exception as exc:
            log.warning("Ссылка не открылась: %s", exc, exc_info=True)
            self._on_notice("error", friendly_error(exc))
            return
        title = ctx.display_name or provider.display_name
        if self._last_url:
            self.ctx.config.remember_link(self._last_url, f"{provider.display_name} · {title}")
            self._save_config()
            self.start_page.set_recent(self.ctx.config.recent_links)
        self.tree.set_subtitle(f"{provider.display_name} · {title}")
        root = self.tree.add_root(title, ctx, provider.display_name, provider=provider.id)
        self.tree.set_children(root, items)
        self.tree.expand_item(root)
        if items:
            sound.play("open")
            self._on_notice(
                "info",
                f"Открыто: {title}. Отметьте нужные файлы и нажмите «Скачать выбранное».",
            )
        else:
            self._on_notice("warn", f"«{title}» открыта, но внутри ничего нет.")

    def _open_my_drive(self) -> None:
        """«Мой диск»: рабочий экран и свои диски всех облаков, где выполнен вход."""
        self.show_workspace()
        logged = [pid for pid in (ProviderId.YANDEX, ProviderId.GOOGLE) if self.ctx.oauth.is_logged_in(pid)]
        if not logged:
            self._on_notice("warn", "Чтобы видеть «Мой диск», войдите в аккаунт.")
            self._open_accounts()
            logged = [pid for pid in (ProviderId.YANDEX, ProviderId.GOOGLE)
                      if self.ctx.oauth.is_logged_in(pid)]
        for pid in logged:
            drive = self.tree.drive(pid)
            if not drive.data(LOADED_ROLE):     # ещё не загружен и не грузится
                self._open_my_drive_for(pid, interactive=False)
            self.tree.expand_item(drive)
        if logged:
            self.tree.set_subtitle("Мой диск")

    def _open_my_drive_for(self, pid: ProviderId, interactive: bool = True) -> None:
        """Загружает «Мой диск» облака в его постоянный пункт дерева.

        interactive=False — автоматически (при запуске, после входа):
        без окна «Аккаунты».
        """
        if not self.ctx.oauth.is_logged_in(pid):
            if interactive:
                self._on_notice("warn", "Сначала войдите в аккаунт (кнопка «Аккаунты»).")
                self._open_accounts()
            if not self.ctx.oauth.is_logged_in(pid):
                self.tree.drive_failed(pid, "войдите в аккаунт («Аккаунты») и раскройте снова")
                return
        self.tree.set_drive_loading(pid)
        ctx = BrowseContext(source=SourceKind.OAUTH, root_path="/", display_name="Мой диск")
        provider = self.ctx.registry.get(pid)
        future = self.ctx.runtime.submit(provider.list_children(ctx, None))
        future.add_done_callback(self._in_gui(lambda fut: self._after_my_drive(pid, fut)))

    def _after_my_drive(self, pid: ProviderId, future) -> None:
        try:
            items = future.result()
        except Exception as exc:
            log.warning("Не удалось открыть «Мой диск»: %s", exc, exc_info=True)
            self._on_notice("error", friendly_error(exc, provider=pid.value))
            self.tree.drive_failed(pid, "не загрузилось — сверните и раскройте снова")
            return
        ctx = BrowseContext(source=SourceKind.OAUTH, root_path="/", display_name="Мой диск")
        root = self.tree.add_root("Мой диск", ctx, provider=pid)
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
        if self.tree.loading():
            # Содержимое отмеченных папок ещё приходит из облака — иначе часть
            # файлов молча не попала бы в загрузку.
            self._on_notice("warn", "Список файлов в папках ещё загружается — "
                                    "нажмите «Скачать выбранное» через секунду.")
            return
        items = self.tree.checked_downloads()
        log.info("Скачать выбранное: отмечено файлов — %d", len(items))
        if not items:
            self._on_notice("warn", "Отметьте хотя бы один файл.")
            return
        # Отмеченная папка скачивается папкой (с вложенными), отдельные файлы —
        # прямо в папку загрузок.
        root = self.ctx.config.download_dir_path
        groups: dict[tuple[str, ...], list[CloudFile]] = {}
        for file, path in items:
            groups.setdefault(path, []).append(file)

        async def enqueue_all():
            jobs = []
            for path, files in groups.items():
                dest = root.joinpath(*(_safe_name(part) for part in path)) if path else root
                jobs += await self.ctx.engine.enqueue(files, dest)
            return jobs

        future = self.ctx.runtime.submit(enqueue_all())
        future.add_done_callback(self._in_gui(self._after_enqueue))

    def _download_one(self, file: CloudFile) -> None:
        log.info("Скачать один файл: %s", file.name)
        self._download_files([file])

    def _download_files(self, files: list[CloudFile]) -> None:
        dest = self.ctx.config.download_dir_path
        future = self.ctx.runtime.submit(self.ctx.engine.enqueue(files, dest))
        future.add_done_callback(self._in_gui(self._after_enqueue))

    def _after_enqueue(self, future) -> None:
        try:
            jobs = future.result()
        except Exception as exc:
            log.warning("Не удалось поставить в очередь: %s", exc, exc_info=True)
            self._on_notice("error", "Не удалось начать загрузку. Подробности в журнале.")
            return
        if jobs:
            sound.play("queue")
        self.show_workspace()
        self.tabs.setCurrentIndex(1)
        self._on_notice("info", f"В очереди: {len(jobs)} файл(ов). Куда: {self.ctx.config.download_dir}")
    # ------------------------------------------------------------------
    def _on_file_selected(self, file: CloudFile) -> None:
        """Курсор встал на файл: показываем превью, если вкладка «Превью» открыта."""
        if self.tabs.currentWidget() is not self.preview_panel:
            return
        self._pending_preview = file
        self._preview_timer.start()
        # Соседей готовим заранее: следующий файл откроется без ожидания облака.
        self.ctx.preview.warm(self.tree.neighbor_files())

    def _on_file_hovered(self, file: CloudFile) -> None:
        self._hover_file = file
        self._hover_folder = None
        self._hover_timer.start()

    def _warm_hovered(self) -> None:
        file, self._hover_file = self._hover_file, None
        folder, self._hover_folder = self._hover_folder, None
        if file is not None:
            self.ctx.preview.warm([file])
        if folder is not None:
            self._prefetch_folder(*folder)

    def _on_tab_changed(self, _index: int) -> None:
        # Вернулись на «Превью», а курсор стоит на другом файле — показываем его.
        if self.tabs.currentWidget() is self.preview_panel:
            file = self.tree.current_file()
            if file is not None:
                self._show_preview(file)

    def _preview_pending(self) -> None:
        file, self._pending_preview = self._pending_preview, None
        if file is not None:
            self._show_preview(file)

    def _activate_preview(self, file: CloudFile) -> None:
        """Двойной клик / Enter: открываем вкладку «Превью» и показываем файл."""
        self._preview_timer.stop()
        self._pending_preview = None
        self.tabs.setCurrentWidget(self.preview_panel)
        self._show_preview(file)

    @staticmethod
    def _file_key(file: CloudFile) -> tuple:
        return (file.provider.value, file.source.value, file.file_id, file.path)

    def _show_preview(self, file: CloudFile) -> None:
        key = self._file_key(file)
        if key == self._preview_key and self._preview_token:
            return  # этот файл уже в превью — не перезапускаем его с начала
        self.preview_panel.stop()  # закрываем соединения прежнего файла
        if self._preview_token:
            self.ctx.preview.drop_session(self._preview_token)
        session = self.ctx.preview.add_session(file)
        self._preview_token = session.token
        self._preview_key = key

        message = ""
        if not file.can_download:
            message = "Владелец запретил скачивание этого файла — превью недоступно."
        elif session.media_kind == "image" and file.size > IMAGE_PREVIEW_LIMIT:
            message = (
                f"Изображение слишком большое для превью ({_human_size(file.size)}). "
                "Скачайте файл, чтобы посмотреть его."
            )
        token, size, preview = session.token, file.size, self.ctx.preview
        self.preview_panel.show_file(
            file,
            preview.media_url(token),
            session.media_kind,
            autoplay=self.ctx.config.preview_autoplay,
            message=message,
            # Сколько уже скачано впереди позиции — для плавной буферизации.
            buffer_probe=lambda fraction: preview.buffer_state(token, int(fraction * size)),
        )

    def _on_preview_failed(self, message: str, format_problem: bool) -> None:
        """Плеер не открыл файл. Если дело в формате и есть ffmpeg — делаем облегчённую копию."""
        token = self._preview_token
        session = self.ctx.preview.get_session(token) if token else None
        log.warning("Превью не открылось: %s (файл: %s, формат: %s)", message,
                    session.file.name if session is not None else "?",
                    "да" if format_problem else "нет")
        if session is None:
            return
        if (
            format_problem
            and session.media_kind in ("video", "audio")
            and self.ctx.ffmpeg.available
            and token not in self._proxy_tried
        ):
            self._proxy_tried.add(token)
            self.preview_panel.player.show_message(
                "Плеер не открыл этот формат — готовим облегчённую копию (первые "
                f"{self.ctx.config.preview_proxy_seconds} с)…"
            )
            self._build_proxy(session)
            return
        self._on_notice("error", f"Превью: {message}")

    def _build_proxy(self, session) -> None:
        token = session.token
        # Источник — наш же сервер превью: он сам ходит в облако с нужными
        # токенами, поэтому ffmpeg не нужны заголовки авторизации.
        source = self.ctx.preview.media_url(token)
        out = self.ctx.preview.cache_dir / f"{token}.mp4"

        async def job() -> bool:
            ok = await self.ctx.ffmpeg.build_proxy(
                source, None, out,
                max_height=self.ctx.config.preview_max_height,
                seconds=self.ctx.config.preview_proxy_seconds,
            )
            if ok:
                session.proxy_path = out
            return ok

        future = self.ctx.runtime.submit(job())
        future.add_done_callback(self._in_gui(lambda fut: self._after_proxy(fut, token)))

    def _after_proxy(self, future, token: str) -> None:
        try:
            ok = future.result()
        except Exception as exc:
            log.info("Облегчённая копия не удалась: %s", exc)
            ok = False
        if token != self._preview_token:
            return  # пользователь уже смотрит другой файл
        session = self.ctx.preview.get_session(token)
        if not ok or session is None:
            self.preview_panel.player.show_message(
                "Этот формат нельзя показать в превью. Скачайте файл, чтобы посмотреть его."
            )
            return
        self.preview_panel.player.play(
            self.ctx.preview.proxy_url(token), "video", session.file.name,
            autoplay=self.ctx.config.preview_autoplay,
        )
        self._on_notice("info", "Превью готово (облегчённая копия).")

    # ------------------------------------------------------------------
    def _open_download_dir(self) -> None:
        path = str(self.ctx.config.download_dir_path)
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_logs(self) -> None:
        # Сначала — журнал ошибок: там видно, где и почему что-то сломалось.
        errors = errors_log_path()
        path = str(errors if errors.exists() else logs_dir() / "noukocloud.log")
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", path])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Работа в фоне
    # ------------------------------------------------------------------
    def _setup_background(self) -> None:
        if not Tray.available():
            log.info("Системный трей недоступен — закрытие окна завершает программу")
            return
        self.tray = Tray(self)
        self.tray.open_requested.connect(self.bring_to_front)
        self.tray.downloads_requested.connect(self._open_download_dir)
        self.tray.quit_requested.connect(self.quit_app)
        self.tray.show()
        self._sync_close_tooltip()
        # Подсказка в трее обновляется и тогда, когда окно скрыто.
        self._tray_timer = QTimer(self)
        self._tray_timer.setInterval(2000)
        self._tray_timer.timeout.connect(self._update_activity)
        self._tray_timer.start()

    def _sync_close_tooltip(self) -> None:
        in_bg = self.tray is not None and bool(self.ctx.config.run_in_background)
        self.title_bar.set_close_tooltip("Закрыть окно (программа останется в фоне)" if in_bg
                                         else "Закрыть программу")

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def handle_instance_message(self, text: str) -> None:
        """Команда от повторного запуска программы."""
        if text == "quit":
            log.info("Выход по команде --quit")
            self.quit_app()
            return
        self.bring_to_front()
        if text.startswith("open "):
            url = text[5:].strip()
            if url:
                self._open_url(url)

    def quit_app(self) -> None:
        """Полный выход (меню значка у часов)."""
        self._quitting = True
        self.close()

    def _notify_hidden(self, level: str, text: str) -> None:
        # Всплывающее сообщение — только когда окна не видно.
        if self.tray is None or self.isVisible() and not self.isMinimized():
            return
        if level == "error":
            self.tray.notify("Ошибка загрузки", text, error=True)
        elif text.startswith(("Скачано", "Все загрузки")):
            self.tray.notify("Готово", text)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.tray is not None and self.ctx.config.run_in_background and not self._quitting:
            # Окно прячем, программа работает дальше (значок у часов).
            event.ignore()
            self.hide()
            if not self.ctx.config.background_hint_shown:
                self.ctx.config.background_hint_shown = True
                self._save_config()
                self.tray.notify(
                    "НоуКо Cloud работает в фоне",
                    "Загрузки продолжаются. Открыть окно — щелчок по значку у часов, "
                    "выйти — «Выход» в его меню.",
                )
            return
        if self.tray is not None:
            self.tray.hide()
        try:
            self.social_page.cancel_all()
        except Exception:
            pass
        # Сначала закрываем плеер — иначе его соединения держат сервер превью.
        try:
            self.preview_panel.stop()
        except Exception:
            pass
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
        # Окно могло быть скрыто в трей: последнего окна нет — выходим явно.
        app = QApplication.instance()
        if app is not None:
            app.quit()
