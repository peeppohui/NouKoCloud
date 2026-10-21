"""Видео + отдельный звук в предпросмотре: звук не дёргаем, видео догоняет скоростью."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.preview import player as player_mod  # noqa: E402

S = QMediaPlayer.MediaStatus
P = QMediaPlayer.PlaybackState


class FakePlayer:
    def __init__(self, position=0, status=S.BufferedMedia, state=P.PausedState) -> None:
        self.pos, self.status, self.state, self.rate = position, status, state, 1.0
        self.seeks: list[int] = []

    def source(self):
        from PySide6.QtCore import QUrl

        return QUrl("http://127.0.0.1/1")

    def position(self):
        return self.pos

    def setPosition(self, ms):  # noqa: N802
        self.seeks.append(ms)
        self.pos = ms

    def mediaStatus(self):  # noqa: N802
        return self.status

    def playbackState(self):  # noqa: N802
        return self.state

    def play(self):
        self.state = P.PlayingState

    def pause(self):
        self.state = P.PausedState

    def playbackRate(self):  # noqa: N802
        return self.rate

    def setPlaybackRate(self, rate):  # noqa: N802
        self.rate = rate


class TestAvSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.p = player_mod.PreviewPlayer()
        self.video = FakePlayer(10_000)
        self.audio = FakePlayer(10_000)
        self.p._player, self.p._aplayer = self.video, self.audio
        self.p._want_play = True
        self.p._av_frame = True

    def tearDown(self) -> None:
        self.p._player = self.p._aplayer = None
        self.p.deleteLater()

    def test_start_audio_first_then_video(self) -> None:
        self.p._sync_audio()
        self.assertEqual(self.audio.state, P.PlayingState)
        self.assertEqual(self.video.state, P.PausedState)        # ждёт, пока звук зазвучит
        self.audio.pos = 10_050                                  # звук пошёл
        self.p._sync_audio()
        self.assertEqual(self.video.state, P.PlayingState)

    def test_drift_fixed_by_video_rate_not_audio_seek(self) -> None:
        self.audio.state = self.video.state = P.PlayingState
        self.video.pos, self.audio.pos = 10_400, 10_000          # видео впереди на 0,4 с
        self.p._sync_audio()
        self.assertLess(self.video.rate, 1.0)
        self.assertEqual(self.audio.seeks, [])                   # звук не трогаем — без пауз в звуке
        self.video.pos = 10_000
        self.p._sync_audio()
        self.assertEqual(self.video.rate, 1.0)

    def test_buffering_status_does_not_pause_sound(self) -> None:
        # BufferingMedia — «играет, буфер неполный»: раньше из-за него звук замолкал.
        self.audio.state = self.video.state = P.PlayingState
        self.video.status = S.BufferingMedia
        self.p._sync_audio()
        self.assertEqual(self.audio.state, P.PlayingState)

    def test_stalled_video_holds_sound(self) -> None:
        self.audio.state = self.video.state = P.PlayingState
        self.video.status = S.StalledMedia
        self.p._sync_audio()
        self.assertEqual(self.audio.state, P.PausedState)

    def test_big_drift_moves_video(self) -> None:
        self.audio.state = self.video.state = P.PlayingState
        self.video.pos, self.audio.pos = 5_000, 10_000
        self.p._sync_audio()
        self.assertEqual(self.video.seeks, [10_000])
        self.assertEqual(self.audio.seeks, [])


if __name__ == "__main__":
    unittest.main()
