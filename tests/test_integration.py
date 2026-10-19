"""Интеграционный тест: провайдер Яндекса + загрузка через мок-API.

Мок повторяет поведение `cloud-api.yandex.net` по официальной
документации (https://yandex.ru/dev/disk-api/doc/en/):
* дети лежат в `_embedded.items`;
* тип папки — `dir`;
* путь своего диска — со схемой `disk:/`;
* `public_key` — полная публичная ссылка.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from app.core.config import AppConfig
from app.core.errors import NotFoundError
from app.core.models import CloudFile, DownloadJob, EntryKind, ProviderId, SourceKind
from app.downloader.transfer import Transfer, TransferContext
from app.net.http_client import HttpClient
from app.net.metadata_cache import MetadataCache
from app.net.ratelimit import RateLimiter
from app.providers.base import BrowseContext
from app.providers.yandex import YandexProvider, disk_path, strip_scheme, unpack_listing

CONTENT = bytes((i * 17 + 3) % 256 for i in range(1_200_000))

# Актуальный формат ответа API
TREE_MODERN = {
    "disk:/": {
        "name": "",
        "path": "disk:/",
        "type": "dir",
        "_embedded": {
            "sort": "",
            "path": "disk:/",
            "items": [
                {"name": "Отснято", "type": "dir", "path": "disk:/Отснято", "modified": "2024-05-01T10:20:30.000Z"},
                {"name": "readme.txt", "type": "file", "path": "disk:/readme.txt", "size": 12,
                 "mime_type": "text/plain", "md5": "d41d8cd98f00b204e9800998ecf8427e"},
            ],
            "limit": 1000,
            "offset": 0,
            "total": 2,
        },
    },
    "disk:/Отснято": {
        "name": "Отснято",
        "path": "disk:/Отснято",
        "type": "dir",
        "_embedded": {
            "items": [
                {"name": "clip.mp4", "type": "file", "path": "disk:/Отснято/clip.mp4",
                 "size": len(CONTENT), "mime_type": "video/mp4",
                 "modified": "2024-05-01T10:20:30.000Z", "md5": ""},
            ],
            "limit": 1000,
            "offset": 0,
            "total": 1,
        },
    },
}


class MockYandexHandler(BaseHTTPRequestHandler):
    last_request: dict = {}

    def log_message(self, *args):
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        MockYandexHandler.last_request = {k: v[0] for k, v in params.items()}

        if parsed.path == "/v1/disk/resources":
            path = (params.get("path") or ["disk:/"])[0]
            node = TREE_MODERN.get(path)
            if node is None:
                self._json({"message": "Resource not found", "error": "NotFound"}, 404)
                return
            # Постраничный ответ, как у Яндекса.
            embedded = dict(node["_embedded"])
            items = embedded["items"]
            limit = int((params.get("limit") or ["1000"])[0])
            offset = int((params.get("offset") or ["0"])[0])
            embedded["items"] = items[offset : offset + limit]
            embedded["offset"] = offset
            embedded["limit"] = limit
            body = {k: v for k, v in node.items() if k != "_embedded"}
            body["_embedded"] = embedded
            self._json(body)
            return

        if parsed.path == "/v1/disk/public/resources":
            key = (params.get("public_key") or [""])[0]
            if not key.startswith("https://disk.yandex.ru/d/"):
                self._json({"message": "Resource not found", "error": "NotFound"}, 404)
                return
            if key.endswith("SINGLEFILE"):
                # Публичная ссылка сразу на файл, а не на папку
                self._json({
                    "name": "clip.mp4",
                    "path": "/",
                    "type": "file",
                    "size": len(CONTENT),
                    "mime_type": "video/mp4",
                    "md5": "",
                })
                return
            self._json({
                "name": "public",
                "path": "/",
                "type": "dir",
                "_embedded": {"items": [], "limit": 1000, "offset": 0, "total": 0, "path": "/"},
            })
            return

        if parsed.path == "/v1/disk/resources/download":
            path = (params.get("path") or [""])[0]
            if path != "disk:/Отснято/clip.mp4":
                self._json({"message": "Resource not found"}, 404)
                return
            host = self.headers.get("Host", "127.0.0.1")
            self._json({
                "href": f"http://{host}/download/clip.mp4",
                "method": "GET",
                "templated": False,
            })
            return

        if parsed.path == "/download/clip.mp4":
            total = len(CONTENT)
            start, end = 0, total - 1
            status = 200
            range_header = self.headers.get("Range")
            if range_header:
                spec = range_header.split("=", 1)[1]
                first, _, last = spec.partition("-")
                start = int(first) if first else 0
                end = int(last) if last else total - 1
                end = min(end, total - 1)
                status = 206
            chunk = CONTENT[start : end + 1]
            self.send_response(status)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk)))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.end_headers()
            self.wfile.write(chunk)
            return

        self._json({"message": "Not found"}, 404)


class FakeOAuth:
    async def access_token(self, pid):
        return "fake-token"


class FakeRegistry:
    def __init__(self, provider):
        self.provider = provider

    def get(self, _pid):
        return self.provider


class TestHelpers(unittest.TestCase):
    def test_strip_scheme(self) -> None:
        self.assertEqual(strip_scheme("disk:/Отснято/clip.mp4"), "/Отснято/clip.mp4")
        self.assertEqual(strip_scheme("/Отснято"), "/Отснято")

    def test_disk_path(self) -> None:
        self.assertEqual(disk_path("/Отснято"), "disk:/Отснято")
        self.assertEqual(disk_path("disk:/Отснято"), "disk:/Отснято")
        self.assertEqual(disk_path(""), "disk:/")

    def test_unpack_modern(self) -> None:
        payload = {"_embedded": {"items": [{"name": "a"}], "limit": 1000, "offset": 0, "total": 7}}
        items, total, limit, offset = unpack_listing(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(total, 7)
        self.assertEqual(limit, 1000)
        self.assertEqual(offset, 0)

    def test_unpack_legacy(self) -> None:
        payload = {"items": [{"name": "a"}], "limit": 100, "offset": 0, "total": 1}
        items, total, limit, offset = unpack_listing(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(total, 1)


class TestYandexIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockYandexHandler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1/disk"

        import app.providers.yandex as yandex_module

        self._original_api = yandex_module.API
        yandex_module.API = self.base

        self.config = AppConfig()
        self.config.max_retries = 1
        self.config.segment_min_mb = 1
        self.config.use_segments = True
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.http = HttpClient(self.config, RateLimiter(50))
        self._cache = MetadataCache(self.tmp / "cache.db")
        self.provider = YandexProvider(self.http, FakeOAuth(), self._cache, self.config)

    async def asyncTearDown(self) -> None:
        import app.providers.yandex as yandex_module

        yandex_module.API = self._original_api
        await self.http.aclose()
        self._cache.close()
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    async def test_lists_folder(self) -> None:
        ctx = BrowseContext(source=SourceKind.OAUTH, root_path="/")
        items = await self.provider.list_children(ctx, None)
        self.assertEqual(len(items), 2)
        names = {item.name for item in items}
        self.assertEqual(names, {"Отснято", "readme.txt"})
        folder = next(item for item in items if item.name == "Отснято")
        self.assertEqual(folder.kind, EntryKind.FOLDER)
        self.assertEqual(folder.path, "/Отснято")

    async def test_uses_disk_scheme_in_request(self) -> None:
        ctx = BrowseContext(source=SourceKind.OAUTH, root_path="/")
        await self.provider.list_children(ctx, None)
        self.assertEqual(MockYandexHandler.last_request.get("path"), "disk:/")

    async def test_lists_subfolder(self) -> None:
        ctx = BrowseContext(source=SourceKind.OAUTH, root_path="/")
        root = await self.provider.list_children(ctx, None)
        folder = next(item for item in root if item.name == "Отснято")
        children = await self.provider.list_children(ctx, folder)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].name, "clip.mp4")
        self.assertEqual(children[0].size, len(CONTENT))
        self.assertIsNotNone(children[0].modified)
        self.assertEqual(MockYandexHandler.last_request.get("path"), "disk:/Отснято")

    async def test_recursive_tree(self) -> None:
        ctx = BrowseContext(source=SourceKind.OAUTH, root_path="/")
        found = []
        async for item in self.provider.list_tree(ctx, None):
            found.append(item.name)
        self.assertIn("clip.mp4", found)

    async def test_public_request_uses_full_url(self) -> None:
        ctx = BrowseContext(
            source=SourceKind.PUBLIC,
            public_key="https://disk.yandex.ru/d/AbCdEf12",
        )
        await self.provider.list_children(ctx, None)
        self.assertEqual(
            MockYandexHandler.last_request.get("public_key"),
            "https://disk.yandex.ru/d/AbCdEf12",
        )
        # Для корня публичной папки путь не передаём
        self.assertNotIn("path", MockYandexHandler.last_request)

    async def test_public_bad_key_gives_friendly_error(self) -> None:
        ctx = BrowseContext(source=SourceKind.PUBLIC, public_key="short-key")
        with self.assertRaises(NotFoundError) as ctx_error:
            await self.provider.list_children(ctx, None)
        self.assertIn("не даёт доступ", ctx_error.exception.user_message)

    async def test_public_single_file(self) -> None:
        """Ссылка сразу на файл — показываем его единственным элементом."""
        ctx = BrowseContext(
            source=SourceKind.PUBLIC,
            public_key="https://disk.yandex.ru/d/SINGLEFILE",
        )
        items = await self.provider.list_children(ctx, None)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, "clip.mp4")
        self.assertTrue(items[0].is_file)
        self.assertEqual(items[0].size, len(CONTENT))

    async def test_download_via_provider(self) -> None:
        ctx = BrowseContext(source=SourceKind.OAUTH, root_path="/")
        root = await self.provider.list_children(ctx, None)
        folder = next(item for item in root if item.name == "Отснято")
        children = await self.provider.list_children(ctx, folder)
        target = children[0]

        link = await self.provider.resolve_download(target)
        self.assertIn("/download/clip.mp4", link.url)
        self.assertEqual(MockYandexHandler.last_request.get("path"), "disk:/Отснято/clip.mp4")

        job = DownloadJob(file=target, dest_dir=self.tmp)
        job.pause_event.set()
        ctx2 = TransferContext(
            http=self.http,
            registry=FakeRegistry(self.provider),  # type: ignore[arg-type]
            config=self.config,
            streams_semaphore=asyncio.Semaphore(4),
            on_progress=lambda _job: None,
        )
        ok = await Transfer(ctx2).run(job)
        self.assertTrue(ok)
        final = self.tmp / "clip.mp4"
        self.assertTrue(final.exists())
        self.assertEqual(final.read_bytes(), CONTENT)

    async def test_missing_file(self) -> None:
        fake = CloudFile(
            provider=ProviderId.YANDEX, file_id="x", path="/нет/такого.mp4",
            name="такого.mp4", kind=EntryKind.FILE,
        )
        with self.assertRaises(NotFoundError):
            await self.provider.resolve_download(fake)


if __name__ == "__main__":
    unittest.main()
