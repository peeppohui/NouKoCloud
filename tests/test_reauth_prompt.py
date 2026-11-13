"""Истёкший сеанс: программа предлагает войти заново.

По ТЗ (§4.3, п. 5) молчать об истёкшем токене нельзя — показываем
кнопку «Войти» и открываем окно аккаунтов.
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
from app.core.errors import (  # noqa: E402
    NetworkError,
    TokenExpiredError,
    message_needs_login,
    needs_login,
)
from app.ui.main_window import MainWindow  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class FakeButton:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeMessageBox:
    """Минимальная замена QMessageBox: «нажимает» кнопку по тексту."""

    Icon = SimpleNamespace(Information=1, Warning=2)
    ButtonRole = SimpleNamespace(
        ActionRole=1, DestructiveRole=2, RejectRole=3, AcceptRole=4)

    click_text: str | None = None
    last_text: str | None = None

    def __init__(self, *_args, **_kwargs) -> None:
        self._buttons: list[FakeButton] = []
        self._clicked: FakeButton | None = None

    def setWindowTitle(self, _title: str) -> None:  # noqa: N802
        pass

    def setIcon(self, _icon) -> None:  # noqa: N802
        pass

    def setText(self, text: str) -> None:  # noqa: N802
        FakeMessageBox.last_text = text

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


class NeedsLoginCase(unittest.TestCase):
    def test_expired_token_needs_login(self) -> None:
        self.assertTrue(needs_login(TokenExpiredError()))

    def test_network_error_does_not_need_login(self) -> None:
        self.assertFalse(needs_login(NetworkError()))

    def test_message_contains_hints(self) -> None:
        self.assertTrue(message_needs_login("Сеанс входа истёк. Войдите заново."))
        self.assertTrue(message_needs_login("Нет доступа к этому файлу."))
        self.assertFalse(message_needs_login("Нет связи с облаком."))
        self.assertFalse(message_needs_login(""))


class ReauthPromptCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = AppConfig()
        self.config.download_dir = str(Path(self._tmp.name))
        self.ctx = build_context(self.config)
        self.win = MainWindow(self.ctx)
        FakeMessageBox.click_text = None
        FakeMessageBox.last_text = None

    def tearDown(self) -> None:
        future = self.ctx.runtime.submit(self.ctx.engine.stop())
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self.ctx.cache.close()
        self.ctx.runtime.stop()
        self._tmp.cleanup()

    def test_expired_session_opens_accounts(self) -> None:
        FakeMessageBox.click_text = "Войти"
        with mock.patch("app.ui.main_window.QMessageBox", FakeMessageBox), \
                mock.patch.object(self.win, "_open_accounts") as accounts:
            self.win._notice_error(TokenExpiredError())
        accounts.assert_called_once()
        self.assertIn("Войдите заново", FakeMessageBox.last_text or "")

    def test_postpone_does_not_open_accounts(self) -> None:
        FakeMessageBox.click_text = "Позже"
        with mock.patch("app.ui.main_window.QMessageBox", FakeMessageBox), \
                mock.patch.object(self.win, "_open_accounts") as accounts:
            self.win._notice_error(TokenExpiredError())
        accounts.assert_not_called()

    def test_other_error_goes_to_status(self) -> None:
        with mock.patch("app.ui.main_window.QMessageBox", FakeMessageBox), \
                mock.patch.object(self.win, "_on_notice") as notice:
            self.win._notice_error(NetworkError())
        notice.assert_called_once()
        self.assertEqual(notice.call_args[0][0], "error")


if __name__ == "__main__":
    unittest.main()
