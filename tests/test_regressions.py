"""Регрессионные тесты: OAuth-refresh, ссылки Google, кэш ссылок, отмена загрузок."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.config import AppConfig
from app.core.errors import AuthError, ServerError
from app.core.models import CloudFile, DownloadJob, EntryKind, JobState, ProviderId, SourceKind
from app.downloader.engine import DownloadEngine
from app.downloader.state import save_state
from app.net.metadata_cache import MetadataCache
from app.oauth.base import OAuthTokens
from app.oauth.google import GoogleOAuth
from app.oauth.manager import OAuthManager
from app.oauth.yandex import YandexOAuth
from app.providers.google import GoogleProvider
from app.providers.yandex import YandexProvider


def _response(status: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status, json=payload or {}, request=httpx.Request("POST", "https://example.test/token")
    )


class MemoryStore:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})

    def get(self, provider: str):
        return self.data.get(provider)

    def set(self, provider: str, payload: dict) -> None:
        self.data[provider] = payload

    def clear(self, provider: str) -> None:
        self.data.pop(provider, None)


class TestRefresh(unittest.TestCase):
    """Временный сбой облака при обновлении токена не должен разлогинивать."""

    def _manager(self) -> tuple[OAuthManager, MemoryStore]:
        expired = OAuthTokens(access_token="old", refresh_token="r", expires_at=1.0).to_dict()
        store = MemoryStore({"google": expired, "yandex": expired})
        config = AppConfig()
        config.google_client_id = config.yandex_client_id = "id"
        return OAuthManager(config, store), store  # type: ignore[arg-type]

    def test_server_error_keeps_tokens(self) -> None:
        manager, store = self._manager()
        for pid in (ProviderId.GOOGLE, ProviderId.YANDEX):
            with mock.patch("httpx.AsyncClient.post", return_value=_response(503)):
                with self.assertRaises(ServerError):
                    asyncio.run(manager.access_token(pid))
            self.assertIsNotNone(store.get(pid.value), pid)
            self.assertTrue(manager.is_logged_in(pid), pid)

    def test_revoked_refresh_token_logs_out(self) -> None:
        manager, store = self._manager()
        with mock.patch("httpx.AsyncClient.post", return_value=_response(400, {"error": "invalid_grant"})):
            with self.assertRaises(AuthError):
                asyncio.run(manager.access_token(ProviderId.GOOGLE))
        self.assertIsNone(store.get("google"))

    def test_yandex_code_is_sent_once(self) -> None:
        oauth = YandexOAuth("id", "secret")
        post = mock.AsyncMock(return_value=_response(400, {"error": "invalid_grant"}))
        with mock.patch("httpx.AsyncClient.post", post):
            with self.assertRaises(AuthError):
                asyncio.run(oauth.exchange_code("1234567"))
        self.assertEqual(post.await_count, 1)

    def test_yandex_accepts_letter_codes(self) -> None:
        """Яндекс теперь выдаёт код из букв и цифр (например, lem4whktcygadzng)."""
        oauth = YandexOAuth("id", "secret")
        ok = _response(200, {"access_token": "t", "refresh_token": "r", "expires_in": 3600})
        post = mock.AsyncMock(return_value=ok)
        with mock.patch("httpx.AsyncClient.post", post):
            tokens = asyncio.run(oauth.exchange_code(" lem4whktcygadzng\n"))
        self.assertEqual(tokens.access_token, "t")
        self.assertEqual(post.await_args.kwargs["data"]["code"], "lem4whktcygadzng")

    def test_yandex_code_from_pasted_url(self) -> None:
        from app.oauth.yandex import normalize_code

        self.assertEqual(
            normalize_code("https://oauth.yandex.com/verification_code?code=lem4whktcygadzng"),
            "lem4whktcygadzng",
        )
        self.assertEqual(normalize_code("1234567"), "1234567")


class TestGoogleBrowserLogin(unittest.IsolatedAsyncioTestCase):
    """Вход через браузер: код ловит локальный сервер, затем обмен на токен.

    Раньше обмен спрашивал адрес у уже остановленного сервера и падал
    («Сервер не запущен») — вход в Google не завершался никогда.
    """

    async def test_full_flow_uses_same_redirect_uri(self) -> None:
        store = MemoryStore()
        config = AppConfig()
        config.google_client_id = "id.apps.googleusercontent.com"
        manager = OAuthManager(config, store)  # type: ignore[arg-type]
        seen: dict[str, str] = {}

        class FakeGoogle:
            display_name = "Google Диск"

            def authorization_url(self, redirect_uri: str, state: str = "") -> str:
                seen["auth"] = redirect_uri
                return f"{redirect_uri}/?code=4/test-code&state={state}"   # «браузер» сразу вернёт код

            async def exchange_code(self, code: str, redirect_uri: str) -> OAuthTokens:
                seen["code"], seen["exchange"] = code, redirect_uri
                return OAuthTokens(access_token="google-token", refresh_token="r", expires_at=9e9)

            async def account_info(self, tokens: OAuthTokens) -> str:
                return "user@gmail.com"

        manager._providers[ProviderId.GOOGLE] = FakeGoogle()  # type: ignore[assignment]

        def fake_browser(url: str) -> bool:
            # Как настоящий браузер после согласия: GET на redirect_uri с кодом.
            httpx.get(url, timeout=5)
            return True

        with mock.patch("webbrowser.open", fake_browser):
            name = await manager.login_with_browser(ProviderId.GOOGLE)
        self.assertEqual(name, "user@gmail.com")
        self.assertEqual(seen["code"], "4/test-code")
        self.assertEqual(seen["exchange"], seen["auth"], "в обмен должен уйти тот же redirect_uri")
        self.assertTrue(manager.is_logged_in(ProviderId.GOOGLE))
        self.assertEqual(store.get("google")["access_token"], "google-token")

    async def test_redirect_uri_survives_stop(self) -> None:
        from app.oauth.loopback import LoopbackRedirectServer

        server = LoopbackRedirectServer()
        await server.start()
        uri = server.redirect_uri
        await server.stop()
        self.assertEqual(server.redirect_uri, uri)

    async def test_malformed_request_does_not_break_server(self) -> None:
        """Мусорный запрос не должен ронять обработчик (был UnboundLocalError)."""
        from app.oauth.loopback import LoopbackRedirectServer

        server = LoopbackRedirectServer()
        await server.start()
        try:
            port = server.port
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GARBAGE\r\n\r\n")      # нет пути — .split(" ")[1] падал
            await writer.drain()
            await reader.read(200)
            writer.close()

            # После мусора сервер обязан по-прежнему ловить код.
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /?code=XYZ HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            await reader.read(200)
            writer.close()
            code = await asyncio.wait_for(server.wait_for_code(), timeout=5)
            self.assertEqual(code, "XYZ")
        finally:
            await server.stop()

    def test_query_parsing_survives_garbage(self) -> None:
        """Разбор строки запроса не должен падать на мусоре."""
        from app.oauth.loopback import _query_of

        self.assertEqual(_query_of(b"GET /?code=1 HTTP/1.1"), "/?code=1")
        self.assertEqual(_query_of(b"GARBAGE\r\n"), "")
        self.assertEqual(_query_of(b""), "")


class TestGoogleDriveScope(unittest.TestCase):
    """Вход без галочки «Google Диск»: вход «успешен», а все запросы — 403."""

    def test_login_without_drive_checkbox_is_rejected(self) -> None:
        oauth = GoogleOAuth("id", "secret")
        granted = _response(200, {"access_token": "t", "expires_in": 3600,
                                  "scope": "https://www.googleapis.com/auth/userinfo.email openid"})
        with mock.patch("httpx.AsyncClient.post", mock.AsyncMock(return_value=granted)):
            with self.assertRaises(AuthError) as ctx:
                asyncio.run(oauth.exchange_code("code", "http://127.0.0.1:1"))
        self.assertIn("галочка", ctx.exception.user_message)

    def test_login_with_drive_is_accepted(self) -> None:
        oauth = GoogleOAuth("id", "secret")
        granted = _response(200, {"access_token": "t", "expires_in": 3600,
                                  "scope": "openid https://www.googleapis.com/auth/drive.readonly"})
        with mock.patch("httpx.AsyncClient.post", mock.AsyncMock(return_value=granted)):
            tokens = asyncio.run(oauth.exchange_code("code", "http://127.0.0.1:1"))
        self.assertEqual(tokens.access_token, "t")

    def test_saved_login_without_drive_is_dropped(self) -> None:
        bad = OAuthTokens(access_token="x", expires_at=9e9,
                          scope="https://www.googleapis.com/auth/userinfo.email openid").to_dict()
        store = MemoryStore({"google": bad})
        manager = OAuthManager(AppConfig(), store)  # type: ignore[arg-type]
        self.assertFalse(manager.is_logged_in(ProviderId.GOOGLE))
        self.assertIsNone(store.get("google"))

    def test_friendly_messages(self) -> None:
        from app.core.errors import friendly_error

        def error(status: int, payload: dict) -> httpx.HTTPStatusError:
            request = httpx.Request("GET", "https://www.googleapis.com/drive/v3/files")
            response = httpx.Response(status, json=payload, request=request)
            return httpx.HTTPStatusError("err", request=request, response=response)

        scope = error(403, {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                                      "errors": [{"reason": "insufficientPermissions"}],
                                      "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}})
        text = friendly_error(scope, provider="google")
        self.assertIn("галочка", text)
        self.assertNotIn("владельца", text)

        disabled = error(403, {"error": {"code": 403, "message": "Google Drive API has not been used in "
                                         "project 1816 before or it is disabled.",
                                         "errors": [{"reason": "accessNotConfigured"}]}})
        self.assertIn("Drive API", friendly_error(disabled, provider="google"))

        owner = error(403, {"error": {"code": 403, "message": "The user does not have sufficient permissions.",
                                      "errors": [{"reason": "insufficientPermissions"}]}})
        self.assertIn("владельца", friendly_error(owner, provider="google"))


class TestConfigSafety(unittest.TestCase):
    """Временная конфигурация не должна попадать в файл настроек пользователя."""

    def setUp(self) -> None:
        from app.core import config as config_module

        self._tmp = tempfile.TemporaryDirectory()
        self.module = config_module
        self.real = config_module.CONFIG_FILE
        config_module.CONFIG_FILE = Path(self._tmp.name) / "config.json"

    def tearDown(self) -> None:
        self.module.CONFIG_FILE = self.real
        self._tmp.cleanup()

    def test_temporary_config_is_never_saved(self) -> None:
        cfg = AppConfig()
        cfg.download_dir = self._tmp.name
        cfg.save()
        self.assertFalse(self.module.CONFIG_FILE.exists(),
                         "AppConfig() из тестов не должен перезаписывать настройки пользователя")

    def test_loaded_config_is_saved_and_keeps_keys(self) -> None:
        self.module.CONFIG_FILE.write_text(json.dumps({"yandex_client_id": "ya-id"}), encoding="utf-8")
        cfg = AppConfig.load()
        cfg.google_client_id = "g-id"
        cfg.save()
        saved = json.loads(self.module.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["yandex_client_id"], "ya-id")
        self.assertEqual(saved["google_client_id"], "g-id")

    def test_temp_download_dir_is_repaired(self) -> None:
        temp_dir = Path(tempfile.gettempdir()) / "tmpe2f_y3fn"
        self.module.CONFIG_FILE.write_text(json.dumps({"download_dir": str(temp_dir)}), encoding="utf-8")
        cfg = AppConfig.load()
        self.assertNotEqual(Path(cfg.download_dir).resolve(), temp_dir.resolve())
        self.assertEqual(Path(cfg.download_dir), self.module.default_download_dir())

    def test_normal_download_dir_is_kept(self) -> None:
        own = Path.home() / "Videos" / "Исходники"
        self.module.CONFIG_FILE.write_text(json.dumps({"download_dir": str(own)}), encoding="utf-8")
        self.assertEqual(AppConfig.load().download_dir, str(own))


class FakeOAuth:
    def __init__(self, logged_in: bool = True) -> None:
        self.logged_in = logged_in

    def is_logged_in(self, _pid) -> bool:
        return self.logged_in

    async def access_token(self, _pid) -> str:
        return "secret-token"


class TestLinks(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = MetadataCache(Path(self._tmp.name) / "cache.db")
        self.config = AppConfig()

    def tearDown(self) -> None:
        self.cache.close()
        self._tmp.cleanup()

    def _google_file(self, **kwargs) -> CloudFile:
        values = dict(provider=ProviderId.GOOGLE, file_id="abc", path="clip.mp4", name="clip.mp4",
                      kind=EntryKind.FILE, size=10, source=SourceKind.OAUTH)
        values.update(kwargs)
        return CloudFile(**values)

    def test_google_link_requests_media(self) -> None:
        provider = GoogleProvider(None, FakeOAuth(), self.cache, self.config)  # type: ignore[arg-type]
        link = asyncio.run(provider.resolve_download(self._google_file(resource_key="rk")))
        query = parse_qs(urlparse(link.url).query)
        self.assertEqual(query.get("alt"), ["media"])
        self.assertEqual(link.headers["Authorization"], "Bearer secret-token")
        self.assertEqual(link.headers["X-Goog-Drive-Resource-Keys"], "abc/rk")

    def test_google_public_download_uses_web_endpoint(self) -> None:
        """Публичный файл качаем как браузер: API-ключ и его квота не нужны."""
        provider = GoogleProvider(None, FakeOAuth(logged_in=False), self.cache, self.config)
        link = asyncio.run(provider.resolve_download(
            self._google_file(source=SourceKind.PUBLIC, resource_key="rk")))
        parsed = urlparse(link.url)
        self.assertEqual(parsed.netloc, "drive.usercontent.google.com")
        query = parse_qs(parsed.query)
        self.assertEqual(query.get("id"), ["abc"])
        self.assertEqual(query.get("export"), ["download"])
        self.assertEqual(query.get("confirm"), ["t"])
        self.assertEqual(query.get("resourcekey"), ["rk"])
        self.assertNotIn("Authorization", link.headers)
        self.assertIsNone(link.fallback, "без ключа и входа резерв бессмысленен")

    def test_google_public_download_keeps_api_as_fallback(self) -> None:
        self.config.google_api_key = "test-key"
        provider = GoogleProvider(None, FakeOAuth(logged_in=False), self.cache, self.config)
        link = asyncio.run(provider.resolve_download(
            self._google_file(source=SourceKind.PUBLIC)))
        self.assertIsNotNone(link.fallback)
        fallback = link.fallback
        assert fallback is not None
        query = parse_qs(urlparse(fallback.url).query)
        self.assertEqual(query.get("alt"), ["media"])
        self.assertEqual(query.get("key"), ["test-key"])
        self.assertNotIn("Authorization", fallback.headers)

    def test_google_public_fallback_uses_login_without_key(self) -> None:
        provider = GoogleProvider(None, FakeOAuth(), self.cache, self.config)
        link = asyncio.run(provider.resolve_download(
            self._google_file(source=SourceKind.PUBLIC)))
        self.assertIsNotNone(link.fallback)
        fallback = link.fallback
        assert fallback is not None
        self.assertEqual(fallback.headers.get("Authorization"), "Bearer secret-token")

    def test_google_public_listing_uses_login_without_key(self) -> None:
        """«Войдите в аккаунт» должно работать и для публичной ссылки."""
        from app.providers.base import BrowseContext

        provider = GoogleProvider(None, FakeOAuth(), self.cache, self.config)
        headers = asyncio.run(provider._headers(BrowseContext(source=SourceKind.PUBLIC)))
        self.assertEqual(headers.get("Authorization"), "Bearer secret-token")

    def test_google_export_has_mime_type(self) -> None:
        provider = GoogleProvider(None, FakeOAuth(), self.cache, self.config)  # type: ignore[arg-type]
        doc = self._google_file(google_mime="application/vnd.google-apps.document")
        link = asyncio.run(provider.resolve_download(doc))
        self.assertTrue(urlparse(link.url).path.endswith("/export"))
        self.assertEqual(parse_qs(urlparse(link.url).query).get("mimeType"), ["application/pdf"])

    def test_google_list_fields_are_valid_v3(self) -> None:
        """Drive v3: длительность видео — durationMillis. С durationMs Google отвечал 400
        на весь список («Облако отклонило запрос»), и «Мой диск» не открывался."""
        from app.providers.google import FIELDS

        self.assertIn("videoMediaMetadata(durationMillis,height,width)", FIELDS)
        self.assertNotIn("durationMs", FIELDS)
        provider = GoogleProvider(None, FakeOAuth(), self.cache, self.config)  # type: ignore[arg-type]
        ctx = mock.Mock(source=SourceKind.OAUTH, drive_id=None)
        clip = provider._parse_item({"id": "1", "name": "a.mp4", "mimeType": "video/mp4",
                                     "videoMediaMetadata": {"durationMillis": "12345",
                                                            "width": 3840, "height": 2160}}, ctx)
        self.assertEqual(clip.media_duration_ms, 12345)
        self.assertEqual(clip.media_width, 3840)

    def test_google_paths_keep_folders(self) -> None:
        provider = GoogleProvider(None, FakeOAuth(), self.cache, self.config)  # type: ignore[arg-type]
        ctx = mock.Mock(source=SourceKind.OAUTH, drive_id=None)
        child = provider._parse_item({"id": "1", "name": "a.mp4"}, ctx, "Съёмка/День 1")
        self.assertEqual(child.path, "Съёмка/День 1/a.mp4")

    def test_cache_never_stores_authorization(self) -> None:
        provider = YandexProvider(None, FakeOAuth(), self.cache, self.config)  # type: ignore[arg-type]
        file = self._google_file(provider=ProviderId.YANDEX)
        from app.core.models import DownloadLink

        provider.remember_link(file, DownloadLink(url="https://x", headers={"Authorization": "OAuth t"},
                                                  expires_at=None))
        row = self.cache._conn.execute("SELECT headers FROM links").fetchone()
        self.assertNotIn("Authorization", json.loads(row["headers"]))

        provider.forget_link(file)
        self.assertIsNone(self.cache.get_link(provider._cache_key(file)))

    def test_old_cached_tokens_are_purged(self) -> None:
        self.cache.put_link("k", "https://x", {"Authorization": "Bearer t"}, None, None)
        self.cache.close()
        self.cache = MetadataCache(Path(self._tmp.name) / "cache.db")
        self.assertIsNone(self.cache.get_link("k"))


class TestCancel(unittest.IsolatedAsyncioTestCase):
    """Отменённая пользователем загрузка не должна воскресать после перезапуска."""

    async def test_cancelled_job_is_not_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            config = AppConfig()
            config.download_dir = tmp
            engine = DownloadEngine(config, None, None, None, mock.Mock())  # type: ignore[arg-type]
            file = CloudFile(provider=ProviderId.YANDEX, file_id="1", path="/a.mp4", name="a.mp4",
                             kind=EntryKind.FILE, size=100, source=SourceKind.PUBLIC)
            job = DownloadJob(file=file, dest_dir=dest)
            job.parts_dir.mkdir(parents=True, exist_ok=True)
            job.part_path.write_bytes(b"\0" * 100)
            save_state(job)

            restored = await engine.recover(dest)
            self.assertEqual(len(restored), 1)
            await engine.pause(restored[0].job_id)
            await engine.cancel(restored[0].job_id)
            await engine.stop()

            self.assertFalse(job.part_path.exists())
            self.assertFalse(job.state_path.exists())
            engine2 = DownloadEngine(config, None, None, None, mock.Mock())  # type: ignore[arg-type]
            self.assertEqual(await engine2.recover(dest), [])
            await engine2.stop()


class TestLiveLimits(unittest.IsolatedAsyncioTestCase):
    """Лимиты должны применяться сразу, без перезапуска программы."""

    async def test_limit_can_be_changed_on_the_fly(self) -> None:
        from app.downloader.engine import _Limit

        limit = _Limit(1)
        await limit.acquire()
        waiter = asyncio.create_task(limit.acquire())
        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done(), "второй должен ждать")
        await limit.set_limit(2)
        await asyncio.wait_for(waiter, timeout=2)
        await limit.release()
        await limit.release()

    async def test_engine_applies_new_file_limit(self) -> None:
        config = AppConfig()
        config.max_parallel_files = 2
        engine = DownloadEngine(config, None, None, None, mock.Mock())  # type: ignore[arg-type]
        config.max_parallel_files = 7
        await engine.apply_config()
        self.assertEqual(engine._file_limit._limit, 7)


class TestKeepPartialFiles(unittest.IsolatedAsyncioTestCase):
    """«Хранить недокачанные файлы» — настройка должна работать."""

    async def test_removing_job_keeps_partial_when_asked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            config = AppConfig()
            config.download_dir = tmp
            config.keep_partial_files = True
            engine = DownloadEngine(config, None, None, None, mock.Mock())  # type: ignore[arg-type]
            file = CloudFile(provider=ProviderId.YANDEX, file_id="1", path="/a.mp4", name="a.mp4",
                             kind=EntryKind.FILE, size=100, source=SourceKind.PUBLIC)
            job = DownloadJob(file=file, dest_dir=dest)
            job.parts_dir.mkdir(parents=True, exist_ok=True)
            job.part_path.write_bytes(b"\0" * 100)
            save_state(job)
            engine._jobs[job.job_id] = job
            engine._order.append(job.job_id)

            await engine.remove(job.job_id)
            self.assertTrue(job.part_path.exists(), "недокачанный файл должен остаться")

    async def test_cancelled_job_always_removes_partial(self) -> None:
        """Отмена пользователем — это отмена: файл убираем независимо от настройки."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            config = AppConfig()
            config.download_dir = tmp
            config.keep_partial_files = True
            engine = DownloadEngine(config, None, None, None, mock.Mock())  # type: ignore[arg-type]
            file = CloudFile(provider=ProviderId.YANDEX, file_id="1", path="/a.mp4", name="a.mp4",
                             kind=EntryKind.FILE, size=100, source=SourceKind.PUBLIC)
            job = DownloadJob(file=file, dest_dir=dest)
            job.parts_dir.mkdir(parents=True, exist_ok=True)
            job.part_path.write_bytes(b"\0" * 100)
            save_state(job)
            engine._jobs[job.job_id] = job
            engine._order.append(job.job_id)

            await engine.cancel(job.job_id)
            self.assertFalse(job.part_path.exists())
            self.assertFalse(job.state_path.exists())
            await engine.stop()


