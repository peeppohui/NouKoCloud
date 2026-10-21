"""Очередь загрузок: карточка на каждый файл, прогресс по частям."""

from __future__ import annotations

import logging

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.models import DownloadJob, JobState
from . import theme
from .widgets import IconLabel, button, icon_button, label

log = logging.getLogger(__name__)

STATE_TEXT = {
    JobState.QUEUED: "В очереди",
    JobState.RUNNING: "Качается",
    JobState.PAUSED: "Пауза",
    JobState.DONE: "Готово",
    JobState.FAILED: "Ошибка",
    JobState.CANCELLED: "Отменено",
    JobState.SKIPPED: "Уже скачан",
}
STATE_ICON = {
    JobState.QUEUED: "clock",
    JobState.RUNNING: "target",
    JobState.PAUSED: "pause",
    JobState.DONE: "check",
    JobState.FAILED: "alert",
    JobState.CANCELLED: "close",
    JobState.SKIPPED: "check",
}
FINISHED = (JobState.DONE, JobState.FAILED, JobState.CANCELLED, JobState.SKIPPED)
MAX_PARTS = 8


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


def human_speed(bps: float) -> str:
    if bps <= 0:
        return "—"
    return f"{human_size(int(bps))}/с"


def human_eta(seconds: float) -> str:
    if seconds <= 0:
        return ""
    if seconds < 60:
        return "ещё ~1 мин" if seconds > 30 else "ещё несколько секунд"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"ещё ~{minutes} мин"
    return f"ещё ~{minutes // 60} ч {minutes % 60:02d} мин"


def job_color_role(job: DownloadJob) -> str:
    """Цвет задания — имя цвета палитры (чтобы перекрашиваться при смене темы)."""
    if job.state is JobState.FAILED:
        return "err"
    if job.state in (JobState.DONE, JobState.SKIPPED):
        return "ok"
    if job.state is JobState.RUNNING:
        return "text"
    return "muted"


def part_fractions(job: DownloadJob) -> list[float]:
    """Доля скачанного в каждой части (не больше MAX_PARTS полосок)."""
    if job.state in (JobState.DONE, JobState.SKIPPED):
        count = max(1, min(MAX_PARTS, len(job.segments) or 1))
        return [1.0] * count
    segments = sorted(job.segments, key=lambda s: s.start)
    if not segments:
        total = job.total_bytes or job.file.size
        return [min(1.0, job.downloaded_bytes / total) if total else 0.0]
    if len(segments) <= MAX_PARTS:
        return [min(1.0, s.received / max(1, s.total)) for s in segments]
    # Частей больше, чем полосок: склеиваем соседние.
    groups: list[float] = []
    per = len(segments) / MAX_PARTS
    for i in range(MAX_PARTS):
        chunk = segments[int(i * per):int((i + 1) * per)] or segments[-1:]
        got = sum(s.received for s in chunk)
        size = sum(s.total for s in chunk)
        groups.append(min(1.0, got / max(1, size)))
    return groups


def status_line(job: DownloadJob) -> tuple[str, str]:
    """(состояние, подробности) — как в макете: «Качается · 42% · 12.6 МБ/с · ещё ~5 мин»."""
    if job.state is JobState.FAILED:
        return (f"Файл не скачан: {job.error}" if job.error else "Ошибка"), ""
    state = STATE_TEXT.get(job.state, "")
    parts: list[str] = []
    if job.state is JobState.RUNNING:
        parts.append(f"{job.percent}%")
        if job.speed_bps > 0:
            parts.append(human_speed(job.speed_bps))
            total = job.total_bytes or job.file.size
            eta = human_eta((total - job.downloaded_bytes) / job.speed_bps) if total else ""
            if eta:
                parts.append(eta)
    elif job.state is JobState.PAUSED:
        parts.append(f"{job.percent}%")
        parts.append("докачается с места остановки")
    elif job.state in (JobState.DONE, JobState.SKIPPED):
        parts.append(str(job.dest_dir))
    return state, ("".join(" · " + p for p in parts))


