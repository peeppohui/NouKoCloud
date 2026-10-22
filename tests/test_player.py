"""Плеер превью целиком: QtMultimedia ← сервер превью ← «облако» (headless).

Проверяем то, что раньше ломалось незаметно: файл действительно
открывается плеером, перематывается, картинка декодируется, а ошибка
доходит до пользователя, а не теряется.
"""

from __future__ import annotations

import io
import math
import os
import struct
import tempfile
import time
import unittest
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.async_runtime import AsyncRuntime  # noqa: E402
from app.core.config import AppConfig  # noqa: E402
from app.core.models import CloudFile, DownloadLink, EntryKind, ProviderId  # noqa: E402
from app.net.http_client import HttpClient  # noqa: E402
from app.net.ratelimit import RateLimiter  # noqa: E402
from app.preview.player import PreviewPlayer, decode_image, format_ms  # noqa: E402
from app.preview.server import PreviewServer  # noqa: E402
from app.preview.stream_cache import BufferState  # noqa: E402

from .helpers import RangeHandler, start_range_server, stop_server  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def make_wav(seconds: float = 2.0, rate: int = 44100) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(seconds * rate))
        )
        wav.writeframes(frames)
    return out.getvalue()


def make_png(width: int = 320, height: int = 200) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(40, 120, 200))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def wait_until(condition, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


class FakeProvider:
    def __init__(self, url: str) -> None:
        self.url = url

    async def resolve_download(self, file: CloudFile) -> DownloadLink:
        return DownloadLink(url=self.url, headers={}, size=file.size)

    def forget_link(self, file: CloudFile) -> None:
        pass


class FakeRegistry:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider

    def get(self, _pid):
        return self.provider


class PlayerTestBase(unittest.TestCase):
    """Плеер + сервер превью + «облако»; тестов не содержит."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = AppConfig()
        self.config.download_dir = self._tmp.name
        self.runtime = AsyncRuntime("test-preview")
        self.runtime.start()
        self.http = HttpClient(self.config, RateLimiter(50))
        self.player = PreviewPlayer()
        self.player.resize(640, 400)
        self.failures: list[tuple[str, bool]] = []
        self.player.failed.connect(lambda msg, fmt: self.failures.append((msg, fmt)))
        self.origin = None

    def tearDown(self) -> None:
        self.player.stop()
        self.player.deleteLater()
        QApplication.processEvents()
        if self.origin is not None:
            stop_server(self.origin)
        self.runtime.submit(self.server.stop()).result(5)
        self.runtime.submit(self.http.aclose()).result(5)
        self.runtime.stop()
        self._tmp.cleanup()

    def serve(self, payload: bytes, name: str) -> str:
        """Поднимает «облако» с payload и сервер превью; возвращает адрес для плеера."""
        _handler, _thread, self.origin = start_range_server(payload)
        RangeHandler.payload = payload
        url = f"http://127.0.0.1:{self.origin.server_address[1]}/{name}"
        self.server = PreviewServer(FakeRegistry(FakeProvider(url)), self.http, self.config)
        self.runtime.submit(self.server.start()).result(5)
        file = CloudFile(provider=ProviderId.YANDEX, file_id=name, path=f"/{name}", name=name,
                         kind=EntryKind.FILE, size=len(payload))
        session = self.server.add_session(file)
        return self.server.media_url(session.token)


class TestPreviewPlayer(PlayerTestBase):
    def test_audio_plays_and_seeks(self) -> None:
        url = self.serve(make_wav(2.0), "voice.wav")
        self.player._audio.setMuted(True)
        self.player.play(url, "audio", "voice.wav")
        mp = self.player._player
        ok = wait_until(lambda: mp.duration() > 0 or self.failures)
        self.assertTrue(ok, "плеер не открыл файл за 15 с")
        self.assertEqual(self.failures, [])
        self.assertAlmostEqual(mp.duration(), 2000, delta=100)
        self.assertTrue(mp.hasAudio())
        self.assertTrue(self.player._controls.isVisibleTo(self.player))

        self.assertTrue(wait_until(mp.isSeekable, 5), "файл должен перематываться (Range)")
        self.player._seek(1500)
        self.assertTrue(wait_until(lambda: mp.position() >= 1400, 10),
                        f"перемотка не сработала: позиция {mp.position()}")

    def test_image_is_decoded(self) -> None:
        url = self.serve(make_png(320, 200), "photo.png")
        self.player.show_image(url)
        ok = wait_until(lambda: self.player._image._pixmap is not None or self.failures)
        self.assertTrue(ok, "изображение не загрузилось")
        self.assertEqual(self.failures, [])
        self.assertEqual(self.player._image._pixmap.width(), 320)

    def test_broken_media_reports_error(self) -> None:
        url = self.serve(b"this is not a video at all" * 100, "broken.mp4")
        self.player.play(url, "video", "broken.mp4")
        self.assertTrue(wait_until(lambda: bool(self.failures)), "ошибка должна дойти до пользователя")
        self.assertTrue(self.player._status.text())

    def test_missing_preview_reports_error(self) -> None:
        self.serve(make_wav(0.5), "a.wav")
        self.player.play(self.server.base_url + "/media/unknown-token", "audio", "a.wav")
        self.assertTrue(wait_until(lambda: bool(self.failures)), "ошибка должна дойти до пользователя")

    def test_stop_releases_media(self) -> None:
        url = self.serve(make_wav(1.0), "a.wav")
        self.player.play(url, "audio", "a.wav")
        self.player.stop()
        self.assertTrue(self.player._player.source().isEmpty())
        self.assertFalse(self.player._controls.isVisibleTo(self.player))


class TestBufferingGate(PlayerTestBase):
    """Видео тяжелее интернета: вместо рывков — буферизация с понятным статусом."""

    def start_with_probe(self) -> dict:
        state = {"value": BufferState(ahead_bytes=0, complete=False, rate=0.0, size=1)}
        url = self.serve(make_wav(4.0), "voice.wav")
        self.player._audio.setMuted(True)
        self.player.play(url, "audio", "voice.wav", buffer_probe=lambda _f: state["value"])
        mp = self.player._player
        # Таймер буфера останавливаем СРАЗУ: иначе он успеет сработать, пока
        # ждём загрузку, и решит, что буфера мало, — тест станет зависеть от
        # того, успел ли таймер.
        self.player._buffer_timer.stop()
        self.player._buffering = False
        self.assertTrue(wait_until(lambda: mp.duration() > 0), "плеер не загрузил")
        size = len(make_wav(4.0))
        state["size"] = size
        return state

    def slow(self, state: dict, ahead_s: float) -> None:
        size = state["size"]
        per_s = size / 4.0
        # интернет в 4 раза медленнее, чем нужно видео
        state["value"] = BufferState(ahead_bytes=int(ahead_s * per_s), complete=False,
                                     rate=per_s / 4, size=size)

    def test_pauses_and_resumes_by_itself(self) -> None:
        state = self.start_with_probe()
        mp = self.player._player
        self.slow(state, ahead_s=0.1)
        self.player._check_buffer()
        self.assertTrue(self.player.buffering)
        self.assertNotEqual(mp.playbackState(), QMediaPlayer.PlaybackState.PlayingState)
        self.assertIn("Буферизация", self.player._status.text())
        self.assertIn("Мбит/с", self.player._status.text(), "объясняем, почему ждём")
        self.assertTrue(self.player._btn_no_wait.isVisibleTo(self.player), "долго ждать — предлагаем «Смотреть сразу»")

        state["value"] = BufferState(ahead_bytes=state["size"], complete=True, rate=1.0, size=state["size"])
        self.player._check_buffer()
        self.assertFalse(self.player.buffering)
        self.assertTrue(wait_until(lambda: mp.playbackState() == QMediaPlayer.PlaybackState.PlayingState, 3))
        self.assertEqual(self.player._status.text(), "")

    def test_user_pause_is_not_overridden(self) -> None:
        state = self.start_with_probe()
        self.slow(state, ahead_s=0.1)
        self.player._check_buffer()
        self.assertTrue(self.player.buffering)
        self.player.pause()                       # пользователь нажал паузу
        state["value"] = BufferState(ahead_bytes=state["size"], complete=True, rate=1.0, size=state["size"])
        self.player._check_buffer()
        wait_until(lambda: False, 0.3)
        self.assertNotEqual(self.player._player.playbackState(), QMediaPlayer.PlaybackState.PlayingState,
                            "после паузы пользователя плеер не должен заиграть сам")

    def test_watch_now_skips_buffering(self) -> None:
        state = self.start_with_probe()
        self.slow(state, ahead_s=0.1)
        self.player._check_buffer()
        self.player._watch_now()
        self.assertFalse(self.player.buffering)
        self.player._check_buffer()
        self.assertFalse(self.player.buffering, "выбрал «смотреть сразу» — больше не останавливаем")

    def test_enough_buffer_keeps_playing(self) -> None:
        state = self.start_with_probe()
        size = state["size"]
        # интернет быстрее видео, запас 2 секунды — играем без остановок
        state["value"] = BufferState(ahead_bytes=size // 2, complete=False, rate=size, size=size)
        self.player._check_buffer()
        self.assertFalse(self.player.buffering)


class TestImageDecoding(unittest.TestCase):
    def test_huge_image_is_downscaled(self) -> None:
        image, problem = decode_image(QByteArray(make_png(6000, 3000)))
        self.assertIsNotNone(image, problem)
        self.assertLessEqual(max(image.width(), image.height()), 4096)

    def test_garbage_is_rejected(self) -> None:
        image, _problem = decode_image(QByteArray(b"not an image"))
        self.assertIsNone(image)

    def test_format_ms(self) -> None:
        self.assertEqual(format_ms(0), "0:00")
        self.assertEqual(format_ms(65_000), "1:05")
        self.assertEqual(format_ms(3_725_000), "1:02:05")


if __name__ == "__main__":
    unittest.main()
