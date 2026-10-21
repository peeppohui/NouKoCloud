"""Вкладка «Соцсети»: ссылка на видео -> скачать видео или только звук."""

from __future__ import annotations

import itertools
import logging
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRegularExpression, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..preview.player import PreviewPlayer
from ..social import downloader as social
from . import sound
from .widgets import (IconLabel, SegmentedTabs, StarsBackground, button, glass, icon_button, label,
                      responsive_box)

log = logging.getLogger(__name__)

SITES = "YouTube, VK, Rutube, Дзен, OK, TikTok, Instagram, X (Twitter), Vimeo, Twitch и сотни других"


def human_size(size: float) -> str:
    value = float(size or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"


def quality_text(height: int) -> str:
    if not height:
        return "Лучшее качество"
    names = {2160: "4K", 1440: "2K"}
    return f"{height}p" + (f" ({names[height]})" if height in names else "")


class _Bridge(QObject):
    """Сигналы из рабочих потоков в интерфейс (Qt сам перенесёт их в GUI-поток)."""

    probed = Signal(int, object)          # номер запроса, MediaInfo
    probe_failed = Signal(int, str)
    preview_ready = Signal(int, object)   # номер запроса, PreviewSession
    preview_failed = Signal(int, str)     # номер запроса, адрес обложки
    progress = Signal(int, object)        # номер загрузки, Progress
    done = Signal(int, str)
    failed = Signal(int, str)


class _JobRow(QFrame):
    def __init__(self, page: "SocialPage", job_id: int, title: str, kind: str) -> None:
        super().__init__()
        self.setProperty("job", True)
        self.job_id = job_id
        self.path: Path | None = None
        self.cancel = threading.Event()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)
        holder = QLabel(self)
        holder.setProperty("jobIcon", True)
        holder.setFixedSize(36, 36)
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(IconLabel("music" if kind == "audio" else "video", "text", 18, holder),
                     0, Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(holder)
        middle = QVBoxLayout()
        middle.setSpacing(6)
        self.title = QLabel(title, self)
        self.title.setMinimumWidth(60)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        middle.addWidget(self.title)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(5)
        middle.addWidget(self.bar)
        self.status = label("Готовим загрузку…", "hint", self)
        middle.addWidget(self.status)
        lay.addLayout(middle, 1)
        self.btn_open = icon_button("folder", "Показать файл в папке", self, size=36)
        self.btn_open.clicked.connect(self._reveal)
        self.btn_open.setVisible(False)
        self.btn_cancel = icon_button("close", "Отменить", self, size=36, icon_size=16)
        self.btn_cancel.clicked.connect(self._cancel)
        lay.addWidget(self.btn_open)
        lay.addWidget(self.btn_cancel)
        self._page = page

    def _cancel(self) -> None:
        if self.path is not None or self.status.property("finished"):
            self._page.remove_row(self)
            return
        # Строка отменяется сразу; загрузка в фоне останавливается сама
        # (соединение, которое ещё устанавливается, прервать нельзя) и
        # убирает за собой временные файлы.
        self.cancel.set()
        self.finish(None, "Отменено")

    def _reveal(self) -> None:
        if self.path is None:
            return
        if sys.platform == "win32":
            import subprocess

            subprocess.Popen(["explorer", "/select,", str(self.path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent)))

    def set_progress(self, p: social.Progress) -> None:
        if p.stage == "process":
            self.bar.setRange(0, 0)          # «бегущая» полоса: склейка/конвертация
            self.status.setText("Обрабатываем (склеиваем видео и звук)…")
            return
        if p.percent < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(p.percent * 10))
        parts = [f"{p.percent:.0f}%" if p.percent >= 0 else "качается"]
        if p.speed > 0:
            parts.append(f"{human_size(p.speed)}/с")
        if p.eta > 0:
            parts.append(f"ещё ~{int(p.eta // 60)} мин" if p.eta >= 60 else f"ещё ~{int(p.eta)} с")
        self.status.setText(" · ".join(parts))

    def finish(self, path: Path | None, error: str = "") -> None:
        self.bar.setRange(0, 1000)
        self.status.setProperty("finished", True)
        self.btn_cancel.setToolTip("Убрать из списка")
        if path is not None:
            self.path = path
            self.bar.setValue(1000)
            self.status.setText(f"Готово · {path.name}")
            self.status.setToolTip(str(path))
            self.btn_open.setVisible(True)
        else:
            self.bar.setValue(0)
            self.status.setText(error)
            self.status.setToolTip(error)
            self.status.setProperty("error", True)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)