class TestDownloadOrder(unittest.IsolatedAsyncioTestCase):
    """Порядок загрузок должен совпадать с порядком в очереди.

    Раньше воркеров в пуле было больше, чем лимит файлов, и они забирали
    задания наперегонки — файл №3 мог стартовать раньше файла №2.
    """

    async def test_files_start_in_order(self) -> None:
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            config.download_dir = tmp
            config.max_parallel_files = 2
            engine = DownloadEngine(config, None, None, None, mock.Mock())  # type: ignore[arg-type]

            started: list[str] = []
            release = asyncio.Event()

            async def fake_run(job) -> None:
                started.append(job.file.name)
                await release.wait()

            engine._run_job = fake_run  # type: ignore[assignment]
            await engine.start()

            files = []
            for index in range(6):
                file = CloudFile(provider=ProviderId.YANDEX, file_id=str(index),
                                 path=f"/{index}.mp4", name=f"{index}.mp4",
                                 kind=EntryKind.FILE, size=100, source=SourceKind.PUBLIC)
                files.append(file)
            await engine.enqueue(files, Path(tmp))

            await asyncio.sleep(0.3)
            release.set()
            await asyncio.sleep(0.3)

            self.assertEqual(started, sorted(started), "файлы должны начинать по порядку")
            await engine.stop()


