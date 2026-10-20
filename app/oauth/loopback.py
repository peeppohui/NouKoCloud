"""Локальный сервер для OAuth-редиректа (desktop flow Google).

Слушает 127.0.0.1 на случайном порту, ловит ?code=... и отдаёт
пользователю страницу «Готово, вернитесь в приложение».
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>svsCloud</title>
<style>
 body{{font-family:Segoe UI,sans-serif;background:#f5f6f8;margin:0;display:flex;
      align-items:center;justify-content:center;height:100vh;color:#202124}}
 .card{{background:#fff;padding:40px 56px;border-radius:16px;text-align:center;
        box-shadow:0 8px 30px rgba(0,0,0,.12)}}
 h1{{font-size:22px;margin:0 0 12px}} p{{margin:0;color:#5f6368}}
</style></head><body><div class="card"><h1>{title}</h1><p>{text}</p></div>
</body></html>"""


def _query_of(request_line: bytes) -> str:
    """Путь из строки запроса: «GET /?code=1 HTTP/1.1» → «/?code=1»."""
    if not request_line:
        return ""
    parts = request_line.decode("latin-1", "ignore").split(" ")
    if len(parts) < 2:
        return ""
    return parts[1]


class LoopbackRedirectServer:
    """Одноразовый сервер: поймал код — остановился."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._host = host
        self._server: asyncio.AbstractServer | None = None
        self._future: asyncio.Future[str] | None = None
        self._port = 0

    @property
    def port(self) -> int:
        # Порт запоминаем при старте: redirect_uri нужен и ПОСЛЕ остановки
        # сервера — при обмене кода на токен (Google сверяет его с тем, что
        # был в запросе авторизации). Раньше здесь падало «Сервер не запущен».
        if not self._port:
            raise RuntimeError("Сервер не запущен")
        return self._port

    @property
    def redirect_uri(self) -> str:
        return f"http://{self._host}:{self.port}"

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        self._server = await asyncio.start_server(self._handle, self._host, 0)
        self._port = int(self._server.sockets[0].getsockname()[1])

    async def wait_for_code(self, timeout: float = 300.0) -> str:
        if self._future is None:
            raise RuntimeError("Сервер не запущен")
        return await asyncio.wait_for(self._future, timeout=timeout)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        if self._future is not None and not self._future.done():
            self._future.cancel()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        code = await self._respond(reader, writer)
        if code and self._future is not None and not self._future.done():
            self._future.set_result(code)

    async def _respond(self, reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter) -> str | None:
        """Показывает страницу браузеру и возвращает код авторизации (если есть)."""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            # Остальные заголовки дочитываем, чтобы клиент не получил RST.
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if line in (b"\r\n", b"\n", b""):
                    break
            query = _query_of(request_line)
            params = parse_qs(urlparse(query).query)
            code = (params.get("code") or [None])[0]
            error = (params.get("error") or [None])[0]

            if error:
                title, text = "Не удалось войти", f"Облако ответило ошибкой: {error}"
            elif code:
                title, text = "Готово", "Вход выполнен. Можно возвращаться в приложению."
            else:
                title, text = "Пустая страница", "Ожидался код авторизации."

            body = _PAGE.format(title=title, text=text).encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
            return code
        except Exception as exc:  # pragma: no cover
            # Мусорный запрос или обрыв — это не повод ронять обработчик.
            log.debug("Ошибка loopback-сервера: %s", exc)
            return None
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
