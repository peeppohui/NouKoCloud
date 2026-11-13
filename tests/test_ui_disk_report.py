"""Проверка места на диске и отчёта CSV из окна.

Диалоги подменяются: тесты не должны ждать нажатия кнопки.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.context import build_context  # noqa: E402
from app.core.config import AppConfig  # noqa: E402
from app.core.diskspace import SpaceCheck  # noqa: E402
from app.core.models import CloudFile, EntryKind, ProviderId, SourceKind  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def space(ok: bool) -> SpaceCheck:
    """Результат проверки: свободно много или мало."""
    return SpaceCheck(Path("."), 0, 0, 100 if ok else 1, 10)


def make_file(name: str = "файл.mp4", size: int = 1024) -> CloudFile:
    return CloudFile(
        provider=ProviderId.YANDEX,
        file_id=name,
        path=f"/исх/{name}",
        name=name,
        kind=EntryKind.FILE,
        size=size,
        source=SourceKind.PUBLIC,
    )


class FakeButton:
    def __init__(self, text: str) -> None:
        self.text = text

    def setDefault(self, _flag: bool) -> None:  # noqa: N802 - API Qt
        pass


class FakeMessageBox:
    """Минимальная замена QMessageBox: «нажимает» кнопку по тексту."""

    Icon = SimpleNamespace(Information=1, Warning=2)
    ButtonRole = SimpleNamespace(
        ActionRole=1, DestructiveRole=2, RejectRole=3, AcceptRole=4)

    click_text: str | None = None

    def __init__(self, *_args, **_kwargs) -> None:
        self._buttons: list[FakeButton] = []
        self._clicked: FakeButton | None = None

    def setWindowTitle(self, _title: str) -> None:  # noqa: N802
        pass

    def setIcon(self, _icon) -> None:  # noqa: N802
        pass

    def setText(self, _text: str) -> None:  # noqa: N802
        pass

    def setInformativeText(self, _text: str) -> None:  # noqa: N802
        pass

    def addButton(self, text: str, _role) -> FakeButton:  # noqa: N802
        button = FakeButton(text)
        self._buttons.append(button)
        return button

    def exec(self) -> int:  # noqa: N802
        for button in self._buttons:
            if button.text == self.click_text:
                self._clicked = button
                break
        return 0

    def clickedButton(self) -> FakeButton | None:  # noqa: N802
        return self._clicked


class SpaceCheckCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = AppConfig()
        self.config.download_dir = str(Path(self._tmp.name))
        self.ctx = build_context(self.config)
        self.win = MainWindow(self.ctx)
        self.dest = Path(self._tmp.name)
        FakeMessageBox.click_text = None

    def tearDown(self) -> None:
        future = self.ctx.runtime.submit(self.ctx.engine.stop())
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self.ctx.cache.close()
        self.ctx.runtime.stop()
        self._tmp.cleanup()

    def test_enough_space_returns_dest_without_dialog(self) -> None:
        ok = space(True)
        with mock.patch("app.ui.main_window.check_space", return_value=ok) as check, \
                mock.patch("app.ui.main_window.QMessageBox", FakeMessageBox):
            result = self.win._ensure_space([make_file()], self.dest)
        self.assertEqual(result, self.dest)
        check.assert_called_once()
        self.assertEqual(FakeMessageBox.click_text, None)

    def test_unreadable_folder_returns_none(self) -> None:
        with mock.patch("app.ui.main_window.check_space", side_effect=OSError("нет")), \
                mock.patch.object(self.win, "_on_notice") as notice:
            result = self.win._ensure_space([make_file()], self.dest)
        self.assertIsNone(result)
        self.assertEqual(notice.call_args[0][0], "err")

    def test_force_button_keeps_folder(self) -> None:
        low = space(False)
        FakeMessageBox.click_text = "Всё равно скачать"
        with mock.patch("app.ui.main_window.check_space", return_value=low), \
                mock.patch("app.ui.main_window.QMessageBox", FakeMessageBox):
            result = self.win._ensure_space([make_file()], self.dest)
        self.assertEqual(result, self.dest)

    def test_cancel_returns_none(self) -> None:
        low = space(False)
        FakeMessageBox.click_text = "Отмена"
        with mock.patch("app.ui.main_window.check_space", return_value=low), \
                mock.patch("app.ui.main_window.QMessageBox", FakeMessageBox):
            result = self.win._ensure_space([make_file()], self.dest)
        self.assertIsNone(result)

    def test_choosing_another_folder_rechecks_space(self) -> None:
        other = Path(self._tmp.name) / "другая"
        other.mkdir()
        low = space(False)
        ok = space(True)
        FakeMessageBox.click_text = "Выбрать папку"
        with mock.patch(
                "app.ui.main_window.check_space", side_effect=[low, ok]) as check, \
                mock.patch("app.ui.main_window.QMessageBox", FakeMessageBox), \
                mock.patch(
                    "app.ui.main_window.QFileDialog.getExistingDirectory",
                    return_value=str(other)), \
                mock.patch.object(self.ctx.config, "save") as save:
            result = self.win._ensure_space([make_file()], self.dest)
        self.assertEqual(result, other)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(self.ctx.config.download_dir, str(other))
        save.assert_called_once()

    def test_cancelled_folder_choice_returns_none(self) -> None:
        low = space(False)
        FakeMessageBox.click_text = "Выбрать папку"
        with mock.patch("app.ui.main_window.check_space", return_value=low), \
                mock.patch("app.ui.main_window.QMessageBox", FakeMessageBox), \
                mock.patch(
                    "app.ui.main_window.QFileDialog.getExistingDirectory",
                    return_value=""):
            result = self.win._ensure_space([make_file()], self.dest)
        self.assertIsNone(result)


class ExportCsvCase(SpaceCheckCase):
    def test_empty_queue_warns_and_writes_nothing(self) -> None:
        with mock.patch.object(self.win, "_on_notice") as notice, \
                mock.patch("app.ui.main_window.write_report") as write:
            self.win._export_csv()
        write.assert_not_called()
        self.assertEqual(notice.call_args[0][0], "warn")

    def test_report_written_next_to_downloads(self) -> None:
        jobs = [object()]
        target = self.dest / "report.csv"
        with mock.patch.object(self.ctx.engine, "snapshot", return_value=jobs), \
                mock.patch("app.ui.main_window.default_report_path",
                           return_value=target) as make_path, \
                mock.patch("app.ui.main_window.write_report") as write, \
                mock.patch("app.ui.main_window.QMessageBox", FakeMessageBox), \
                mock.patch.object(self.win, "_on_notice") as notice:
            self.win._export_csv()
        make_path.assert_called_once_with(self.config.download_dir_path)
        write.assert_called_once_with(jobs, target)
        self.assertEqual(notice.call_args[0][0], "info")

    def test_write_error_is_reported(self) -> None:
        target = self.dest / "report.csv"
        with mock.patch.object(self.ctx.engine, "snapshot", return_value=[object()]), \
                mock.patch("app.ui.main_window.default_report_path", return_value=target), \
                mock.patch("app.ui.main_window.write_report", side_effect=OSError("нет")), \
                mock.patch.object(self.win, "_on_notice") as notice:
            self.win._export_csv()
        self.assertEqual(notice.call_args[0][0], "err")

    def test_queue_exposes_csv_signal(self) -> None:
        self.assertTrue(hasattr(self.win.queue_view, "csv_requested"))


if __name__ == "__main__":
    unittest.main()
