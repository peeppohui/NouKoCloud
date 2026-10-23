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


def tiny_mp4() -> bytes:
    """Крошечный настоящий ролик.

    Подделка не годится: ffprobe должен её разобрать, иначе yt-dlp падает
    на постобработке, а проверить мы хотим именно скачивание.
    """
    import shutil
    import subprocess

    from app.core.paths import bundled_ffmpeg

    ffmpeg = bundled_ffmpeg() or shutil.which("ffmpeg")
    if ffmpeg is None:
        raise unittest.SkipTest("ffmpeg не найден — ролик не сгенерировать")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "tiny.mp4"
        subprocess.run(
            [str(ffmpeg), "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
             str(out)],
            check=True, capture_output=True,
        )
        return out.read_bytes()


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
        warned = social.info_from_dict("u", {"title": "Ролик"}, "No supported JavaScript runtime")
        self.assertEqual(warned.warning, "No supported JavaScript runtime")

    def test_friendly_errors(self) -> None:
        cases = {
            "ERROR: Unsupported URL: https://example.com": "не поддерживается",
            "ERROR: [youtube] x: Private video. Sign in": "приватное",
            "ERROR: Sign in to confirm you're not a bot": "войти в аккаунт",
            "ERROR: Video unavailable": "не найдено",
            "WARNING: No supported JavaScript runtime": "JavaScript-движок",
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

    def test_js_runtime_warning_is_shown(self) -> None:
        info = social.MediaInfo(url="u", title="Ролик", heights=[1080, 720],
                                warning="No supported JavaScript runtime, some formats are missing")
        with mock.patch.object(social, "probe", return_value=info):
            self.page.link_edit.setText("u")
            self.page.find()
            self.assertTrue(pump_until(lambda: self.page.card.isVisibleTo(self.page)))
        self.assertTrue(self.page.warning.isVisibleTo(self.page))
        self.assertIn("JavaScript", self.page.warning.text())
        self.assertIn("не скачаться", self.page.quality.itemText(1))
        self.assertNotIn("не скачаться", self.page.quality.itemText(2))

    def test_clip_toggle_scrolls_to_download_button(self) -> None:
        info = social.MediaInfo(url="u", title="Ролик", heights=[720])
        with mock.patch.object(social, "probe", return_value=info):
            self.page.link_edit.setText("u")
            self.page.find()
            self.assertTrue(pump_until(lambda: self.page.card.isVisibleTo(self.page)))
        scrolled = []
        self.page._scroll.ensureWidgetVisible = lambda widget, *args: scrolled.append(widget)
        self.page.clip.setChecked(True)
        self.assertTrue(pump_until(lambda: bool(scrolled)))
        self.assertIs(scrolled[-1], self.page.btn_download)

    def test_bad_link_shows_message(self) -> None:
        with mock.patch.object(social, "probe", side_effect=social.SocialError("нет такой ссылки")):
            self.page.link_edit.setText("https://example.com")
            self.page.find()
            self.assertTrue(pump_until(lambda: "нет такой ссылки" in self.page.status.text()))
        self.assertFalse(self.page.card.isVisibleTo(self.page))


class TestPiecesDownload(unittest.TestCase):
    """Скачивание обычного файла частями по Range.
    Раньше файл превращался в «нарезанный» DASH, но yt-dlp про Range в таких
    частях не знает: каждая часть тянулась целиком, а потом они склеивались
    подряд — на выходе был файл в N раз больше нужного, и ролик на 2 ГБ
    качался две тысячи раз, то есть никогда.
    """

    def test_dl_returns_pair(self) -> None:
        """yt-dlp ждёт от dl() пару (успех, real_download)."""
        from .helpers import start_range_server, stop_server

        payload = bytes((i * 31 + 7) % 256 for i in range(200 * 1024))
        handler, thread, server = start_range_server(payload)
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/movie.mp4"
            with tempfile.TemporaryDirectory() as tmp:
                target = str(Path(tmp) / "movie.mp4")
                ydl_cls = social._yt_class()
                opts = {"quiet": True, "no_warnings": True, "noprogress": True,
                        "outtmpl": target, "paths": {"home": tmp}}
                with ydl_cls(opts) as ydl:
                    info = {"url": url, "ext": "mp4", "protocol": "https",
                            "filesize": len(payload), "http_headers": {},
                            "extractor_key": "Test"}
                    result = ydl.dl(target, info)
                self.assertIsInstance(result, tuple, "dl() должен вернуть пару")
                self.assertEqual(len(result), 2)
                self.assertTrue(result[0], "загрузка должна быть успешной")
                data = Path(target).read_bytes()
                self.assertEqual(len(data), len(payload), "размер должен совпасть")
                self.assertEqual(data, payload, "части должны лечь на свои места")
        finally:
            stop_server(server)

    def test_pieces_fall_back_when_range_not_supported(self) -> None:
        """Сервер без Range — качаем обычным способом, а не падаем."""
        from .helpers import start_range_server, stop_server

        payload = b"x" * 4096
        handler, thread, server = start_range_server(payload)
        try:
            handler.payload = payload
            url = f"http://127.0.0.1:{server.server_address[1]}/movie.mp4"
            with tempfile.TemporaryDirectory() as tmp:
                target = str(Path(tmp) / "movie.mp4")
                ydl_cls = social._yt_class()
                opts = {"quiet": True, "no_warnings": True, "noprogress": True,
                        "outtmpl": target, "paths": {"home": tmp}}
                with ydl_cls(opts) as ydl:
                    info = {"url": url, "ext": "mp4", "protocol": "https",
                            "filesize": len(payload), "http_headers": {},
                            "extractor_key": "Test"}
                    result = ydl.dl(target, info)
                self.assertTrue(result[0])
        finally:
            stop_server(server)

    def test_whole_way_through_the_page(self) -> None:
        """Страница «Соцсети» находит ролик и кладёт файл на диск.

        С настоящим разбором и настоящим скачиванием, но по локальному
        серверу — сеть не нужна.
        """
        from .helpers import start_range_server, stop_server

        # Настоящий короткий ролик: иначе ffprobe не сможет его разобрать
        # и yt-dlp ругнётся на постобработке.
        video = tiny_mp4()
        handler, thread, server = start_range_server(video)
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/movie.mp4"
            with tempfile.TemporaryDirectory() as tmp:
                from app.ui.social_page import SocialPage

                page = SocialPage(lambda: Path(tmp))
                page.link_edit.setText(url)
                page.find()
                self.assertTrue(pump_until(lambda: page.card.isVisibleTo(page)),
                                "ролик не нашёлся")
                page.start_download()
                row = next(iter(page._rows.values()))
                self.assertTrue(pump_until(lambda: row.path is not None, 60),
                                "файл не скачался")
                self.assertTrue(Path(row.path).exists())
                # Размер может отличаться от исходного: видео и звук
                # склеиваются заново.
                self.assertGreater(Path(row.path).stat().st_size, 0)
        finally:
            stop_server(server)


if __name__ == "__main__":
    unittest.main()


