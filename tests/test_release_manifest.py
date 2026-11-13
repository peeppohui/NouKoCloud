"""Тесты инструментов релиза: SHA256, latest.json, проверка ключей в архиве."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import release_manifest  # noqa: E402


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


class TestSha256(unittest.TestCase):
    def test_sha256_matches_hashlib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.bin"
            target.write_bytes(b"hello" * 1000)
            self.assertEqual(
                release_manifest.sha256_file(target),
                hashlib.sha256(b"hello" * 1000).hexdigest(),
            )

    def test_write_sha256_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "NoukoCloud-win64.zip"
            archive.write_bytes(b"data")
            out = release_manifest.write_sha256(archive)
            self.assertEqual(out.name, "NoukoCloud-win64.zip.sha256")
            text = out.read_text(encoding="utf-8")
            digest, _, name = text.strip().partition("  ")
            self.assertEqual(name, "NoukoCloud-win64.zip")
            self.assertEqual(digest, hashlib.sha256(b"data").hexdigest())


class TestLatestJson(unittest.TestCase):
    def test_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "NoukoCloud-win64.zip"
            archive.write_bytes(b"12345")
            data = release_manifest.build_latest_json("1.0.0", archive,
                                                     published_at="2026-10-23T12:00:00Z")
            self.assertEqual(data["version"], "1.0.0")
            self.assertEqual(data["published_at"], "2026-10-23T12:00:00Z")
            self.assertTrue(data["notes_url"].endswith("/releases/tag/v1.0.0"))
            self.assertEqual(data["min_version"], "0.5.0")
            self.assertFalse(data["mandatory"])
            self.assertEqual(data["asset"]["name"], "NoukoCloud-win64.zip")
            self.assertEqual(data["asset"]["size"], 5)
            self.assertEqual(data["asset"]["sha256"], hashlib.sha256(b"12345").hexdigest())

    def test_write_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            archive = folder / "NoukoCloud-win64.zip"
            archive.write_bytes(b"abc")
            sha_path, json_path = release_manifest.write_release_artifacts(
                "1.0.0", archive, release_dir=folder)
            self.assertTrue(sha_path.exists())
            self.assertTrue(json_path.exists())
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], "1.0.0")
            self.assertEqual(data["asset"]["name"], "NoukoCloud-win64.zip")


class TestSecretCheck(unittest.TestCase):
    def test_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(Path(tmp) / "a.zip", {
                "NoukoCloud/config.json": json.dumps({"download_dir": "D:\\"}),
            })
            self.assertEqual(release_manifest.archive_secret_fields(archive), {})

    def test_secrets_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(Path(tmp) / "a.zip", {
                "config.json": json.dumps({"yandex_client_id": "abc", "google_client_secret": "s"}),
            })
            found = release_manifest.archive_secret_fields(archive)
            self.assertIn("yandex_client_id", found)
            self.assertIn("google_client_secret", found)

    def test_broken_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.zip"
            broken.write_bytes(b"not a zip")
            self.assertEqual(release_manifest.archive_secret_fields(broken), {})

    def test_cli_returns_two_when_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(Path(tmp) / "NoukoCloud-win64.zip", {
                "config.json": json.dumps({"yandex_client_id": "abc"}),
            })
            argv = ["release_manifest.py", "--version", "1.0.0", "--archive", str(archive)]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(release_manifest.main(), 2)
            argv.append("--allow-secrets")
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(release_manifest.main(), 0)


if __name__ == "__main__":
    unittest.main()
