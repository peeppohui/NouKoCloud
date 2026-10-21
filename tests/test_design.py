"""Новый интерфейс «НоуКо Cloud»: тема, стартовый экран, очередь, шапка окна."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import AppConfig  # noqa: E402
from app.core.models import CloudFile, DownloadJob, EntryKind, JobState, ProviderId, Segment, SourceKind  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.file_tree import file_icon_name  # noqa: E402
from app.ui.queue_view import QueueView, part_fractions, status_line  # noqa: E402
from app.ui.start_page import StartPage, short_url, when_text  # noqa: E402
from app.ui.widgets import TitleBar  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def make_file(name: str, kind=EntryKind.FILE, size: int = 1000, mime: str = "") -> CloudFile:
    return CloudFile(provider=ProviderId.YANDEX, file_id=name, path=f"/{name}", name=name,
                     kind=kind, size=size, mime=mime, source=SourceKind.PUBLIC)


class TestTheme(unittest.TestCase):
    def tearDown(self) -> None:
        theme.manager().set_theme("dark")

    def test_fonts_from_design_are_bundled(self) -> None:
        from PySide6.QtGui import QFontDatabase

        theme.load_fonts()
        families = set(QFontDatabase.families())
        for family in (theme.BODY, theme.WIDE, theme.HEAD, theme.THIN):
            self.assertIn(family, families)

    def test_both_themes_build_and_switch(self) -> None:
        seen = []
        theme.manager().changed.connect(seen.append)
        try:
            theme.manager().set_theme("light")
            self.assertIn("#FFFFFF", _app.styleSheet())
            self.assertEqual(theme.manager().toggle(), "dark")
            self.assertIn("#06141B", _app.styleSheet())
        finally:
            theme.manager().changed.disconnect(seen.append)
        self.assertEqual([p.name for p in seen], ["light", "dark"])

    def test_pill_radius_is_half_height(self) -> None:
        """Qt рисует прямые углы, если радиус больше половины высоты."""
        import re

        css = theme.stylesheet(theme.DARK)
        for block in re.findall(r"\{([^{}]*min-height:\s*\d+px[^{}]*)\}", css):
            height = re.search(r"min-height:\s*(\d+)px", block)
            radius = re.search(r"border-radius:\s*(\d+)px", block)
            if height and radius:
                # min-height в стилях — без рамки (1 px сверху и снизу).
                border = 2 if re.search(r"border:\s*1px", block) else 0
                self.assertLessEqual(int(radius.group(1)) * 2, int(height.group(1)) + border, block)

    def test_icons_render(self) -> None:
        for name in list(theme.ICONS) + ["play", "pause", "target"]:
            self.assertFalse(theme.icon(name, "#CCD0CF").isNull(), name)
        self.assertFalse(theme.app_icon().isNull())
        self.assertFalse(theme.logo_pixmap(20).isNull())

    def test_qcolor_parses_rgba(self) -> None:
        color = theme.qcolor("rgba(17, 33, 45, 0.5)")
        self.assertEqual((color.red(), color.green(), color.blue()), (17, 33, 45))
        self.assertAlmostEqual(color.alphaF(), 0.5, places=1)


class TestRecentLinks(unittest.TestCase):
    def test_remember_moves_to_top_and_limits(self) -> None:
        cfg = AppConfig()
        for i in range(10):
            cfg.remember_link(f"https://disk.yandex.ru/d/{i}", f"Папка {i}")
        self.assertEqual(len(cfg.recent_links), 8)
        cfg.remember_link("https://disk.yandex.ru/d/5", "Папка 5")
        self.assertEqual(cfg.recent_links[0]["url"], "https://disk.yandex.ru/d/5")
        self.assertEqual(sum(1 for r in cfg.recent_links if r["url"].endswith("/5")), 1)
        cfg.forget_link("https://disk.yandex.ru/d/5")
        self.assertNotIn("https://disk.yandex.ru/d/5", [r["url"] for r in cfg.recent_links])

    def test_when_text(self) -> None:
        today = date(2026, 10, 21)
        ts = time.mktime((2026, 10, 21, 12, 0, 0, 0, 0, -1))
        self.assertEqual(when_text(ts, today), "сегодня")
        self.assertEqual(when_text(ts - 86400, today), "вчера")
        self.assertEqual(when_text(ts - 9 * 86400, today), "12.10")
        self.assertEqual(when_text("мусор", today), "")
        self.assertEqual(short_url("https://disk.yandex.ru/d/X"), "disk.yandex.ru/d/X")

    def test_start_page_lists_and_signals(self) -> None:
        page = StartPage()
        page.resize(1200, 900)
        page.show()
        opened, forgotten = [], []
        page.open_link.connect(opened.append)
        page.forget_link.connect(forgotten.append)
        page.set_recent([])
        self.assertFalse(page._recent_box.isVisibleTo(page), "нет ссылок — нет раздела")
        page.set_recent([{"title": "Съёмка", "url": "https://disk.yandex.ru/d/A", "ts": time.time()}])
        self.assertTrue(page._recent_box.isVisibleTo(page))
        page.link_edit.setText("  https://disk.yandex.ru/d/B ")
        page._emit_link()
        self.assertEqual(opened, ["https://disk.yandex.ru/d/B"])


class TestQueueCards(unittest.TestCase):
    def _job(self, state, parts, speed=0.0, error=None) -> DownloadJob:
        size = 400
        job = DownloadJob(file=make_file("clip.mov", size=size), dest_dir=Path(tempfile.gettempdir()))
        job.state = state
        job.total_bytes = size
        step = size // len(parts)
        job.segments = [Segment(index=i, start=i * step, end=(i + 1) * step - 1, received=int(step * p))
                        for i, p in enumerate(parts)]
        job.downloaded_bytes = sum(s.received for s in job.segments)
        job.speed_bps = speed
        job.error = error
        return job

    def test_parts_progress(self) -> None:
        job = self._job(JobState.RUNNING, [1.0, 0.5, 0.0, 0.25])
        self.assertEqual(part_fractions(job), [1.0, 0.5, 0.0, 0.25])
        many = self._job(JobState.RUNNING, [1.0] * 16)
        self.assertEqual(len(part_fractions(many)), 8, "больше 8 частей склеиваем")
        done = self._job(JobState.DONE, [1.0, 1.0])
        self.assertEqual(part_fractions(done), [1.0, 1.0])

    def test_status_line(self) -> None:
        state, details = status_line(self._job(JobState.RUNNING, [0.5, 0.5], speed=10.0))
        self.assertEqual(state, "Качается")
        self.assertIn("50%", details)
        self.assertIn("/с", details)
        state, _ = status_line(self._job(JobState.FAILED, [0.2], error="превышен лимит запросов"))
        self.assertIn("превышен лимит", state)

    def test_card_buttons_emit_job_actions(self) -> None:
        view = QueueView()
        running = self._job(JobState.RUNNING, [0.5])
        done = self._job(JobState.DONE, [1.0])
        view.sync([running, done])
        self.assertEqual(view.job_count(), 2)
        paused, cancelled, removed = [], [], []
        view.pause_requested.connect(paused.append)
        view.cancel_requested.connect(cancelled.append)
        view.remove_requested.connect(removed.append)
        view._cards[running.job_id]._btn_pause.click()
        view._cards[running.job_id]._btn_x.click()
        view._cards[done.job_id]._btn_x.click()
        self.assertEqual(paused, [running.job_id])
        self.assertEqual(cancelled, [running.job_id], "у идущей загрузки × — отмена")
        self.assertEqual(removed, [done.job_id], "у готовой × — убрать из списка")
        removed.clear()
        view._btn_clear.click()
        self.assertEqual(removed, [done.job_id], "«Убрать готовые» не трогает идущие")
        view.sync([running])
        self.assertEqual(view.job_count(), 1)


class TestTreeIcons(unittest.TestCase):
    def test_icon_by_kind(self) -> None:
        self.assertEqual(file_icon_name(make_file("A", EntryKind.FOLDER)), "folder")
        self.assertEqual(file_icon_name(make_file("A001.MOV")), "video")
        self.assertEqual(file_icon_name(make_file("zoom.wav")), "audio")
        self.assertEqual(file_icon_name(make_file("x.bin", mime="video/mp4")), "video")
        self.assertEqual(file_icon_name(make_file("кадр.JPG")), "image")
        self.assertEqual(file_icon_name(make_file("лист.pdf")), "doc")


class TestTitleBar(unittest.TestCase):
    def test_link_field_and_nav(self) -> None:
        bar = TitleBar()
        links = []
        bar.open_link.connect(links.append)
        bar.link_edit.setText(" https://disk.yandex.ru/d/X ")
        bar.link_edit.returnPressed.emit()
        self.assertEqual(links, ["https://disk.yandex.ru/d/X"])
        bar.set_active("settings")
        self.assertTrue(bar._nav["settings"].property("active"))
        self.assertFalse(bar._nav["accounts"].property("active"))
        bar.set_field_visible(False)
        self.assertFalse(bar._form.isVisibleTo(bar))

    def test_drag_area_excludes_buttons(self) -> None:
        bar = TitleBar()
        bar.resize(1280, 64)
        bar.show()
        _app.processEvents()
        button_center = bar._nav["accounts"].mapToGlobal(bar._nav["accounts"].rect().center())
        self.assertFalse(bar.is_drag_area(button_center), "по кнопке окно не таскаем")
        word = bar._word.mapToGlobal(bar._word.rect().center())
        self.assertTrue(bar.is_drag_area(word), "за надпись CLOUD — таскаем")
        bar.close()


if __name__ == "__main__":
    unittest.main()