class PartsBar(QWidget):
    """Полоски прогресса по частям файла."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fractions: list[float] = [0.0]
        self._role = "text"
        self.setFixedHeight(5)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_state(self, fractions: list[float], role: str) -> None:
        if fractions != self._fractions or role != self._role:
            self._fractions = fractions or [0.0]
            self._role = role
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        palette = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        count = len(self._fractions)
        gap = 3.0
        width = (self.width() - gap * (count - 1)) / count
        h = float(self.height())
        track = theme.qcolor(palette.raised)
        fill = theme.qcolor(getattr(palette, self._role))
        for i, fraction in enumerate(self._fractions):
            x = i * (width + gap)
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, 0, width, h), h / 2, h / 2)
            painter.fillPath(path, track)
            if fraction > 0:
                painter.save()
                painter.setClipPath(path)
                painter.fillRect(QRectF(x, 0, width * fraction, h), fill)
                painter.restore()
        painter.end()


class JobCard(QFrame):
    def __init__(self, view: "QueueView", job_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.job_id = job_id
        self._view = view
        self._state: JobState | None = None
        self.setProperty("job", True)
        grid = QHBoxLayout(self)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setSpacing(12)

        self._icon = IconLabel("clock", "muted", 18, self)
        holder = QLabel(self)
        holder.setProperty("jobIcon", True)
        holder.setFixedSize(36, 36)
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(holder, 0, Qt.AlignmentFlag.AlignVCenter)

        middle = QVBoxLayout()
        middle.setSpacing(7)
        top = QHBoxLayout()
        top.setSpacing(12)
        self._name = QLabel("", self)
        self._name.setMinimumWidth(60)
        self._name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(self._name, 1)
        self._size = label("", "muted", self)
        top.addWidget(self._size)
        middle.addLayout(top)
        self._bar = PartsBar(self)
        middle.addWidget(self._bar)
        self._line = QLabel("", self)
        self._line.setProperty("role", "hint")
        self._line.setTextFormat(Qt.TextFormat.RichText)
        self._line.setMinimumWidth(60)
        self._line.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        middle.addWidget(self._line)
        grid.addLayout(middle, 1)

        actions = QHBoxLayout()
        actions.setSpacing(4)
        self._btn_pause = icon_button("pause", "Пауза", self, size=36, icon_size=14)
        self._btn_resume = icon_button("play", "Продолжить", self, size=36, icon_size=16)
        self._btn_open = icon_button("folder", "Открыть папку", self, size=36)
        self._btn_x = icon_button("close", "Отменить", self, size=36, icon_size=16)
        self._btn_pause.clicked.connect(lambda: view.pause_requested.emit(self.job_id))
        self._btn_resume.clicked.connect(lambda: view.resume_requested.emit(self.job_id))
        self._btn_open.clicked.connect(view.open_folder_requested)
        self._btn_x.clicked.connect(self._on_x)
        for btn in (self._btn_pause, self._btn_resume, self._btn_open, self._btn_x):
            actions.addWidget(btn)
        grid.addLayout(actions)

    def _on_x(self) -> None:
        if self._state in FINISHED:
            self._view.remove_requested.emit(self.job_id)
        else:
            self._view.cancel_requested.emit(self.job_id)

    def update_job(self, job: DownloadJob) -> None:
        role = job_color_role(job)
        color = getattr(theme.current(), role)
        if job.state is not self._state:
            self._state = job.state
            self._icon.set_icon(STATE_ICON.get(job.state, "clock"), role)
            self._btn_pause.setVisible(job.state in (JobState.RUNNING, JobState.QUEUED))
            self._btn_resume.setVisible(job.state in (JobState.PAUSED, JobState.FAILED, JobState.CANCELLED))
            self._btn_open.setVisible(job.state in (JobState.DONE, JobState.SKIPPED))
            finished = job.state in FINISHED
            self._btn_x.setToolTip("Убрать из списка" if finished else "Отменить")
            self._btn_x.setAccessibleName(self._btn_x.toolTip())
        self._name.setText(job.file.name)
        self._name.setToolTip(job.final_path.as_posix())
        self._size.setText(human_size(job.total_bytes or job.file.size))
        self._bar.set_state(part_fractions(job), role)
        state, details = status_line(job)
        self._line.setText(
            f'<span style="color:{color}">{_escape(state)}</span>{_escape(details)}'
        )
        self._line.setToolTip(state + details)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class QueueView(QWidget):
    pause_requested = Signal(str)
    resume_requested = Signal(str)
    cancel_requested = Signal(str)
    remove_requested = Signal(str)
    open_folder_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards: dict[str, JobCard] = {}
        self._jobs: dict[str, DownloadJob] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(12)

        top = QHBoxLayout()
        top.setContentsMargins(4, 0, 4, 0)
        title = label("очередь", "h3", self)
        top.addWidget(title, 0, Qt.AlignmentFlag.AlignBottom)
        top.addStretch(1)
        self._summary = label("", "muted", self)
        top.addWidget(self._summary, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(top)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self._list = QVBoxLayout(body)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(8)
        self._empty = label("Очередь пуста. Отметьте файлы слева и нажмите «Скачать выбранное».",
                            "muted", body, wrap=True)
        self._list.addWidget(self._empty)
        self._list.addStretch(1)
        self._scroll.setWidget(body)
        layout.addWidget(self._scroll, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._btn_open = button("Открыть папку", "outline", self, icon_name="folder")
        self._btn_open.clicked.connect(self.open_folder_requested)
        self._btn_clear = button("Убрать готовые", "ghost", self)
        self._btn_clear.setToolTip("Убрать из списка скачанные, отменённые и неудачные файлы")
        self._btn_clear.clicked.connect(self._remove_finished)
        buttons.addWidget(self._btn_open)
        buttons.addWidget(self._btn_clear)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        theme.manager().changed.connect(self._on_theme)

    # ------------------------------------------------------------------
    def selected_job_ids(self) -> list[str]:
        """Совместимость: в карточках действия у каждого файла свои."""
        return []

    def job_count(self) -> int:
        return len(self._cards)

    def _remove_finished(self) -> None:
        for job_id, job in list(self._jobs.items()):
            if job.state in FINISHED:
                self.remove_requested.emit(job_id)

    def _on_theme(self, _palette) -> None:
        for job_id, card in self._cards.items():
            job = self._jobs.get(job_id)
            if job is not None:
                card.update_job(job)

    # ------------------------------------------------------------------
    def sync(self, jobs: list[DownloadJob]) -> None:
        """Полная перерисовка списка."""
        wanted = {job.job_id for job in jobs}
        for job_id in [jid for jid in self._cards if jid not in wanted]:
            card = self._cards.pop(job_id)
            self._jobs.pop(job_id, None)
            self._list.removeWidget(card)
            card.hide()
            card.deleteLater()
        for job in jobs:
            if job.job_id not in self._cards:
                card = JobCard(self, job.job_id, self._scroll.widget())
                self._cards[job.job_id] = card
                self._list.insertWidget(self._list.count() - 1, card)
            self.update_job(job, full=True)
        self._empty.setVisible(not self._cards)
        self._btn_clear.setEnabled(any(j.state in FINISHED for j in self._jobs.values()))

    def update_job(self, job: DownloadJob, full: bool = False) -> None:
        card = self._cards.get(job.job_id)
        if card is None:
            return
        self._jobs[job.job_id] = job
        card.update_job(job)

    def set_summary(self, total: int, done: int, active: int) -> None:
        self._summary.setText(f"всего {total} · готово {done} · качается {active}")
