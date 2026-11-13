"""Тесты диалога «О программе»."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.about_dialog import (  # noqa: E402
    LICENSES,
    RELEASE_URL,
    AboutDialog,
    about_html,
    build_date,
)

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class TestBuildDate(unittest.TestCase):
    def test_date_is_not_empty(self) -> None:
        self.assertTrue(build_date())

    def test_env_override(self) -> None:
        os.environ["NOUKOCLOUD_BUILD_DATE"] = "01.02.2026"
        try:
            self.assertEqual(build_date(), "01.02.2026")
        finally:
            os.environ.pop("NOUKOCLOUD_BUILD_DATE", None)

    def test_bad_env_value_is_ignored(self) -> None:
        os.environ["NOUKOCLOUD_BUILD_DATE"] = "   "
        try:
            self.assertTrue(build_date())
        finally:
            os.environ.pop("NOUKOCLOUD_BUILD_DATE", None)


class TestAboutHtml(unittest.TestCase):
    def test_contains_version_and_links(self) -> None:
        html = about_html("1.2.3", "05.06.2026")
        self.assertIn("1.2.3", html)
        self.assertIn("05.06.2026", html)
        self.assertIn(RELEASE_URL, html)

    def test_contains_every_license(self) -> None:
        html = about_html()
        for name, license_name, link in LICENSES:
            self.assertIn(name, html)
            self.assertIn(license_name, html)
            self.assertIn(link, html)


class TestAboutDialog(unittest.TestCase):
    def _make(self, **kwargs) -> AboutDialog:
        return AboutDialog(**kwargs)

    def test_shows_version(self) -> None:
        from app import __version__

        dialog = self._make()
        try:
            self.assertIn(__version__, dialog.view.toHtml())
        finally:
            dialog.deleteLater()

    def test_buttons_call_callbacks(self) -> None:
        calls: list[str] = []
        dialog = self._make(on_check_updates=lambda: calls.append("update"),
                            on_diagnostics=lambda: calls.append("diag"))
        try:
            buttons = None
            texts = []
            for i in range(dialog.footer.count()):
                widget = dialog.footer.itemAt(i).widget()
                if widget is not None and hasattr(widget, "text"):
                    texts.append(widget.text())
            self.assertIn("Проверить обновления", texts)
            self.assertIn("Собрать диагностику", texts)
            for i in range(dialog.footer.count()):
                widget = dialog.footer.itemAt(i).widget()
                if widget is None or not hasattr(widget, "text"):
                    continue
                title = widget.text()
                if title == "Проверить обновления":
                    widget.click()
                elif title == "Собрать диагностику":
                    widget.click()
            self.assertEqual(calls, ["update", "diag"])
        finally:
            dialog.deleteLater()

    def test_without_callbacks_no_action_buttons(self) -> None:
        dialog = self._make()
        try:
            texts = []
            for i in range(dialog.footer.count()):
                widget = dialog.footer.itemAt(i).widget()
                if widget is not None and hasattr(widget, "text"):
                    texts.append(widget.text())
            self.assertNotIn("Проверить обновления", texts)
            self.assertNotIn("Собрать диагностику", texts)
            self.assertIn("Закрыть", texts)
        finally:
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
