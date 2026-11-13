"""Таймкод, покадровое движение, отметки in/out и J/K/L (ТЗ 4.9)."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.preview.player import (  # noqa: E402
    PreviewPlayer,
    format_timecode,
    frame_step_ms,
    normalize_fps,
    nominal_fps,
)


class TestNormalizeFps(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(normalize_fps(25), 25.0)
        self.assertEqual(normalize_fps(24), 24.0)
        self.assertEqual(normalize_fps(30), 30.0)
        self.assertEqual(normalize_fps(50), 50.0)
        self.assertEqual(normalize_fps(60), 60.0)
        self.assertEqual(normalize_fps(29.97), 29.97)
        self.assertEqual(normalize_fps(23.98), 23.976)
        self.assertEqual(normalize_fps(59.94), 59.94)

    def test_fraction(self):
        self.assertEqual(normalize_fps(24000 / 1001), 23.976)
        self.assertEqual(normalize_fps(30000 / 1001), 29.97)

    def test_unknown(self):
        self.assertEqual(normalize_fps(0), 0.0)
        self.assertEqual(normalize_fps(None), 0.0)
        self.assertEqual(normalize_fps(""), 0.0)
        self.assertEqual(normalize_fps("abc"), 0.0)

    def test_string_number(self):
        self.assertEqual(normalize_fps("25"), 25.0)

    def test_nominal(self):
        self.assertEqual(nominal_fps(23.976), 24)
        self.assertEqual(nominal_fps(29.97), 30)
        self.assertEqual(nominal_fps(25), 25)
        self.assertEqual(nominal_fps(0), 0)


class TestFrameStep(unittest.TestCase):
    def test_step_ms(self):
        self.assertEqual(frame_step_ms(25), 40)
        self.assertEqual(frame_step_ms(30), 33)
        self.assertEqual(frame_step_ms(50), 20)
        self.assertEqual(frame_step_ms(60), 17)

    def test_step_without_fps(self):
        self.assertEqual(frame_step_ms(0), 40)


class TestFormatTimecode(unittest.TestCase):
    def test_25fps_one_second(self):
        self.assertEqual(format_timecode(1000, 25), "00:00:01:00")

    def test_frames_in_second(self):
        self.assertEqual(format_timecode(40, 25), "00:00:00:01")
        self.assertEqual(format_timecode(1000 + 40, 25), "00:00:01:01")

    def test_minutes_and_hours(self):
        self.assertEqual(format_timecode(61_000, 25), "00:01:01:00")
        self.assertEqual(format_timecode(3_661_000, 25), "01:01:01:00")

    def test_fractional_fps_no_negative_frames(self):
        for fps in (23.976, 29.97, 30):
            for ms in (0, 40, 1000, 45_000):
                code = format_timecode(ms, fps)
                frames = int(code.split(":")[3])
                self.assertGreaterEqual(frames, 0)
                self.assertLess(frames, nominal_fps(fps))

    def test_unknown_fps_milliseconds(self):
        self.assertEqual(format_timecode(1500, 0), "00:00:01.500")
        self.assertEqual(format_timecode(0, 0), "00:00:00.000")

    def test_negative_is_clamped(self):
        self.assertEqual(format_timecode(-500, 25), "00:00:00:00")


class TestPlayerFrames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.player = PreviewPlayer()
        self.player._kind = "video"
        self.player._fps = 25
        self.seeks: list[int] = []
        self.player._seek = self.seeks.append
        self.position = [1000]
        self.player.position_ms = lambda: self.position[0]

    def tearDown(self):
        self.player.deleteLater()

    def test_step_frame_forward_and_back(self):
        self.player.pause = lambda: None
        self.player.step_frame(1)
        self.position[0] = 1040
        self.player.step_frame(1)
        self.assertEqual(self.seeks, [1040, 1080])
        self.position[0] = 1080
        self.player.step_frame(-1)
        self.assertEqual(self.seeks[-1], 1040)

    def test_step_frame_never_negative(self):
        self.player.pause = lambda: None
        self.player.position_ms = lambda: 0
        self.player.step_frame(-1)
        self.assertEqual(self.seeks[-1], 0)

    def test_marks_and_clear(self):
        self.player.position_ms = lambda: 1000
        self.player.mark_in()
        self.player.position_ms = lambda: 3000
        self.player.mark_out()
        self.assertEqual(self.player._in_ms, 1000)
        self.assertEqual(self.player._out_ms, 3000)
        self.assertEqual(self.player._slider._marks, (1000, 3000))
        self.player.clear_marks()
        self.assertIsNone(self.player._in_ms)
        self.assertIsNone(self.player._out_ms)
        self.assertEqual(self.player._slider._marks, (None, None))

    def test_play_range_uses_marks(self):
        self.player.position_ms = lambda: 1000
        self.player.mark_in()
        self.player.position_ms = lambda: 3000
        self.player.mark_out()
        self.player._is_playing = lambda: True
        self.player.play_marked_range()
        self.assertEqual(self.player._stop_at, 3000)

    def test_jkl_speed(self):
        self.player._is_playing = lambda: True
        self.player.jkl("l")
        self.assertEqual(self.player._speed_index, 1)
        self.assertEqual(self.player._speed.text(), "2×")
        self.player.jkl("l")
        self.assertEqual(self.player._speed_index, 2)
        self.player.jkl("k")
        self.assertEqual(self.player._speed_index, 0)
        self.assertFalse(self.player._speed.isVisible())

    def test_j_steps_back_one_frame(self):
        self.player.pause = lambda: None
        self.player.jkl("j")
        self.assertEqual(self.seeks[-1], 960)

    def test_timecode_label_updates(self):
        self.player._timecode.setVisible(True)
        self.player._update_time(1000)
        self.assertEqual(self.player._timecode.text(), "00:00:01:00")

    def test_timecode_hidden_for_audio(self):
        self.player._kind = "audio"
        self.player._timecode.setVisible(False)
        self.player._update_time(1000)
        self.assertEqual(self.player._timecode.text(), "")


if __name__ == "__main__":
    unittest.main()