#: Физические клавиши I и O (коды клавиш Windows) — одинаковы в любой раскладке.
VK_I, VK_O = 0x49, 0x4F
#: Ш и Щ — что дают эти клавиши в русской раскладке (на случай без кода клавиши).
CYR_SHA, CYR_SHCHA = 0x428, 0x429


class _MarkKeys(QObject):
    """Ловит I / O для вкладки «Соцсети» в любой раскладке.

    Обычные сочетания Qt (QShortcut) сравнивают символ, а в русской раскладке
    та же клавиша даёт «Ш»/«Щ» — поэтому смотрим на саму клавишу.
    """

    def __init__(self, page: "SocialPage") -> None:
        super().__init__(page)
        self._page = page
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.KeyPress:
            return False
        action = self._page.mark_key_action(event)
        if action is None:
            return False
        action()
        return True


class SocialPage(StarsBackground):
    """Ссылка из соцсети -> предпросмотр и сведения -> видео / звук / фрагмент -> скачать."""

    notice = Signal(str, str)        # уровень, текст — в строку состояния окна

    def __init__(self, dest_dir, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dest_dir = dest_dir             # функция -> Path (папка загрузок из настроек)
        self._bridge = _Bridge(self)
        self._bridge.probed.connect(self._on_probed)
        self._bridge.probe_failed.connect(self._on_probe_failed)
        self._bridge.preview_ready.connect(self._on_preview_ready)
        self._bridge.preview_failed.connect(self._on_preview_failed)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.done.connect(self._on_done)
        self._bridge.failed.connect(self._on_failed)
        self._ids = itertools.count(1)
        self._probe_id = 0
        self._info: social.MediaInfo | None = None
        self._session = None                  # PreviewSession текущего ролика
        self._rows: dict[int, _JobRow] = {}

        outer = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)

        # --- левая колонка: ссылка, предпросмотр, выбор (прокручивается)
        left = glass(self)
        self._left = left
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(left)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(24, 22, 24, 24)
        lay.setSpacing(16)
        scroll.setWidget(content)
        left_lay.addWidget(scroll)

        lay.addWidget(label("соцсети", "h3", content))
        lay.addWidget(label(f"Вставьте ссылку на видео или пост — {SITES}. "
                            "Можно скачать видео, только звук или фрагмент.", "muted", content, wrap=True))

        form = QFrame(content)
        form.setObjectName("linkForm")
        form.setFixedHeight(52)
        row = QHBoxLayout(form)
        row.setContentsMargins(16, 0, 4, 0)
        row.setSpacing(8)
        row.addWidget(IconLabel("link", "text", 18, form))
        self.link_edit = QLineEdit(form)
        self.link_edit.setPlaceholderText("https://www.youtube.com/watch?v=…")
        self.link_edit.setAccessibleName("Ссылка на видео")
        self.link_edit.returnPressed.connect(self.find)
        row.addWidget(self.link_edit, 1)
        self.btn_find = button("Найти", "primary", form, size="m")
        self.btn_find.clicked.connect(self.find)
        row.addWidget(self.btn_find)
        form.setStyleSheet("QFrame#linkForm { border-radius: 26px; }")
        lay.addWidget(form)

        self.status = label("", "muted", content, wrap=True)
        self.status.setVisible(False)
        # Пустая строка статуса не должна занимать место.
        _set_text = self.status.setText
        self.status.setText = lambda text: (_set_text(text), self.status.setVisible(bool(text)))
        lay.addWidget(self.status)

        # карточка найденного видео
        self.card = QFrame(content)
        self.card.setProperty("card", True)
        card = QVBoxLayout(self.card)
        card.setContentsMargins(18, 18, 18, 18)
        card.setSpacing(14)

        self.player = PreviewPlayer(self.card)
        card.addWidget(self.player)
        self.card.installEventFilter(self)    # высота плеера — по ширине карточки (16:9)

        self.title = label("", "name", self.card, wrap=True)
        card.addWidget(self.title)
        self.meta = label("", "muted", self.card, wrap=True)
        card.addWidget(self.meta)

        self.mode = SegmentedTabs(["Видео", "Только звук"], self.card)
        self.mode.changed.connect(self._sync_mode)
        card.addWidget(self.mode, 0, Qt.AlignmentFlag.AlignLeft)

        options = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        options.setSpacing(12)
        self.quality = QComboBox(self.card)
        self.quality.setAccessibleName("Качество видео")
        self.quality.setMinimumWidth(180)
        options.addWidget(self.quality)
        self.audio_format = QComboBox(self.card)
        self.audio_format.setAccessibleName("Формат звука")
        self.audio_format.addItem("MP3 · 320 кбит/с", "mp3")
        self.audio_format.addItem("M4A · без перекодирования", "m4a")
        self.audio_format.addItem("WAV · без сжатия", "wav")
        self.audio_format.setMinimumWidth(220)
        options.addWidget(self.audio_format)
        options.addStretch(1)
        card.addLayout(options)
        self.for_editing = QCheckBox("Для монтажа: H.264 в MP4 (на YouTube — до 1080p)", self.card)
        self.for_editing.setToolTip("Файл сразу откроется в Premiere, DaVinci и After Effects без перекодирования. "
                                    "Снимите галочку, чтобы взять максимальное качество (4K на YouTube — "
                                    "только в кодеках VP9/AV1).")
        self.for_editing.setChecked(True)
        card.addWidget(self.for_editing)

        # --- фрагмент: вручную (поля) или на предпросмотре (I / O)
        self.clip = QCheckBox("Только фрагмент", self.card)
        self.clip.setToolTip("Скачать не всё видео, а кусок с указанного времени до указанного. "
                             "На предпросмотре: I — начало, O — конец")
        card.addWidget(self.clip)
        self.clip_box = QWidget(self.card)
        clip_lay = QVBoxLayout(self.clip_box)
        clip_lay.setContentsMargins(28, 0, 0, 0)
        clip_lay.setSpacing(10)
        validator = QRegularExpressionValidator(QRegularExpression(r"[0-9:.,]{0,12}"), self)
        self.clip_start = QLineEdit(self.clip_box)
        self.clip_end = QLineEdit(self.clip_box)
        self.btn_in = button("Начало · I", "outline", self.clip_box, size="plain")
        self.btn_out = button("Конец · O", "outline", self.clip_box, size="plain")
        self.btn_in.setToolTip("Начало фрагмента — текущий момент предпросмотра (клавиша I)")
        self.btn_out.setToolTip("Конец фрагмента — текущий момент предпросмотра (клавиша O)")
        self.btn_in.clicked.connect(self.mark_in)
        self.btn_out.clicked.connect(self.mark_out)
        times = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        times.setSpacing(16)
        for edit, name, hint, caption, btn in (
                (self.clip_start, "Начало фрагмента", "0:00", "с", self.btn_in),
                (self.clip_end, "Конец фрагмента", "1:30", "до", self.btn_out)):
            edit.setValidator(validator)
            edit.setPlaceholderText(hint)
            edit.setAccessibleName(name)
            edit.setToolTip("Время: секунды (90), минуты:секунды (1:30) или часы:минуты:секунды (1:02:30). "
                            "Можно с десятыми: 1:30.5")
            edit.setFixedWidth(110)
            edit.textChanged.connect(self._sync_clip)
            edit.editingFinished.connect(lambda e=edit: self._seek_to_field(e))
            group = QHBoxLayout()
            group.setSpacing(8)
            group.addWidget(label(caption, None, self.clip_box))
            group.addWidget(edit)
            group.addWidget(btn)
            times.addLayout(group)
        times.addStretch(1)
        clip_lay.addLayout(times)
        actions = QHBoxLayout()
        actions.setSpacing(12)
        self.btn_play_clip = button("Проиграть фрагмент", "soft", self.clip_box, icon_name="play", size="plain")
        self.btn_play_clip.clicked.connect(self._play_clip)
        actions.addWidget(self.btn_play_clip)
        self.clip_info = label("", "muted", self.clip_box)
        actions.addWidget(self.clip_info, 1)
        clip_lay.addLayout(actions)
        self.clip_precise = QCheckBox("Точно по кадрам (края перекодируются — чуть дольше)", self.clip_box)
        self.clip_precise.setToolTip("Без галочки фрагмент режется по ближайшим ключевым кадрам: быстрее, "
                                     "но начало может сдвинуться на несколько секунд раньше.")
        self.clip_precise.setChecked(True)
        clip_lay.addWidget(self.clip_precise)
        clip_lay.addWidget(label("Вручную — впишите время в поля. На предпросмотре — поставьте видео на нужный "
                                 "момент и нажмите I (начало) или O (конец). Пробел — пауза, ← → — 5 с, "
                                 "Shift + ← → — 1 с.", "hint", self.clip_box, wrap=True))
        card.addWidget(self.clip_box)
        self.clip_box.setVisible(False)
        self.clip.toggled.connect(self.clip_box.setVisible)
        self.clip.toggled.connect(lambda _on: self._sync_clip())

        self.btn_download = button("Скачать", "primary", self.card, icon_name="download")
        self.btn_download.clicked.connect(self.start_download)
        card.addWidget(self.btn_download, 0, Qt.AlignmentFlag.AlignLeft)
        self.card.setVisible(False)
        lay.addWidget(self.card)
        lay.addStretch(1)
        lay.addWidget(label("Скачивайте только то, на что у вас есть права: свои видео, исходники "
                            "по договорённости с автором или материалы со свободной лицензией.",
                            "hint", content, wrap=True))
        outer.addWidget(left, 5)

        # --- правая колонка: загрузки
        right = glass(self)
        self._right = right
        rl = QVBoxLayout(right)
        rl.setContentsMargins(16, 20, 16, 16)
        rl.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(label("загрузки", "h3", right))
        top.addStretch(1)
        rl.addLayout(top)
        list_scroll = QScrollArea(right)
        list_scroll.setWidgetResizable(True)
        list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self._list = QVBoxLayout(body)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(8)
        self._empty = label("Здесь появятся скачанные из соцсетей видео и звук.", "muted", body, wrap=True)
        self._list.addWidget(self._empty)
        self._list.addStretch(1)
        list_scroll.setWidget(body)
        rl.addWidget(list_scroll, 1)
        open_dir = button("Открыть папку загрузок", "outline", right, icon_name="folder")
        open_dir.clicked.connect(self._open_dir)
        rl.addWidget(open_dir, 0, Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(right, 4)

        # --- подстройка под размер окна
        # По краям — распорки: на очень широком экране колонки по центру.
        outer.insertStretch(0, 0)
        outer.addStretch(0)

        def narrow_page(narrow: bool) -> None:
            # Узкое окно: загрузки под основной колонкой.
            outer.setStretch(1, 3)
            outer.setStretch(2, 1 if narrow else 2)
            right.setMinimumHeight(170 if narrow else 0)
            left.setMaximumWidth(16777215 if narrow else 1040)
            right.setMaximumWidth(16777215 if narrow else 600)

        self._page_bp = responsive_box(outer, self, 1050, narrow_page)
        self._options_bp = responsive_box(options, self.card, 520)
        self._times_bp = responsive_box(times, self.card, 560)

        # Клавиши I и O — в любой раскладке (см. _MarkKeys).
        self._mark_keys = _MarkKeys(self)
        self._sync_mode(0)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.card and event.type() == QEvent.Type.Resize:
            # Сцена 16:9 по ширине карточки, но не выше половины окна.
            width = self.card.width() - 36
            stage = max(170, min(int(width * 9 / 16), int(self.height() * 0.36)))
            self.player.setFixedHeight(stage + 96)       # + панель управления и строка статуса
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    def find(self) -> None:
        url = self.link_edit.text().strip()
        if not url:
            self.status.setText("Вставьте ссылку на видео.")
            return
        self._probe_id = next(self._ids)
        probe_id = self._probe_id
        self.btn_find.setEnabled(False)
        self.status.setText("Ищем видео по ссылке…")
        self.card.setVisible(False)
        self._close_preview()

        def work() -> None:
            try:
                info = social.probe(url)
            except social.SocialError as exc:
                self._bridge.probe_failed.emit(probe_id, str(exc))
                return
            except Exception as exc:  # pragma: no cover - на всякий случай
                log.warning("Соцсети: поиск не удался", exc_info=True)
                self._bridge.probe_failed.emit(probe_id, social.friendly(exc))
                return
            self._bridge.probed.emit(probe_id, info)
            if not info.raw:
                return
            try:
                from ..social.preview import PreviewSession

                session = PreviewSession(info.raw)
            except Exception as exc:
                log.info("Соцсети: предпросмотр недоступен: %s", exc)
                self._bridge.preview_failed.emit(probe_id, info.thumbnail)
                return
            self._bridge.preview_ready.emit(probe_id, session)

        threading.Thread(target=work, name="social-probe", daemon=True).start()

    def _close_preview(self) -> None:
        self.player.stop()
        if self._session is not None:
            session, self._session = self._session, None
            threading.Thread(target=session.close, name="social-preview-close", daemon=True).start()

    def _on_probed(self, probe_id: int, info: social.MediaInfo) -> None:
        if probe_id != self._probe_id:
            return
        self.btn_find.setEnabled(True)
        self._info = info
        self.status.setText("")
        self.title.setText(info.title)
        meta = [part for part in (info.site, info.uploader, info.duration_text) if part]
        self.meta.setText(" · ".join(meta))
        self.player.show_message("Готовим предпросмотр…")
        self.quality.clear()
        for height in social.quality_choices(info):
            self.quality.addItem(quality_text(height), height)
        if not info.has_video:
            self.mode.set_current(1)
            self._sync_mode(1)
        self.mode.buttons[0].setEnabled(info.has_video)
        # Новое видео — фрагмент по умолчанию «всё видео».
        self.clip_start.setText("0:00")
        self.clip_end.setText(social.format_time(info.duration) if info.duration else "")
        self._sync_clip()
        self.card.setVisible(True)
        sound.play("open")

    def _on_preview_ready(self, probe_id: int, session) -> None:
        if probe_id != self._probe_id or self._info is None:
            threading.Thread(target=session.close, daemon=True).start()   # уже ищем другое видео
            return
        self._session = session
        self.player.play(session.video_url, session.kind, self._info.title, autoplay=False,
                         audio_url=session.audio_url)
        self._sync_clip()

    def _on_preview_failed(self, probe_id: int, thumbnail: str) -> None:
        if probe_id != self._probe_id:
            return
        if thumbnail:
            self.player.show_image(thumbnail)
        else:
            self.player.show_message("Предпросмотр для этой ссылки недоступен — фрагмент можно задать вручную.")

    def _on_probe_failed(self, probe_id: int, message: str) -> None:
        if probe_id != self._probe_id:
            return
        self.btn_find.setEnabled(True)
        self.status.setText(message)
        self.notice.emit("error", f"Соцсети: {message}")

    def _sync_mode(self, index: int) -> None:
        video = index == 0
        self.quality.setVisible(video)
        self.for_editing.setVisible(video)
        self.audio_format.setVisible(not video)

    # --- фрагмент -------------------------------------------------------
    def mark_key_action(self, event):
        """Что делать по нажатию: mark_in / mark_out / None (не наша клавиша)."""
        if not self.isVisible() or event.isAutoRepeat():
            return None
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                                | Qt.KeyboardModifier.MetaModifier):
            return None
        vk, key = event.nativeVirtualKey(), event.key()
        if vk == VK_I or key in (Qt.Key.Key_I, CYR_SHA):
            action = self.mark_in
        elif vk == VK_O or key in (Qt.Key.Key_O, CYR_SHCHA):
            action = self.mark_out
        else:
            return None
        focus = QApplication.focusWidget()
        if focus is not None:
            if focus.window() is not self.window():
                return None                  # открыт диалог (настройки и т. п.)
            if isinstance(focus, QLineEdit) and focus not in (self.clip_start, self.clip_end):
                return None                  # печатают ссылку — буквы нужны там
        return action

    def _media_ready(self) -> bool:
        return self._session is not None and self.player.duration_ms() > 0

    def mark_in(self) -> None:
        """Начало фрагмента = текущий момент предпросмотра (клавиша I)."""
        if not self._mark_allowed():
            return
        position = self.player.position_ms() / 1000
        self.clip.setChecked(True)
        self.clip_start.setText(social.format_time(position, precise=True))
        end = self._parse_or_none(self.clip_end.text())
        if end is None or end <= position:
            # Как в монтажных программах: начало позже конца — конец сбрасываем на конец ролика.
            self.clip_end.setText(social.format_time(self._duration(), precise=True))

    def mark_out(self) -> None:
        """Конец фрагмента = текущий момент предпросмотра (клавиша O)."""
        if not self._mark_allowed():
            return
        position = self.player.position_ms() / 1000
        self.clip.setChecked(True)
        self.clip_end.setText(social.format_time(position, precise=True))
        start = self._parse_or_none(self.clip_start.text())
        if start is None or start >= position:
            self.clip_start.setText("0:00")

    def _mark_allowed(self) -> bool:
        if self._info is None or not self.card.isVisibleTo(self):
            return False
        if not self._media_ready():
            self.status.setText("Дождитесь предпросмотра — или впишите время в поля вручную.")
            return False
        return True

    def _duration(self) -> float:
        media = self.player.duration_ms() / 1000
        return media or (self._info.duration if self._info is not None else 0)

    @staticmethod
    def _parse_or_none(text: str) -> float | None:
        try:
            return social.parse_time(text)
        except ValueError:
            return None

    def _seek_to_field(self, edit: QLineEdit) -> None:
        # Вписали время вручную — показываем этот кадр в предпросмотре.
        value = self._parse_or_none(edit.text())
        if value is not None and self._media_ready():
            self.player.seek_to(int(value * 1000))

    def _play_clip(self) -> None:
        value = self._clip_range()
        if isinstance(value, str) or not self._media_ready():
            return
        self.player.play_range(int(value[0] * 1000), int(value[1] * 1000))
        self.player.setFocus()

    def _clip_range(self) -> tuple[float, float] | str:
        """(начало, конец) в секундах или текст ошибки."""
        try:
            start = social.parse_time(self.clip_start.text() or "0")
        except ValueError:
            return "Начало — время вида 1:30 или 90"
        try:
            end = social.parse_time(self.clip_end.text())
        except ValueError:
            return "Конец — время вида 2:15 или 135"
        duration = self._info.duration if self._info is not None else 0
        if duration and start >= duration:
            return f"Видео длится {social.format_time(duration)}"
        if duration and end > duration + 1:
            return f"Видео длится {social.format_time(duration)} — конец позже"
        if duration:
            end = min(end, duration)          # «до конца»: лишняя секунда от округления — не ошибка
        if end <= start:
            return "Конец должен быть позже начала"
        return start, end

    def _sync_clip(self) -> None:
        if not self.clip.isChecked():
            self.btn_download.setEnabled(True)
            self.player.set_marks(None, None)
            return
        value = self._clip_range()
        ok = not isinstance(value, str)
        self.btn_download.setEnabled(ok)
        self.btn_play_clip.setEnabled(ok)
        if ok:
            start, end = value
            self.clip_info.setText(f"длина {social.format_time(end - start, precise=True)}")
            self.player.set_marks(int(start * 1000), int(end * 1000))
        else:
            self.clip_info.setText(value)
            self.player.set_marks(None, None)
        self.clip_info.setProperty("error", not ok)
        self.clip_info.style().unpolish(self.clip_info)
        self.clip_info.style().polish(self.clip_info)

    # ------------------------------------------------------------------
    def start_download(self) -> None:
        info = self._info
        if info is None:
            return
        audio = not self.mode.buttons[0].isChecked()
        mode = "audio" if audio else "video"
        height = int(self.quality.currentData() or 0)
        fmt = self.audio_format.currentData() or "mp3"
        editing = self.for_editing.isChecked()
        section = None
        precise = self.clip_precise.isChecked()
        title = info.title
        if self.clip.isChecked():
            value = self._clip_range()
            if isinstance(value, str):
                self.status.setText(value)
                return
            section = value
            title = f"{info.title} · {social.format_time(value[0])}–{social.format_time(value[1])}"
        dest = Path(self._dest_dir())
        job_id = next(self._ids)
        row = _JobRow(self, job_id, title, mode)
        self._rows[job_id] = row
        self._empty.setVisible(False)
        self._list.insertWidget(0, row)
        sound.play("queue")
        bridge = self._bridge
        cancel = row.cancel

        def work() -> None:
            try:
                path = social.download(info.url, dest, mode, height, fmt, editing,
                                       on_progress=lambda p: bridge.progress.emit(job_id, p), cancel=cancel,
                                       section=section, precise=precise)
            except social.Cancelled:
                bridge.failed.emit(job_id, "Отменено")
                return
            except social.SocialError as exc:
                bridge.failed.emit(job_id, str(exc))
                return
            except Exception as exc:  # pragma: no cover
                log.warning("Соцсети: загрузка не удалась", exc_info=True)
                bridge.failed.emit(job_id, social.friendly(exc))
                return
            bridge.done.emit(job_id, str(path))

        threading.Thread(target=work, name=f"social-{job_id}", daemon=True).start()

    def _on_progress(self, job_id: int, progress) -> None:
        row = self._rows.get(job_id)
        if row is not None and not row.cancel.is_set():
            row.set_progress(progress)

    def _on_done(self, job_id: int, path: str) -> None:
        row = self._rows.get(job_id)
        if row is not None:
            row.finish(Path(path))
        sound.play("success")
        self.notice.emit("info", f"Скачано: {Path(path).name}")

    def _on_failed(self, job_id: int, message: str) -> None:
        row = self._rows.get(job_id)
        if row is not None and row.cancel.is_set():
            return                   # уже показано «Отменено»
        if row is not None:
            row.finish(None, message)
        if message != "Отменено":
            self.notice.emit("error", f"Соцсети: {message}")

    def remove_row(self, row: _JobRow) -> None:
        self._rows.pop(row.job_id, None)
        self._list.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._empty.setVisible(not self._rows)

    def active_count(self) -> int:
        return sum(1 for row in self._rows.values()
                   if not row.cancel.is_set() and not row.status.property("finished"))

    def cancel_all(self) -> None:
        for row in self._rows.values():
            row.cancel.set()
        self._close_preview()

    def _open_dir(self) -> None:
        path = Path(self._dest_dir())
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
