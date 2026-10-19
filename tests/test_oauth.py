"""Тесты OAuth: формат запросов Яндекса и разбор ошибок."""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.errors import AuthError
from app.core.models import ProviderId
from app.oauth.base import OAuthTokens
from app.oauth.google import GoogleOAuth
from app.oauth.yandex import REDIRECT_URIS, YandexOAuth


class TestYandexOAuth(unittest.TestCase):
    def setUp(self) -> None:
        self.oauth = YandexOAuth("client-id", "client-secret", device_id="dev123", device_name="Тест")

    def test_authorization_url(self) -> None:
        url = self.oauth.authorization_url(REDIRECT_URIS[0], state="svscloud")
        parsed = urlparse(url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.assertEqual(parsed.netloc, "oauth.yandex.com")
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["client_id"], "client-id")
        self.assertEqual(params["redirect_uri"], REDIRECT_URIS[0])
        self.assertEqual(params["device_id"], "dev123")
        # scope не шлём — Яндекс выдаст права, заданные при регистрации
        self.assertNotIn("scope", params)

    def test_redirect_uri_is_verification_code(self) -> None:
        self.assertEqual(REDIRECT_URIS[0], "https://oauth.yandex.com/verification_code")

    def test_code_must_be_digits(self) -> None:
        import asyncio

        with self.assertRaises(AuthError) as ctx:
            asyncio.run(self.oauth.exchange_code("не цифры"))
        self.assertIn("7 цифр", ctx.exception.user_message)

    def test_empty_code(self) -> None:
        import asyncio

        with self.assertRaises(AuthError):
            asyncio.run(self.oauth.exchange_code("   "))

    def test_error_texts(self) -> None:
        cases = {
            "invalid_grant": "Код не подошёл",
            "bad_verification_code": "7 цифр",
            "invalid_client": "Ключи приложения неверны",
            "invalid_scope": "нет прав на Яндекс.Диск",
        }
        for code, expected in cases.items():
            response = httpx.Response(
                400,
                json={"error": code, "error_description": "some text"},
                request=httpx.Request("POST", "https://oauth.yandex.com/token"),
            )
            error = self.oauth._error(response)
            self.assertIn(expected, error.user_message, msg=code)

    def test_error_without_known_code(self) -> None:
        response = httpx.Response(
            400,
            json={"error": "something_new", "error_description": "непонятно"},
            request=httpx.Request("POST", "https://oauth.yandex.com/token"),
        )
        error = self.oauth._error(response)
        self.assertIn("непонятно", error.user_message)

    def test_account_info_fallback(self) -> None:
        import asyncio

        name = asyncio.run(self.oauth.account_info(OAuthTokens(access_token="x")))
        self.assertEqual(name, "Яндекс.Диск")


class TestGoogleOAuth(unittest.TestCase):
    def setUp(self) -> None:
        self.oauth = GoogleOAuth("id.apps.googleusercontent.com", "secret")

    def test_authorization_url(self) -> None:
        url = self.oauth.authorization_url("http://127.0.0.1:5555/", state="svscloud")
        params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["access_type"], "offline")
        self.assertEqual(params["redirect_uri"], "http://127.0.0.1:5555/")
        self.assertIn("drive.readonly", params["scope"])

    def test_provider_ids(self) -> None:
        self.assertIs(YandexOAuth.id, ProviderId.YANDEX)
        self.assertIs(GoogleOAuth.id, ProviderId.GOOGLE)


if __name__ == "__main__":
    unittest.main()
