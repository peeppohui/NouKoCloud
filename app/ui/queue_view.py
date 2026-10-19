"""Панель очереди загрузок."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.models import DownloadJob, JobState

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


class QueueView(QWidget):
    pause_requested = Signal(str)
    resume_requested = Signal(str)
    cancel_requested = Signal(str)
    remove_requested = Signal(str)
    open_folder_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[str, int] = {}
        self._bars: dict[str, QProgressBar] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        title = QLabel("Очередь загрузок", self)
        title.setProperty("heading", True)
        top.addWidget(title)
        top.addStretch(1)
        self._summary = QLabel("", self)
        self._summary.setProperty("muted", True)
        top.addWidget(self._summary)
        layout.addLayout(top)

        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(["Файл", "Размер", "Прогресс", "Скорость", "Состояние"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setColumnWidth(2, 170)
        layout.addWidget(self._table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._btn_pause = QPushButton("Пауза", self)
        self._btn_resume = QPushButton("Продолжить", self)
        self._btn_cancel = QPushButton("Отменить", self)
        self._btn_remove = QPushButton("Убрать из списка", self)
        self._btn_open = QPushButton("Открыть папку", self)
        for button in (self._btn_pause, self._btn_resume, self._btn_cancel,
                       self._btn_remove, self._btn_open):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._btn_pause.clicked.connect(lambda: self._for_selected(self.pause_requested))
        self._btn_resume.clicked.connect(lambda: self._for_selected(self.resume_requested))
        self._btn_cancel.clicked.connect(lambda: self._for_selected(self.cancel_requested))
        self._btn_remove.clicked.connect(lambda: self._for_selected(self.remove_requested))
        self._btn_open.clicked.connect(self.open_folder_requested)

        self._update_buttons()

    # ------------------------------------------------------------------
    def _for_selected(self, signal: Signal) -> None:
        for job_id in self.selected_job_ids():
            signal.emit(job_id)

    def selected_job_ids(self) -> list[str]:
        rows = sorted({index.row() for index in self._table.selectedIndexes()})
        result = []
        for row in rows:
            item = self._table.item(row, 0)
            if item is not None:
                result.append(item.data(Qt.ItemDataRole.UserRole))
        return result

    def _update_buttons(self) -> None:
        has = bool(self.selected_job_ids())
        self._btn_pause.setEnabled(has)
        self._btn_resume.setEnabled(has)
        self._btn_cancel.setEnabled(has)
        self._btn_remove.setEnabled(has)

    # ------------------------------------------------------------------
    def sync(self, jobs: list[DownloadJob]) -> None:
        """Полная перерисовка списка."""
        self._table.setUpdatesEnabled(False)
        try:
            wanted = {job.job_id for job in jobs}
            for job_id in [jid for jid in self._rows if jid not in wanted]:
                row = self._rows.pop(job_id)
                self._bars.pop(job_id, None)
                self._table.removeRow(row)
                self._reindex()
            for job in jobs:
                if job.job_id in self._rows:
                    continue
                row = self._table.rowCount()
                self._table.insertRow(row)
                self._rows[job.job_id] = row

                bar = QProgressBar(self)
                bar.setRange(0, 100)
                bar.setTextVisible(False)
                bar.setFixedHeight(10)
                self._table.setCellWidget(row, 2, bar)
                self._bars[job.job_id] = bar

                for column in (0, 1, 3, 4):
                    item = QTableWidgetItem()
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, job.job_id)
                    self._table.setItem(row, column, item)
            for job in jobs:
                self.update_job(job, full=True)
        finally:
            self._table.setUpdatesEnabled(True)
        self._update_buttons()

    def _reindex(self) -> None:
        self._rows = {}
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is None:
                continue
            job_id = item.data(Qt.ItemDataRole.UserRole)
            if job_id:
                self._rows[job_id] = row

    def update_job(self, job: DownloadJob, full: bool = False) -> None:
        row = self._rows.get(job.job_id)
        if row is None:
            return
        name_item = self._table.item(row, 0)
        if name_item is None:
            return
        name_item.setText(job.file.name)
        name_item.setData(Qt.ItemDataRole.UserRole, job.job_id)
        name_item.setToolTip(job.final_path.as_posix())

        self._table.item(row, 1).setText(human_size(job.total_bytes or job.file.size))

        bar = self._bars.get(job.job_id)
        if bar is not None:
            bar.setValue(job.percent)

        speed = job.speed_bps
        self._table.item(row, 3).setText(human_speed(speed) if job.state is JobState.RUNNING else "—")

        state_item = self._table.item(row, 4)
        state_item.setText(job.error or STATE_TEXT.get(job.state, ""))
        if job.state is JobState.FAILED:
            state_item.setForeground(Qt.GlobalColor.red)
        elif job.state is JobState.DONE:
            state_item.setForeground(Qt.GlobalColor.darkGreen)
        else:
            state_item.setForeground(Qt.GlobalColor.gray)

    def set_summary(self, total: int, done: int, active: int) -> None:
        self._summary.setText(f"всего {total} · готово {done} · качается {active}")
