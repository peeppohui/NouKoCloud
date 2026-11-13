"""Тесты поиска и фильтра по типу в дереве файлов (headless, Qt offscreen).

Поиск идёт только по уже загруженным узлам, не ходит в сеть, не меняет
отметки и восстанавливает раскрытие при сбросе.
"""

from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.models import CloudFile, EntryKind, ProviderId, SourceKind  # noqa: E402
from app.providers.base import BrowseContext  # noqa: E402
from app.ui.file_tree import HINT_ROLE, FileTree, file_type_group, normalize_search  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def make_file(name: str, kind=EntryKind.FILE, size: int = 1024, mime: str = "") -> CloudFile:
    return CloudFile(
        provider=ProviderId.YANDEX,
        file_id=name,
        path=f"/{name}",
        name=name,
        kind=kind,
        size=size,
        source=SourceKind.PUBLIC,
        mime=mime,
    )


def names(item) -> list[str]:
    return [item.child(r).text() for r in range(item.rowCount())]


def find_child(item, name: str):
    for row in range(item.rowCount()):
        child = item.child(row)
        if child is not None and child.text() == name:
            return child
    return None


def row_hidden(tree: FileTree, item) -> bool:
    parent = item.parent()
    parent_index = tree._model.indexFromItem(parent) if parent is not None else None
    if parent_index is None:
        from PySide6.QtCore import QModelIndex

        parent_index = QModelIndex()
    return tree._view.isRowHidden(item.row(), parent_index)


class TestNormalization(unittest.TestCase):
    def test_case_and_yo(self) -> None:
        self.assertEqual(normalize_search("ЁЛКА"), "елка")
        self.assertEqual(normalize_search("ВиДеО"), "видео")

    def test_type_groups(self) -> None:
        self.assertEqual(file_type_group(make_file("a.mp4")), "video")
        self.assertEqual(file_type_group(make_file("a.WAV")), "audio")
        self.assertEqual(file_type_group(make_file("a.jpg")), "image")
        self.assertEqual(file_type_group(make_file("a.pdf")), "doc")
        self.assertEqual(file_type_group(make_file("a.zip")), "other")
        self.assertEqual(file_type_group(make_file("папка", EntryKind.FOLDER)), "folder")


