"""Тесты окна: массовое скачивание и кнопка «Скачать только этот файл»."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import AppConfig  # noqa: E402
from app.core.models import CloudFile, EntryKind, ProviderId, SourceKind  # noqa: E402
from app.core.update_check import ReleaseInfo  # noqa: E402
from app.context import build_context  # noqa: E402
from app.providers.base import BrowseContext  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def make_file(name: str, size: int = 1024) -> CloudFile:
    return CloudFile(
        provider=ProviderId.YANDEX,
        file_id=name,
        path=f"/исх/{name}",
        name=name,
        kind=EntryKind.FILE,
        size=size,
        source=SourceKind.PUBLIC,
    )


class TestMainWindow(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config = AppConfig()
        config.download_dir = str(Path(self._tmp.name))
        self.ctx = build_context(config)
        self.win = MainWindow(self.ctx)

    def tearDown(self) -> None:
        future = self.ctx.runtime.submit(self.ctx.engine.stop())
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self.ctx.cache.close()
        # Фоновой цикл тоже гасим: иначе его задачи остаются «незабранными»
        # и при выходе asyncio пишет «Task was destroyed but it is pending».
        self.ctx.runtime.stop()

    def _fill_tree(self, count: int) -> None:
        ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X")
        root = self.win.tree.add_root("Яндекс.Диск · X", ctx)
        folder = None
        for row in range(root.rowCount()):
            if root.child(row).text() == "исх":
                folder = root.child(row)
        if folder is None:
            self.fail("в дереве нет папки «исх»")

    def test_button_shows_count_of_checked_files(self) -> None:
        """Кнопка должна показывать, сколько файлов уедет в очередь."""
        tree = self.win.tree
        ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X")
        root = tree.add_root("корень", ctx)
        tree.set_children(root, [make_file(f"C{i:04d}.MP4", 28 * 1024 * 1024) for i in range(1, 142)])

        button = self.win._btn_download
        self.assertFalse(button.isEnabled())
        self.assertEqual(button.text(), "Скачать выбранное (0)")

        tree._set_all_checked(True)
        self.assertTrue(button.isEnabled())
        self.assertEqual(button.text(), "Скачать выбранное (141)")
        self.assertIn("141", button.toolTip())
        self.assertIn("141", self.win._selection_label.text())

    def test_checked_files_are_all_enqueued(self) -> None:
        """Все отмеченные файлы должны попасть в очередь (а не один)."""
        tree = self.win.tree
        ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X")
        root = tree.add_root("корень", ctx)
        tree.set_children(root, [make_file(f"C{i:04d}.MP4") for i in range(1, 142)])
        tree._set_all_checked(True)

        self.assertEqual(len(tree.checked_files()), 141)
        self.win._download_selected()
        # очередь наполняется в фоновом потоке — проверяем счётчик заданий
        import time

        deadline = time.time() + 5
        while time.time() < deadline and not self.ctx.engine.snapshot():
            time.sleep(0.05)
        jobs = self.ctx.engine.snapshot()
        self.assertEqual(len(jobs), 141, "в очередь должно встать 141 файл")

    def test_already_downloaded_file_is_reported_not_queued(self) -> None:
        """Готовый файл того же размера не встаёт в очередь второй раз."""
        import time

        dest = Path(self.ctx.config.download_dir)
        (dest / "a.mp4").write_bytes(b"\0" * 1024)
        tree = self.win.tree
        ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X")
        root = tree.add_root("корень", ctx)
        tree.set_children(root, [make_file("a.mp4", 1024)])
        tree._set_all_checked(True)

        with patch.object(self.win, "_on_notice") as notice:
            self.win._download_selected()
            deadline = time.time() + 5
            while not notice.called and time.time() < deadline:
                QApplication.processEvents()
                time.sleep(0.01)
        self.assertTrue(notice.called, "окно должно сказать, что файл уже есть")
        self.assertEqual(self.ctx.engine.snapshot(), [])
        message = notice.call_args[0][1]
        self.assertIn("Уже", message)
        self.assertIn("a.mp4", message)

    def test_hovered_folder_is_loaded_once(self) -> None:
        """Мышь задержалась над папкой — список берётся заранее, раскрытие его не повторяет."""
        import concurrent.futures

        calls = []

        class Provider:
            async def list_children(self_inner, ctx, file):
                calls.append(file.name)
                return [make_file("внутри.mp4")]

        self.ctx.registry.get = lambda pid: Provider()
        folder = CloudFile(provider=ProviderId.YANDEX, file_id="d", path="/исх", name="исх",
                           kind=EntryKind.FOLDER, source=SourceKind.PUBLIC)
        ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X")
        self.win._on_folder_hovered(folder, ctx)
        self.win._warm_hovered()
        future = self.win._prefetched[self.win._folder_key(folder, ctx)][1]
        future.result(timeout=5)
        got = []
        self.win._load_children(folder, ctx, got.append)
        import time

        deadline = time.time() + 5
        while not got and time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        self.assertEqual([f.name for f in got[0]], ["внутри.mp4"])
        self.assertEqual(calls, ["исх"], "облако спросили один раз")

    def test_my_drive_without_login_shows_hint(self) -> None:
        """Автозагрузка «Моего диска» без входа: без окна, с подсказкой в дереве."""
        self.win.ctx.oauth.is_logged_in = lambda pid: False
        self.win._open_my_drive_for(ProviderId.GOOGLE, interactive=False)
        drive = self.win.tree.drive(ProviderId.GOOGLE)
        self.assertIn("войдите", drive.child(0).text())

    def test_click_on_my_drive_without_login_opens_accounts(self) -> None:
        """Клик по «Мой диск» без входа должен сразу вести в аккаунты."""
        self.win.ctx.oauth.is_logged_in = lambda pid: False
        drive = self.win.tree.drive(ProviderId.GOOGLE)
        with patch.object(self.win, "_open_accounts") as accounts:
            self.win.tree._on_clicked(drive.index())
            QApplication.processEvents()
        accounts.assert_called_once()

    def test_link_opened_next_to_my_drive(self) -> None:
        """После открытия ссылки «Мой диск» остаётся в разделе."""
        class Done:
            def result(self_inner):
                ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X",
                                    display_name="Исходники")
                return self.ctx.registry.get(ProviderId.YANDEX), ctx, [make_file("a.mp4")]

        self.win._after_open(Done())
        group = self.win.tree.group(ProviderId.YANDEX)
        self.assertEqual([group.child(r).text() for r in range(group.rowCount())],
                         ["Мой диск", "Исходники"])

    def test_window_size_is_restored(self) -> None:
        """Окно должно открываться того же размера, каким его закрыли."""
        # Размер берём небольшим: в headless-режиме экран всего 800×800,
        # и окно шире него всё равно зажмёт система.
        self.win.resize(760, 600)       # не меньше минимума окна (720×520)
        saved = self.win.saveGeometry().toBase64().data().decode()
        self.win.resize(500, 400)
        self.ctx.config.window_geometry = saved
        self.win._restore_geometry()
        self.assertEqual(self.win.size().width(), 760)
        self.assertEqual(self.win.size().height(), 600)

    def test_new_release_opens_browser_once(self) -> None:
        """Новый релиз открывается в браузере, повторно — уже нет."""
        class Done:
            def result(self_inner):
                return ReleaseInfo(
                    "9.9.9",
                    "https://github.com/peeppohui/NouKoCloud/releases/tag/v9.9.9",
                )

        self.win._update_check_running = True
        with patch.object(self.win, "_open_release_page", return_value=True) as opened:
            self.win._after_update_check(Done())
        opened.assert_called_once_with(
            "https://github.com/peeppohui/NouKoCloud/releases/tag/v9.9.9")
        self.assertEqual(self.ctx.config.last_update_notified, "9.9.9")
        self.assertGreater(self.ctx.config.last_update_check, 0)
        self.assertFalse(self.win._update_check_running)

        with patch.object(self.win, "_open_release_page", return_value=True) as opened:
            self.win._after_update_check(Done())
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
