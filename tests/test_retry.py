"""Повтор упавших загрузок: только FAILED, без дублей и потери прогресса."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import AppConfig  # noqa: E402
from app.core.models import (  # noqa: E402
    CloudFile,
    DownloadJob,
    EntryKind,
    JobState,
    ProviderId,
    Segment,
)
from app.downloader.engine import DownloadEngine  # noqa: E402
from app.ui.queue_view import QueueView  # noqa: E402


def _file(name: str, file_id: str = "1") -> CloudFile:
    return CloudFile(
        provider=ProviderId.YANDEX,
        file_id=file_id,
        path=name,
        name=name,
        kind=EntryKind.FILE,
        size=100,
    )


def _job(name: str, dest: Path, state: JobState = JobState.FAILED) -> DownloadJob:
    job = DownloadJob(file=_file(name, name), dest_dir=dest)
    job.state = state
    job.error = "старая ошибка"
    job.finished_at = 123.0
    job.attempts = 7
    return job


class TestRetryFailed(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.engine = DownloadEngine(
            AppConfig(), None, None, None, mock.Mock())  # type: ignore[arg-type]
        # Рабочие не нужны: проверяем состояние очереди, а не сеть.
        self.engine._spawn_workers = mock.Mock()  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, job: DownloadJob) -> DownloadJob:
        self.engine._jobs[job.job_id] = job
        self.engine._order.append(job.job_id)
        return job

    async def test_retry_failed_only_touches_failed(self) -> None:
        failed_1 = self._add(_job("first.mp4", self.root))
        done = self._add(_job("done.mp4", self.root, JobState.DONE))
        failed_2 = self._add(_job("second.mp4", self.root))
        paused = self._add(_job("paused.mp4", self.root, JobState.PAUSED))

        count = await self.engine.retry_failed()

        self.assertEqual(count, 2)
        for job in (failed_1, failed_2):
            self.assertIs(job.state, JobState.QUEUED)
            self.assertIsNone(job.error)
            self.assertIsNone(job.finished_at)
            self.assertEqual(job.attempts, 0)
        self.assertIs(done.state, JobState.DONE)
        self.assertIs(paused.state, JobState.PAUSED)
        self.assertEqual(self.engine._queue.qsize(), 2)

    async def test_second_retry_does_not_duplicate_queue_entries(self) -> None:
        job = self._add(_job("one.mp4", self.root))

        self.assertEqual(await self.engine.retry_failed(), 1)
        self.assertEqual(await self.engine.retry_failed(), 0)
        self.assertEqual(self.engine._queue.qsize(), 1)
        self.assertIn(job.job_id, self.engine._queued)

    async def test_retry_preserves_segments_and_state(self) -> None:
        job = self._add(_job("resume.mp4", self.root))
        job.total_bytes = 100
        job.segments = [Segment(index=0, start=0, end=99, received=40)]
        job.downloaded_bytes = 40
        job.parts_dir.mkdir(parents=True)
        job.part_path.write_bytes(b"x" * 40)

        await self.engine.retry(job.job_id)

        self.assertIs(job.state, JobState.QUEUED)
        self.assertEqual(job.segments[0].received, 40)
        self.assertTrue(job.state_path.exists(), "докачка должна пережить повтор")

    async def test_retry_ignores_unknown_and_non_failed(self) -> None:
        self.assertIsNone(await self.engine.retry("нет-такого"))
        active = self._add(_job("active.mp4", self.root, JobState.RUNNING))
        await self.engine.retry(active.job_id)
        self.assertIs(active.state, JobState.RUNNING)
        self.assertEqual(self.engine._queue.qsize(), 0)


class TestQueueRetryButton(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_button_counts_only_failed(self) -> None:
        view = QueueView()
        jobs = [
            _job("a.mp4", Path("."), JobState.FAILED),
            _job("b.mp4", Path("."), JobState.FAILED),
            _job("c.mp4", Path("."), JobState.DONE),
        ]
        view.sync(jobs)
        self.assertTrue(view._btn_retry.isEnabled())
        self.assertEqual(view._btn_retry.text(), "Повторить неудачные (2)")

    def test_button_disabled_without_failures(self) -> None:
        view = QueueView()
        view.sync([_job("a.mp4", Path("."), JobState.DONE)])
        self.assertFalse(view._btn_retry.isEnabled())

    def test_button_emits_request(self) -> None:
        view = QueueView()
        view.sync([_job("a.mp4", Path("."), JobState.FAILED)])
        received: list[bool] = []
        view.retry_failed_requested.connect(lambda: received.append(True))
        view._btn_retry.click()
        self.assertEqual(received, [True])
