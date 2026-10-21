"""Отмеченная папка скачивается папкой, отдельные файлы — прямо в папку загрузок."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import AppConfig  # noqa: E402
from app.core.models import CloudFile, EntryKind, ProviderId, SourceKind  # noqa: E402
from app.providers.base import BrowseContext  # noqa: E402
from app.ui.file_tree import FileTree  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def f(name: str, kind=EntryKind.FILE) -> CloudFile:
    return CloudFile(provider=ProviderId.YANDEX, file_id=name, path=f"/{name}", name=name, kind=kind,
                     size=10, source=SourceKind.PUBLIC)


class TestCheckedDownloads(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = FileTree()
        self.ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X")
        self.root = self.tree.add_root("Съёмка", self.ctx, provider=ProviderId.YANDEX)
        self.tree.set_children(self.root, [f("Камера A", EntryKind.FOLDER), f("Звук", EntryKind.FOLDER),
                                           f("лист.pdf")])
        self.cam = self._child(self.root, "Камера A")
        self.tree.set_children(self.cam, [f("День 1", EntryKind.FOLDER), f("A001.mov"), f("A002.mov")])
        self.day = self._child(self.cam, "День 1")
        self.tree.set_children(self.day, [f("C0001.MP4")])
        self.sound = self._child(self.root, "Звук")
        self.tree.set_children(self.sound, [f("ZOOM1.wav"), f("ZOOM2.wav")])

    @staticmethod
    def _child(parent, name):
        for row in range(parent.rowCount()):
            if parent.child(row).text() == name:
                return parent.child(row)
        raise AssertionError(name)

    def _result(self):
        return sorted((file.name, path) for file, path in self.tree.checked_downloads())

    def test_checked_folder_becomes_folder_with_subfolders(self) -> None:
        self.cam.setCheckState(Qt.CheckState.Checked)
        self.assertEqual(self._result(), [
            ("A001.mov", ("Камера A",)),
            ("A002.mov", ("Камера A",)),
            ("C0001.MP4", ("Камера A", "День 1")),
        ])

    def test_single_files_go_straight_to_downloads(self) -> None:
        self._child(self.root, "лист.pdf").setCheckState(Qt.CheckState.Checked)
        self._child(self.sound, "ZOOM1.wav").setCheckState(Qt.CheckState.Checked)
        self.assertEqual(self._result(), [("ZOOM1.wav", ()), ("лист.pdf", ())],
                         "папка «Звук» отмечена не целиком — файл без папки")

    def test_folder_and_single_file_together(self) -> None:
        self.sound.setCheckState(Qt.CheckState.Checked)
        self._child(self.cam, "A001.mov").setCheckState(Qt.CheckState.Checked)
        self.assertEqual(self._result(), [
            ("A001.mov", ()), ("ZOOM1.wav", ("Звук",)), ("ZOOM2.wav", ("Звук",)),
        ])


class TestWindowQueuesIntoFolders(unittest.TestCase):
    def test_jobs_land_in_folder(self) -> None:
        from app.context import build_context
        from app.ui.main_window import MainWindow

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = AppConfig()
        config.download_dir = tmp.name
        ctx = build_context(config)
        win = MainWindow(ctx)
        # Гасим и фоновой цикл — иначе его задачи переживут тест.
        # addCleanup выполняется в обратном порядке, поэтому цикл гасим
        # первым в списке: тогда он остановится последним, после движка.
        self.addCleanup(ctx.runtime.stop)
        self.addCleanup(lambda: ctx.runtime.submit(ctx.engine.stop()).result(timeout=5))
        ctx.engine.start = _noop                 # без настоящих загрузок
        root = win.tree.add_root("Съёмка", BrowseContext(source=SourceKind.PUBLIC, public_key="k"),
                                 provider=ProviderId.YANDEX)
        win.tree.set_children(root, [f("Камера: A", EntryKind.FOLDER), f("лист.pdf")])
        cam = root.child(0) if root.child(0).text().startswith("Камера") else root.child(1)
        win.tree.set_children(cam, [f("A001.mov")])
        cam.setCheckState(Qt.CheckState.Checked)
        for row in range(root.rowCount()):
            if root.child(row).text() == "лист.pdf":
                root.child(row).setCheckState(Qt.CheckState.Checked)
        win._download_selected()
        deadline = time.time() + 5
        while time.time() < deadline and len(ctx.engine.snapshot()) < 2:
            QApplication.processEvents()
            time.sleep(0.02)
        dests = sorted((job.file.name, job.dest_dir) for job in ctx.engine.snapshot())
        self.assertEqual(dests, [
            ("A001.mov", Path(tmp.name) / "Камера_ A"),     # «:» в Windows нельзя
            ("лист.pdf", Path(tmp.name)),
        ])


async def _noop() -> None:
    return None


if __name__ == "__main__":
    unittest.main()
