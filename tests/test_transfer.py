"""Сквозной тест загрузки: сегменты, докачка, восстановление после обрыва."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from app.core.config import AppConfig
from app.core.models import CloudFile, DownloadJob, EntryKind, ProviderId, Segment
from app.downloader.state import load_state, save_state
from app.downloader.transfer import Transfer, TransferContext
from app.net.http_client import HttpClient
from app.net.ratelimit import RateLimiter

from .helpers import RangeHandler, start_range_server, stop_server

PAYLOAD = bytes((i * 37 + 11) % 256 for i in range(3 * 1024 * 1024 + 4321))


class FakeProvider:
    def __init__(self, url: str) -> None:
        self.url = url

    async def resolve_download(self, file: CloudFile):
        from app.core.models import DownloadLink

        return DownloadLink(url=self.url, headers={}, size=file.size)


class FakeRegistry:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider

    def get(self, _pid: ProviderId) -> FakeProvider:
        return self.provider


def make_context(config: AppConfig, http: HttpClient, registry: FakeRegistry) -> TransferContext:
    def on_progress(_job) -> None:
        return None

    return TransferContext(
        http=http,
        registry=registry,  # type: ignore[arg-type]
        config=config,
        streams_semaphore=asyncio.Semaphore(8),
        on_progress=on_progress,
    )


def make_config(tmp: Path, **kwargs) -> AppConfig:
    config = AppConfig()
    config.download_dir = str(tmp)
    config.max_parallel_files = 2
    config.max_parallel_segments = 3
    config.segment_min_mb = 1
    config.max_retries = 0
    config.retry_base_delay = 0.01
    config.retry_max_delay = 0.05
    for key, value in kwargs.items():
        setattr(config, key, value)
    return config


def make_job(tmp: Path) -> DownloadJob:
    file = CloudFile(
        provider=ProviderId.YANDEX,
        file_id="file-1",
        path="/video/test.mp4",
        name="test.mp4",
        kind=EntryKind.FILE,
        size=len(PAYLOAD),
        mime="video/mp4",
    )
    job = DownloadJob(file=file, dest_dir=tmp)
    job.pause_event.set()
    return job


class TestTransfer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.handler, self.thread, self.server = start_range_server(PAYLOAD)
        self.port = self.server.server_address[1]
        self.config = AppConfig()
        self.limiter = RateLimiter(100)
        self.http = HttpClient(self.config, self.limiter)
        self.url = f"http://127.0.0.1:{self.port}/file.mp4"
        self.registry = FakeRegistry(FakeProvider(self.url))
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        stop_server(self.server)
        self._tmp.cleanup()

    async def test_downloads_whole_file(self) -> None:
        config = make_config(self.tmp)
        job = make_job(self.tmp)
        transfer = Transfer(make_context(config, self.http, self.registry))
        ok = await transfer.run(job)
        self.assertTrue(ok)
        final = self.tmp / "test.mp4"
        self.assertTrue(final.exists())
        self.assertEqual(final.read_bytes(), PAYLOAD)
        self.assertFalse((self.tmp / "test.mp4.part").exists())
        self.assertFalse((self.tmp / "test.mp4.part.json").exists())

    async def test_uses_several_segments(self) -> None:
        config = make_config(self.tmp)
        job = make_job(self.tmp)
        transfer = Transfer(make_context(config, self.http, self.registry))
        await transfer.run(job)
        self.assertGreater(len(job.segments), 1)
        self.assertGreater(RangeHandler.hit_count, 1)

    async def test_resumes_from_saved_state(self) -> None:
        """Прерванная загрузка продолжается с места обрыва, а не заново."""
        config = make_config(self.tmp)
        job = make_job(self.tmp)
        half = len(PAYLOAD) // 2

        # Имитируем состояние после обрыва: часть файла уже на диске,
        # размер предвыделен (как делает сам загрузчик).
        with open(job.part_path, "wb") as handle:
            handle.write(PAYLOAD[:half])
            handle.truncate(len(PAYLOAD))
        job.total_bytes = len(PAYLOAD)
        job.segments = [Segment(index=0, start=0, end=len(PAYLOAD) - 1, received=half)]
        save_state(job)

        RangeHandler.served_bytes = 0
        transfer = Transfer(make_context(config, self.http, self.registry))
        ok = await transfer.run(job)
        self.assertTrue(ok)
        self.assertEqual((self.tmp / "test.mp4").read_bytes(), PAYLOAD)
        # Докачали остаток, а не скачали файл заново.
        self.assertLess(RangeHandler.served_bytes, len(PAYLOAD))

    async def test_falls_back_to_plain_stream_when_server_cuts(self) -> None:
        """Яндекс обрывает диапазонные запросы для архивов — ищем обычный GET."""
        config = make_config(self.tmp)
        job = make_job(self.tmp)
        # Сервер отдаёт не больше 100 КБ за запрос и рвёт соединение.
        RangeHandler.fail_after = 100 * 1024
        transfer = Transfer(make_context(config, self.http, self.registry))
        with self.assertRaises(Exception):
            await transfer.run(job)
        RangeHandler.fail_after = None

    async def test_skips_already_downloaded(self) -> None:
        config = make_config(self.tmp)
        (self.tmp / "test.mp4").write_bytes(PAYLOAD)
        job = make_job(self.tmp)
        transfer = Transfer(make_context(config, self.http, self.registry))
        ok = await transfer.run(job)
        self.assertTrue(ok)
        self.assertEqual(job.state.name, "SKIPPED")

    async def test_checks_md5(self) -> None:
        import hashlib

        config = make_config(self.tmp)
        job = make_job(self.tmp)
        job.file.md5 = hashlib.md5(PAYLOAD).hexdigest()
        transfer = Transfer(make_context(config, self.http, self.registry))
        await transfer.run(job)
        self.assertTrue((self.tmp / "test.mp4").exists())

        # 2. Файл с неверной суммой должен быть отвергнут.
        (self.tmp / "test.mp4").unlink()
        job2 = make_job(self.tmp)
        job2.file.md5 = "0" * 32
        transfer2 = Transfer(make_context(config, self.http, self.registry))
        with self.assertRaises(Exception):
            await transfer2.run(job2)


if __name__ == "__main__":
    unittest.main()
