"""HTTP-клиент: повторы при 429/5xx и Retry-After.

Раньше повторялись только 429 и 503, а на пути байтов (потоков) повторов
не было вовсе — облако отвечало «слишком много запросов», и следующий
сегмент бил по тому же хосту без паузы.
"""

from __future__ import annotations

import http.server
import threading
import unittest

import httpx

from app.core.config import AppConfig
from app.net.http_client import HttpClient
from app.net.ratelimit import RateLimiter


class FlakyHandler(http.server.BaseHTTPRequestHandler):
    """Отдаёт заранее заданные коды ответа по очереди."""

    statuses: list[int] = []
    calls = 0
    retry_after: str | None = None

    def log_message(self, *args):  # noqa: D102 - глушим вывод
        pass

    def do_GET(self):  # noqa: N802
        FlakyHandler.calls += 1
        index = min(FlakyHandler.calls - 1, len(FlakyHandler.statuses) - 1)
        status = FlakyHandler.statuses[index]
        body = b"{}" if status < 400 else b'{"error": "nope"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if FlakyHandler.retry_after and status == 429:
            self.send_header("Retry-After", FlakyHandler.retry_after)
        self.end_headers()
        self.wfile.write(body)


class TestHttpClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FlakyHandler.statuses = [200]
        FlakyHandler.calls = 0
        FlakyHandler.retry_after = None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/x"
        self.config = AppConfig(max_retries=3, retry_base_delay=0.01, retry_max_delay=0.05)
        self.http = HttpClient(self.config, RateLimiter(100))

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        self.server.shutdown()
        self.server.server_close()

    async def test_502_is_retried(self) -> None:
        """Раньше 502 доходил до пользователя с первой попытки."""
        FlakyHandler.statuses = [502, 502, 200]
        response = await self.http.request("GET", self.url, provider="yandex")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FlakyHandler.calls, 3)

    async def test_500_is_retried(self) -> None:
        FlakyHandler.statuses = [500, 200]
        response = await self.http.request("GET", self.url, provider="yandex")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FlakyHandler.calls, 2)

    async def test_404_is_not_retried(self) -> None:
        """Ошибку клиента повторять бессмысленно — она не пройдёт."""
        FlakyHandler.statuses = [404]
        with self.assertRaises(httpx.HTTPStatusError):
            await self.http.request("GET", self.url, provider="yandex")
        self.assertEqual(FlakyHandler.calls, 1)

    async def test_retry_after_is_honoured(self) -> None:
        FlakyHandler.statuses = [429, 200]
        FlakyHandler.retry_after = "0"
        response = await self.http.request("GET", self.url, provider="yandex")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FlakyHandler.calls, 2)

    async def test_zero_retries_still_sends_one_request(self) -> None:
        """max_retries=0 — это одна попытка, а не ноль (было исключение)."""
        config = AppConfig(max_retries=0)
        http = HttpClient(config, RateLimiter(100))
        try:
            FlakyHandler.statuses = [200]
            response = await http.request("GET", self.url, provider="yandex")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(FlakyHandler.calls, 1)
        finally:
            await http.aclose()

    async def test_stream_retries_on_503(self) -> None:
        """Путь байтов тоже должен терпеть 503 и повторять запрос."""
        FlakyHandler.statuses = [503, 200]
        response = await self.http.stream("GET", self.url, provider="yandex")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FlakyHandler.calls, 2)
        await response.aclose()

    async def test_stream_honours_retry_after(self) -> None:
        FlakyHandler.statuses = [429, 200]
        FlakyHandler.retry_after = "0"
        response = await self.http.stream("GET", self.url, provider="yandex")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FlakyHandler.calls, 2)
        await response.aclose()


if __name__ == "__main__":
    unittest.main()
