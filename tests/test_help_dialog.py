"""Кнопка диагностики в окне помощи."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from app.ui.help_dialog import HelpDialog  # noqa: E402


class TestHelpDialogDiagnostics(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _buttons(dialog: HelpDialog) -> list[QPushButton]:
        return [b for b in dialog.findChildren(QPushButton)
                if b.text() == "Собрать диагностику"]

    def test_button_calls_callback(self) -> None:
        calls: list[bool] = []
        dialog = HelpDialog(on_diagnostics=lambda: calls.append(True))
        buttons = self._buttons(dialog)
        self.assertEqual(len(buttons), 1)
        buttons[0].click()
        self.assertEqual(calls, [True])
        dialog.close()

    def test_button_absent_without_callback(self) -> None:
        dialog = HelpDialog()
        self.assertEqual(self._buttons(dialog), [])
        dialog.close()
