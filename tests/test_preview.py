"""Тесты сервера превью: Range-прокси облака, ошибки облака, локальная копия."""

from __future__ import annotations

import asyncio
import http.server
import tempfile
import threading
import unittest
from pathlib import Path

from app.core.config import AppConfig
from app.core.models import CloudFile, DownloadLink, EntryKind, ProviderId
from app.net.http_client import HttpClient
from app.net.ratelimit import RateLimiter
from app.preview.server import PreviewServer, media_kind, parse_range

from .helpers import start_range_server, stop_server

PAYLOAD = bytes((i * 13 + 7) % 256 for i in range(200000))


class FakeProvider:
    def __init__(self, urls: list[str]) -> None:
        self.urls = urls          # каждая следующая выдача ссылки — следующий URL
        self.resolved = 0
        self.forgotten = 0

    async def resolve_download(self, file: CloudFile) -> DownloadLink:
        url = self.urls[min(self.resolved, len(self.urls) - 1)]
        self.resolved += 1
        return DownloadLink(url=url, headers={}, size=file.size)

    def forget_link(self, file: CloudFile) -> None:
        self.forgotten += 1


class FakeRegistry:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider

    def get(self, _pid: ProviderId) -> FakeProvider:
        return self.provider


class StatusHandler(http.server.BaseHTTPRequestHandler):
    """Облако, отвечающее заданным статусом (протухшая ссылка, сбой)."""

    status = 403

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        body = b'{"error": "expired"}'
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NoRangeHandler(http.server.BaseHTTPRequestHandler):
    """Облако, которое не отдаёт файл по частям (500 на Range), а целиком — отдаёт."""

    payload = b""

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.headers.get("Range"):
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)


