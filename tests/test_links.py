"""Тесты разбора ссылок и разбора ответов Яндекса."""

from __future__ import annotations

import unittest

from app.core.errors import BadLinkError, friendly_error
from app.core.models import ProviderId, SourceKind
from app.providers.link_router import parse_link
from app.providers.yandex import YandexProvider


class TestLinkRouter(unittest.TestCase):
    def test_yandex_public_d(self) -> None:
        parsed = parse_link("https://disk.yandex.ru/d/AbCdEf12")
        self.assertEqual(parsed.provider, ProviderId.YANDEX)
        self.assertEqual(parsed.source, SourceKind.PUBLIC)
        # API принимает полную ссылку, а не короткий идентификатор
        self.assertEqual(parsed.public_key, "https://disk.yandex.ru/d/AbCdEf12")
        self.assertEqual(parsed.path, "/")

    def test_yandex_file_link_keeps_i(self) -> None:
        """/i/ — ссылка на отдельный файл. Превращать её в /d/ нельзя: Яндекс ответит 404."""
        parsed = parse_link("https://disk.yandex.ru/i/8NDhH7I41pQqoQ")
        self.assertEqual(parsed.public_key, "https://disk.yandex.ru/i/8NDhH7I41pQqoQ")
        self.assertEqual(parse_link("https://yadi.sk/i/AbC-12_x").public_key,
                         "https://disk.yandex.ru/i/AbC-12_x")

    def test_yandex_short_link(self) -> None:
        parsed = parse_link("https://yadi.sk/d/XyZ987")
        self.assertEqual(parsed.provider, ProviderId.YANDEX)
        self.assertEqual(parsed.public_key, "https://disk.yandex.ru/d/XyZ987")

    def test_yandex_with_subpath(self) -> None:
        parsed = parse_link("https://disk.yandex.ru/d/AbCdEf12?path=%2FDir%2FSub")
        self.assertEqual(parsed.path, "/Dir/Sub")

    def test_google_folder(self) -> None:
        parsed = parse_link("https://drive.google.com/drive/folders/1AbCdEfGhIjK")
        self.assertEqual(parsed.provider, ProviderId.GOOGLE)
        self.assertEqual(parsed.file_id, "1AbCdEfGhIjK")
        self.assertTrue(parsed.is_folder_hint)

    def test_google_file(self) -> None:
        parsed = parse_link("https://drive.google.com/file/d/1AbCdEfGhIjK/view?usp=sharing")
        self.assertEqual(parsed.provider, ProviderId.GOOGLE)
        self.assertEqual(parsed.file_id, "1AbCdEfGhIjK")
        self.assertFalse(parsed.is_folder_hint)

    def test_google_docs(self) -> None:
        parsed = parse_link("https://docs.google.com/document/d/1AbCdEfGhIjK/edit")
        self.assertEqual(parsed.provider, ProviderId.GOOGLE)
        self.assertEqual(parsed.file_id, "1AbCdEfGhIjK")

    def test_scheme_is_optional(self) -> None:
        parsed = parse_link("disk.yandex.ru/d/AbCdEf12")
        self.assertEqual(parsed.provider, ProviderId.YANDEX)

    def test_unknown_host(self) -> None:
        with self.assertRaises(BadLinkError):
            parse_link("https://example.com/folder/1")

    def test_empty(self) -> None:
        with self.assertRaises(BadLinkError):
            parse_link("")


class TestYandexParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = YandexProvider.__new__(YandexProvider)

    def test_parse_file_item(self) -> None:
        raw = {
            "name": "clip.mp4",
            "type": "file",
            "size": 12345,
            "mime_type": "video/mp4",
            "modified": "2024-05-01T10:20:30.000Z",
            "md5": "abc",
            "path": "/Folder/clip.mp4",
        }
        item = self.provider._parse_item(raw, self._ctx())
        self.assertIsNotNone(item)
        self.assertEqual(item.name, "clip.mp4")
        self.assertEqual(item.size, 12345)
        self.assertEqual(item.path, "/Folder/clip.mp4")
        self.assertIsNotNone(item.modified)

    def test_parse_folder_item(self) -> None:
        raw = {"name": "Folder", "type": "folder", "path": "/Folder"}
        item = self.provider._parse_item(raw, self._ctx())
        self.assertFalse(item.is_file)

    @staticmethod
    def _ctx():
        from app.core.models import SourceKind
        from app.providers.base import BrowseContext

        return BrowseContext(source=SourceKind.PUBLIC, public_key="key", root_path="/")


class TestFriendlyErrors(unittest.TestCase):
    def test_rate_limit_yandex(self) -> None:
        import httpx

        response = httpx.Response(429, json={"message": "Rate limit exceeded"}, request=httpx.Request("GET", "http://x"))
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        message = friendly_error(exc, provider="yandex")
        self.assertIn("запрос", message.lower())
        self.assertNotIn("429", message)

    def test_not_found(self) -> None:
        import httpx

        response = httpx.Response(404, request=httpx.Request("GET", "http://x"))
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        self.assertIn("не найден", friendly_error(exc).lower())

    def test_google_quota(self) -> None:
        import httpx

        response = httpx.Response(
            403,
            json={"error": {"message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
            request=httpx.Request("GET", "http://x"),
        )
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        self.assertIn("лимит", friendly_error(exc, provider="google").lower())

    def test_no_stacktrace_in_message(self) -> None:
        message = friendly_error(ValueError("какая-то внутренняя ошибка"))
        self.assertNotIn("Traceback", message)
        self.assertNotIn("ValueError", message)

    def test_yandex_not_found_body(self) -> None:
        import httpx

        response = httpx.Response(
            404,
            json={"error": "DiskNotFoundError", "description": "Resource not found.",
                  "message": "Не удалось найти запрошенный ресурс."},
            request=httpx.Request("GET", "http://x"),
        )
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        message = friendly_error(exc, provider="yandex")
        self.assertIn("не найден", message.lower())
        self.assertNotIn("DiskNotFoundError", message)

    def test_server_cut_stream(self) -> None:
        from app.downloader.transfer import ServerCutStream

        message = friendly_error(ServerCutStream("сервер оборвал передачу на 100 из 900 байт"))
        self.assertIn("оборвал", message.lower())
        self.assertNotIn("Traceback", message)


if __name__ == "__main__":
    unittest.main()
