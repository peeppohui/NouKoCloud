"""Тесты дерева файлов (headless, Qt offscreen).

Проверяем то, что нельзя поймать без UI: подстановку детей в модель,
раскрытие папок и работу чекбоксов.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timezone  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.models import CloudFile, EntryKind, ProviderId, SourceKind  # noqa: E402
from app.providers.base import BrowseContext  # noqa: E402
from app.ui.file_tree import DRIVE_ROLE, LOADED_ROLE, FileTree  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def make_file(name: str, kind=EntryKind.FILE, size: int = 1024,
              modified: datetime | None = None) -> CloudFile:
    return CloudFile(
        provider=ProviderId.YANDEX,
        file_id=name,
        path=f"/{name}",
        name=name,
        kind=kind,
        size=size,
        source=SourceKind.PUBLIC,
        modified=modified,
    )


def child_names(item) -> list[str]:
    return [item.child(r).text() for r in range(item.rowCount())]


class TestFileTree(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = FileTree()
        self.ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X")
        self.oauth_ctx = BrowseContext(source=SourceKind.OAUTH, root_path="/", display_name="Мой диск")

    def test_add_root_and_children(self) -> None:
        root = self.tree.add_root("Яндекс.Диск · X", self.ctx, provider=ProviderId.YANDEX)
        self.tree.set_children(root, [make_file("папка", EntryKind.FOLDER), make_file("video.mp4")])

        model = self.tree._model
        self.assertEqual(model.rowCount(), 2, "наверху только разделы облаков")
        self.assertIs(root.parent(), self.tree.group(ProviderId.YANDEX))
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

    def test_sections_exist_from_start(self) -> None:
        """Разделы «Яндекс.Диск» и «Google Диск» видны сразу, в каждом — «Мой диск»."""
        model = self.tree._model
        self.assertEqual([model.item(r).text() for r in range(model.rowCount())],
                         ["Яндекс.Диск", "Google Диск"])
        for pid in (ProviderId.YANDEX, ProviderId.GOOGLE):
            group = self.tree.group(pid)
            self.assertEqual(group.rowCount(), 1)
            self.assertIs(group.child(0), self.tree.drive(pid))
            self.assertEqual(group.child(0).text(), "Мой диск")
            self.assertTrue(group.child(0).hasChildren(), "есть стрелка — можно раскрыть")
            self.assertFalse(group.isCheckable())

    def test_link_goes_to_its_section_and_my_drive_stays(self) -> None:
        """Раньше после открытия ссылки пропадал способ открыть «Мой диск»."""
        root = self.tree.add_root("Яндекс.Диск · abcdef", self.ctx, provider=ProviderId.YANDEX)
        yandex = self.tree.group(ProviderId.YANDEX)
        self.assertIs(root.parent(), yandex)
        self.assertEqual(yandex.rowCount(), 2)
        self.assertIs(yandex.child(0), self.tree.drive(ProviderId.YANDEX), "«Мой диск» — первым")
        self.assertIs(yandex.child(1), root)
        self.assertEqual(root.text(), "Ссылка · abcdef", "приставка облака внутри его раздела лишняя")
        self.assertEqual(self.tree.group(ProviderId.GOOGLE).rowCount(), 1)

    def test_my_drive_of_both_clouds_do_not_clash(self) -> None:
        """Раньше «Мой диск» Google подменял содержимое «Моего диска» Яндекса (одно имя)."""
        ya = self.tree.add_root("Мой диск", self.oauth_ctx, provider=ProviderId.YANDEX)
        self.tree.set_children(ya, [make_file("из_яндекса.mp4")])
        go = self.tree.add_root("Мой диск", self.oauth_ctx, provider=ProviderId.GOOGLE)
        self.tree.set_children(go, [make_file("из_гугла.mp4")])
        self.assertIsNot(ya, go)
        self.assertIs(ya, self.tree.drive(ProviderId.YANDEX), "загружается в постоянный пункт")
        self.assertEqual(ya.child(0).text(), "из_яндекса.mp4")
        self.assertEqual(go.child(0).text(), "из_гугла.mp4")
        # повторное открытие того же облака — тот же корень, без дублей
        self.assertIs(self.tree.add_root("Мой диск", self.oauth_ctx, provider=ProviderId.YANDEX), ya)
        self.assertEqual(self.tree.group(ProviderId.YANDEX).rowCount(), 1)

    def test_checkboxes_work_inside_sections(self) -> None:
        root = self.tree.add_root("Мой диск", self.oauth_ctx, provider=ProviderId.GOOGLE)
        self.tree.set_children(root, [make_file("a.mp4"), make_file("b.mp4")])
        self.tree._set_all_checked(True)
        self.assertEqual(sorted(f.name for f in self.tree.checked_files()), ["a.mp4", "b.mp4"])

    def test_expanding_my_drive_requests_it_once(self) -> None:
        asked = []
        self.tree.open_drive_requested.connect(asked.append)
        drive = self.tree.drive(ProviderId.GOOGLE)
        self.tree._on_expanded(drive.index())
        self.tree._on_expanded(drive.index())          # пока грузится — не дублируем
        self.tree._on_double_clicked(drive.index())
        self.assertEqual(asked, [ProviderId.GOOGLE])
        self.assertEqual(drive.child(0).text(), "загрузка…")

    def test_my_drive_failure_allows_retry(self) -> None:
        asked = []
        self.tree.open_drive_requested.connect(asked.append)
        drive = self.tree.drive(ProviderId.YANDEX)
        self.tree._on_expanded(drive.index())
        self.tree.drive_failed(ProviderId.YANDEX, "войдите в аккаунт")
        self.assertFalse(drive.data(LOADED_ROLE))
        self.assertEqual(drive.child(0).text(), "войдите в аккаунт")
        self.tree._on_expanded(drive.index())
        self.assertEqual(asked, [ProviderId.YANDEX, ProviderId.YANDEX])

    def test_link_can_be_removed_but_my_drive_cannot(self) -> None:
        drive = self.tree.add_root("Мой диск", self.oauth_ctx, provider=ProviderId.YANDEX)
        self.tree.set_children(drive, [make_file("своё.mp4")])
        link = self.tree.add_root("Исходники", self.ctx, provider=ProviderId.YANDEX)
        self.tree.set_children(link, [make_file("чужое.mp4")])
        self.tree._set_all_checked(True)
        counts = []
        removed = []
        self.tree.checked_changed.connect(counts.append)
        self.tree.link_removed.connect(removed.append)

        self.assertFalse(self.tree.is_link_root(drive))
        self.tree.remove_root(drive)                   # не убирается
        self.assertTrue(self.tree.is_link_root(link))
        self.tree.remove_root(link)

        yandex = self.tree.group(ProviderId.YANDEX)
        self.assertEqual(yandex.rowCount(), 1)
        self.assertIs(yandex.child(0), drive)
        self.assertEqual([f.name for f in self.tree.checked_files()], ["своё.mp4"])
        self.assertEqual(counts[-1], 1)
        self.assertIs(removed[0], self.ctx)
        # ту же ссылку можно открыть снова
        again = self.tree.add_root("Исходники", self.ctx, provider=ProviderId.YANDEX)
        self.assertEqual(yandex.rowCount(), 2)
        self.assertIsNot(again, drive)

    def test_removing_link_forgets_pending_loads(self) -> None:
        """Ответ по папке убранной ссылки пришёл позже — не падаем."""
        link = self.tree.add_root("Исходники", self.ctx, provider=ProviderId.YANDEX)
        self.tree.set_children(link, [make_file("папка", EntryKind.FOLDER)])
        self.tree._loader = lambda file, ctx, done: None
        self.tree.request_children(link.child(0), make_file("папка", EntryKind.FOLDER), self.ctx)
        token = self.tree._counter
        self.tree.remove_root(link)
        self.tree.handle_children_loaded(token, [make_file("внутри.mp4")])
        self.assertEqual(self.tree._pending, {})

    def test_delete_key_removes_current_link(self) -> None:
        link = self.tree.add_root("Исходники", self.ctx, provider=ProviderId.YANDEX)
        self.tree._view.setCurrentIndex(link.index())
        self.tree._remove_current_link()
        self.assertEqual(self.tree.group(ProviderId.YANDEX).rowCount(), 1)
        self.tree._view.setCurrentIndex(self.tree.drive(ProviderId.YANDEX).index())
        self.tree._remove_current_link()
        self.assertEqual(self.tree.group(ProviderId.YANDEX).rowCount(), 1)

    def test_links_are_told_apart_by_key_not_name(self) -> None:
        """Две разные ссылки на папки «Исходники» — два корня; та же ссылка — один."""
        a = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/AAA")
        b = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/BBB")
        first = self.tree.add_root("Исходники", a, provider=ProviderId.YANDEX)
        second = self.tree.add_root("Исходники", b, provider=ProviderId.YANDEX)
        again = self.tree.add_root("Исходники", a, provider=ProviderId.YANDEX)
        self.assertIsNot(first, second)
        self.assertIs(first, again)
        self.assertIn("AAA", first.toolTip())

    def test_clear_rebuilds_sections(self) -> None:
        self.tree.add_root("Исходники", self.ctx, provider=ProviderId.YANDEX)
        self.tree.clear()
        self.assertEqual(self.tree.group(ProviderId.YANDEX).rowCount(), 1)
        self.assertTrue(self.tree.group(ProviderId.YANDEX).child(0).data(DRIVE_ROLE))

    def test_same_root_is_not_duplicated(self) -> None:
        first = self.tree.add_root("Яндекс.Диск · X", self.ctx, provider=ProviderId.YANDEX)
        second = self.tree.add_root("Яндекс.Диск · X", self.ctx, provider=ProviderId.YANDEX)
        self.assertIs(first, second)
        self.assertEqual(self.tree.group(ProviderId.YANDEX).rowCount(), 2)  # «Мой диск» + ссылка

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

    def test_checking_unloaded_folder_loads_and_checks_children(self) -> None:
        """Отмеченная, но не раскрытая папка должна попасть в скачивание целиком."""
        calls = []
        self.tree._loader = lambda file, ctx, done: calls.append(file.name)
        root = self.tree.add_root("корень", self.ctx)
        self.tree.set_children(root, [make_file("папка", EntryKind.FOLDER)])
        folder = root.child(0)

        folder.setCheckState(Qt.CheckState.Checked)
        self.assertEqual(calls, ["папка"], "содержимое отмеченной папки должно запроситься")
        token = max(self.tree._pending)
        self.tree.handle_children_loaded(token, [make_file("a.mp4"), make_file("b.mp4")])
        names = sorted(f.name for f in self.tree.checked_files())
        self.assertEqual(names, ["a.mp4", "b.mp4"])

    def test_failed_load_can_be_retried(self) -> None:
        calls = []
        self.tree._loader = lambda file, ctx, done: calls.append(file.name)
        root = self.tree.add_root("корень", self.ctx)
        self.tree.set_children(root, [make_file("папка", EntryKind.FOLDER)])
        folder = root.child(0)
        folder.setCheckState(Qt.CheckState.Checked)
        self.tree.handle_children_loaded(max(self.tree._pending), None)
        self.assertEqual(calls, ["папка"], "после ошибки не должно быть самоповтора")

        self.tree._on_expanded(self.tree._model.indexFromItem(folder))
        self.assertEqual(calls, ["папка", "папка"], "повторное раскрытие запрашивает снова")


class TestSorting(unittest.TestCase):
    """Сортировка по клику на заголовок колонки."""

    def setUp(self) -> None:
        self.tree = FileTree()
        self.ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X")
        self.root = self.tree.add_root("Ссылка · X", self.ctx, provider=ProviderId.YANDEX)
        self.tree.set_children(self.root, [
            make_file("b.mp4", size=100),
            make_file("A.mp4", size=9_000_000),
            make_file("c.mp4", size=100),
            make_file("папка", EntryKind.FOLDER),
            make_file("a.mp4", size=2_000_000),
        ])

    def click(self, column: int) -> None:
        self.tree._on_section_clicked(column)

    def test_sort_by_size_is_numeric_not_textual(self) -> None:
        """«9 МБ» больше «2 МБ» — строковое сравнение дало бы обратный порядок."""
        self.click(1)
        self.assertEqual(child_names(self.root), ["папка", "b.mp4", "c.mp4", "a.mp4", "A.mp4"])

    def test_folders_stay_on_top_in_both_directions(self) -> None:
        self.click(1)
        self.assertEqual(child_names(self.root)[0], "папка")
        self.click(1)                                   # тот же столбец — наоборот
        self.assertEqual(child_names(self.root)[0], "папка")
        self.assertEqual(child_names(self.root), ["папка", "A.mp4", "a.mp4", "b.mp4", "c.mp4"])

    def test_repeated_click_reverses_order(self) -> None:
        self.click(1)
        first = child_names(self.root)
        self.click(1)
        self.assertNotEqual(first, child_names(self.root))
        self.assertEqual(sorted(first), sorted(child_names(self.root)))

    def test_sort_by_name_ignores_case(self) -> None:
        """Имя сравнивается без учёта регистра: A.mp4 рядом с a.mp4."""
        # По умолчанию дерево уже отсортировано по имени — клик по активному
        # столбцу разворачивает порядок, как в проводнике Windows.
        self.click(0)
        self.assertEqual(child_names(self.root), ["папка", "c.mp4", "b.mp4", "A.mp4", "a.mp4"])
        self.click(0)
        self.assertEqual(child_names(self.root), ["папка", "A.mp4", "a.mp4", "b.mp4", "c.mp4"])

    def test_sort_by_date(self) -> None:
        self.tree.set_children(self.root, [
            make_file("старый.mp4", modified=datetime(2024, 1, 1, tzinfo=timezone.utc)),
            make_file("новый.mp4", modified=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ])
        self.click(2)
        self.assertEqual(child_names(self.root), ["старый.mp4", "новый.mp4"])
        self.click(2)
        self.assertEqual(child_names(self.root), ["новый.mp4", "старый.mp4"])

    def test_new_files_appear_in_sorted_order(self) -> None:
        """Папка догрузилась позже — её файлы тоже должны встать по сортировке."""
        self.click(1)
        self.tree.set_children(self.root, [
            make_file("b.mp4", size=100),
            make_file("A.mp4", size=9_000_000),
            make_file("a.mp4", size=2_000_000),
        ])
        self.assertEqual(child_names(self.root), ["b.mp4", "a.mp4", "A.mp4"])

    def test_sections_keep_their_order(self) -> None:
        """Разделы облаков и «Мой диск» на своих местах, ссылки сортируются."""
        self.tree.add_root("Ссылка · ZZZ", self.ctx, provider=ProviderId.YANDEX)
        self.click(1)
        model = self.tree._model
        self.assertEqual([model.item(r).text() for r in range(model.rowCount())],
                         ["Яндекс.Диск", "Google Диск"])
        yandex = self.tree.group(ProviderId.YANDEX)
        self.assertEqual(child_names(yandex)[0], "Мой диск")
        self.assertTrue(yandex.child(0).data(DRIVE_ROLE))

    def test_checkboxes_survive_sorting(self) -> None:
        b = [self.root.child(r) for r in range(self.root.rowCount()) if self.root.child(r).text() == "b.mp4"][0]
        b.setCheckState(Qt.CheckState.Checked)
        self.click(1)
        self.assertEqual([f.name for f in self.tree.checked_files()], ["b.mp4"])

    def test_expanded_folders_stay_expanded(self) -> None:
        folder = [self.root.child(r) for r in range(self.root.rowCount())
                  if self.root.child(r).text() == "папка"][0]
        self.tree.set_children(folder, [make_file("внутри.mp4")])
        self.tree.expand_item(folder)
        self.assertTrue(self.tree._view.isExpanded(self.tree._model.indexFromItem(folder)))
        self.click(1)
        self.assertTrue(self.tree._view.isExpanded(self.tree._model.indexFromItem(folder)))
        self.assertEqual(child_names(folder), ["внутри.mp4"])

    def test_subfolder_is_sorted_too(self) -> None:
        folder = [self.root.child(r) for r in range(self.root.rowCount())
                  if self.root.child(r).text() == "папка"][0]
        self.tree.set_children(folder, [
            make_file("big.mp4", size=900),
            make_file("small.mp4", size=10),
        ])
        self.click(1)
        self.assertEqual(child_names(folder), ["small.mp4", "big.mp4"])

    def test_size_and_date_survive_sorting(self) -> None:
        """Раньше перестановка строк съедала колонки «Размер» и «Изменён»."""
        def row_texts(item):
            out = []
            for r in range(item.rowCount()):
                cells = [item.child(r, c) for c in range(3)]
                out.append(tuple(c.text() if c is not None else "" for c in cells))
            return out

        before = row_texts(self.root)
        self.assertTrue(any(cells[1] for cells in before), "в колонке «Размер» что-то есть")
        self.click(1)
        after = row_texts(self.root)
        self.assertEqual(sorted(before), sorted(after), "содержимое строк не изменилось")
        self.assertEqual(sorted(c for c in after if c[1]), sorted(c for c in before if c[1]),
                         "колонка «Размер» не должна опустеть")


if __name__ == "__main__":
    unittest.main()
