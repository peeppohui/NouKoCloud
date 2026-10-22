"""Сквозной тест загрузки: сегменты, докачка, восстановление после обрыва."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

import httpx

from app.core.config import AppConfig
from app.core.models import CloudFile, DownloadJob, DownloadLink, EntryKind, ProviderId, Segment
from app.downloader.state import load_state, save_state
from app.downloader.transfer import Transfer, TransferContext
from app.net.http_client import HttpClient
from app.net.ratelimit import RateLimiter

from .helpers import RangeHandler, reset_handler_counters, start_range_server, stop_server

PAYLOAD = bytes((i * 37 + 11) % 256 for i in range(3 * 1024 * 1024 + 4321))
QUOTA_BODY = b'{"error":{"message":"The download quota for this file has been exceeded"}}'


class FakeProvider:
    def __init__(self, url: str) -> None:
        self.url = url

    async def resolve_download(self, file: CloudFile):
        from app.core.models import DownloadLink

        return DownloadLink(url=self.url, headers={}, size=file.size)

    def forget_link(self, file: CloudFile) -> None:
        pass


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


class FakeStreamResponse:
    def __init__(self, status: int, url: str, body: bytes = b"",
                 headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.request = httpx.Request("GET", url)

    async def aread(self) -> bytes:
        return self._body

    async def aiter_bytes(self, _chunk: int):
        yield self._body

    async def aclose(self) -> None:
        return None


class FakeDownloadHttp:
    """Первый адрес отвечает ошибкой, резервный — отдаёт файл."""

    def __init__(self, primary: str, body: bytes, *, status: int = 403,
                 error_type: str = "application/json",
                 error_body: bytes = QUOTA_BODY) -> None:
        self.primary = primary
        self.body = body
        self.status = status
        self.error_type = error_type
        self.error_body = error_body
        self.urls: list[str] = []

    async def stream(self, _method: str, url: str, **_kwargs):
        self.urls.append(url)
        if url == self.primary:
            return FakeStreamResponse(
                self.status, url, self.error_body, {"content-type": self.error_type}
            )
        return FakeStreamResponse(200, url, self.body, {"content-type": "video/mp4"})


class FallbackProvider(FakeProvider):
    def __init__(self, primary: str, fallback: str) -> None:
        super().__init__(primary)
        self.fallback = fallback

    async def resolve_download(self, file: CloudFile) -> DownloadLink:
        return DownloadLink(
            url=self.url,
            headers={},
            size=file.size,
            fallback=DownloadLink(url=self.fallback, headers={}, size=file.size),
        )


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
        job.parts_dir.mkdir(parents=True, exist_ok=True)
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
        # Битый файл и его состояние удалены — повтор начнёт с нуля,
        # а не «докачает» пустой остаток и снова упадёт на той же сумме.
        self.assertFalse(job2.part_path.exists())
        self.assertFalse(job2.state_path.exists())

    async def test_different_file_with_same_name_is_kept(self) -> None:
        """Чужой файл с тем же именем не удаляется — качаем рядом под другим именем."""
        config = make_config(self.tmp)
        (self.tmp / "test.mp4").write_bytes(b"other file")
        job = make_job(self.tmp)
        transfer = Transfer(make_context(config, self.http, self.registry))
        await transfer.run(job)
        self.assertEqual((self.tmp / "test.mp4").read_bytes(), b"other file")
        self.assertEqual((self.tmp / "test (1).mp4").read_bytes(), PAYLOAD)
        self.assertEqual(job.file.name, "test (1).mp4")

    async def test_unknown_size_is_not_taken_for_downloaded(self) -> None:
        """Файл без известного размера не должен молча помечаться «уже скачан»."""
        config = make_config(self.tmp)
        (self.tmp / "test.mp4").write_bytes(b"other file too")
        job = make_job(self.tmp)
        job.file.size = 0
        transfer = Transfer(make_context(config, self.http, self.registry))
        await transfer.run(job)
        self.assertNotEqual(job.state.name, "SKIPPED")
        self.assertEqual((self.tmp / "test.mp4").read_bytes(), b"other file too")
        self.assertEqual((self.tmp / "test (1).mp4").read_bytes(), PAYLOAD)

    async def test_empty_file_is_taken_for_downloaded(self) -> None:
        """Настоящий пустой файл (размер 0) повторно не качаем."""
        config = make_config(self.tmp)
        (self.tmp / "test.mp4").write_bytes(b"")
        job = make_job(self.tmp)
        job.file.size = 0
        transfer = Transfer(make_context(config, self.http, self.registry))
        await transfer.run(job)
        self.assertEqual(job.state.name, "SKIPPED")

    async def test_unknown_size_cut_is_not_accepted(self) -> None:
        """Файл без известного размера, оборванный сервером, не считается скачанным."""
        config = make_config(self.tmp, max_retries=1)
        job = make_job(self.tmp)
        job.file.size = 0
        RangeHandler.fail_after = 100 * 1024
        try:
            transfer = Transfer(make_context(config, self.http, self.registry))
            with self.assertRaises(Exception):
                await transfer.run(job)
        finally:
            RangeHandler.fail_after = None
        self.assertFalse((self.tmp / "test.mp4").exists())

    async def test_unknown_size_downloads_whole_stream(self) -> None:
        config = make_config(self.tmp)
        job = make_job(self.tmp)
        job.file.size = 0
        transfer = Transfer(make_context(config, self.http, self.registry))
        await transfer.run(job)
        self.assertEqual((self.tmp / "test.mp4").read_bytes(), PAYLOAD)

    async def test_short_stream_is_not_taken_as_complete(self) -> None:
        """Облако обещало больше, чем отдало, — файл не должен считаться скачанным."""
        config = make_config(self.tmp, max_retries=0)
        job = make_job(self.tmp)
        job.file.size = len(PAYLOAD) + 4096
        transfer = Transfer(make_context(config, self.http, self.registry))
        with self.assertRaises(Exception):
            await transfer.run(job)
        self.assertFalse((self.tmp / "test.mp4").exists())

    async def test_segments_of_one_file_run_in_parallel_but_not_more(self) -> None:
        """«Частей одного файла» должно ограничивать число одновременных запросов."""
        reset_handler_counters()
        RangeHandler.delay = 0.15          # запросы должны пересекаться по времени
        try:
            config = make_config(self.tmp, max_parallel_segments=2, segment_min_mb=1)
            job = make_job(self.tmp)
            transfer = Transfer(make_context(config, self.http, self.registry))
            await transfer.run(job)
        finally:
            RangeHandler.delay = 0.0
        self.assertGreater(RangeHandler.max_in_flight, 1, "сегменты должны идти параллельно")
        self.assertLessEqual(RangeHandler.max_in_flight, 2,
                             "но не больше, чем разрешено настройкой")

    def _fallback_transfer(self, config: AppConfig, fake_http: FakeDownloadHttp,
                           primary: str, fallback: str) -> Transfer:
        return Transfer(TransferContext(
            http=fake_http,  # type: ignore[arg-type]
            registry=FakeRegistry(FallbackProvider(primary, fallback)),  # type: ignore[arg-type]
            config=config,
            streams_semaphore=asyncio.Semaphore(8),
            on_progress=lambda _job: None,
        ))

    async def test_403_switches_to_fallback_link(self) -> None:
        """403 от основного адреса — сразу пробуем резервный, без паузы."""
        config = make_config(self.tmp, use_segments=False)
        job = make_job(self.tmp)
        primary = "https://primary.test/file"
        fallback = "https://fallback.test/file"
        fake_http = FakeDownloadHttp(primary, PAYLOAD)
        transfer = self._fallback_transfer(config, fake_http, primary, fallback)

        with self.assertLogs("app.downloader.transfer", level="INFO") as logs:
            ok = await transfer.run(job)

        self.assertTrue(ok)
        self.assertEqual((self.tmp / "test.mp4").read_bytes(), PAYLOAD)
        self.assertEqual(fake_http.urls[:2], [primary, fallback])
        self.assertIn("download quota", "\n".join(logs.output),
                      "тело 403 должно попасть в журнал")

    async def test_html_instead_of_file_switches_to_fallback(self) -> None:
        """HTML-заглушка Google — тоже повод перейти на резервную ссылку."""
        config = make_config(self.tmp, use_segments=False)
        job = make_job(self.tmp)
        primary = "https://primary.test/file"
        fallback = "https://fallback.test/file"
        fake_http = FakeDownloadHttp(
            primary, PAYLOAD, status=200, error_type="text/html",
            error_body=b"<html><body>download quota</body></html>",
        )
        transfer = self._fallback_transfer(config, fake_http, primary, fallback)

        ok = await transfer.run(job)

        self.assertTrue(ok)
        self.assertEqual((self.tmp / "test.mp4").read_bytes(), PAYLOAD)
        self.assertEqual(fake_http.urls[:2], [primary, fallback])


if __name__ == "__main__":
    unittest.main()
