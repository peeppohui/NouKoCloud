"""Кэш листингов, TTL и миграции config/state (ТЗ 4.8)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.core import config as config_module
from app.core import paths
from app.core.config import CONFIG_VERSION, AppConfig
from app.core.models import CloudFile, EntryKind, ProviderId, SourceKind
from app.downloader import state as state_module
from app.downloader.state import STATE_VERSION, load_state, save_state
from app.net.metadata_cache import (
    EMPTY_TTL,
    ENTRY_TTL,
    SCHEMA_VERSION,
    MetadataCache,
)


def make_file(name: str = "файл.mp4", file_id: str = "id1") -> CloudFile:
    return CloudFile(
        provider=ProviderId.YANDEX,
        file_id=file_id,
        path=f"disk/{name}",
        name=name,
        kind=EntryKind.FILE,
        size=123,
        mime="video/mp4",
        modified=datetime(2024, 5, 1, 12, 30, tzinfo=timezone.utc),
        source=SourceKind.PUBLIC,
        public_key="pub",
    )


class TestCloudFileDict(unittest.TestCase):
    def test_roundtrip(self) -> None:
        original = make_file()
        restored = CloudFile.from_dict(original.to_dict())
        self.assertEqual(restored, original)

    def test_unknown_fields_ignored_and_missing_defaulted(self) -> None:
        data = {"provider": "google", "file_id": "x", "path": "p", "name": "n",
                "kind": "folder", "unknown": 1}
        restored = CloudFile.from_dict(data)
        self.assertEqual(restored.provider, ProviderId.GOOGLE)
        self.assertEqual(restored.kind, EntryKind.FOLDER)
        self.assertEqual(restored.source, SourceKind.OAUTH)
        self.assertEqual(restored.size, 0)


class TestListingCache(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "cache.db"
        self.cache = MetadataCache(self.path)

    def tearDown(self) -> None:
        self.cache.close()
        self._tmp.cleanup()

    def test_put_get_and_schema_version(self) -> None:
        self.cache.put_listing("yandex", "oauth", "/", [make_file()])
        files, updated = self.cache.get_listing("yandex", "oauth", "/")
        self.assertEqual([f.name for f in files], ["файл.mp4"])
        self.assertAlmostEqual(updated, time.time(), delta=5)
        conn = sqlite3.connect(str(self.path))
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, SCHEMA_VERSION)

    def test_ttl_normal_and_empty(self) -> None:
        now = time.time()
        self.assertTrue(MetadataCache.listing_is_fresh([make_file()], now - ENTRY_TTL + 5, now))
        self.assertFalse(MetadataCache.listing_is_fresh([make_file()], now - ENTRY_TTL - 5, now))
        self.assertTrue(MetadataCache.listing_is_fresh([], now - EMPTY_TTL + 1, now))
        self.assertFalse(MetadataCache.listing_is_fresh([], now - EMPTY_TTL - 1, now))

    def test_keys_are_isolated_by_provider_and_source(self) -> None:
        self.cache.put_listing("yandex", "oauth", "/", [make_file("ya")])
        self.cache.put_listing("google", "oauth", "/", [make_file("g")])
        self.cache.put_listing("yandex", "public:KEY", "/", [make_file("pub")])
        self.assertEqual(self.cache.get_listing("yandex", "oauth", "/")[0][0].name, "ya")
        self.assertEqual(self.cache.get_listing("google", "oauth", "/")[0][0].name, "g")
        self.assertEqual(self.cache.get_listing("yandex", "public:KEY", "/")[0][0].name, "pub")
        self.assertIsNone(self.cache.get_listing("yandex", "oauth", "/other"))

    def test_clear_metadata_keeps_links(self) -> None:
        self.cache.put_listing("yandex", "oauth", "/", [make_file()])
        self.cache.put_link("yandex|oauth|/|f", "https://example.test/f", {}, 1, None)
        self.cache.clear_metadata("yandex")
        self.assertIsNone(self.cache.get_listing("yandex", "oauth", "/"))
        self.assertIsNotNone(self.cache.get_link("yandex|oauth|/|f"))

    def test_clear_provider_removes_only_its_entries(self) -> None:
        self.cache.put_listing("yandex", "oauth", "/", [make_file("ya")])
        self.cache.put_listing("google", "oauth", "/", [make_file("g")])
        self.cache.clear("yandex")
        self.assertIsNone(self.cache.get_listing("yandex", "oauth", "/"))
        self.assertIsNotNone(self.cache.get_listing("google", "oauth", "/"))

    def test_broken_payload_is_a_miss(self) -> None:
        with self.cache._lock:
            self.cache._conn.execute(
                "INSERT INTO entries (key, provider, source, path, payload, modified_ts, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("yandex|oauth|/", "yandex", "oauth", "/", "not json", 0, time.time()),
            )
            self.cache._conn.commit()
        self.assertIsNone(self.cache.get_listing("yandex", "oauth", "/"))

    def test_broken_database_is_set_aside(self) -> None:
        self.cache.close()
        self.path.write_bytes(b"not a database at all")
        cache = MetadataCache(self.path)
        try:
            self.assertTrue((self.path.parent / "cache.db.broken").exists())
            cache.put_listing("yandex", "oauth", "/", [make_file()])
            self.assertIsNotNone(cache.get_listing("yandex", "oauth", "/"))
        finally:
            cache.close()


class TestConfigMigration(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.config_path = self.base / "config.json"
        self._orig_config_file = config_module.CONFIG_FILE
        self._orig_portable = paths.portable_config_path
        config_module.CONFIG_FILE = self.config_path
        paths.portable_config_path = lambda: None

    def tearDown(self) -> None:
        config_module.CONFIG_FILE = self._orig_config_file
        paths.portable_config_path = self._orig_portable
        self._tmp.cleanup()

    def test_v2_is_backed_up_and_upgraded(self) -> None:
        self.config_path.write_text(json.dumps({
            "config_version": 2, "ui_theme": "light", "max_parallel_segments": 4,
        }), encoding="utf-8")
        cfg = AppConfig.load()
        self.assertEqual(cfg.config_version, CONFIG_VERSION)
        self.assertEqual(cfg.ui_theme, "light")
        # Версия 2 — «4 части» это уже выбор пользователя, его не трогаем.
        self.assertEqual(cfg.max_parallel_segments, 4)
        backup = self.base / "config.v2.bak"
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["config_version"], 2)

    def test_v1_old_defaults_are_upgraded(self) -> None:
        """До версии 2 делили файл на 4 части по 64 МБ — это чиним."""
        self.config_path.write_text(json.dumps({
            "config_version": 1, "max_parallel_segments": 4, "segment_min_mb": 64,
        }), encoding="utf-8")
        cfg = AppConfig.load()
        self.assertEqual(cfg.config_version, CONFIG_VERSION)
        self.assertEqual(cfg.max_parallel_segments, 12)
        self.assertEqual(cfg.segment_min_mb, 4)

    def test_migration_is_idempotent(self) -> None:
        self.config_path.write_text(json.dumps({"config_version": 2}), encoding="utf-8")
        AppConfig.load()
        backup = self.base / "config.v2.bak"
        stamp = backup.stat().st_mtime_ns
        AppConfig.load()
        self.assertEqual(backup.stat().st_mtime_ns, stamp, "копию не перезаписываем")
        self.assertEqual(AppConfig.load().config_version, CONFIG_VERSION)

    def test_broken_config_is_kept_aside_tokens_untouched(self) -> None:
        self.config_path.write_text("{битый json", encoding="utf-8")
        tokens = self.base / "tokens.dat"
        tokens.write_bytes(b"secret")
        cfg = AppConfig.load()
        self.assertTrue((self.base / "config.broken.json").exists())
        self.assertFalse(self.config_path.exists())
        self.assertIn("поврежд", getattr(cfg, "_load_warning", ""))
        self.assertEqual(tokens.read_bytes(), b"secret")

    def test_non_object_config_is_treated_as_broken(self) -> None:
        self.config_path.write_text("[1, 2, 3]", encoding="utf-8")
        cfg = AppConfig.load()
        self.assertTrue((self.base / "config.broken.json").exists())
        self.assertIn("поврежд", getattr(cfg, "_load_warning", ""))


class TestStateMigration(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.file = make_file()
        from app.core.models import DownloadJob, JobState, Segment

        self.job = DownloadJob(file=self.file, dest_dir=self.dir, total_bytes=101,
                               segments=[Segment(0, 0, 100, 40)])
        self.job.state = JobState.PAUSED

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_new_state_has_state_version(self) -> None:
        save_state(self.job)
        payload = json.loads(self.job.state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["state_version"], STATE_VERSION)
        self.assertEqual(payload["version"], 1)

    def test_legacy_version_one_reads_segments(self) -> None:
        payload = {
            "version": 1,
            "file": self.file.to_dict(),
            "dest": str(self.dir),
            "total": 101,
            "segments": [{"index": 0, "start": 0, "end": 100, "received": 40}],
        }
        path = self.dir / "legacy.part.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_state(path)
        self.assertIsNotNone(loaded)
        file, total, segments = loaded
        self.assertEqual(file.name, self.file.name)
        self.assertEqual(total, 101)
        self.assertEqual(segments[0].received, 40)

    def test_unknown_fields_ignored_and_missing_defaulted(self) -> None:
        payload = {
            "state_version": 2,
            "file": self.file.to_dict(),
            "total": 101,
            "segments": [{"index": 0, "start": 0, "end": 100}],
            "future_field": {"x": 1},
        }
        path = self.dir / "unknown.part.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_state(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[1], 101)          # total из файла состояния
        self.assertEqual(loaded[2][0].received, 0)

    def test_future_state_version_is_refused(self) -> None:
        payload = {"state_version": STATE_VERSION + 1, "file": self.file.to_dict()}
        path = self.dir / "future.part.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(load_state(path))

    def test_damaged_state_is_none(self) -> None:
        path = self.dir / "bad.part.json"
        path.write_text("не json", encoding="utf-8")
        self.assertIsNone(load_state(path))
        path.write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(load_state(path))
        self.assertIsNone(state_module.state_dest(path))
        self.assertIsNone(state_module.state_job_state(path))


if __name__ == "__main__":
    unittest.main()
