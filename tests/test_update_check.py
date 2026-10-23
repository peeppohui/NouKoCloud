"""Проверка новой версии на GitHub: версии, ответ API и ошибки сети."""

from __future__ import annotations

import asyncio
import unittest

import httpx

from app.core.update_check import (
    CHECK_INTERVAL,
    RELEASES_URL,
    ReleaseInfo,
    fetch_latest_release,
    is_check_due,
    is_newer,
    parse_release,
    parse_version,
    should_notify,
)


class TestVersionComparison(unittest.TestCase):
    def test_parse_versions(self) -> None:
        self.assertEqual(parse_version("0.5.4"), (0, 5, 4))
        self.assertEqual(parse_version("v1.0.0"), (1, 0, 0))
        self.assertIsNone(parse_version("0.5"))
        self.assertIsNone(parse_version("0.5.4-beta"))
        self.assertIsNone(parse_version(""))

    def test_numeric_not_lexicographic(self) -> None:
        self.assertTrue(is_newer("0.5.10", "0.5.9"))
        self.assertTrue(is_newer("0.10.0", "0.9.99"))
        self.assertTrue(is_newer("v1.0.0", "0.99.99"))
        self.assertFalse(is_newer("0.5.4", "0.5.4"))
        self.assertFalse(is_newer("0.5.3", "0.5.4"))
        self.assertFalse(is_newer("unknown", "0.5.4"))

    def test_notify_only_once_per_version(self) -> None:
        release = ReleaseInfo("0.5.5", "https://github.com/peeppohui/NouKoCloud/releases")
        self.assertTrue(should_notify(release, "0.5.4", ""))
        self.assertFalse(should_notify(release, "0.5.4", "0.5.5"))
        self.assertFalse(should_notify(release, "0.5.5", ""))
        self.assertFalse(should_notify(None, "0.5.4", ""))

    def test_check_interval(self) -> None:
        now = 1_000_000.0
        self.assertTrue(is_check_due(0.0, now=now))
        self.assertTrue(is_check_due(now - CHECK_INTERVAL - 1, now=now))
        self.assertFalse(is_check_due(now - 60, now=now))


class TestReleasePayload(unittest.TestCase):
    def _payload(self, **overrides):
        payload = {
            "tag_name": "v0.5.5",
            "html_url": "https://github.com/peeppohui/NouKoCloud/releases/tag/v0.5.5",
            "name": "0.5.5",
            "published_at": "2026-10-23T12:00:00Z",
            "draft": False,
            "prerelease": False,
        }
        payload.update(overrides)
        return payload

    def test_valid_release(self) -> None:
        release = parse_release(self._payload())
        self.assertIsNotNone(release)
        assert release is not None
        self.assertEqual(release.version, "0.5.5")
        self.assertEqual(release.name, "0.5.5")
        self.assertIn("/releases/tag/v0.5.5", release.url)

    def test_draft_and_prerelease_are_ignored(self) -> None:
        self.assertIsNone(parse_release(self._payload(draft=True)))
        self.assertIsNone(parse_release(self._payload(prerelease=True)))

    def test_bad_tag_or_payload(self) -> None:
        self.assertIsNone(parse_release(self._payload(tag_name="latest")))
        self.assertIsNone(parse_release("not a dict"))
        self.assertIsNone(parse_release({"tag_name": "release-1"}))
        self.assertIsNone(parse_release({}))

    def test_foreign_url_falls_back_to_releases_page(self) -> None:
        release = parse_release(self._payload(html_url="https://evil.example/release"))
        self.assertIsNotNone(release)
        assert release is not None
        self.assertEqual(release.url, RELEASES_URL)


class TestFetchRelease(unittest.TestCase):
    def _fetch(self, handler):
        async def run():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                return await fetch_latest_release(client)
        return asyncio.run(run())

    def test_success(self) -> None:
        def handler(request):
            return httpx.Response(200, json={
                "tag_name": "v0.5.5",
                "html_url": "https://github.com/peeppohui/NouKoCloud/releases/tag/v0.5.5",
                "draft": False,
                "prerelease": False,
            }, request=request)

        release = self._fetch(handler)
        self.assertIsNotNone(release)
        assert release is not None
        self.assertEqual(release.version, "0.5.5")

    def test_http_error_is_silent(self) -> None:
        def handler(request):
            return httpx.Response(404, request=request)

        self.assertIsNone(self._fetch(handler))

    def test_redirect_is_followed(self) -> None:
        """После переименования репозитория GitHub отвечает 301."""
        def handler(request):
            if request.url.path == "/old":
                return httpx.Response(
                    301,
                    headers={"Location": "https://api.github.com/repos/new/latest"},
                    request=request,
                )
            return httpx.Response(200, json={
                "tag_name": "v0.5.5",
                "draft": False,
                "prerelease": False,
            }, request=request)

        async def run():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                return await fetch_latest_release(client, url="https://api.github.com/old")
        release = asyncio.run(run())
        self.assertIsNotNone(release)
        assert release is not None
        self.assertEqual(release.version, "0.5.5")

    def test_network_error_is_silent(self) -> None:
        def handler(request):
            raise httpx.ConnectError("нет сети", request=request)

        self.assertIsNone(self._fetch(handler))

    def test_invalid_json_is_silent(self) -> None:
        def handler(request):
            return httpx.Response(200, content=b"<html>not json</html>", request=request)

        self.assertIsNone(self._fetch(handler))


if __name__ == "__main__":
    unittest.main()
