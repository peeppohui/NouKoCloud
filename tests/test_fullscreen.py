"""Превью во весь экран: вход/выход, клавиши, двойной щелчок, возврат на место."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

from app.preview.player import PreviewPlayer  # noqa: E402

_app = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def press(widget, key, vk=0, text=""):
    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, int(key), Qt.KeyboardModifier.NoModifier,
                                             0, vk, 0, text))


class TestFullscreen(unittest.TestCase):
    def setUp(self) -> None:
        self.window = QWidget()
        self.window.resize(900, 700)
        layout = QVBoxLayout(self.window)
        self.above = QLabel("сверху", self.window)
        layout.addWidget(self.above)
        self.player = PreviewPlayer(self.window)
        layout.addWidget(self.player, 1)
        self.below = QLabel("снизу", self.window)
        layout.addWidget(self.below)
        self.window.show()
        self.player._kind = "video"           # как будто открыто видео
        self.player._sync_fullscreen_button()

    def tearDown(self) -> None:
        self.player.exit_fullscreen()
        self.window.close()
        self.window.deleteLater()
        _app.processEvents()

    def test_enter_and_exit_keep_place_and_playback(self) -> None:
        self.player.set_embedded_height(320)
        self.player._want_play = True         # видео играет
        self.player.enter_fullscreen()
        self.assertTrue(self.player.is_fullscreen())
        host = self.player.window()
        self.assertIsNot(host, self.window)
        self.assertTrue(host.windowState() & Qt.WindowState.WindowFullScreen)
        self.assertEqual(self.player.maximumHeight(), 16777215)   # во весь экран — без фиксированной высоты
        layout = self.window.layout()
        self.assertEqual(layout.indexOf(self.player), -1)
        self.assertEqual(layout.itemAt(1).widget().height(), 320)  # заглушка держит место
        self.assertTrue(self.player._want_play, "переезд не должен ставить паузу")

        self.player.exit_fullscreen()
        self.assertFalse(self.player.is_fullscreen())
        self.assertIs(self.player.window(), self.window)
        self.assertEqual(layout.indexOf(self.player), 1)           # на прежнем месте
        self.assertEqual(self.player.maximumHeight(), 320)
        self.assertEqual(layout.count(), 3)                        # заглушки не осталось
        self.assertTrue(self.player._want_play)

    def test_height_change_while_fullscreen_applies_after_exit(self) -> None:
        self.player.set_embedded_height(300)
        self.player.enter_fullscreen()
        self.player.set_embedded_height(250)  # страница поменяла размер, пока смотрели во весь экран
        self.assertEqual(self.player.maximumHeight(), 16777215)
        self.player.exit_fullscreen()
        self.assertEqual(self.player.height(), 250)

    def test_keys(self) -> None:
        self.player.setFocus()
        press(self.player, Qt.Key.Key_F, 0x46, "f")
        self.assertTrue(self.player.is_fullscreen())
        press(self.player, Qt.Key.Key_Escape)
        self.assertFalse(self.player.is_fullscreen())
        press(self.player, 0x410, 0x46, "а")  # русская раскладка: та же клавиша даёт «А»
        self.assertTrue(self.player.is_fullscreen())
        press(self.player, Qt.Key.Key_F11)
        self.assertFalse(self.player.is_fullscreen())

    def test_double_click_on_video(self) -> None:
        target = self.player._video or self.player._image
        event = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(10, 10), QPointF(10, 10),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(target, event)
        self.assertTrue(self.player.is_fullscreen())
        QApplication.sendEvent(target, QMouseEvent(
            QEvent.Type.MouseButtonDblClick, QPointF(10, 10), QPointF(10, 10), Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        self.assertFalse(self.player.is_fullscreen())

    def test_audio_has_no_fullscreen(self) -> None:
        self.player._kind = "audio"
        self.player._sync_fullscreen_button()
        self.assertFalse(self.player._btn_fullscreen.isVisibleTo(self.player))
        self.player.enter_fullscreen()
        self.assertFalse(self.player.is_fullscreen())

    def test_exits_when_main_window_hides(self) -> None:
        self.player.enter_fullscreen()
        self.window.hide()                    # программу спрятали в трей
        for _ in range(5):
            _app.processEvents()
        self.assertFalse(self.player.is_fullscreen())

    def test_controls_hide_only_while_playing(self) -> None:
        self.player.enter_fullscreen()
        self.player._controls.setVisible(True)
        self.player._fs_hide_controls()      # на паузе — панель остаётся
        self.assertTrue(self.player._controls.isVisibleTo(self.player))
        with mock.patch.object(self.player, "_is_playing", return_value=True):
            self.player._fs_hide_controls()
        self.assertFalse(self.player._controls.isVisibleTo(self.player))
        self.player._fs_activity()            # мышь шевельнулась — панель снова видна
        self.assertTrue(self.player._controls.isVisibleTo(self.player))


class TestSocialFullscreen(unittest.TestCase):
    def test_marks_work_in_fullscreen(self) -> None:
        from app.social import downloader as social
        from app.ui.social_page import SocialPage

        page = SocialPage(lambda: Path("D:/x"))
        page.resize(1100, 900)
        page.show()
        page._on_probed(page._probe_id, social.MediaInfo(url="u", title="Ролик", duration=300))
        page._session = mock.Mock()
        page.player._kind = "video"
        page.player.position_ms = lambda: 42_000
        page.player.duration_ms = lambda: 300_000
        page.player.enter_fullscreen()
        try:
            self.assertTrue(page.player.is_fullscreen())
            with mock.patch.object(QApplication, "focusWidget", return_value=page.player):
                press(page.player, Qt.Key.Key_I, 0x49, "i")
            self.assertEqual(page.clip_start.text(), "0:42")
        finally:
            page.player.exit_fullscreen()
            page.close()
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
