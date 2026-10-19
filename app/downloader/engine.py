"""Менеджер загрузок: очередь, параллельность, пауза, возобновление."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import replace
from pathlib import Path

from ..core.bus import Bus
from ..core.config import AppConfig
from ..core.errors import friendly_error
from ..core.models import CloudFile, DownloadJob, JobState
from ..net.http_client import HttpClient
from ..net.metadata_cache import MetadataCache
from ..providers.registry import ProviderRegistry
from .state import find_states, load_state, save_state, state_dest
from .transfer import Cancelled, Transfer, TransferContext

log = logging.getLogger(__name__)

PROGRESS_INTERVAL = 0.3      # как часто обновлять интерфейс
STATE_SAVE_INTERVAL = 2.0    # как часто сохранять состояние на диск

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(name: str) -> str:
    """Имя файла, безопасное для Windows."""
    cleaned = _FORBIDDEN.sub("_", name or "").strip().rstrip(". ")
    if not cleaned:
        cleaned = "file"
    return cleaned[:180]


class DownloadEngine:
    def __init__(
        self,
        config: AppConfig,
        http: HttpClient,
        registry: ProviderRegistry,
        cache: MetadataCache,
        bus: Bus,
    ) -> None:
        self._config = config
        self._http = http
        self._registry = registry
        self._cache = cache
        self._bus = bus

        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._jobs: dict[str, DownloadJob] = {}
        self._order: list[str] = []
        self._workers: list[asyncio.Task] = []
        self._file_sem = asyncio.Semaphore(max(1, config.max_parallel_files))
        self._stream_sem = asyncio.Semaphore(max(2, config.max_parallel_files * config.max_parallel_segments))
        self._closing = False
        self._last_emit = 0.0
        self._last_save = 0.0
        self._running: set[str] = set()
        self._speed_state: dict[str, tuple[float, int]] = {}

    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._workers:
            return
        self._closing = False
        for index in range(max(1, self._config.max_parallel_files)):
            self._workers.append(asyncio.create_task(self._worker(), name=f"dl-{index}"))
        log.info("Движок загрузок запущен (%d потоков)", len(self._workers))

    async def stop(self) -> None:
        self._closing = True
        for job in self._jobs.values():
            job.cancel_event.set()
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    # ------------------------------------------------------------------
    async def enqueue(self, files: list[CloudFile], dest_dir: Path) -> list[DownloadJob]:
        """Ставит файлы в очередь. Возвращает созданные задания."""
        created: list[DownloadJob] = []
        for file in files:
            safe_file = replace(file, name=_safe_name(file.name))
            job = DownloadJob(file=safe_file, dest_dir=self._dest_for(safe_file, dest_dir))
            job.pause_event.set()
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            created.append(job)
            self._queue.put_nowait(job.job_id)
        log.info("В очередь добавлено файлов: %d", len(created))
        self._emit_snapshot()
        await self.start()
        return created

    @staticmethod
    def _dest_for(file: CloudFile, root: Path) -> Path:
        """Сохраняет структуру папок облака внутри папки загрузок."""
        rel = (file.path or file.name).replace("\\", "/").strip("/")
        parts = [part for part in rel.split("/") if part and part not in (".", "..")]
        if len(parts) > 1:
            return root.joinpath(*parts[:-1])
        return root

    async def recover(self, dest_dir: Path) -> list[DownloadJob]:
        """Поднимает незаконченные загрузки после перезапуска программы."""
        restored: list[DownloadJob] = []
        for state_path in find_states(dest_dir):
            state = load_state(state_path)
            if not state:
                continue
            file, total, segments = state
            target = state_dest(state_path) or dest_dir
            job = DownloadJob(file=file, dest_dir=target)
            job.total_bytes = total
            job.segments = segments
            job.downloaded_bytes = sum(seg.received for seg in segments)
            job.pause_event.set()
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._queue.put_nowait(job.job_id)
            restored.append(job)
        if restored:
            log.info("Восстановлено незаконченных загрузок: %d", len(restored))
            self._emit_snapshot()
            await self.start()
        return restored

    # ------------------------------------------------------------------
    # Управление заданием выполняется в потоке event loop: asyncio.Event
    # нельзя трогать из другого потока — иначе ожидающий не проснётся.
    # ------------------------------------------------------------------
    async def pause(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job and job.state in (JobState.QUEUED, JobState.RUNNING):
            job.pause_event.clear()
            job.state = JobState.PAUSED
            self._emit_snapshot()

    async def resume(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job and job.state is JobState.PAUSED:
            job.pause_event.set()
            job.state = JobState.RUNNING if job_id in self._running else JobState.QUEUED
            self._emit_snapshot()

    async def cancel(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.cancel_event.set()
        job.pause_event.set()
        if job.state in (JobState.QUEUED, JobState.PAUSED):
            job.state = JobState.CANCELLED
            job.finished_at = time.time()
        self._emit_snapshot()

    async def remove(self, job_id: str) -> None:
        """Убирает задание из списка (файл на диске не трогаем)."""
        job = self._jobs.pop(job_id, None)
        if job is None:
            return
        job.cancel_event.set()
        job.pause_event.set()
        if job.job_id in self._order:
            self._order.remove(job.job_id)
        self._emit_snapshot()

    async def clear_finished(self) -> None:
        for job_id, job in list(self._jobs.items()):
            if job.state in (JobState.DONE, JobState.SKIPPED, JobState.CANCELLED, JobState.FAILED):
                await self.remove(job_id)

    # ------------------------------------------------------------------
    async def _worker(self) -> None:
        while not self._closing:
            try:
                job_id = await self._queue.get()
            except asyncio.CancelledError:
                return
            job = self._jobs.get(job_id)
            if job is None:
                self._queue.task_done()
                continue
            async with self._file_sem:
                if job.state is JobState.CANCELLED:
                    self._queue.task_done()
                    continue
                await self._run_job(job)
            self._queue.task_done()

    async def _run_job(self, job: DownloadJob) -> None:
        job.state = JobState.RUNNING
        job.attempts += 1
        job.error = None
        self._running.add(job.job_id)
        self._emit_snapshot()

        ctx = TransferContext(
            http=self._http,
            registry=self._registry,
            config=self._config,
            streams_semaphore=self._stream_sem,
            on_progress=self._on_progress,
        )
        transfer = Transfer(ctx)

        try:
            ok = await transfer.run(job)
        except Cancelled:
            job.state = JobState.CANCELLED
            job.finished_at = time.time()
            log.info("Отменено: %s", job.file.name)
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            raise
        except Exception as exc:
            job.state = JobState.FAILED
            job.error = friendly_error(exc, provider=job.file.provider.value)
            job.finished_at = time.time()
            log.warning("Не скачано %s: %s", job.file.name, exc, exc_info=True)
        else:
            if ok:
                job.state = JobState.DONE
            else:
                job.state = JobState.FAILED
            job.finished_at = time.time()

        self._emit_snapshot()
        self._bus.job_finished.emit(job)
        self._running.discard(job.job_id)

    # ------------------------------------------------------------------
    def _on_progress(self, job: DownloadJob) -> None:
        now = time.monotonic()
        self._update_speed(job, now)
        if now - self._last_emit >= PROGRESS_INTERVAL:
            self._last_emit = now
            self._bus.job_progress.emit(job)
        if now - self._last_save >= STATE_SAVE_INTERVAL:
            self._last_save = now
            try:
                save_state(job)
            except Exception as exc:
                log.debug("Не удалось сохранить состояние: %s", exc)

    def _update_speed(self, job: DownloadJob, now: float) -> None:
        """Скорость считаем скользящим окном в 3 секунды."""
        previous_time, previous_bytes = self._speed_state.get(job.job_id, (now, job.downloaded_bytes))
        elapsed = now - previous_time
        if elapsed >= 0.5:
            speed = (job.downloaded_bytes - previous_bytes) / elapsed
            job.speed_bps = max(0.0, job.speed_bps * 0.6 + speed * 0.4)
            self._speed_state[job.job_id] = (now, job.downloaded_bytes)
        else:
            self._speed_state.setdefault(job.job_id, (now, job.downloaded_bytes))

    def _emit_snapshot(self) -> None:
        self._bus.jobs_snapshot.emit(self.snapshot())

    def snapshot(self) -> list[DownloadJob]:
        return [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]

    def stats(self) -> tuple[int, int, int]:
        jobs = self.snapshot()
        total = len(jobs)
        done = sum(1 for job in jobs if job.state in (JobState.DONE, JobState.SKIPPED))
        active = sum(1 for job in jobs if job.state is JobState.RUNNING)
        return total, done, active
