"""Анимации кнопок, «живой» фон и звуки интерфейса."""

from __future__ import annotations

import os
import tempfile
import unittest
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QEnterEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from app.core.config import AppConfig  # noqa: E402
from app.ui import motion, sound, theme  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class TestSounds(unittest.TestCase):
    def test_sound_files_are_short_and_quiet_wav(self) -> None:
        for name in sound.SOUNDS:
            path = theme.ASSETS_DIR / "sounds" / f"{name}.wav"
            self.assertTrue(path.exists(), name)
            with wave.open(str(path)) as handle:
                seconds = handle.getnframes() / handle.getframerate()
            self.assertLess(seconds, 3.0, f"{name}: звук интерфейса — короткий")

    def test_sounds_fade_out_smoothly(self) -> None:
        """Конец каждого звука — тишина, а не обрыв на громкой ноте."""
        import struct

        for name in sound.SOUNDS:
            with wave.open(str(theme.ASSETS_DIR / "sounds" / f"{name}.wav")) as handle:
                frames = handle.readframes(handle.getnframes())
                rate = handle.getframerate()
            samples = struct.unpack(f"<{len(frames) // 2}h", frames)
            peak = max(abs(v) for v in samples)
            tail = samples[-int(0.05 * rate):]
            self.assertLess(max(abs(v) for v in tail), peak * 0.01, f"{name}: обрыв в конце")

    def test_disabled_bank_is_silent(self) -> None:
        bank = sound.SoundBank(enabled=False)
        played = []
        for effect in bank._effects.values():
            effect.play = lambda played=played: played.append(1)
        bank.play("click")
        self.assertEqual(played, [])

    def test_clicks_are_rate_limited(self) -> None:
        bank = sound.SoundBank(enabled=True)
        played = []
        bank._effects = {"click": type("E", (), {"play": lambda self: played.append(1)})()}
        for _ in range(10):
            bank.play("click")
        self.assertEqual(len(played), 1, "дробь из щелчков не нужна")


class TestMotion(unittest.TestCase):
    def test_instant_hover_removed_when_animated(self) -> None:
        plain = theme.stylesheet(theme.DARK)
        animated = theme.stylesheet(theme.DARK, motion=True)
        self.assertIn("QPushButton:hover", plain)
        self.assertNotIn("QPushButton:hover", animated)
        self.assertIn("#closeBtn:hover", animated, "красное «закрыть» остаётся")

    def test_background_is_static(self) -> None:
        """Фон без анимации: не подписан на «метроном» движения."""
        from app.ui.widgets import StarsBackground

        background = StarsBackground()
        self.assertFalse(hasattr(background, "_on_tick"))
        self.assertEqual(motion.motion()._users, 0)

    def test_background_drift_is_gentle(self) -> None:
        for step in range(0, 600):
            dx, dy, glow = motion.stars_offset(step / 10)
            self.assertLessEqual(abs(dx), 8)
            self.assertLessEqual(abs(dy), 12)
            self.assertTrue(0.7 <= glow <= 1.0)

    def test_button_gets_animated_glow(self) -> None:
        motion.motion().set_enabled(True)
        motion.install(_app)
        button = QPushButton("Кнопка")
        button.resize(120, 40)
        button.show()
        QApplication.sendEvent(button, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        glow = button.findChild(motion._Glow)
        self.assertIsNotNone(glow)
        self.assertEqual(glow.size(), button.size())
        press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(button, press)
        self.assertEqual(glow._press_anim.endValue(), 1.0)
        button.close()


class TestSettingsToggles(unittest.TestCase):
    def test_toggles_saved(self) -> None:
        from app.ui.settings_dialog import SettingsDialog

        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            config.download_dir = tmp
            dialog = SettingsDialog(config)
            self.assertTrue(dialog._ui_motion.isChecked())
            self.assertTrue(dialog._ui_sounds.isChecked())
            dialog._ui_motion.setChecked(False)
            dialog._ui_sounds.setChecked(False)
            dialog._save()
            self.assertFalse(config.ui_motion)
            self.assertFalse(config.ui_sounds)

    def test_volume_saved(self) -> None:
        from app.ui.settings_dialog import SettingsDialog

        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            config.download_dir = tmp
            dialog = SettingsDialog(config)
            dialog._ui_volume.setValue(25)
            self.assertEqual(dialog._ui_volume_label.text(), "25%")
            dialog._save()
            self.assertEqual(config.ui_volume, 25)

    def test_update_check_saved(self) -> None:
        from app.ui.settings_dialog import SettingsDialog

        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            config.download_dir = tmp
            dialog = SettingsDialog(config)
            self.assertTrue(dialog._update_check.isChecked())
            dialog._update_check.setChecked(False)
            dialog._save()
            self.assertFalse(config.update_check_on_start)


if __name__ == "__main__":
    unittest.main()
