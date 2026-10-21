"""Предпросмотр в «Соцсетях», отметки I/O и подстройка интерфейса под размер окна."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app.social import downloader as social  # noqa: E402
from app.social.preview import pick_streams  # noqa: E402

_app = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def fmt(fid, vcodec="none", acodec="none", height=None, protocol="https", **extra):
    return {"format_id": fid, "url": f"https://cdn/{fid}", "vcodec": vcodec, "acodec": acodec,
            "height": height, "protocol": protocol, **extra}


class TestPickStreams(unittest.TestCase):
    def test_youtube_separate_tracks(self) -> None:
        info = {"formats": [
            fmt("140", acodec="mp4a.40.2", abr=128), fmt("251", acodec="opus", abr=160),
            fmt("134", "avc1.4d401e", height=360), fmt("136", "avc1.4d401f", height=720),
            fmt("137", "avc1.640028", height=1080), fmt("247", "vp9", height=720),
            fmt("398", "av01.0.08M.08", height=720), fmt("312", "avc1", height=1080, protocol="m3u8_native"),
        ]}
        pick = pick_streams(info)
        self.assertEqual(pick.video["format_id"], "136")        # H.264, не выше 720p
        self.assertEqual(pick.audio["format_id"], "140")        # AAC

    def test_single_file_with_sound(self) -> None:
        info = {"formats": [fmt("sd", "h264", "aac", 480), fmt("hd", "h264", "aac", 720),
                            fmt("fhd", "h264", "aac", 1080)]}
        pick = pick_streams(info)
        self.assertEqual(pick.video["format_id"], "hd")
        self.assertIsNone(pick.audio)

    def test_only_hls(self) -> None:
        info = {"formats": [fmt("hls-720", "avc1", "mp4a", 720, protocol="m3u8_native")]}
        self.assertEqual(pick_streams(info).video["format_id"], "hls-720")

    def test_nothing(self) -> None:
        self.assertIsNone(pick_streams({"formats": []}))


class TestPreciseTime(unittest.TestCase):
    def test_tenths(self) -> None:
        self.assertEqual(social.format_time(65.43, precise=True), "1:05.4")
        self.assertEqual(social.format_time(65.0, precise=True), "1:05")
        self.assertEqual(social.format_time(65.43), "1:05")
        self.assertEqual(social.parse_time(social.format_time(3725.5, precise=True)), 3725.5)


class TestMarks(unittest.TestCase):
    def setUp(self) -> None:
        from app.ui.social_page import SocialPage

        self.page = SocialPage(lambda: Path("D:/x"))
        self.page.resize(1000, 800)
        self.page._on_probed(self.page._probe_id, social.MediaInfo(url="u", title="Ролик", duration=600))
        self.page._session = mock.Mock()
        self.position = 0
        self.page.player.position_ms = lambda: self.position
        self.page.player.duration_ms = lambda: 600_000

    def tearDown(self) -> None:
        self.page.deleteLater()

    def test_i_and_o(self) -> None:
        self.assertFalse(self.page.clip.isChecked())
        self.position = 65_400
        self.page.mark_in()
        self.assertTrue(self.page.clip.isChecked())               # I сам включает «фрагмент»
        self.assertEqual(self.page.clip_start.text(), "1:05.4")
        self.position = 90_000
        self.page.mark_out()
        self.assertEqual(self.page.clip_end.text(), "1:30")
        self.assertEqual(self.page._clip_range(), (65.4, 90.0))
        self.assertIn("0:24.6", self.page.clip_info.text())

    def test_in_after_out_resets_out(self) -> None:
        self.position = 30_000
        self.page.mark_out()
        self.position = 45_000
        self.page.mark_in()                                       # начало позже конца
        self.assertEqual(self.page._clip_range(), (45.0, 600.0))
        self.position = 10_000
        self.page.mark_out()                                      # конец раньше начала
        self.assertEqual(self.page._clip_range(), (0.0, 10.0))

    def test_keys_need_preview(self) -> None:
        self.page._session = None
        self.page.mark_in()
        self.assertFalse(self.page.clip.isChecked())
        self.assertIn("предпросмотр", self.page.status.text())

    def test_marks_on_timeline(self) -> None:
        with mock.patch.object(self.page.player, "set_marks") as marks:
            self.position = 5_000
            self.page.mark_in()
        marks.assert_called_with(5000, 600_000)
        with mock.patch.object(self.page.player, "set_marks") as marks:
            self.page.clip.setChecked(False)
        marks.assert_called_with(None, None)

    def _press(self, key: int, vk: int, text: str, target=None, modifiers=None) -> None:
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        event = QKeyEvent(QEvent.Type.KeyPress, key, modifiers or Qt.KeyboardModifier.NoModifier,
                          0, vk, 0, text)
        QApplication.sendEvent(target or self.page.player, event)

    def test_keys_any_layout(self) -> None:
        from PySide6.QtCore import Qt

        self.page.show()
        self.position = 12_300
        self._press(0x428, 0x49, "ш")                             # русская раскладка: «Ш», клавиша I
        self.assertEqual(self.page.clip_start.text(), "0:12.3")
        self.position = 20_000
        self._press(int(Qt.Key.Key_O), 0x4F, "o")                 # английская
        self.assertEqual(self.page.clip_end.text(), "0:20")
        self.position = 15_000
        self._press(0x429, 0, "щ")                                # без кода клавиши — по символу
        self.assertEqual(self.page.clip_end.text(), "0:15")

    def test_keys_ignored_while_typing_link_or_with_ctrl(self) -> None:
        from PySide6.QtCore import Qt

        self.page.show()
        self.page.activateWindow()
        self.page.link_edit.setFocus()
        self.position = 5_000
        with mock.patch.object(QApplication, "focusWidget", return_value=self.page.link_edit):
            self._press(0x428, 0x49, "ш", target=self.page.link_edit)
        self.assertFalse(self.page.clip.isChecked())
        self._press(int(Qt.Key.Key_I), 0x49, "i", modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertFalse(self.page.clip.isChecked())


class TestResponsive(unittest.TestCase):
    def test_breakpoint(self) -> None:
        from app.ui.widgets import Breakpoint

        host = QWidget()
        host.resize(1200, 800)
        host.show()
        seen: list[bool] = []
        Breakpoint(host, 900, seen.append)
        host.resize(800, 800)
        host.resize(850, 800)
        host.resize(1000, 800)
        self.assertEqual(seen, [False, True, False])              # только при переходе порога

    def test_title_bar_compacts(self) -> None:
        from app.ui.widgets import TitleBar

        host = QWidget()                     # как в окне: шапка — дочерний виджет
        host.resize(1400, 64)
        host.show()
        bar = TitleBar(host)
        bar.setGeometry(0, 0, 1400, 64)
        bar.show()
        self.assertEqual(bar._nav["social"].text(), "Соцсети")
        bar.setGeometry(0, 0, 720, 64)          # минимальная ширина окна
        self.assertEqual(bar._nav["social"].text(), "")           # только значок
        self.assertEqual(bar._nav["social"].toolTip(), "Соцсети")
        self.assertLessEqual(bar.layout().minimumSize().width(), 720)
        bar.setGeometry(0, 0, 1400, 64)
        self.assertEqual(bar._nav["social"].text(), "Соцсети")
        host.deleteLater()

    def test_social_stacks_in_narrow_window(self) -> None:
        from PySide6.QtWidgets import QBoxLayout

        from app.ui.social_page import SocialPage

        page = SocialPage(lambda: Path("D:/x"))
        page.show()
        page.resize(1300, 800)
        self.assertEqual(page.layout().direction(), QBoxLayout.Direction.LeftToRight)
        page.resize(800, 800)
        self.assertEqual(page.layout().direction(), QBoxLayout.Direction.TopToBottom)
        page.deleteLater()


if __name__ == "__main__":
    unittest.main()
