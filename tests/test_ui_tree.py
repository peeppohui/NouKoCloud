"""Тесты дерева файлов (headless, Qt offscreen).

Проверяем то, что нельзя поймать без UI: подстановку детей в модель,
раскрытие папок и работу чекбоксов.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.models import CloudFile, EntryKind, ProviderId, SourceKind  # noqa: E402
from app.providers.base import BrowseContext  # noqa: E402
from app.ui.file_tree import FileTree  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def make_file(name: str, kind=EntryKind.FILE, size: int = 1024) -> CloudFile:
    return CloudFile(
        provider=ProviderId.YANDEX,
        file_id=name,
        path=f"/{name}",
        name=name,
        kind=kind,
        size=size,
        source=SourceKind.PUBLIC,
    )


class TestFileTree(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = FileTree()
        self.ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X")

    def test_add_root_and_children(self) -> None:
        root = self.tree.add_root("Яндекс.Диск · X", self.ctx)
        self.tree.set_children(root, [make_file("папка", EntryKind.FOLDER), make_file("video.mp4")])

        model = self.tree._model
        self.assertEqual(model.rowCount(), 1)
        root_index = model.indexFromItem(root)
        self.assertEqual(model.rowCount(root_index), 2)

        first = root.child(0)
        self.assertEqual(first.text(), "папка")
        self.assertTrue(first.hasChildren(), "у папки должен быть плейсхолдер до загрузки")

    def test_children_replaced_not_duplicated(self) -> None:
        root = self.tree.add_root("корень", self.ctx)
        self.tree.set_children(root, [make_file("a.mp4"), make_file("b.mp4")])
        self.tree.set_children(root, [make_file("c.mp4")])
        model = self.tree._model
        self.assertEqual(model.rowCount(model.indexFromItem(root)), 1)
        self.assertEqual(root.child(0).text(), "c.mp4")

    def test_subfolder_children(self) -> None:
        root = self.tree.add_root("корень", self.ctx)
        self.tree.set_children(root, [make_file("папка", EntryKind.FOLDER)])
        folder = root.child(0)
        self.tree.set_children(folder, [make_file("внутри.mp4")])
        model = self.tree._model
        self.assertEqual(model.rowCount(model.indexFromItem(folder)), 1)
        self.assertEqual(folder.child(0).text(), "внутри.mp4")

    def test_expand_does_not_raise(self) -> None:
        root = self.tree.add_root("корень", self.ctx)
        self.tree.set_children(root, [make_file("папка", EntryKind.FOLDER)])
        self.tree.expand_item(root)          # раньше здесь падало setExpanded
        self.tree.expand_item(root.child(0))

    def test_checkbox_selection(self) -> None:
        root = self.tree.add_root("корень", self.ctx)
        self.tree.set_children(root, [make_file("a.mp4"), make_file("b.mp4")])
        self.assertEqual(self.tree.checked_files(), [])

        root.child(0).setCheckState(Qt.CheckState.Checked)
        selected = self.tree.checked_files()
        self.assertEqual([f.name for f in selected], ["a.mp4"])

    def test_checking_folder_checks_children(self) -> None:
        root = self.tree.add_root("корень", self.ctx)
        self.tree.set_children(root, [make_file("папка", EntryKind.FOLDER)])
        folder = root.child(0)
        self.tree.set_children(folder, [make_file("внутри.mp4")])

        folder.setCheckState(Qt.CheckState.Checked)
        selected = self.tree.checked_files()
        self.assertEqual([f.name for f in selected], ["внутри.mp4"])

    def test_select_all(self) -> None:
        root = self.tree.add_root("корень", self.ctx)
        self.tree.set_children(root, [make_file("a.mp4"), make_file("папка", EntryKind.FOLDER)])
        self.tree.set_children(root.child(1), [make_file("b.mp4")])

        self.tree._set_all_checked(True)
        names = sorted(f.name for f in self.tree.checked_files())
        self.assertEqual(names, ["a.mp4", "b.mp4"])

        self.tree._set_all_checked(False)
        self.assertEqual(self.tree.checked_files(), [])

    def test_empty_state(self) -> None:
        self.assertTrue(self.tree._view.isHidden())
        root = self.tree.add_root("корень", self.ctx)
        self.tree.set_children(root, [])
        self.assertFalse(self.tree._view.isHidden())

    def test_same_root_is_not_duplicated(self) -> None:
        first = self.tree.add_root("Яндекс.Диск · X", self.ctx)
        second = self.tree.add_root("Яндекс.Диск · X", self.ctx)
        self.assertIs(first, second)
        self.assertEqual(self.tree._model.rowCount(), 1)

    def test_lazy_loading_of_subfolder(self) -> None:
        """Папка подгружается по раскрытию — дети должны появиться."""
        calls = []

        def loader(file, ctx, done):
            calls.append(file.name)
            done([make_file("внутри.mp4")])

        self.tree._loader = loader
        root = self.tree.add_root("корень", self.ctx)
        self.tree.set_children(root, [make_file("папка", EntryKind.FOLDER)])
        folder = root.child(0)

        self.tree.request_children(folder, make_file("папка", EntryKind.FOLDER), self.ctx)
        self.assertEqual(calls, ["папка"])
        self.tree.handle_children_loaded(1, [make_file("внутри.mp4")])

        model = self.tree._model
        self.assertEqual(model.rowCount(model.indexFromItem(folder)), 1)
        self.assertEqual(folder.child(0).text(), "внутри.mp4")


if __name__ == "__main__":
    unittest.main()
