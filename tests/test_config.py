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

    def test_no_portable_config_is_fine(self) -> None:
        paths.portable_config_path = lambda: None
        cfg = AppConfig()
        self.assertIsInstance(cfg, AppConfig)


if __name__ == "__main__":
    unittest.main()
