"""Хранилище токенов: запись, шифрование и чтение после перезапуска."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.secrets_store import TokenStore


class TestTokenStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "tokens.dat"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tokens_survive_restart(self) -> None:
        store = TokenStore(self.path)
        store.set("yandex", {"access_token": "секрет-токен", "refresh_token": "r"})
        self.assertTrue(self.path.exists())

        reopened = TokenStore(self.path)
        self.assertEqual(reopened.get("yandex"), {"access_token": "секрет-токен", "refresh_token": "r"})

    def test_file_is_not_plain_text_on_windows(self) -> None:
        from app.core import secrets_store

        TokenStore(self.path).set("yandex", {"access_token": "plain-marker"})
        if secrets_store._HAS_DPAPI:
            self.assertNotIn(b"plain-marker", self.path.read_bytes())

    def test_clear(self) -> None:
        store = TokenStore(self.path)
        store.set("google", {"access_token": "x"})
        store.clear("google")
        self.assertIsNone(TokenStore(self.path).get("google"))


if __name__ == "__main__":
    unittest.main()
