"""Мастер первого запуска: проверки и сам мастер.

Диалоги не показываются: тесты вызывают шаги мастера напрямую.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import AppConfig, manual_keys_present, should_show_wizard  # noqa: E402
from app.ui.wizard import (  # noqa: E402
    STEP_DONE,
    STEP_FOLDER,
    STEP_KEYS,
    STEP_WELCOME,
    FirstRunWizard,
    check_writable,
    free_space_text,
    keys_present,
    validate_key_pair,
)

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class TestKeyValidation(unittest.TestCase):
    def test_empty_pair_is_allowed(self) -> None:
        self.assertEqual(validate_key_pair("", ""), "")
        self.assertEqual(validate_key_pair("   ", "  "), "")

    def test_half_filled_pair_is_an_error(self) -> None:
        self.assertTrue(validate_key_pair("123456789", ""))
        self.assertTrue(validate_key_pair("", "123456789"))

    def test_short_values_are_rejected(self) -> None:
        self.assertTrue(validate_key_pair("short", "alsoshort"))
        self.assertTrue(validate_key_pair("longenoughid", "sh"))

    def test_plausible_pair_passes(self) -> None:
        self.assertEqual(validate_key_pair("1234567890abcdef", "secretsecret"), "")

    def test_keys_present(self) -> None:
        cfg = AppConfig()
        self.assertFalse(keys_present(cfg))
        cfg.yandex_client_id = "1234567890"
        self.assertTrue(keys_present(cfg))


class TestFolderChecks(unittest.TestCase):
    def test_writable_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ok, error = check_writable(tmp)
            self.assertTrue(ok)
            self.assertEqual(error, "")
            # Служебный файл не остался.
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_dir_is_created_if_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "папка" / "вложенная"
            ok, _ = check_writable(str(target))
            self.assertTrue(ok)
            self.assertTrue(target.is_dir())

    def test_unwritable_location_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "файл"
            blocker.write_text("x", encoding="utf-8")
            ok, error = check_writable(str(blocker / "внутрь"))
            self.assertFalse(ok)
            self.assertTrue(error)

    def test_free_space_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("Свободно", free_space_text(tmp))


class TestShouldShowWizard(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp.name) / "config.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_shows_when_config_file_missing(self) -> None:
        cfg = AppConfig()
        with mock.patch("app.core.config.CONFIG_FILE", self.config_path):
            self.assertTrue(should_show_wizard(cfg))

    def test_hidden_after_wizard_completed(self) -> None:
        cfg = AppConfig()
        cfg.first_run_completed = True
        with mock.patch("app.core.config.CONFIG_FILE", self.config_path):
            self.assertFalse(should_show_wizard(cfg))

    def test_hidden_when_user_configured_keys_manually(self) -> None:
        self.config_path.write_text(
            json.dumps({"yandex_client_id": "1234567890"}), encoding="utf-8")
        cfg = AppConfig()
        with mock.patch("app.core.config.CONFIG_FILE", self.config_path):
            self.assertTrue(manual_keys_present())
            self.assertFalse(should_show_wizard(cfg))

    def test_shown_when_config_exists_without_keys(self) -> None:
        self.config_path.write_text(json.dumps({"download_dir": "D:\\"}), encoding="utf-8")
        cfg = AppConfig()
        with mock.patch("app.core.config.CONFIG_FILE", self.config_path):
            self.assertFalse(manual_keys_present())
            self.assertTrue(should_show_wizard(cfg))


class TestWizardFlow(unittest.TestCase):
    """Сценарий мастера целиком: шаги, проверки и сохранение."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.config = AppConfig()
        self.config._config_path = self.config_path
        self.config.download_dir = str(self.root / "загрузки")
        self.wizard = FirstRunWizard(self.config)

    def tearDown(self) -> None:
        self.wizard.close()
        self.wizard.deleteLater()
        self.tmp.cleanup()

    def test_starts_on_welcome(self) -> None:
        self.assertEqual(self.wizard.step, STEP_WELCOME)
        self.assertFalse(self.wizard._back.isEnabled())

    def test_next_goes_to_keys(self) -> None:
        self.wizard.go_next()
        self.assertEqual(self.wizard.step, STEP_KEYS)

    def test_invalid_keys_are_reported(self) -> None:
        self.wizard.go_next()
        self.wizard._yandex_id.setText("123")
        self.wizard._yandex_secret.setText("456")
        self.wizard.go_next()
        self.assertEqual(self.wizard.step, STEP_KEYS)
        self.assertFalse(self.wizard._keys_error.isHidden())
        self.assertTrue(self.wizard._keys_error.text())

    def test_valid_keys_pass(self) -> None:
        self.wizard.go_next()
        self.wizard._yandex_id.setText("1234567890abcdef")
        self.wizard._yandex_secret.setText("secretsecretvalue")
        self.wizard.go_next()
        self.assertEqual(self.wizard.step, STEP_FOLDER)

    def test_folder_without_write_access_is_rejected(self) -> None:
        blocker = self.root / "файл"
        blocker.write_text("x", encoding="utf-8")
        self.wizard.go_to(STEP_FOLDER)
        self.wizard._dir.setText(str(blocker / "внутрь"))
        self.wizard.go_next()
        self.assertEqual(self.wizard.step, STEP_FOLDER)
        self.assertFalse(self.wizard._dir_error.isHidden())

    def test_finish_saves_settings(self) -> None:
        target = self.root / "загрузки"
        target.mkdir()
        self.wizard.go_to(STEP_KEYS)
        self.wizard._yandex_id.setText("1234567890abcdef")
        self.wizard._yandex_secret.setText("secretsecretvalue")
        self.wizard.go_to(STEP_FOLDER)
        self.wizard._dir.setText(str(target))
        self.wizard.go_next()
        self.assertEqual(self.wizard.step, STEP_DONE)
        self.wizard.finish()
        self.assertTrue(self.config.first_run_completed)
        self.assertEqual(self.config.download_dir, str(target))
        self.assertEqual(self.config.yandex_client_id, "1234567890abcdef")
        self.assertTrue(self.config_path.exists())
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertTrue(data["first_run_completed"])
        self.assertEqual(data["download_dir"], str(target))

    def test_skip_marks_wizard_done_but_keeps_settings(self) -> None:
        self.config.download_dir = str(self.root / "старая")
        self.wizard.skip()
        self.assertTrue(self.config.first_run_completed)
        self.assertEqual(self.config.download_dir, str(self.root / "старая"))
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertTrue(data["first_run_completed"])

    def test_existing_keys_are_not_shown_in_open_form(self) -> None:
        self.config.yandex_client_id = "portable-id"
        self.config.yandex_client_secret = "portable-secret"
        wizard = FirstRunWizard(self.config)
        try:
            wizard.go_to(STEP_KEYS)
            self.assertTrue(wizard._keys_preset)
            self.assertIsNone(wizard._yandex_id)
            # Секрет в открытом виде не показываем.
            self.assertNotIn("portable-secret", wizard._pages.currentWidget().text()
                             if hasattr(wizard._pages.currentWidget(), "text") else "")
        finally:
            wizard.close()
            wizard.deleteLater()

    def test_back_returns_to_previous_step(self) -> None:
        self.wizard.go_next()
        self.wizard.go_back()
        self.assertEqual(self.wizard.step, STEP_WELCOME)

    def test_accounts_button_applies_settings_first(self) -> None:
        called: list[bool] = []
        target = self.root / "загрузки"
        target.mkdir()
        self.wizard._dir.setText(str(target))
        self.wizard._on_accounts = lambda: called.append(self.config.first_run_completed)
        self.wizard._open_accounts()
        self.assertEqual(called, [True])
        self.assertTrue(self.config.first_run_completed)


if __name__ == "__main__":
    unittest.main()
