"""Вкладка «Соцсети»: параметры yt-dlp, понятные ошибки, сценарий страницы (без сети)."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.social import downloader as social  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def pump_until(cond, limit=5.0) -> bool:
    end = time.time() + limit
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


class TestOptions(unittest.TestCase):
    def test_video_for_editing_prefers_h264_in_mp4(self) -> None:
        opts = social.build_options(Path("C:/x"), "video", 1080, for_editing=True)
        self.assertEqual(opts["merge_output_format"], "mp4")
        self.assertEqual(opts["format_sort"][0], "vcodec:h264")
        self.assertIn("res:1080", opts["format_sort"])
        self.assertIn("[height<=1080]", opts["format"])
        self.assertTrue(opts["noplaylist"], "ссылка из плейлиста — одно видео, а не весь список")

    def test_best_quality_without_editing(self) -> None:
        opts = social.build_options(Path("C:/x"), "video", 0, for_editing=False)
        self.assertEqual(opts["format_sort"], ["res"])
        self.assertNotIn("height<=", opts["format"])

    def test_audio_extracts_chosen_format(self) -> None:
        opts = social.build_options(Path("C:/x"), "audio", audio_format="wav")
        self.assertEqual(opts["postprocessors"][0]["preferredcodec"], "wav")
        mp3 = social.build_options(Path("C:/x"), "audio", audio_format="mp3")
        self.assertEqual(mp3["postprocessors"][0]["preferredquality"], "320")

    def test_files_go_to_download_folder(self) -> None:
        opts = social.build_options(Path("D:/Загрузки"), "video")
        self.assertEqual(opts["paths"]["home"], str(Path("D:/Загрузки")))
        self.assertNotIn("/", opts["outtmpl"].replace("\\", "/"))

    def test_quality_choices_only_existing(self) -> None:
        info = social.MediaInfo(url="u", title="t", heights=[1080, 720, 360])
        self.assertEqual(social.quality_choices(info), [0, 1080, 720, 480, 360][:1] + [1080, 720, 360])
        self.assertEqual(social.quality_choices(social.MediaInfo(url="u", title="t")), [0])

    def test_info_from_dict(self) -> None:
        info = social.info_from_dict("u", {
            "title": "Ролик", "uploader": "Автор", "extractor_key": "Youtube", "duration": 125,
            "formats": [{"height": 720, "vcodec": "avc1"}, {"height": None, "vcodec": "none"}],
        })
        self.assertEqual((info.title, info.uploader, info.site, info.duration_text), ("Ролик", "Автор", "Youtube", "2:05"))
        self.assertEqual(info.heights, [720])
        self.assertTrue(info.has_video)
        audio_only = social.info_from_dict("u", {"title": "Подкаст", "formats": [{"vcodec": "none"}]})
        self.assertFalse(audio_only.has_video)

    def test_friendly_errors(self) -> None:
        cases = {
            "ERROR: Unsupported URL: https://example.com": "не поддерживается",
            "ERROR: [youtube] x: Private video. Sign in": "приватное",
            "ERROR: Sign in to confirm you're not a bot": "войти в аккаунт",
            "ERROR: Video unavailable": "не найдено",
            "что-то странное": "Подробности — в журнале",
        }
        for text, expected in cases.items():
            self.assertIn(expected, social.friendly(Exception(text)), text)


class TestSocialPage(unittest.TestCase):
    def setUp(self) -> None:
        from app.ui.social_page import SocialPage

        self._tmp = tempfile.TemporaryDirectory()
        self.page = SocialPage(lambda: Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_find_then_download_video(self) -> None:
        info = social.MediaInfo(url="https://youtu.be/x", title="Ролик", site="Youtube",
                                duration=61, heights=[1080, 720])
        calls = []

        def fake_download(url, dest, mode, height, fmt, editing, on_progress=None, cancel=None,
                          section=None, precise=True):
            calls.append((url, dest, mode, height, fmt, editing))
            on_progress(social.Progress(percent=50, speed=1024 * 1024, eta=3))
            path = Path(dest) / "Ролик.mp4"
            path.write_bytes(b"x")
            return path

        with mock.patch.object(social, "probe", return_value=info), \
                mock.patch.object(social, "download", side_effect=fake_download):
            self.page.link_edit.setText("https://youtu.be/x")
            self.page.find()
            self.assertTrue(pump_until(lambda: self.page.card.isVisibleTo(self.page)))
            self.assertEqual(self.page.title.text(), "Ролик")
            self.assertIn("1:01", self.page.meta.text())
            self.assertEqual([self.page.quality.itemData(i) for i in range(self.page.quality.count())],
                             [0, 1080, 720])
            self.page.quality.setCurrentIndex(2)
            self.page.start_download()
            row = next(iter(self.page._rows.values()))
            self.assertTrue(pump_until(lambda: row.path is not None))
        self.assertEqual(calls[0][2:5], ("video", 720, "mp3"))
        self.assertEqual(calls[0][1], Path(self._tmp.name))
        self.assertTrue(row.btn_open.isVisibleTo(row))

    def test_audio_mode_and_errors(self) -> None:
        info = social.MediaInfo(url="u", title="Подкаст", heights=[])
        with mock.patch.object(social, "probe", return_value=info), \
                mock.patch.object(social, "download", side_effect=social.SocialError("Видео закрыто")):
            self.page.link_edit.setText("u")
            self.page.find()
            self.assertTrue(pump_until(lambda: self.page.card.isVisibleTo(self.page)))
            self.page.mode.set_current(1)
            self.page._sync_mode(1)
            self.assertTrue(self.page.audio_format.isVisibleTo(self.page))
            self.assertFalse(self.page.quality.isVisibleTo(self.page))
            self.page.start_download()
            row = next(iter(self.page._rows.values()))
            self.assertTrue(pump_until(lambda: bool(row.status.property("finished"))))
        self.assertEqual(row.status.text(), "Видео закрыто")

    def test_bad_link_shows_message(self) -> None:
        with mock.patch.object(social, "probe", side_effect=social.SocialError("Этот сайт не поддерживается")):
            self.page.link_edit.setText("https://example.com")
            self.page.find()
            self.assertTrue(pump_until(lambda: "не поддерживается" in self.page.status.text()))
        self.assertFalse(self.page.card.isVisibleTo(self.page))


if __name__ == "__main__":
    unittest.main()
