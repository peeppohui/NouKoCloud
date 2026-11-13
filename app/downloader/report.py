"""CSV-отчёт по очереди загрузок для монтажёров и продюсеров."""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..core.diagnostics import sanitize_text
from ..core.models import DownloadJob, JobState

CSV_COLUMNS = (
    "file_name", "provider", "source", "cloud_path", "local_path",
    "size_bytes", "downloaded_bytes", "state", "state_label", "error",
    "md5_expected", "md5_actual", "started_at", "finished_at",
    "duration_sec", "speed_avg_bps",
)

STATE_LABELS = {
    JobState.QUEUED: "В очереди",
    JobState.RUNNING: "Качается",
    JobState.PAUSED: "Пауза",
    JobState.DONE: "Готово",
    JobState.FAILED: "Ошибка",
    JobState.CANCELLED: "Отменено",
    JobState.SKIPPED: "Уже скачан",
}


def default_report_path(download_dir: Path | str, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return Path(download_dir).expanduser() / f"NoukoCloud-report-{stamp}.csv"


def _iso(timestamp: float | None) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def _row(job: DownloadJob) -> list[object]:
    duration = 0.0
    if job.finished_at:
        duration = max(0.0, job.finished_at - job.created_at)
    speed = (job.downloaded_bytes / duration) if duration > 0 else 0
    return [
        job.file.name,
        job.file.provider.value,
        job.file.source.value,
        job.file.path,
        str(job.final_path),
        int(job.total_bytes or job.file.size or 0),
        int(job.downloaded_bytes or 0),
        job.state.value,
        STATE_LABELS.get(job.state, job.state.value),
        sanitize_text(job.error or ""),
        job.file.md5 or "",
        job.md5_actual or "",
        _iso(job.created_at),
        _iso(job.finished_at),
        f"{duration:.1f}",
        int(speed),
    ]


def write_report(jobs: Iterable[DownloadJob], path: Path | str) -> Path:
    """Пишет отчёт атомарно: недописанный CSV не должен выглядеть готовым."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    try:
        with temp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";", lineterminator="\r\n")
            writer.writerow(CSV_COLUMNS)
            for job in jobs:
                writer.writerow(_row(job))
        os.replace(temp, target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    return target