class TestTreeSearch(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = FileTree()
        self.ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X")
        self.root = self.tree.add_root("Ссылка · X", self.ctx, provider=ProviderId.YANDEX)
        self.tree.set_children(self.root, [
            make_file("Ёлка.mp4"),
            make_file("праздник.MOV"),
            make_file("звук.wav"),
            make_file("фото.jpg"),
            make_file("документ.pdf"),
            make_file("архив.zip"),
            make_file("материалы", EntryKind.FOLDER),
        ])
        self.folder = find_child(self.root, "материалы")
        self.tree.set_children(self.folder, [
            make_file("елка_2.mp4"),
            make_file("заметки.txt"),
        ])

    def apply(self, text: str = "", group: str = "all") -> None:
        self.tree._search.setText(text)
        index = self.tree._type.findData(group)
        self.tree._type.setCurrentIndex(index)
        self.tree._apply_filter_now()

    def assert_visible(self, item) -> None:
        self.assertFalse(row_hidden(self.tree, item), f"{item.text()} должен быть виден")

    def assert_hidden(self, item) -> None:
        self.assertTrue(row_hidden(self.tree, item), f"{item.text()} должен быть скрыт")

    def test_search_by_part_of_name_is_case_insensitive(self) -> None:
        self.apply("елка")                     # «Ёлка» без учёта регистра и ё/е
        self.assert_visible(find_child(self.root, "Ёлка.mp4"))
        self.assert_hidden(find_child(self.root, "праздник.MOV"))
        self.assert_visible(self.folder, )     # папка ведёт к совпадению
        self.assert_visible(find_child(self.folder, "елка_2.mp4"))

    def test_search_matches_latin_and_keeps_parents(self) -> None:
        self.apply("MOV")
        self.assert_visible(find_child(self.root, "праздник.MOV"))
        self.assert_hidden(find_child(self.root, "Ёлка.mp4"))
        self.assert_hidden(self.folder)
        self.assertEqual(self.tree._match_count, 1)
        self.assertEqual(self.tree._count_label.text(), "Найдено: 1")
        self.assertTrue(self.tree._view.isExpanded(self.tree._model.indexFromItem(self.root)),
                        "родители совпадений раскрываются")

    def test_video_filter_keeps_videos_and_their_folders(self) -> None:
        self.apply("", "video")
        for name in ("Ёлка.mp4", "праздник.MOV"):
            self.assert_visible(find_child(self.root, name))
        for name in ("звук.wav", "фото.jpg", "документ.pdf", "архив.zip"):
            self.assert_hidden(find_child(self.root, name))
        self.assert_visible(self.folder)
        self.assert_visible(find_child(self.folder, "елка_2.mp4"))
        self.assert_hidden(find_child(self.folder, "заметки.txt"))

    def test_documents_and_other_are_different_groups(self) -> None:
        self.apply("", "doc")
        self.assert_visible(find_child(self.root, "документ.pdf"))
        self.assert_visible(find_child(self.folder, "заметки.txt"))
        self.assert_hidden(find_child(self.root, "архив.zip"))
        self.apply("", "other")
        self.assert_visible(find_child(self.root, "архив.zip"))
        self.assert_hidden(find_child(self.root, "документ.pdf"))

    def test_checkbox_state_survives_filter_and_clear(self) -> None:
        video = find_child(self.root, "Ёлка.mp4")
        video.setCheckState(Qt.CheckState.Checked)
        self.apply("елка")
        self.assertEqual([f.name for f in self.tree.checked_files()], ["Ёлка.mp4"])
        self.tree.clear_filter()
        self.assertEqual([f.name for f in self.tree.checked_files()], ["Ёлка.mp4"])
        self.assertEqual(video.checkState(), Qt.CheckState.Checked)

    def test_nothing_found_shows_hint_and_reset(self) -> None:
        self.apply("нет-такого")
        self.assertFalse(self.tree._filter_hint.isHidden())
        self.assertEqual(self.tree._filter_hint.text(), "Ничего не найдено")
        self.assertFalse(self.tree._reset_btn.isHidden())
        self.assertEqual(self.tree._count_label.text(), "Найдено: 0")

    def test_unloaded_folders_hint(self) -> None:
        # «Мой диск» Яндекса не загружен — поиск туда не смотрит, об этом и подсказка.
        self.apply("елка")
        self.assertIn("неоткрытых", self.tree._filter_hint.text())

    def test_search_does_not_request_network(self) -> None:
        requested = []
        loads = []
        self.tree.open_drive_requested.connect(requested.append)
        self.tree._loader = lambda file, ctx, done: loads.append(file.name)
        self.apply("елка")
        self.assertEqual(requested, [])
        self.assertEqual(loads, [])

    def test_clear_restores_previous_expansion(self) -> None:
        self.tree._view.collapse(self.tree._model.indexFromItem(self.folder))
        self.apply("елка")
        self.assertTrue(self.tree._view.isExpanded(self.tree._model.indexFromItem(self.folder)),
                        "папка раскрыта, чтобы показать совпадение")
        self.tree.clear_filter()
        self.assertFalse(self.tree._view.isExpanded(self.tree._model.indexFromItem(self.folder)),
                         "прежнее состояние раскрытия вернулось")
        self.assertEqual(self.tree.search_text(), "")
        self.assertFalse(self.tree.filter_active())

    def test_clear_unhides_everything(self) -> None:
        self.apply("елка")
        self.tree.clear_filter()
        for item in self.tree._all_items():
            if item.data(HINT_ROLE):
                continue
            self.assertFalse(row_hidden(self.tree, item), f"{item.text()} снова виден")

    def test_empty_section_hidden_and_drive_with_matches_kept(self) -> None:
        self.apply("елка")
        google = self.tree.group(ProviderId.GOOGLE)
        self.assertTrue(row_hidden(self.tree, google), "пустой раздел можно скрыть")
        self.assertFalse(row_hidden(self.tree, self.tree.group(ProviderId.YANDEX)))

    def test_new_children_are_filtered_after_load(self) -> None:
        empty = make_file("пустая", EntryKind.FOLDER)
        self.tree.set_children(self.root, [empty])
        empty_item = find_child(self.root, "пустая")
        self.apply("найдено")
        self.assertTrue(row_hidden(self.tree, empty_item))
        self.tree.set_children(empty_item, [make_file("найдено.mp4")])
        self.assertFalse(row_hidden(self.tree, empty_item))
        self.assertFalse(row_hidden(self.tree, find_child(empty_item, "найдено.mp4")))

    def test_focus_and_shortcuts_exist(self) -> None:
        self.assertEqual(self.tree._find_shortcut.key().toString(), "Ctrl+F")
        self.tree.focus_search()
        self.assertEqual(self.tree._search.text(), self.tree._search.selectedText())


class TestBigTree(unittest.TestCase):
    """Поиск на 10 000 узлах не должен подвешивать окно."""

    def setUp(self) -> None:
        self.tree = FileTree()
        self.ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X")
        self.root = self.tree.add_root("Ссылка · X", self.ctx, provider=ProviderId.YANDEX)
        files = [make_file(f"файл_{i:05d}.mp4") for i in range(10_000)]
        files.append(make_file("нужный.mp4"))
        self.tree.set_children(self.root, files)

    def test_filter_over_ten_thousand_nodes_is_fast(self) -> None:
        start = time.perf_counter()
        self.tree._search.setText("нужный")
        self.tree._apply_filter_now()
        elapsed = time.perf_counter() - start
        self.assertEqual(self.tree._match_count, 1)
        self.assertLess(elapsed, 0.5, f"фильтр занял {elapsed:.3f} с")


if __name__ == "__main__":
    unittest.main()
