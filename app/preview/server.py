"""Локальный сервер превью.

Плеер (QtMultimedia, внутри — FFmpeg) получает файл не из облака напрямую,
а с этого сервера на 127.0.0.1: так ему не нужны ни токены облака,
ни их обновление, ни перевыпуск протухших ссылок — всё это делает сервер.

Сервер полностью поддерживает Range, поэтому плеер умеет перематывать,
а монтажёр не скачивает файл целиком.

* `/media/<token>`  — файл из облака (Range). Видео и звук идут через кэш
  потока (stream_cache): файл качается вперёд на полной скорости канала,
  а плеер читает уже скачанное — без «3 кадров в секунду» и долгих прыжков;
* `/proxy/<token>`  — локальная облегчённая копия, сделанная ffmpeg
  (запасной путь для форматов, которые плеер не открыл).
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from ..core.config import AppConfig
from ..core.models import CloudFile, DownloadLink
from ..net.http_client import HttpClient
from ..providers.registry import ProviderRegistry
from .ffmpeg import FfmpegTools
from .stream_cache import BufferState, CacheError, StreamCache, cache_key, evict

log = logging.getLogger(__name__)

# Что открывает плеер (QtMultimedia + FFmpeg). Проверено на реальных
# файлах: H.264/HEVC/ProRes/DNxHD/MPEG-4/VP9 в mp4/mov/mkv/avi/mts/mxf/webm.
VIDEO_EXT = {
    ".mp4", ".m4v", ".mov", ".qt", ".mkv", ".webm", ".avi", ".wmv", ".asf", ".flv",
    ".mts", ".m2ts", ".ts", ".mxf", ".mpg", ".mpeg", ".m2v", ".vob", ".3gp", ".3g2",
    ".ogv", ".dv", ".f4v", ".hevc", ".h264", ".264",
}
AUDIO_EXT = {
    ".mp3", ".m4a", ".aac", ".wav", ".wave", ".flac", ".ogg", ".oga", ".opus", ".wma",
    ".aif", ".aiff", ".aifc", ".ac3", ".eac3", ".amr", ".mka", ".caf", ".m4b",
}
IMAGE_EXT = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".ico", ".svg", ".tga", ".pbm", ".pgm", ".ppm", ".xbm", ".xpm", ".jp2", ".icns",
}

CHUNK = 256 * 1024
HEADER_TIMEOUT = 15.0
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")
# При этих ответах облака ссылка, скорее всего, протухла — перевыпускаем.
_STALE_LINK = {401, 403, 404, 410}
_REASONS = {
    200: "OK", 206: "Partial Content", 400: "Bad Request", 404: "Not Found",
    405: "Method Not Allowed", 416: "Range Not Satisfiable", 502: "Bad Gateway",
}


@dataclass
class PreviewSession:
    token: str
    file: CloudFile
    media_kind: str = "unknown"
    proxy_path: Optional[Path] = None
    cache: Optional[StreamCache] = None


class _RangeSource:
    """Открывает диапазоны файла в облаке для кэша потока.

    Помнит прямой адрес хранилища (после редиректа downloader -> storage):
    так каждый новый запрос стоит ~0.1 с вместо ~0.5 с. Если прямой адрес
    перестал работать — возвращается к обычной ссылке, протухшую ссылку
    перевыпускает.
    """

    def __init__(self, server: "PreviewServer", file: CloudFile) -> None:
        self._server = server
        self._file = file
        self._link: Optional[DownloadLink] = None
        self._direct_url = ""

    async def open(self, start: int, end: int) -> httpx.Response:
        file = self._file
        provider = self._server._registry.get(file.provider)
        response: httpx.Response | None = None
        for attempt in range(4):
            if response is not None:
                await response.aclose()
            if self._link is None or self._link.is_expired():
                self._link = await provider.resolve_download(file)
                self._direct_url = ""
            via_direct = bool(self._direct_url)
            headers = {**self._link.headers, "Range": f"bytes={start}-{end}"}
            response = await self._server._http.stream(
                "GET", self._direct_url or self._link.url,
                # Отдельный ограничитель: превью не должно тормозить загрузки и API.
                provider=f"{file.provider.value}-preview", headers=headers,
                # Повторы здесь лишние: ниже свои попытки с перевыпуском ссылки.
                retries=1,
            )
            status = response.status_code
            if via_direct and (status in _STALE_LINK or status >= 500):
                self._direct_url = ""          # прямой адрес устарел — через ссылку
                continue
            if status in _STALE_LINK and attempt < 3:
                forget = getattr(provider, "forget_link", None)
                if forget is not None:
                    forget(file)
                self._link = None
                log.info("Превью %s: облако ответило %d — перевыпускаем ссылку", file.name, status)
                continue
            # Прямой адрес запоминаем только без токена в заголовках: токен
            # нельзя слать на другой домен (httpx и сам его туда не отправляет).
            if response.history and status == 206 and not any(
                key.lower() == "authorization" for key in self._link.headers
            ):
                self._direct_url = str(response.url)
            return response
        assert response is not None
        return response


def media_kind(file: CloudFile) -> str:
    """video | audio | image | unknown — по расширению, затем по MIME."""
    ext = Path(file.name).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    mime = (file.mime or "").lower()
    for prefix, kind in (("image/", "image"), ("video/", "video"), ("audio/", "audio")):
        if mime.startswith(prefix):
            return kind
    return "unknown"


def parse_range(header: str | None, size: int) -> Optional[tuple[int, int]]:
    """Разбирает `Range: bytes=a-b` для файла размера size.

    Возвращает (start, end) включительно; None — заголовка нет или он
    не поддерживается (тогда отдаём файл целиком). ValueError — диапазон
    за пределами файла (ответ 416).
    """
    if not header:
        return None
    match = _RANGE_RE.fullmatch(header.strip())
    if not match:
        return None  # несколько диапазонов и т. п. — отдадим файл целиком
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:  # bytes=-N — последние N байт
        length = int(last)
        if length == 0:
            raise ValueError("пустой диапазон")
        return max(0, size - length), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        raise ValueError("диапазон за пределами файла")
    return start, min(end, size - 1)


class PreviewServer:
    """HTTP-сервер на 127.0.0.1, живущий в фоновом asyncio-потоке."""

    def __init__(
        self,
        registry: ProviderRegistry,
        http: HttpClient,
        config: AppConfig,
        ffmpeg: Optional[FfmpegTools] = None,
    ) -> None:
        self._registry = registry
        self._http = http
        self._config = config
        self._ffmpeg = ffmpeg or FfmpegTools(config)
        self._sessions: dict[str, PreviewSession] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._port = 0
        self._handlers: set[asyncio.Task] = set()   # открытые соединения
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Кэши потока по файлу: [кэш, источник, сколько сессий его держат].
        self._caches: dict[str, list] = {}

    # ------------------------------------------------------------------
    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def cache_dir(self) -> Path:
        """Папка кэша превью и облегчённых копий (выбирается в «Настройках»)."""
        path = self._config.preview_cache_path
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def close_caches(self) -> None:
        """Закрывает все кэши потока — перед очисткой или сменой папки кэша.

        Открытые файлы Windows удалить не даст, поэтому сначала закрываем.
        Сессии остаются: при следующем запросе кэш откроется заново.
        """
        for session in self._sessions.values():
            session.cache = None
        entries, self._caches = list(self._caches.values()), {}
        for cache, _source, _users in entries:
            await cache.close()

    async def start(self) -> int:
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self._port = int(self._server.sockets[0].getsockname()[1])
        log.info("Сервер превью запущен: %s", self.base_url)
        return self._port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            # С Python 3.12 wait_closed() ждёт, пока закроются ВСЕ соединения.
            # Плеер закрывает свои асинхронно (в своём потоке), поэтому без
            # явной отмены закрытие программы висело десятки секунд.
            handlers = [task for task in self._handlers if not task.done()]
            for task in handlers:
                task.cancel()
            if handlers:
                await asyncio.gather(*handlers, return_exceptions=True)
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=3)
            except Exception:
                pass
            self._server = None
        for token in list(self._sessions):
            self._drop_proxy(self._sessions.pop(token))
        for cache, _source, _users in list(self._caches.values()):
            await cache.close()
        self._caches.clear()

    # ------------------------------------------------------------------
    def add_session(self, file: CloudFile) -> PreviewSession:
        # Случайный токен: другие программы на этом компьютере не смогут
        # угадать адрес и вытянуть файл через наш прокси.
        token = secrets.token_urlsafe(18)
        session = PreviewSession(token=token, file=file, media_kind=media_kind(file))
        self._sessions[token] = session
        # Начинаем качать сразу, не дожидаясь первого запроса плеера.
        if self._cacheable(session) and self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._attach_cache, session)
        return session

    def get_session(self, token: str) -> Optional[PreviewSession]:
        return self._sessions.get(token)

    def drop_session(self, token: str) -> None:
        session = self._sessions.pop(token, None)
        if session is None:
            return
        self._drop_proxy(session)
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._detach_cache, session)

    @staticmethod
    def _drop_proxy(session: PreviewSession) -> None:
        if session.proxy_path:
            try:
                session.proxy_path.unlink(missing_ok=True)
            except OSError:
                pass

    def buffer_state(self, token: str, position: int) -> Optional[BufferState]:
        """Сколько скачано впереди позиции (байт) — для индикатора буферизации.

        Вызывается из GUI-потока; None — у этой сессии нет кэша потока.
        """
        session = self._sessions.get(token)
        cache = session.cache if session is not None else None
        if cache is None or cache.closed:
            return None
        return cache.buffer_state(position)

    # --- кэш потока (только в потоке event loop) ----------------------
    @staticmethod
    def _cacheable(session: PreviewSession) -> bool:
        return session.file.size > 0 and session.media_kind in ("video", "audio")

    def _attach_cache(self, session: PreviewSession) -> None:
        if session.cache is not None or session.token not in self._sessions:
            return
        file = session.file
        key = cache_key(file.provider.value, file.source.value, file.file_id, file.path,
                        file.size, file.md5 or "", file.modified_ts)
        entry = self._caches.get(key)
        if entry is None:
            budget = int(max(0.5, float(self._config.preview_cache_gb)) * 1024 ** 3)
            evict(self.cache_dir, keep=set(self._caches) | {key}, budget_bytes=budget)
            source = _RangeSource(self, file)
            cache = StreamCache(key, file.size, self.cache_dir, source.open, name=file.name)
            cache.open()
            entry = [cache, source, 0]
            self._caches[key] = entry
        entry[2] += 1
        session.cache = entry[0]

    def _detach_cache(self, session: PreviewSession) -> None:
        cache, session.cache = session.cache, None
        if cache is None:
            return
        entry = self._caches.get(cache.key)
        if entry is None:
            return
        entry[2] -= 1
        if entry[2] <= 0:
            del self._caches[cache.key]
            asyncio.ensure_future(cache.close())

    def media_url(self, token: str) -> str:
        return f"{self.base_url}/media/{token}"

    def proxy_url(self, token: str) -> str:
        return f"{self.base_url}/proxy/{token}"

    # ------------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=HEADER_TIMEOUT)
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, path = parts[0].upper(), parts[1]

            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=HEADER_TIMEOUT)
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()

            if method not in ("GET", "HEAD"):
                await self._send_status(writer, 405, "Метод не поддерживается")
                return
            route, _, rest = path.lstrip("/").partition("/")
            token = rest.split("?", 1)[0]
            session = self._sessions.get(token)
            if route not in ("media", "proxy") or session is None:
                await self._send_status(writer, 404, "Превью не найдено")
                return
            if route == "proxy":
                if session.proxy_path is None or not session.proxy_path.exists():
                    await self._send_status(writer, 404, "Облегчённая копия ещё не готова")
                    return
                await self._send_local_file(writer, session.proxy_path, method,
                                            headers.get("range"), "video/mp4")
                return
            if session.cache is None and self._cacheable(session):
                self._attach_cache(session)
            if session.cache is not None and not session.cache.closed:
                await self._send_cached(writer, session, headers, method)
            else:
                await self._send_media(writer, session, headers, method)
        except (ConnectionError, asyncio.IncompleteReadError):
            # Плеер закрыл соединение (перемотка, смена файла) — это норма.
            pass
        except asyncio.TimeoutError:
            pass
        except Exception:
            log.warning("Ошибка сервера превью", exc_info=True)
        finally:
            if task is not None:
                self._handlers.discard(task)
            try:
                writer.close()
                # Ждём закрытия недолго: при отмене (stop) клиент может уже не ответить.
                await asyncio.wait_for(writer.wait_closed(), timeout=1)
            except BaseException:
                pass

    # ------------------------------------------------------------------
    async def _send_status(self, writer: asyncio.StreamWriter, status: int, text: str,
                           extra: dict[str, str] | None = None) -> None:
        body = text.encode("utf-8")
        head = {"Content-Type": "text/plain; charset=utf-8", "Content-Length": str(len(body))}
        head.update(extra or {})
        self._write_head(writer, status, head)
        writer.write(body)
        await writer.drain()

    @staticmethod
    def _write_head(writer: asyncio.StreamWriter, status: int, headers: dict[str, str]) -> None:
        lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}"]
        lines += [f"{key}: {value}" for key, value in headers.items()]
        lines += ["Connection: close", "", ""]
        writer.write("\r\n".join(lines).encode("latin-1"))

    async def _open_upstream(self, session: PreviewSession,
                             range_header: str | None) -> httpx.Response:
        """Открывает поток из облака; протухшую ссылку перевыпускает один раз."""
        file = session.file
        provider = self._registry.get(file.provider)
        response: httpx.Response | None = None
        for attempt in range(2):
            if attempt:
                forget = getattr(provider, "forget_link", None)
                if forget is not None:
                    forget(file)
            link = await provider.resolve_download(file)
            upstream_headers = dict(link.headers)
            if range_header:
                upstream_headers["Range"] = range_header
            # HEAD облака часто не поддерживают — всегда спрашиваем GET,
            # а для HEAD просто не передаём тело.
            response = await self._http.stream(
                "GET", link.url, provider=file.provider.value, headers=upstream_headers,
                # Повторы здесь лишние: выше уже есть свои попытки с перевыпуском.
                retries=1,
            )
            if response.status_code not in _STALE_LINK or attempt:
                return response
            log.info("Превью %s: облако ответило %d — перевыпускаем ссылку",
                     file.name, response.status_code)
            await response.aclose()
        assert response is not None
        return response

    async def _send_media(self, writer: asyncio.StreamWriter, session: PreviewSession,
                          headers: dict[str, str], method: str) -> None:
        file = session.file
        range_header = headers.get("range")
        try:
            response = await self._open_upstream(session, range_header)
        except Exception as exc:
            log.warning("Превью %s: не удалось открыть файл в облаке: %s", file.name, exc)
            await self._send_status(writer, 502, "Не удалось получить файл из облака")
            return

        try:
            status = response.status_code
            if status == 416:
                extra = {}
                if "content-range" in response.headers:
                    extra["Content-Range"] = response.headers["content-range"]
                await self._send_status(writer, 416, "Диапазон за пределами файла", extra)
                return
            if status not in (200, 206):
                body = (await response.aread())[:300]
                log.warning("Превью %s: облако ответило %d: %s", file.name, status,
                            body.decode("utf-8", "replace"))
                await self._send_status(writer, 502, f"Облако ответило ошибкой {status}")
                return

            out = {
                "Content-Type": response.headers.get("content-type") or file.mime
                or "application/octet-stream",
                "Accept-Ranges": "bytes",
            }
            if status == 206 and "content-range" in response.headers:
                out["Content-Range"] = response.headers["content-range"]
            length = response.headers.get("content-length")
            if length:
                out["Content-Length"] = length
            self._write_head(writer, status, out)
            if method != "HEAD":
                async for chunk in response.aiter_bytes(CHUNK):
                    writer.write(chunk)
                    await writer.drain()
            await writer.drain()
        finally:
            await response.aclose()

    async def _send_cached(self, writer: asyncio.StreamWriter, session: PreviewSession,
                           headers: dict[str, str], method: str) -> None:
        """Отдаёт файл из кэша потока; недостающее кэш докачивает на лету."""
        cache = session.cache
        assert cache is not None
        size = cache.size
        try:
            rng = parse_range(headers.get("range"), size)
        except ValueError:
            await self._send_status(writer, 416, "Диапазон за пределами файла",
                                    {"Content-Range": f"bytes */{size}"})
            return
        start, end = rng if rng else (0, size - 1)
        # Первый кусок получаем ДО ответа: если облако отказало, плеер должен
        # получить ошибку (502), а не «200 OK» с оборванным телом.
        reader = cache.new_reader()   # этот запрос теперь ведёт загрузку
        try:
            first = await cache.read(start, min(end - start + 1, 1024 * 1024), reader)
        except CacheError as exc:
            log.warning("Превью %s: %s", session.file.name, exc)
            await self._send_status(writer, 502, "Не удалось получить файл из облака")
            return
        content_type = (session.file.mime or mimetypes.guess_type(session.file.name)[0]
                        or "application/octet-stream")
        out = {
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        }
        if rng:
            out["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._write_head(writer, 206 if rng else 200, out)
        if method == "HEAD":
            await writer.drain()
            return
        writer.write(first)
        await writer.drain()
        pos = start + len(first)
        try:
            while pos <= end:
                data = await cache.read(pos, min(end - pos + 1, 1024 * 1024), reader)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
                pos += len(data)
        except CacheError as exc:
            log.warning("Превью %s: %s", session.file.name, exc)

    async def _send_local_file(self, writer: asyncio.StreamWriter, path: Path, method: str,
                               range_header: str | None, content_type: str) -> None:
        size = path.stat().st_size
        try:
            rng = parse_range(range_header, size)
        except ValueError:
            await self._send_status(writer, 416, "Диапазон за пределами файла",
                                    {"Content-Range": f"bytes */{size}"})
            return
        start, end = rng if rng else (0, size - 1)
        out = {
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Content-Length": str(max(0, end - start + 1)),
        }
        if rng:
            out["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._write_head(writer, 206 if rng else 200, out)
        if method != "HEAD":
            left = end - start + 1
            with open(path, "rb") as handle:
                handle.seek(start)
                while left > 0:
                    chunk = handle.read(min(CHUNK, left))
                    if not chunk:
                        break
                    writer.write(chunk)
                    await writer.drain()
                    left -= len(chunk)
        await writer.drain()