class TestProgressThrottle(unittest.TestCase):
    """Прогресс и сохранение состояния — на каждое задание, а не на движок.

    Раньше троттлинг был общий: при четырёх файлах только один получал
    обновление интерфейса, а состояние на диск писалось для одного из них.
    """

    def test_every_job_gets_its_own_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            bus = mock.Mock()
            engine = DownloadEngine(config, None, None, None, bus)  # type: ignore[arg-type]
            jobs = []
            for index in range(3):
                file = CloudFile(provider=ProviderId.YANDEX, file_id=str(index), path=f"/{index}.mp4",
                                 name=f"{index}.mp4", kind=EntryKind.FILE, size=100,
                                 source=SourceKind.PUBLIC)
                jobs.append(DownloadJob(file=file, dest_dir=Path(tmp)))

            for job in jobs:
                engine._on_progress(job)

            emitted = [call.args[0] for call in bus.job_progress.emit.call_args_list]
            self.assertEqual(len(emitted), 3, "каждое задание должно получить свой прогресс")
            self.assertEqual({job.job_id for job in emitted}, {job.job_id for job in jobs})

    def test_repeated_progress_of_one_job_is_throttled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            bus = mock.Mock()
            engine = DownloadEngine(config, None, None, None, bus)  # type: ignore[arg-type]
            file = CloudFile(provider=ProviderId.YANDEX, file_id="1", path="/a.mp4", name="a.mp4",
                             kind=EntryKind.FILE, size=100, source=SourceKind.PUBLIC)
            job = DownloadJob(file=file, dest_dir=Path(tmp))
            for _ in range(20):
                engine._on_progress(job)
            self.assertLessEqual(len(bus.job_progress.emit.call_args_list), 2)


class TestBrokenCache(unittest.TestCase):
    """Испорченный cache.db не должен мешать запуску программы."""

    def test_malformed_cache_is_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.db"
            cache = MetadataCache(path)
            cache.put_link("k", "https://x", {}, 1, None)
            cache.close()
            data = bytearray(path.read_bytes())
            data[100:4096] = b"\xff" * (4096 - 100)     # портим заголовок и схему
            path.write_bytes(bytes(data))

            cache = MetadataCache(path)
            try:
                self.assertIsNone(cache.get_link("k"), "кэш начат с чистого")
                cache.put_link("k2", "https://y", {}, 1, None)
                self.assertEqual(cache.get_link("k2")["url"], "https://y")
            finally:
                cache.close()
            self.assertTrue((Path(tmp) / "cache.db.broken").exists(), "испорченный файл отложен")

    def test_garbage_file_is_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.db"
            path.write_bytes(b"not a database at all" * 100)
            cache = MetadataCache(path)
            cache.put_link("k", "https://x", {}, 1, None)
            self.assertEqual(cache.get_link("k")["url"], "https://x")
            cache.close()


if __name__ == "__main__":
    unittest.main()
