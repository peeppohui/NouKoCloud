"""Выход из аккаунта: токены, кэш, дерево и диалог входа."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.models import CloudFile, EntryKind, ProviderId, SourceKind  # noqa: E402
from app.net.metadata_cache import MetadataCache  # noqa: E402
from app.providers.base import BrowseContext  # noqa: E402
from app.ui.auth_dialog import AuthDialog  # noqa: E402
from app.ui.file_tree import FileTree, HINT_ROLE, LOADED_ROLE  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def make_file(name: str, provider: ProviderId = ProviderId.YANDEX) -> CloudFile:
    return CloudFile(
        provider=provider,
        file_id=name,
        path=f"/{name}",
        name=name,
        kind=EntryKind.FILE,
        size=1024,
        source=SourceKind.OAUTH,
    )


class FakeOAuth:
    def __init__(self, logged_in: bool = True) -> None:
        self.logged_in = logged_in
        self.calls: list[tuple[str, str]] = []

    def is_logged_in(self, _pid: ProviderId) -> bool:
        return self.logged_in

    def account_name(self, _pid: ProviderId) -> str:
        return "Тестовый аккаунт" if self.logged_in else ""

    def configured(self, _pid: ProviderId) -> bool:
        return True

    def auth_url(self, _pid: ProviderId) -> str:
        return "https://example.invalid/auth"


class TestCacheClearScoped(unittest.TestCase):
    def test_logout_keeps_other_provider_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = MetadataCache(Path(tmp) / "cache.db")
            try:
                cache.put_entry("yandex", "oauth", "/", {"items": []})
                cache.put_entry("google", "oauth", "/", {"items": []})
                cache.put_link("yandex|oauth|/|f1", "https://ya/1", {}, 1, None)
                cache.put_link("google|oauth|/|f2", "https://go/2", {}, 2, None)

                cache.clear("yandex")

                self.assertIsNone(cache.get_link("yandex|oauth|/|f1"))
                self.assertIsNotNone(cache.get_link("google|oauth|/|f2"))
                self.assertIsNone(cache.get_entry("yandex", "oauth", "/"))
                self.assertIsNotNone(cache.get_entry("google", "oauth", "/"))
            finally:
                cache.close()

    def test_full_clear_removes_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = MetadataCache(Path(tmp) / "cache.db")
            try:
                cache.put_entry("yandex", "oauth", "/", {"items": []})
                cache.put_link("yandex|oauth|/|f1", "https://ya/1", {}, 1, None)
                cache.put_link("google|oauth|/|f2", "https://go/2", {}, 2, None)

                cache.clear()

                self.assertIsNone(cache.get_entry("yandex", "oauth", "/"))
                self.assertIsNone(cache.get_link("yandex|oauth|/|f1"))
                self.assertIsNone(cache.get_link("google|oauth|/|f2"))
            finally:
                cache.close()


class TestFileTreeLogout(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = FileTree()
        self.public = self.tree.add_root(
            "Яндекс.Диск · ссылка",
            BrowseContext(source=SourceKind.PUBLIC, public_key="https://disk.yandex.ru/d/X"),
            provider=ProviderId.YANDEX,
        )
        self.drive = self.tree.drive(ProviderId.YANDEX)

    def test_reset_clears_drive_but_keeps_public_link(self) -> None:
        self.tree.set_drive_loading(ProviderId.YANDEX)
        self.tree.set_children(self.drive, [make_file("старый.mp4")])
        self.tree.set_children(self.public, [make_file("публичный.mp4")])

        self.tree.reset_drive(ProviderId.YANDEX, "войдите снова")

        self.assertEqual(self.drive.rowCount(), 1)
        self.assertTrue(self.drive.child(0).data(HINT_ROLE))
        self.assertEqual(self.drive.child(0).text(), "войдите снова")
        self.assertFalse(self.drive.data(LOADED_ROLE))
        self.assertEqual(self.public.child(0).text(), "публичный.mp4")
        self.assertIs(self.public.parent(), self.tree.group(ProviderId.YANDEX))

    def test_reset_forgets_pending_responses(self) -> None:
        self.tree.set_children(self.drive, [make_file("старый.mp4")])
        child_item = self.drive.child(0)
        self.tree._pending["token"] = child_item

        self.tree.reset_drive(ProviderId.YANDEX)

        self.assertNotIn("token", self.tree._pending)


class TestAuthDialogLogout(unittest.TestCase):
    def setUp(self) -> None:
        self.oauth = FakeOAuth()
        self.dialog = AuthDialog(self.oauth, None)
        self.dialog._confirm_logout = lambda _pid: True  # type: ignore[method-assign]

    def test_buttons_appear_only_when_logged_in(self) -> None:
        logout, switch = self.dialog._logout_buttons[ProviderId.YANDEX]
        self.assertTrue(logout.isVisibleTo(self.dialog))
        self.assertTrue(switch.isVisibleTo(self.dialog))

        self.oauth.logged_in = False
        self.dialog.refresh()
        self.assertFalse(logout.isVisibleTo(self.dialog))
        self.assertFalse(switch.isVisibleTo(self.dialog))

    def test_logout_request_emits_provider(self) -> None:
        emitted: list[str] = []
        self.dialog.logout_requested.connect(emitted.append)

        self.dialog._request_logout(ProviderId.YANDEX, switch=False)

        self.assertEqual(emitted, ["yandex"])
        self.assertFalse(self.dialog._logout_buttons[ProviderId.YANDEX][0].isEnabled())

    def test_switch_schedules_login_after_logout(self) -> None:
        calls: list[ProviderId] = []
        self.dialog._login = calls.append  # type: ignore[method-assign]
        self.dialog._request_logout(ProviderId.YANDEX, switch=True)
        with patch("app.ui.auth_dialog.QTimer.singleShot",
                   side_effect=lambda _ms, fn: fn()):
            self.dialog.logout_finished("yandex")
        self.assertEqual(calls, [ProviderId.YANDEX])


if __name__ == "__main__":
    unittest.main()
