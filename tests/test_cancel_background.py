"""Мгновенная отмена загрузок, части для соцсетей, работа в фоне."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.core.config import AppConfig
from app.core.models import CloudFile, EntryKind, JobState, ProviderId, SourceKind
from app.downloader.engine import DownloadEngine
from app.social import downloader as social


class _StuckTransfer:
    """Загрузка, которая «висит» на чтении и сама отмену не проверяет."""

    started = None

    def __init__(self, ctx) -> None:
        pass

    async def run(self, job) -> bool:
        _StuckTransfer.started.set()
        await asyncio.sleep(3600)
        return True


class TestInstantCancel(unittest.IsolatedAsyncioTestCase):
    async def test_running_job_cancels_without_waiting_for_data(self) -> None:
        _StuckTransfer.started = asyncio.Event()
        with tempfile.TemporaryDirectory() as tmp, mock.patch("app.downloader.engine.Transfer", _StuckTransfer):
            config = AppConfig()
            config.download_dir = tmp
            engine = DownloadEngine(config, None, None, None, mock.Mock())  # type: ignore[arg-type]
            await engine.start()
            file = CloudFile(provider=ProviderId.YANDEX, file_id="1", path="/a.mp4", name="a.mp4",
                             kind=EntryKind.FILE, size=100, source=SourceKind.PUBLIC)
            jobs = await engine.enqueue([file], Path(tmp))
            await asyncio.wait_for(_StuckTransfer.started.wait(), 5)

            t0 = time.monotonic()
            await engine.cancel(jobs[0].job_id)
            self.assertIs(jobs[0].state, JobState.CANCELLED)     # сразу — в списке
            for _ in range(50):
                if jobs[0].job_id not in engine._running:
                    break
                await asyncio.sleep(0.01)
            self.assertNotIn(jobs[0].job_id, engine._running)
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertIs(jobs[0].state, JobState.CANCELLED)
            await engine.stop()


class TestSocialPieces(unittest.TestCase):
    def test_pieces_cover_whole_file(self) -> None:
        """Части должны покрывать файл целиком — без дыр и нахлёстов."""
        size = 5 * 1024 * 1024 + 123
        ranges = social.split_ranges(size, piece=1024 * 1024)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], size - 1)
        for a, b in zip(ranges, ranges[1:]):
            self.assertEqual(a[1] + 1, b[0])        # без дыр и нахлёстов

    def test_pieces_write_at_their_own_places(self) -> None:
        """Часть пишется на своё место — иначе файл получится из копий.

        Раньше части отдавались yt-dlp как «нарезанный» DASH: тот про
        Range не знает и тянул целиком каждую часть, а потом склеивал их
        подряд — на выходе был файл в N раз больше нужного.
        """
        import tempfile

        from .helpers import start_range_server, stop_server

        payload = bytes((i * 37 + 11) % 256 for i in range(300 * 1024))
        handler, thread, server = start_range_server(payload)
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/file.bin"
            with tempfile.TemporaryDirectory() as tmp:
                target = str(Path(tmp) / "part.bin")
                social._download_ranges(url, {}, len(payload), 64 * 1024, target, 4, [], None)
                data = Path(target).read_bytes()
                self.assertEqual(len(data), len(payload), "размер должен совпасть")
                self.assertEqual(data, payload, "части должны лечь на свои места")
        finally:
            stop_server(server)

    def test_cancel_is_not_swallowed_by_ytdlp(self) -> None:
        # yt-dlp ловит Exception — отмена должна пролетать мимо.
        self.assertFalse(issubclass(social.Cancelled, Exception))

    def test_many_connections(self) -> None:
        opts = social.build_options(Path("D:/x"), "video")
        self.assertGreaterEqual(opts["concurrent_fragment_downloads"], 16)
        self.assertFalse(opts["skip_unavailable_fragments"])

    def test_cancelled_download_leaves_nothing(self) -> None:
        import threading

        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(social.Cancelled):
                social.download("https://example.invalid/v", Path(tmp), cancel=cancel)
            self.assertEqual(os.listdir(tmp), [])


class TestQuitCommand(unittest.TestCase):
    def test_quit_message_exits_fully(self) -> None:
        from app.ui.main_window import MainWindow

        win = mock.Mock(spec=MainWindow)
        MainWindow.handle_instance_message(win, "quit")
        win.quit_app.assert_called_once()
        win.bring_to_front.assert_not_called()      # не показываем окно перед выходом

    def test_quit_without_running_copy_does_not_start_app(self) -> None:
        from PySide6.QtWidgets import QApplication

        from app import main as app_main

        QApplication.instance() or QApplication([])
        with mock.patch("app.core.single_instance.send_to_running", return_value=False) as send,                 mock.patch.object(app_main, "MainWindow") as window:
            self.assertEqual(app_main.main(["NoukoCloud.exe", "--quit"]), 0)
        self.assertEqual(send.call_args.args[0], "quit")
        window.assert_not_called()


class TestBackgroundConfig(unittest.TestCase):
    def test_background_on_by_default(self) -> None:
        self.assertTrue(AppConfig().run_in_background)

    def test_autostart_command_starts_hidden(self) -> None:
        from app.core import autostart

        self.assertIn(autostart.BACKGROUND_ARG, autostart.command())
        if not autostart.supported():
            self.assertFalse(autostart.enabled())
            self.assertFalse(autostart.set_enabled(True))


_SERVER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QCoreApplication
from app.core.single_instance import InstanceServer
app = QCoreApplication([])
server = InstanceServer()
got = []
server.message.connect(got.append)
print("ready", flush=True)
deadline = time.monotonic() + 20
while not got and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
for _ in range(20):                  # дать ответу «ok» уйти
    app.processEvents()
    time.sleep(0.01)
print("got", got[0] if got else "", flush=True)
"""

_CLIENT = """
import sys
sys.path.insert(0, sys.argv[1])
from app.core.single_instance import send_to_running
print(send_to_running("open https://disk.yandex.ru/d/x", 3000))
"""


class TestSingleInstance(unittest.TestCase):
    """Повторный запуск передаёт команду уже работающей копии (оба — отдельные процессы)."""

    def test_second_launch_reaches_first(self) -> None:
        import subprocess
        import sys

        root = str(Path(__file__).resolve().parent.parent)
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        server = subprocess.Popen([sys.executable, "-c", _SERVER, root], stdout=subprocess.PIPE,
                                  text=True, encoding="utf-8", env=env)
        try:
            self.assertEqual(server.stdout.readline().strip(), "ready")
            client = subprocess.run([sys.executable, "-c", _CLIENT, root], capture_output=True,
                                    text=True, encoding="utf-8", env=env, timeout=30)
            out = server.communicate(timeout=30)[0]
        finally:
            if server.poll() is None:
                server.kill()
        self.assertEqual(client.stdout.strip(), "True")
        self.assertIn("got open https://disk.yandex.ru/d/x", out)


if __name__ == "__main__":
    unittest.main()