async def read_http(port: int, path: str, headers: dict[str, str] | None = None,
                    method: str = "GET") -> tuple[int, dict, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
    for key, value in (headers or {}).items():
        request += f"{key}: {value}\r\n"
    request += "Connection: close\r\n\r\n"
    writer.write(request.encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    parsed = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        parsed[key.strip().lower()] = value.strip()
    return status, parsed, body


class TestPreviewServer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.handler, self.thread, self.origin = start_range_server(PAYLOAD)
        self.port = self.origin.server_address[1]
        self.good_url = f"http://127.0.0.1:{self.port}/file.mp4"
        self._tmp = tempfile.TemporaryDirectory()
        self.config = AppConfig()
        self.config.download_dir = self._tmp.name
        self.http = HttpClient(self.config, RateLimiter(50))
        self.provider = FakeProvider([self.good_url])
        self.server = PreviewServer(FakeRegistry(self.provider), self.http, self.config)
        await self.server.start()
        self.file = CloudFile(
            provider=ProviderId.YANDEX, file_id="f1", path="/video/clip.mp4", name="clip.mp4",
            kind=EntryKind.FILE, size=len(PAYLOAD), mime="video/mp4",
        )
        self.session = self.server.add_session(self.file)
        self.path = f"/media/{self.session.token}"

    async def asyncTearDown(self) -> None:
        await self.server.stop()
        await self.http.aclose()
        stop_server(self.origin)
        self._tmp.cleanup()

    async def test_media_full(self) -> None:
        status, headers, body = await read_http(self.server.port, self.path)
        self.assertEqual(status, 200)
        self.assertEqual(body, PAYLOAD)
        self.assertEqual(headers.get("accept-ranges"), "bytes")
        self.assertEqual(headers.get("content-length"), str(len(PAYLOAD)))

    async def test_media_range(self) -> None:
        status, headers, body = await read_http(self.server.port, self.path, {"Range": "bytes=100-199"})
        self.assertEqual(status, 206)
        self.assertEqual(body, PAYLOAD[100:200])
        self.assertIn("bytes 100-199", headers.get("content-range", ""))

    async def test_media_open_ended_range(self) -> None:
        """Так перематывает FFmpeg: bytes=N- (до конца файла)."""
        start = len(PAYLOAD) - 5000
        status, _headers, body = await read_http(self.server.port, self.path, {"Range": f"bytes={start}-"})
        self.assertEqual(status, 206)
        self.assertEqual(body, PAYLOAD[start:])

    async def test_head_has_no_body(self) -> None:
        status, headers, body = await read_http(self.server.port, self.path, method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("content-length"), str(len(PAYLOAD)))

    async def test_unknown_token(self) -> None:
        status, _headers, _body = await read_http(self.server.port, "/media/nope")
        self.assertEqual(status, 404)

    async def test_tokens_are_unguessable(self) -> None:
        other = self.server.add_session(self.file)
        self.assertNotEqual(other.token, self.session.token)
        self.assertGreaterEqual(len(other.token), 20)

    def _new_session(self, file_id: str):
        """Сессия для нового файла — после того, как тест настроил «облако»
        (загрузка в кэш начинается сразу при создании сессии)."""
        file = CloudFile(
            provider=ProviderId.YANDEX, file_id=file_id, path=f"/video/{file_id}.mp4",
            name=f"{file_id}.mp4", kind=EntryKind.FILE, size=len(PAYLOAD), mime="video/mp4",
        )
        return f"/media/{self.server.add_session(file).token}"

    def _status_server(self, status: int):
        StatusHandler.status = status
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StatusHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/x"

    async def test_stale_link_is_refreshed(self) -> None:
        """Облако ответило 403 (протухшая ссылка) — сервер берёт новую и отдаёт файл."""
        self.provider.urls = [self._status_server(403), self.good_url]
        self.provider.resolved = 0
        status, _headers, body = await read_http(self.server.port, self._new_session("stale"))
        self.assertEqual(status, 200)
        self.assertEqual(body, PAYLOAD)
        self.assertEqual(self.provider.forgotten, 1)

    async def test_missing_file_is_reported_not_passed_off_as_media(self) -> None:
        """Файл удалён (404 и после перевыпуска ссылки) — плеер получает 502, а не «200 OK»."""
        self.provider.urls = [self._status_server(404)]
        self.provider.resolved = 0
        started = asyncio.get_running_loop().time()
        status, _headers, _body = await read_http(self.server.port, self._new_session("gone"))
        self.assertEqual(status, 502)
        self.assertLess(asyncio.get_running_loop().time() - started, 5, "отказ облака — без долгих повторов")

    async def test_storage_node_error_gets_new_link(self) -> None:
        """Узел хранилища Яндекса отвечает 500 — берём новую ссылку (другой узел), а не
        долбим тот же адрес: раньше шесть повторов шли в один сломанный узел."""
        self.provider.urls = [self._status_server(500), self.good_url]
        self.provider.resolved = 0
        status, _headers, body = await read_http(self.server.port, self._new_session("node500"))
        self.assertEqual(status, 200)
        self.assertEqual(body, PAYLOAD)
        self.assertEqual(self.provider.resolved, 2)
        self.assertEqual(self.provider.forgotten, 1)

    async def test_file_without_range_support_still_previews(self) -> None:
        """Яндекс отвечает 500 на любой запрос части файла, а целиком отдаёт —
        превью читает файл по порядку («МИР ТРЕЙДИНГА рилс.mp4», 21.10.2026)."""
        NoRangeHandler.payload = PAYLOAD
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NoRangeHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.provider.urls = [f"http://127.0.0.1:{server.server_address[1]}/f"]
        self.provider.resolved = 0
        path = self._new_session("norange")
        status, _headers, body = await read_http(self.server.port, path, {"Range": "bytes=150000-"})
        self.assertEqual(status, 206)
        self.assertEqual(body, PAYLOAD[150000:])

    async def test_warm_prefetches_only_a_little_then_serves_instantly(self) -> None:
        """Предзагрузка соседа: ссылка и начало файла готовы заранее, но качаем
        немного; открыли файл — берём тот же кэш и качаем уже с запасом."""
        from app.preview import stream_cache

        big = CloudFile(provider=ProviderId.YANDEX, file_id="warm", path="/video/warm.mp4",
                        name="warm.mp4", kind=EntryKind.FILE, size=len(PAYLOAD), mime="video/mp4")
        original = stream_cache.WARM_AHEAD, stream_cache.CHUNK
        stream_cache.CHUNK = 16 * 1024                    # файл — 13 кусков
        stream_cache.WARM_AHEAD = stream_cache.CHUNK      # «немного» = один кусок
        try:
            self.server.warm([big])
            for _ in range(100):
                await asyncio.sleep(0.02)
                entry = self.server._caches.get(self.server._file_key(big))
                if entry is not None and entry[0]._index[0] >= 0:
                    break
            cache = self.server._caches[self.server._file_key(big)][0]
            await asyncio.sleep(0.3)
            have = sum(1 for o in cache._index if o >= 0)
            self.assertGreaterEqual(have, 1)
            self.assertLess(have, cache.count, "предзагрузка не качает весь файл")
            resolved = self.provider.resolved
            session = self.server.add_session(big)
            status, _headers, body = await read_http(self.server.port, f"/media/{session.token}")
            self.assertEqual(status, 200)
            self.assertEqual(body, PAYLOAD)
            self.assertIs(session.cache, cache, "тот же кэш, что готовили заранее")
            self.assertEqual(self.provider.resolved, resolved, "ссылку заново не просили")
        finally:
            stream_cache.WARM_AHEAD, stream_cache.CHUNK = original

    async def test_server_error_is_retried_then_reported(self) -> None:
        from app.preview import stream_cache

        self.provider.urls = [self._status_server(500)]
        self.provider.resolved = 0
        original = stream_cache.MAX_FAILURES
        stream_cache.MAX_FAILURES = 2
        try:
            status, _headers, _body = await read_http(self.server.port, self._new_session("broken"))
        finally:
            stream_cache.MAX_FAILURES = original
        self.assertEqual(status, 502)

    async def test_image_goes_straight_through(self) -> None:
        """Картинки (и файлы без размера) идут без кэша потока — напрямую из облака."""
        file = CloudFile(provider=ProviderId.YANDEX, file_id="img", path="/a.jpg", name="a.jpg",
                         kind=EntryKind.FILE, size=len(PAYLOAD))
        session = self.server.add_session(file)
        status, _headers, body = await read_http(self.server.port, f"/media/{session.token}")
        self.assertEqual(status, 200)
        self.assertEqual(body, PAYLOAD)
        self.assertIsNone(session.cache)

    async def test_local_proxy_supports_range(self) -> None:
        proxy = Path(self._tmp.name) / "proxy.mp4"
        proxy.write_bytes(PAYLOAD)
        self.session.proxy_path = proxy
        url = f"/proxy/{self.session.token}"

        status, headers, body = await read_http(self.server.port, url, {"Range": "bytes=10-19"})
        self.assertEqual(status, 206)
        self.assertEqual(body, PAYLOAD[10:20])
        self.assertEqual(headers.get("content-range"), f"bytes 10-19/{len(PAYLOAD)}")

        status, _headers, body = await read_http(self.server.port, url)
        self.assertEqual(status, 200)
        self.assertEqual(body, PAYLOAD)

        status, headers, _body = await read_http(self.server.port, url, {"Range": f"bytes={len(PAYLOAD)}-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers.get("content-range"), f"bytes */{len(PAYLOAD)}")

    async def test_proxy_not_ready(self) -> None:
        status, _headers, _body = await read_http(self.server.port, f"/proxy/{self.session.token}")
        self.assertEqual(status, 404)

    async def test_drop_session(self) -> None:
        self.server.drop_session(self.session.token)
        status, _headers, _body = await read_http(self.server.port, self.path)
        self.assertEqual(status, 404)


class TestHelpers(unittest.TestCase):
    def test_parse_range(self) -> None:
        self.assertIsNone(parse_range(None, 100))
        self.assertEqual(parse_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(parse_range("bytes=90-", 100), (90, 99))
        self.assertEqual(parse_range("bytes=90-500", 100), (90, 99))
        self.assertEqual(parse_range("bytes=-10", 100), (90, 99))
        self.assertIsNone(parse_range("bytes=0-1,5-6", 100))  # несколько диапазонов — целиком
        with self.assertRaises(ValueError):
            parse_range("bytes=100-", 100)

    def test_media_kind(self) -> None:
        def kind(name: str, mime: str = "") -> str:
            return media_kind(CloudFile(provider=ProviderId.YANDEX, file_id=name, path=name,
                                        name=name, kind=EntryKind.FILE, mime=mime))

        for name in ("A001.MOV", "clip.mp4", "cam.MTS", "x.mkv", "x.avi", "x.mxf", "x.m2ts"):
            self.assertEqual(kind(name), "video", name)
        for name in ("voice.WAV", "music.mp3", "a.m4a", "a.flac", "a.aiff"):
            self.assertEqual(kind(name), "audio", name)
        for name in ("photo.JPG", "a.png", "a.webp", "a.tif", "a.heic.jpg"):
            self.assertEqual(kind(name), "image", name)
        self.assertEqual(kind("doc.pdf"), "unknown")
        self.assertEqual(kind("noext", "video/quicktime"), "video")


if __name__ == "__main__":
    unittest.main()
