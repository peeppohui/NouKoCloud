"""Фрагменты из соцсетей: время, параметры yt-dlp, посредник для ffmpeg, поля во вкладке."""

from __future__ import annotations

import os
import threading
import unittest
import urllib.request
from pathlib import Path

from app.social import downloader as social
from app.social.range_proxy import RangeProxy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestTime(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(social.parse_time("90"), 90)
        self.assertEqual(social.parse_time("1:30"), 90)
        self.assertEqual(social.parse_time("1:02:30"), 3750)
        self.assertEqual(social.parse_time(" 0:05,5 "), 5.5)
        for bad in ("", "1:", "1:75", "a", "1:2:3:4", "-5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                social.parse_time(bad)

    def test_format_and_label(self) -> None:
        self.assertEqual(social.format_time(75), "1:15")
        self.assertEqual(social.format_time(3750), "1:02:30")
        self.assertNotIn(":", social.clip_label(65, 100))     # двоеточие в именах Windows нельзя
        # Метка не должна выглядеть как десятичная дробь: 2–5 секунд — это
        # «00-02–00-05», а не «0.02–0.05».
        self.assertEqual(social.clip_label(2, 5), "00-02–00-05")
        self.assertEqual(social.clip_label(60, 90), "01-00–01-30")


class TestClipOptions(unittest.TestCase):
    def test_section_options(self) -> None:
        opts = social.build_options(Path("D:/x"), "video", section=(60, 90), precise=True)
        self.assertIn("download_ranges", opts)
        self.assertTrue(opts["force_keyframes_at_cuts"])
        self.assertIn("[01-00–01-30]", opts["outtmpl"])           # не затрёт целое видео
        self.assertIn("libx264", opts["external_downloader_args"]["ffmpeg_o"])

    def test_fast_cut_and_audio(self) -> None:
        opts = social.build_options(Path("D:/x"), "video", section=(0, 10), precise=False)
        self.assertFalse(opts["force_keyframes_at_cuts"])
        self.assertNotIn("external_downloader_args", opts)
        audio = social.build_options(Path("D:/x"), "audio", section=(0, 10), precise=True)
        self.assertNotIn("external_downloader_args", audio)    # звук перекодирует сам конвертер

    def test_whole_video_has_no_ranges(self) -> None:
        self.assertNotIn("download_ranges", social.build_options(Path("D:/x"), "video"))


class TestRangeProxy(unittest.TestCase):
    def setUp(self) -> None:
        self.data = os.urandom(3 * 1024 * 1024 + 777)
        self.calls: list[tuple[int, int]] = []
        lock = threading.Lock()

        def fetch(url, headers, start, end):
            with lock:
                self.calls.append((start, end))
            self.assertEqual(url, "https://cdn/v.mp4")
            self.assertEqual(headers.get("User-Agent"), "UA")
            return self.data[start:end + 1]

        self.proxy = RangeProxy(fetch, piece=256 * 1024, window=8)
        self.url = self.proxy.add("https://cdn/v.mp4", {"User-Agent": "UA"}, len(self.data))

    def tearDown(self) -> None:
        self.proxy.close()

    def _get(self, rng: str | None) -> tuple[int, dict, bytes]:
        req = urllib.request.Request(self.url, headers={"Range": rng} if rng else {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()

    def test_whole_file(self) -> None:
        status, headers, body = self._get(None)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.data)
        self.assertGreater(len(self.calls), 8)                 # частями, а не одним запросом

    def test_ranges(self) -> None:
        status, headers, body = self._get("bytes=1000000-")
        self.assertEqual(status, 206)
        self.assertEqual(body, self.data[1000000:])
        self.assertEqual(headers["Content-Range"], f"bytes 1000000-{len(self.data) - 1}/{len(self.data)}")
        _, _, body = self._get("bytes=10-19")
        self.assertEqual(body, self.data[10:20])
        _, _, body = self._get("bytes=-100")
        self.assertEqual(body, self.data[-100:])

    def test_out_of_range(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as err:
            self._get(f"bytes={len(self.data) + 5}-")
        self.assertEqual(err.exception.code, 416)

    def test_unknown_path(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"http://127.0.0.1:{self.proxy.port}/99", timeout=5)
        self.assertEqual(err.exception.code, 404)


class TestClipFields(unittest.TestCase):
    def test_validation(self) -> None:
        from PySide6.QtWidgets import QApplication

        from app.ui.social_page import SocialPage

        QApplication.instance() or QApplication([])
        page = SocialPage(lambda: Path("D:/x"))
        page._on_probed(page._probe_id, social.MediaInfo(url="u", title="Ролик", duration=125))
        self.assertEqual(page.clip_start.text(), "0:00")
        self.assertEqual(page.clip_end.text(), "2:05")
        page.clip.setChecked(True)
        self.assertEqual(page._clip_range(), (0.0, 125.0))
        page.clip_start.setText("1:00")
        page.clip_end.setText("1:30")
        self.assertEqual(page._clip_range(), (60.0, 90.0))
        self.assertTrue(page.btn_download.isEnabled())
        self.assertIn("0:30", page.clip_info.text())
        page.clip_end.setText("0:30")
        self.assertIsInstance(page._clip_range(), str)
        self.assertFalse(page.btn_download.isEnabled())
        page.clip_end.setText("5:00")
        self.assertIn("2:05", page._clip_range())
        page.clip.setChecked(False)
        self.assertTrue(page.btn_download.isEnabled())         # всё видео — фрагмент не мешает
        page.deleteLater()


if __name__ == "__main__":
    unittest.main()
