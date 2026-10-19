"""Локальный сервер превью.

Отдаёт медиа из облака в браузер (QtWebEngine) с поддержкой Range —
поэтому плеер умеет перематывать, а монтажёр не скачивает файл целиком.
Дополнительно отдаёт «облегчённую» копию для форматов, которые
браузер не играет (mkv, avi, prores).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..core.config import AppConfig
from ..core.models import CloudFile
from ..net.http_client import HttpClient
from ..providers.registry import ProviderRegistry
from .ffmpeg import FfmpegTools

log = logging.getLogger(__name__)

BROWSER_VIDEO = {".mp4", ".m4v", ".webm", ".mov", ".ogv"}
BROWSER_AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac"}
BROWSER_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".svg"}


@dataclass
class PreviewSession:
    token: str
    file: CloudFile
    media_kind: str = "unknown"
    poster_ready: bool = False
    proxy_path: Optional[Path] = None


def _guess_kind(file: CloudFile) -> str:
    ext = Path(file.name).suffix.lower()
    if ext in BROWSER_IMAGE:
        return "image"
    if ext in BROWSER_VIDEO or (file.mime or "").startswith("video/"):
        return "video"
    if ext in BROWSER_AUDIO or (file.mime or "").startswith("audio/"):
        return "audio"
    return "unknown"


def browser_can_play(file: CloudFile) -> bool:
    ext = Path(file.name).suffix.lower()
    return ext in BROWSER_VIDEO or ext in BROWSER_AUDIO or ext in BROWSER_IMAGE


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
        self._cache_dir = Path(config.download_dir_path) / ".svscloud-preview"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def start(self) -> int:
        loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self._port = int(self._server.sockets[0].getsockname()[1])
        log.info("Сервер превью запущен: %s", self.base_url)
        return self._port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    # ------------------------------------------------------------------
    def add_session(self, file: CloudFile) -> PreviewSession:
        token = f"{file.provider.value}-{abs(hash(file.file_id + file.path)) % (10 ** 10):010d}"
        session = PreviewSession(token=token, file=file, media_kind=_guess_kind(file))
        self._sessions[token] = session
        return session

    def get_session(self, token: str) -> Optional[PreviewSession]:
        return self._sessions.get(token)

    def drop_session(self, token: str) -> None:
        session = self._sessions.pop(token, None)
        if session and session.proxy_path:
            try:
                session.proxy_path.unlink(missing_ok=True)
            except OSError:
                pass

    def player_url(self, token: str) -> str:
        return f"{self.base_url}/player/{token}"

    # ------------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=15)
            if not request_line:
                return
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1]

            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=15)
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()

            if path.startswith("/player/"):
                token = path.split("/player/", 1)[1].split("?")[0]
                await self._send_player(writer, token)
            elif path.startswith("/media/"):
                token = path.split("/media/", 1)[1].split("?")[0]
                await self._send_media(writer, token, headers, method)
            elif path.startswith("/proxy/"):
                token = path.split("/proxy/", 1)[1].split("?")[0]
                await self._send_local(writer, token, method)
            else:
                await self._send_text(writer, 404, "Не найдено")
        except Exception as exc:
            log.debug("Ошибка сервера превью: %s", exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    async def _send_text(self, writer: asyncio.StreamWriter, status: int, text: str,
                         content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        reason = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        )
        writer.write(body)
        await writer.drain()

    async def _send_player(self, writer: asyncio.StreamWriter, token: str) -> None:
        session = self._sessions.get(token)
        if session is None:
            await self._send_text(writer, 404, "Превью не найдено")
            return
        kind = session.media_kind
        if kind == "image":
            tag = f'<img src="/media/{token}" style="max-width:100%;max-height:100%">'
        elif kind == "audio":
            autoplay = " autoplay" if self._config.preview_autoplay else ""
            tag = f'<audio src="/media/{token}" controls{autoplay} style="width:100%"></audio>'
        elif kind == "video":
            autoplay = " autoplay" if self._config.preview_autoplay else ""
            poster = ""
            if session.file.preview_hint:
                poster = f' poster="{session.file.preview_hint}"'
            tag = (
                f'<video src="/media/{token}"{poster} controls{autoplay} '
                'style="max-width:100%;max-height:100%;background:#000"></video>'
            )
        else:
            tag = (
                '<p style="color:#aaa">Превью этого формата недоступно. '
                'Скачайте файл, чтобы посмотреть его.</p>'
            )
        html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>
 body{{margin:0;background:#111;display:flex;align-items:center;justify-content:center;
      height:100vh;font-family:Segoe UI,sans-serif}}
</style></head><body>{tag}</body></html>"""
        body = html.encode("utf-8")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()

    async def _send_media(self, writer: asyncio.StreamWriter, token: str,
                          headers: dict[str, str], method: str) -> None:
        session = self._sessions.get(token)
        if session is None:
            await self._send_text(writer, 404, "Файл не найден")
            return
        file = session.file

        # Локальная облегчённая копия (mkv/avi -> mp4)
        if session.proxy_path and session.proxy_path.exists():
            await self._send_local_file(writer, session.proxy_path, method,
                                        content_type="video/mp4")
            return

        try:
            provider = self._registry.get(file.provider)
            link = await provider.resolve_download(file)
        except Exception as exc:
            log.warning("Превью недоступно: %s", exc)
            await self._send_text(writer, 502, "Не удалось получить файл из облака")
            return

        upstream_headers = dict(link.headers)
        range_header = headers.get("range")
        if range_header:
            upstream_headers["Range"] = range_header

        try:
            response = await self._http.stream(
                "GET", link.url, provider=file.provider.value, headers=upstream_headers
            )
        except Exception as exc:
            log.warning("Поток превью не открылся: %s", exc)
            await self._send_text(writer, 502, "Нет связи с облаком")
            return

        try:
            content_type = response.headers.get("content-type") or file.mime or "application/octet-stream"
            out_headers = {
                "Content-Type": content_type,
                "Accept-Ranges": "bytes",
            }
            if response.status_code == 206 and "content-range" in response.headers:
                out_headers["Content-Range"] = response.headers["content-range"]
            length = response.headers.get("content-length")
            if length:
                out_headers["Content-Length"] = length
            status = 206 if response.status_code == 206 else 200
            reason = "Partial Content" if status == 206 else "OK"
            head = f"HTTP/1.1 {status} {reason}\r\n" + "".join(
                f"{k}: {v}\r\n" for k, v in out_headers.items()
            ) + "Connection: close\r\n\r\n"
            writer.write(head.encode("latin-1"))
            if method != "HEAD":
                try:
                    async for chunk in response.aiter_bytes(256 * 1024):
                        writer.write(chunk)
                        await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    pass
            await writer.drain()
        finally:
            await response.aclose()

    async def _send_local(self, writer: asyncio.StreamWriter, token: str, method: str) -> None:
        session = self._sessions.get(token)
        if session is None or not session.proxy_path:
            await self._send_text(writer, 404, "Прокси не найден")
            return
        await self._send_local_file(writer, session.proxy_path, method, content_type="video/mp4")

    async def _send_local_file(self, writer: asyncio.StreamWriter, path: Path,
                               method: str, content_type: str) -> None:
        size = path.stat().st_size
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
            f"Content-Length: {size}\r\nAccept-Ranges: bytes\r\nConnection: close\r\n\r\n".encode()
        )
        if method != "HEAD":
            try:
                with open(path, "rb") as handle:
                    while True:
                        chunk = handle.read(256 * 1024)
                        if not chunk:
                            break
                        writer.write(chunk)
                        await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
        await writer.drain()
