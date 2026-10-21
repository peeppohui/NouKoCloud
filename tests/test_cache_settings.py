"""Кэш превью в «Настройках»: своя папка, сколько занято, «Очистить кэш»."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.config import AppConfig, PREVIEW_CACHE_FOLDER  # noqa: E402
from app.core.models import CloudFile, DownloadLink, EntryKind, ProviderId  # noqa: E402
from app.net.http_client import HttpClient  # noqa: E402
from app.net.ratelimit import RateLimiter  # noqa: E402
from app.preview.server import PreviewServer  # noqa: E402
from app.preview.stream_cache import cache_usage, clear_directory  # noqa: E402
from app.ui.settings_dialog import SettingsDialog  # noqa: E402

from .helpers import start_range_server, stop_server  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class TestCachePath(unittest.TestCase):
    def test_default_is_inside_download_dir(self) -> None:
        cfg = AppConfig()
        cfg.download_dir = r"C:\Загрузки\НоуКо Cloud"
        self.assertEqual(cfg.preview_cache_path, Path(r"C:\Загрузки\НоуКо Cloud") / PREVIEW_CACHE_FOLDER)

    def test_custom_folder_gets_own_subfolder(self) -> None:
        """Кэш — всегда в отдельной подпапке: очистка не заденет файлы в выбранной папке."""
        cfg = AppConfig()
        cfg.preview_cache_dir = r"D:\\"
        self.assertEqual(cfg.preview_cache_path, Path(r"D:\\") / PREVIEW_CACHE_FOLDER)


class TestUsageAndClear(unittest.TestCase):
    def test_usage_and_clear_touch_only_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "a.dat").write_bytes(b"x" * 1000)
            (d / "a.idx").write_bytes(b"y" * 24)
            (d / "sub").mkdir()
            (d / "sub" / "keep.mp4").write_bytes(b"z" * 10)
            self.assertEqual(cache_usage(d), (1024, 2))
            freed, failed = clear_directory(d)
            self.assertEqual((freed, failed), (1024, 0))
            self.assertEqual(cache_usage(d), (0, 0))
            self.assertTrue((d / "sub" / "keep.mp4").exists(), "подпапки не трогаем")

    def test_missing_folder(self) -> None:
        missing = Path(tempfile.gettempdir()) / "noukocloud-no-such-dir-xyz"
        self.assertEqual(cache_usage(missing), (0, 0))
        self.assertEqual(clear_directory(missing), (0, 0))


class FakeProvider:
    def __init__(self, url: str) -> None:
        self.url = url

    async def resolve_download(self, file):
        return DownloadLink(url=self.url, headers={}, size=file.size)

    def forget_link(self, file) -> None:
        pass


class FakeRegistry:
    def __init__(self, provider) -> None:
        self.provider = provider

    def get(self, _pid):
        return self.provider


class TestCloseCaches(unittest.IsolatedAsyncioTestCase):
    async def test_clear_while_preview_open(self) -> None:
        """Файлы открытого кэша закрываются — их можно удалить, а превью потом работает."""
        payload = bytes(range(256)) * 4000
        _h, _t, origin = start_range_server(payload)
        tmp = tempfile.TemporaryDirectory()
        config = AppConfig()
        config.download_dir = tmp.name
        config.preview_cache_dir = str(Path(tmp.name) / "cache-here")
        http = HttpClient(config, RateLimiter(50))
        url = f"http://127.0.0.1:{origin.server_address[1]}/f.mp4"
        server = PreviewServer(FakeRegistry(FakeProvider(url)), http, config)
        await server.start()
        try:
            file = CloudFile(provider=ProviderId.YANDEX, file_id="v", path="/v.mp4", name="v.mp4",
                             kind=EntryKind.FILE, size=len(payload))
            session = server.add_session(file)
            for _ in range(200):
                await asyncio.sleep(0.01)
                if session.cache is not None and session.cache.contiguous_end(0) >= len(payload):
                    break
            self.assertEqual(server.cache_dir, Path(tmp.name) / "cache-here" / PREVIEW_CACHE_FOLDER)
            size, files = cache_usage(server.cache_dir)
            self.assertGreaterEqual(size, len(payload))

            await server.close_caches()
            freed, failed = clear_directory(server.cache_dir)
            self.assertEqual(failed, 0, "после закрытия кэша Windows должна дать удалить файлы")
            self.assertGreaterEqual(freed, len(payload))

            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(f"GET /media/{session.token} HTTP/1.1\r\nRange: bytes=0-99\r\n\r\n".encode())
            await writer.drain()
            raw = await reader.read()
            writer.close()
            self.assertIn(b"206", raw.split(b"\r\n", 1)[0])
            self.assertTrue(raw.endswith(payload[:100]), "после очистки превью снова работает")
        finally:
            await server.stop()
            await http.aclose()
            stop_server(origin)
            tmp.cleanup()


class FakeOps:
    def __init__(self) -> None:
        self.sizes: dict[str, tuple[int, int]] = {}
        self.cleared: list[Path] = []
        self.opened: list[Path] = []

    def usage(self, path: Path) -> tuple[int, int]:
        return self.sizes.get(str(path), (0, 0))

    def clear(self, path: Path) -> tuple[int, int]:
        self.cleared.append(path)
        freed = self.sizes.pop(str(path), (0, 0))[0]
        return freed, 0

    def open_folder(self, path: Path) -> None:
        self.opened.append(path)


class TestSettingsDialog(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = AppConfig()        # не из файла — save() ничего не пишет
        self.config.download_dir = self._tmp.name
        self.ops = FakeOps()
        self.current = self.config.preview_cache_path
        self.ops.sizes[str(self.current)] = (3 * 1024 ** 3 // 2, 12)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_shows_usage_and_path(self) -> None:
        dialog = SettingsDialog(self.config, cache_ops=self.ops)
        self.assertIn("1.5 ГБ", dialog._cache_usage.text())
        self.assertIn("12", dialog._cache_usage.text())
        self.assertIn(str(self.current), dialog._cache_path_label.text())
        self.assertTrue(dialog._cache_clear.isEnabled())

    def test_clear_button(self) -> None:
        dialog = SettingsDialog(self.config, cache_ops=self.ops)
        dialog._cache_clear.click()
        self.assertEqual(self.ops.cleared, [self.current])
        self.assertIn("Кэш пуст", dialog._cache_usage.text())
        self.assertIn("Освобождено", dialog._cache_usage.text())
        self.assertFalse(dialog._cache_clear.isEnabled())

    def test_open_folder_button(self) -> None:
        dialog = SettingsDialog(self.config, cache_ops=self.ops)
        dialog._cache_open.click()
        self.assertEqual(self.ops.opened, [self.current])

    def test_new_path_is_previewed_before_save(self) -> None:
        dialog = SettingsDialog(self.config, cache_ops=self.ops)
        new_dir = str(Path(self._tmp.name) / "fast-ssd")
        dialog._cache_dir.setText(new_dir)
        self.assertIn(str(Path(new_dir) / PREVIEW_CACHE_FOLDER), dialog._cache_path_label.text())
        self.assertIn("после сохранения", dialog._cache_path_label.text())

    def test_save_new_folder_offers_to_clear_old(self) -> None:
        dialog = SettingsDialog(self.config, cache_ops=self.ops)
        asked = []
        dialog._ask_clear_old_cache = lambda old, size: asked.append((old, size)) or True
        new_dir = str(Path(self._tmp.name) / "fast-ssd")
        dialog._cache_dir.setText(new_dir)
        dialog._cache_limit.setValue(20)
        dialog._save()
        self.assertEqual(self.config.preview_cache_dir, new_dir)
        self.assertEqual(self.config.preview_cache_gb, 20.0)
        self.assertEqual(asked[0][0], self.current)
        self.assertEqual(self.ops.cleared, [self.current], "старый кэш удалён по согласию")

    def test_save_same_folder_does_not_ask(self) -> None:
        dialog = SettingsDialog(self.config, cache_ops=self.ops)
        dialog._ask_clear_old_cache = lambda old, size: self.fail("не должно спрашивать")
        dialog._save()
        self.assertEqual(self.ops.cleared, [])

    def test_default_button(self) -> None:
        self.config.preview_cache_dir = str(Path(self._tmp.name) / "x")
        dialog = SettingsDialog(self.config, cache_ops=self.ops)
        dialog._cache_dir.setText("")
        self.assertIn(str(Path(self._tmp.name) / PREVIEW_CACHE_FOLDER), dialog._cache_path_label.text())


if __name__ == "__main__":
    unittest.main()
