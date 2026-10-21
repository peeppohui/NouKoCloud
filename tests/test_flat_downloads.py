"""Файл ложится прямо в папку загрузок: без папок из облака и без «.part» рядом."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.config import AppConfig
from app.core.models import PARTS_DIR, CloudFile, EntryKind, ProviderId
from app.downloader.engine import DownloadEngine
from app.downloader.transfer import Transfer
from app.net.http_client import HttpClient
from app.net.ratelimit import RateLimiter

from .helpers import start_range_server, stop_server
from .test_transfer import PAYLOAD, FakeProvider, FakeRegistry, make_config, make_context


def clip(name: str, folder: str, file_id: str, size: int = 100) -> CloudFile:
    return CloudFile(provider=ProviderId.YANDEX, file_id=file_id, path=f"/{folder}/{name}", name=name,
                     kind=EntryKind.FILE, size=size, public_key="https://disk.yandex.ru/d/X")


class TestFlatEnqueue(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.engine = DownloadEngine(AppConfig(), None, None, None, mock.Mock())  # type: ignore[arg-type]
        self.engine.start = mock.AsyncMock()     # без настоящих загрузок

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_no_cloud_folders_on_disk(self) -> None:
        jobs = await self.engine.enqueue([clip("A001.mov", "Съёмка/Камера A", "1")], self.root)
        self.assertEqual(jobs[0].dest_dir, self.root, "без папок «Съёмка/Камера A»")
        self.assertEqual(jobs[0].final_path, self.root / "A001.mov")

    async def test_same_names_from_different_folders_do_not_clash(self) -> None:
        jobs = await self.engine.enqueue([clip("C0001.MP4", "Камера A", "a"),
                                          clip("C0001.MP4", "Камера B", "b")], self.root)
        self.assertEqual([j.file.name for j in jobs], ["C0001.MP4", "C0001 (2).MP4"])

    async def test_same_file_twice_is_queued_once(self) -> None:
        first = await self.engine.enqueue([clip("C0001.MP4", "Камера A", "a")], self.root)
        again = await self.engine.enqueue([clip("C0001.MP4", "Камера A", "a")], self.root)
        self.assertEqual(len(first), 1)
        self.assertEqual(again, [], "второй поток писал бы в тот же файл")

    async def test_foreign_file_with_same_name_is_kept(self) -> None:
        (self.root / "C0001.MP4").write_bytes(b"x" * 7)     # чужой файл другого размера
        jobs = await self.engine.enqueue([clip("C0001.MP4", "Камера A", "a")], self.root)
        self.assertEqual(jobs[0].file.name, "C0001 (2).MP4")


class TestNoPartFilesInDownloads(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.handler, self.thread, self.server = start_range_server(PAYLOAD)
        self.config = AppConfig()
        self.http = HttpClient(self.config, RateLimiter(100))
        self.registry = FakeRegistry(FakeProvider(f"http://127.0.0.1:{self.server.server_address[1]}/f"))
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        stop_server(self.server)
        self._tmp.cleanup()

    async def test_only_finished_file_appears(self) -> None:
        file = CloudFile(provider=ProviderId.YANDEX, file_id="1", path="/Съёмка/clip.mp4", name="clip.mp4",
                         kind=EntryKind.FILE, size=len(PAYLOAD))
        engine = DownloadEngine(AppConfig(), None, None, None, mock.Mock())  # type: ignore[arg-type]
        engine.start = mock.AsyncMock()
        job = (await engine.enqueue([file], self.root))[0]
        self.assertEqual(job.part_path.parent.name, PARTS_DIR)

        transfer = Transfer(make_context(make_config(self.root), self.http, self.registry))
        self.assertTrue(await transfer.run(job))
        visible = sorted(p.name for p in self.root.iterdir() if p.name != PARTS_DIR)
        self.assertEqual(visible, ["clip.mp4"], "в папке загрузок — только готовый файл")
        self.assertEqual((self.root / "clip.mp4").read_bytes(), PAYLOAD)
        self.assertEqual(list((self.root / PARTS_DIR).iterdir()), [], "служебная папка пуста")


if __name__ == "__main__":
    unittest.main()
