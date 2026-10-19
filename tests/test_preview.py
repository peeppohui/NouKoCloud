"""Тесты сервера превью: Range-запросы и страница плеера."""

from __future__ import annotations

import asyncio
import unittest

from app.core.config import AppConfig
from app.core.models import CloudFile, DownloadLink, EntryKind, ProviderId
from app.net.http_client import HttpClient
from app.net.ratelimit import RateLimiter
from app.preview.server import PreviewServer

from .helpers import start_range_server, stop_server

PAYLOAD = bytes((i * 13 + 7) % 256 for i in range(200000))


class FakeProvider:
    def __init__(self, url: str) -> None:
        self.url = url

    async def resolve_download(self, file: CloudFile) -> DownloadLink:
        return DownloadLink(url=self.url, headers={}, size=file.size)


class FakeRegistry:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider

    def get(self, _pid: ProviderId) -> FakeProvider:
        return self.provider


async def read_http(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
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
        self.config = AppConfig()
        self.http = HttpClient(self.config, RateLimiter(50))
        self.server = PreviewServer(
            FakeRegistry(FakeProvider(f"http://127.0.0.1:{self.port}/file.mp4")),
            self.http,
            self.config,
        )
        await self.server.start()
        self.file = CloudFile(
            provider=ProviderId.YANDEX,
            file_id="f1",
            path="/video/clip.mp4",
            name="clip.mp4",
            kind=EntryKind.FILE,
            size=len(PAYLOAD),
            mime="video/mp4",
        )
        self.session = self.server.add_session(self.file)

    async def asyncTearDown(self) -> None:
        await self.server.stop()
        await self.http.aclose()
        stop_server(self.origin)

    async def test_player_page(self) -> None:
        status, headers, body = await read_http(self.server.port, f"/player/{self.session.token}")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn(b"<video", body)

    async def test_media_full(self) -> None:
        status, headers, body = await read_http(self.server.port, f"/media/{self.session.token}")
        self.assertEqual(status, 200)
        self.assertEqual(body, PAYLOAD)
        self.assertEqual(headers.get("accept-ranges"), "bytes")

    async def test_media_range(self) -> None:
        status, headers, body = await read_http(
            self.server.port, f"/media/{self.session.token}", {"Range": "bytes=100-199"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, PAYLOAD[100:200])
        self.assertIn("bytes 100-199", headers.get("content-range", ""))

    async def test_unknown_token(self) -> None:
        status, _headers, _body = await read_http(self.server.port, "/media/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
