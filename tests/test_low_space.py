"""Мало места на диске: активные загрузки встают на паузу, но не теряются."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.config import AppConfig
from app.core.diskspace import LOW_SPACE_BYTES
from app.core.models import CloudFile, DownloadJob, EntryKind, JobState, ProviderId
from app.downloader.engine import DownloadEngine


def _job(name: str, dest: Path, state: JobState) -> DownloadJob:
    file = CloudFile(
        provider=ProviderId.YANDEX,
        file_id=name,
        path=name,
        name=name,
        kind=EntryKind.FILE,
        size=100,
    )
    job = DownloadJob(file=file, dest_dir=dest)
    job.state = state
    job.pause_event.set()
    return job


class TestLowSpace(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bus = mock.Mock()
        self.engine = DownloadEngine(
            AppConfig(), None, None, None, self.bus)  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, job: DownloadJob) -> DownloadJob:
        self.engine._jobs[job.job_id] = job
        self.engine._order.append(job.job_id)
        return job

    def test_low_space_pauses_active_and_notifies_once(self) -> None:
        running = self._add(_job("run.mp4", self.root, JobState.RUNNING))
        queued = self._add(_job("queued.mp4", self.root, JobState.QUEUED))
        done = self._add(_job("done.mp4", self.root, JobState.DONE))

        usage = mock.Mock(free=LOW_SPACE_BYTES - 1)
        with mock.patch("app.downloader.engine.shutil.disk_usage", return_value=usage):
            self.engine._on_progress(running)
            self.engine._on_progress(queued)

        for job in (running, queued):
            self.assertIs(job.state, JobState.PAUSED)
            self.assertFalse(job.pause_event.is_set(), "передача должна остановиться")
        self.assertIs(done.state, JobState.DONE)
        notices = [call.args for call in self.bus.notice.emit.call_args_list]
        self.assertEqual(len(notices), 1, "предупреждение не должно повторяться каждые 0.3 с")
        self.assertIn("Мало места", notices[0][1])

    def test_space_recovered_resets_flag(self) -> None:
        job = self._add(_job("run.mp4", self.root, JobState.PAUSED))
        self.engine._space_low = True
        usage = mock.Mock(free=LOW_SPACE_BYTES + 1)
        with mock.patch("app.downloader.engine.shutil.disk_usage", return_value=usage):
            self.engine._check_free_space(job, 100.0)
        self.assertFalse(self.engine._space_low)

    async def test_worker_does_not_start_paused_job(self) -> None:
        job = self._add(_job("paused.mp4", self.root, JobState.PAUSED))
        self.engine._queue.put_nowait(job.job_id)
        self.engine._queued.add(job.job_id)
        self.engine._run_job = mock.AsyncMock()  # type: ignore[method-assign]

        async def scenario() -> None:
            worker = asyncio.create_task(self.engine._worker())
            await asyncio.sleep(0.05)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        await scenario()
        self.engine._run_job.assert_not_awaited()
        self.assertIs(job.state, JobState.PAUSED)
