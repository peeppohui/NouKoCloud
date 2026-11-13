"""Диагностический архив: состав, имена файлов и вычистка секретов."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app.core.config import AppConfig
from app.core.diagnostics import (
    build_manifest,
    create_diagnostics,
    safe_config,
    sanitize_text,
)
from app.core.models import (
    CloudFile,
    DownloadJob,
    EntryKind,
    JobState,
    ProviderId,
)


class TestSanitize(unittest.TestCase):
    def test_authorization_and_bearer(self) -> None:
        text = "Authorization: Bearer very-secret-token\nnext line"
        clean = sanitize_text(text)
        self.assertNotIn("very-secret-token", clean)
        self.assertIn("<redacted>", clean)
        self.assertIn("next line", clean)

    def test_query_and_named_secrets(self) -> None:
        text = (
            "https://storage.example/file?token=abc123&signature=def456 "
            'client_secret="qwerty" access_token=xyz'
        )
        clean = sanitize_text(text)
        for secret in ("abc123", "def456", "qwerty", "xyz"):
            self.assertNotIn(secret, clean)
        self.assertIn("https://storage.example/file?<redacted>", clean)

    def test_ordinary_paths_survive(self) -> None:
        text = r"C:\Users\Монтажёр\Downloads\clip.mov и обычный текст"
        self.assertEqual(sanitize_text(text), text)

    def test_url_userinfo_removed(self) -> None:
        clean = sanitize_text("https://user:pass@example.com/file")
        self.assertNotIn("user:pass", clean)
        self.assertIn("https://<redacted>@example.com/file", clean)


class TestSafeConfig(unittest.TestCase):
    def test_secrets_are_only_marked_as_set(self) -> None:
        config = AppConfig(
            yandex_client_secret="yandex-secret",
            google_client_secret="google-secret",
            google_api_key="google-key",
        )
        data = safe_config(config)
        self.assertEqual(data["yandex_client_secret"], "задан")
        self.assertEqual(data["google_api_key"], "задан")
        self.assertNotIn("device_id", data)
        dumped = json.dumps(data, ensure_ascii=False)
        for secret in ("yandex-secret", "google-secret", "google-key"):
            self.assertNotIn(secret, dumped)

    def test_empty_secret_is_marked_as_missing(self) -> None:
        data = safe_config(AppConfig())
        self.assertEqual(data["yandex_client_secret"], "не задан")
        self.assertIn("recent_links_count", data)

    def test_proxy_credentials_are_removed(self) -> None:
        data = safe_config(AppConfig(proxy="http://user:pass@proxy.local:8080"))
        self.assertNotIn("user:pass", json.dumps(data, ensure_ascii=False))


class TestDiagnosticsArchive(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        (self.logs / "noukocloud.log").write_text(
            "start\nAuthorization: Bearer super-secret\n"
            "https://cloud/file?token=hidden\n",
            encoding="utf-8",
        )
        (self.logs / "errors.log").write_text("ошибка без секретов\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _engine(self):
        engine = mock.Mock()
        job = DownloadJob(
            file=CloudFile(
                provider=ProviderId.YANDEX,
                file_id="1",
                path="Съёмка/clip.mov",
                name="clip.mov",
                kind=EntryKind.FILE,
                size=100,
            ),
            dest_dir=self.root,
        )
        job.state = JobState.FAILED
        job.error = "token=bad-secret"
        engine.snapshot.return_value = [job]
        return engine

    def test_archive_contents_and_redaction(self) -> None:
        oauth = mock.Mock()
        oauth.configured.return_value = True
        oauth.is_logged_in.return_value = True
        with mock.patch("app.core.diagnostics.logs_dir", return_value=self.logs):
            path = create_diagnostics(
                self.root / "out",
                config=AppConfig(google_api_key="top-secret-key"),
                oauth=oauth,
                engine=self._engine(),
            )
        self.assertTrue(path.is_file())
        self.assertRegex(path.name, r"^NoukoCloud-diagnostics-\d{8}-\d{6}\.zip$")
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            self.assertTrue({"README.txt", "manifest.txt", "settings.json",
                             "queue.json", "noukocloud.log", "errors.log"} <= names)
            blob = "\n".join(zf.read(n).decode("utf-8", "replace") for n in names)
        for secret in ("super-secret", "hidden", "bad-secret", "top-secret-key"):
            self.assertNotIn(secret, blob)
        self.assertIn("clip.mov", blob)
        self.assertIn("<redacted>", blob)

    def test_missing_logs_are_not_an_error(self) -> None:
        with mock.patch("app.core.diagnostics.logs_dir", return_value=self.root / "empty"):
            path = create_diagnostics(
                self.root / "out2", config=AppConfig(), engine=None)
        with zipfile.ZipFile(path) as zf:
            self.assertNotIn("noukocloud.log", zf.namelist())

    def test_manifest_has_no_account_name_or_token(self) -> None:
        oauth = mock.Mock()
        oauth.configured.return_value = False
        oauth.is_logged_in.return_value = False
        text = build_manifest(AppConfig(), oauth=oauth, engine=None)
        self.assertIn("Версия:", text)
        self.assertIn("yandex: ключи=нет, вход=нет", text)
        self.assertNotIn("access_token", text.lower().replace("access_token", "x"))


if __name__ == "__main__":
    unittest.main()
