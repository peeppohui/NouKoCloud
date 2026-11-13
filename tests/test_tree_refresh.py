"""Фоновое обновление папки не сбрасывает отметки (ТЗ 4.8)."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                          # noqa: E402
from PySide6.QtWidgets import QApplication             # noqa: E402

from app.core.models import CloudFile, EntryKind, ProviderId, SourceKind  # noqa: E402
from app.providers.base import BrowseContext            # noqa: E402
from app.ui.file_tree import FILE_ROLE, FileTree       # noqa: E402


def make_file(name: str, file_id: str) -> CloudFile:
    return CloudFile(
        provider=ProviderId.YANDEX,
        file_id=file_id,
        path=f"disk/{name}",
        name=name,
        kind=EntryKind.FILE,
        size=10,
        source=SourceKind.PUBLIC,
        public_key="pub",
    )


class TestRefreshFolder(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tree = FileTree()
        self.ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="pub", root_path="/")
        self.root = self.tree.add_root("Ссылка", self.ctx)

    def tearDown(self) -> None:
        self.tree.deleteLater()

    def _folder(self):
        folder = CloudFile(
            provider=ProviderId.YANDEX, file_id="dir", path="disk/папка", name="папка",
            kind=EntryKind.FOLDER, source=SourceKind.PUBLIC, public_key="pub",
        )
        self.tree.set_children(self.root, [folder])
        return folder, self.root.child(0, 0)

    def test_same_listing_keeps_rows(self) -> None:
        files = [make_file("a.mp4", "a"), make_file("b.mp4", "b")]
        folder, item = self._folder()
        self.tree.set_children(item, files)
        before = item.child(0, 0).data(FILE_ROLE)
        self.assertTrue(self.tree.refresh_folder(folder, self.ctx, files))
        # Список тот же — строки не пересоздавались (тот же объект CloudFile).
        self.assertIs(item.child(0, 0).data(FILE_ROLE), before)

    def test_changed_listing_keeps_checks(self) -> None:
        files = [make_file("a.mp4", "a"), make_file("b.mp4", "b")]
        folder, item = self._folder()
        self.tree.set_children(item, files)
        item.child(0, 0).setCheckState(Qt.CheckState.Checked)
        updated = [make_file("a.mp4", "a"), make_file("b.mp4", "b"), make_file("c.mp4", "c")]
        self.assertTrue(self.tree.refresh_folder(folder, self.ctx, updated))
        self.assertEqual(item.rowCount(), 3)
        checked = [
            item.child(row, 0).text()
            for row in range(item.rowCount())
            if item.child(row, 0).checkState() == Qt.CheckState.Checked
        ]
        self.assertIn("a.mp4", checked)

    def test_unknown_folder_is_ignored(self) -> None:
        other = make_file("other.mp4", "z")
        self.assertFalse(self.tree.refresh_folder(other, self.ctx, [other]))


if __name__ == "__main__":
    unittest.main()
