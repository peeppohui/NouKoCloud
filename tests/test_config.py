"""Тест «портативного» конфига: ключи сборки подхватываются из папки программы."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.core import paths
from app.core.config import AppConfig


class TestPortableConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._original = paths.portable_config_path
        self._original_config = AppConfig.load

    def tearDown(self) -> None:
        paths.portable_config_path = self._original
        AppConfig.load = self._original_config
        self._tmp.cleanup()

    def test_portable_config_provides_defaults(self) -> None:
        portable = self.tmp / "config.json"
        portable.write_text(
            json.dumps({"yandex_client_id": "portable-id",
                        "yandex_client_secret": "portable-secret"}),
            encoding="utf-8",
        )
        paths.portable_config_path = lambda: portable

        cfg = AppConfig()
        cfg._apply_file(portable)
        self.assertEqual(cfg.yandex_client_id, "portable-id")
        self.assertEqual(cfg.yandex_client_secret, "portable-secret")

    def test_empty_values_do_not_erase_portable(self) -> None:
        portable = self.tmp / "config.json"
        portable.write_text(json.dumps({"google_client_id": "portable-google"}), encoding="utf-8")
        paths.portable_config_path = lambda: portable

        user = self.tmp / "user.json"
        user.write_text(json.dumps({"google_client_id": "", "download_dir": str(self.tmp)}),
                        encoding="utf-8")

        cfg = AppConfig()
        cfg._apply_file(portable)
        cfg._apply_file(user)
        # пустое значение из пользовательского файла не затирает ключ сборки
        self.assertEqual(cfg.google_client_id, "portable-google")
        # а непустое — затирает
        self.assertEqual(cfg.download_dir, str(self.tmp))

    def test_portable_config_with_bom(self) -> None:
        """build.ps1 (Windows PowerShell 5) пишет config.json с BOM."""
        portable = self.tmp / "config.json"
        portable.write_text(json.dumps({"yandex_client_id": "bom-id"}), encoding="utf-8-sig")
        cfg = AppConfig()
        cfg._apply_file(portable)
        self.assertEqual(cfg.yandex_client_id, "bom-id")

    def test_no_portable_config_is_fine(self) -> None:
        paths.portable_config_path = lambda: None
        cfg = AppConfig()
        self.assertIsInstance(cfg, AppConfig)

    def test_update_check_can_be_disabled(self) -> None:
        """false в файле — это значение, а не «пусто»: проверка выключается."""
        user = self.tmp / "user.json"
        user.write_text(json.dumps({"update_check_on_start": False}), encoding="utf-8")
        cfg = AppConfig()
        cfg._apply_file(user)
        self.assertFalse(cfg.update_check_on_start)


class TestRenameMigration(unittest.TestCase):
    """После переименования svsCloud → НоуКо Cloud ключи и вход не теряются."""

    def test_settings_and_tokens_copied_from_svscloud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old = base / "svsCloud"
            old.mkdir()
            (old / "config.json").write_text('{"yandex_client_id": "old-id"}', encoding="utf-8")
            (old / "tokens.dat").write_bytes(b"secret")
            (base / "YaGoDownloader").mkdir()
            (base / "YaGoDownloader" / "config.json").write_text("{}", encoding="utf-8")
            new = base / paths.APP_NAME
            new.mkdir()

            paths._migrate_legacy(base)

            self.assertEqual(paths.APP_NAME, "NoukoCloud")
            self.assertIn("old-id", (new / "config.json").read_text(encoding="utf-8"),
                          "берём из svsCloud, а не из ещё более старой папки")
            self.assertEqual((new / "tokens.dat").read_bytes(), b"secret")
            self.assertTrue((old / "config.json").exists(), "старую папку не трогаем")

    def test_existing_new_settings_win(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "svsCloud").mkdir()
            (base / "svsCloud" / "config.json").write_text('{"a": 1}', encoding="utf-8")
            new = base / paths.APP_NAME
            new.mkdir()
            (new / "config.json").write_text('{"a": 2}', encoding="utf-8")
            paths._migrate_legacy(base)
            self.assertEqual((new / "config.json").read_text(encoding="utf-8"), '{"a": 2}')

    def test_preview_cache_folder_renamed(self) -> None:
        from app.core.config import LEGACY_PREVIEW_CACHE_FOLDER, migrate_preview_cache

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            cfg.download_dir = tmp
            old = Path(tmp) / LEGACY_PREVIEW_CACHE_FOLDER
            old.mkdir()
            (old / "x.dat").write_bytes(b"1")
            migrate_preview_cache(cfg)
            self.assertFalse(old.exists())
            self.assertTrue((cfg.preview_cache_path / "x.dat").exists())


if __name__ == "__main__":
    unittest.main()
