"""Локальный посредник для ffmpeg: один поток наружу -> много соединений.

Фрагмент видео (с минуты по минуту) вырезает ffmpeg: он сам ищет нужное
место в файле и читает только его. Но читает в ОДНО соединение, а YouTube
режет каждое соединение до ~70 КБ/с. Поэтому ffmpeg получает адрес
http://127.0.0.1:…/N, а посредник отдаёт запрошенный диапазон, скачивая его
частями по PIECE во много соединений с опережением (окно WINDOW частей).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger(__name__)

PIECE = 512 * 1024
WINDOW = 32
RETRIES = 4

#: fetch(url, headers, start, end) -> bytes (end включительно)
Fetcher = Callable[[str, dict, int, int], bytes]


class _Source:
    def __init__(self, url: str, headers: dict, size: int) -> None:
        self.url = url
        self.headers = headers
        self.size = size


class RangeProxy:
    def __init__(self, fetch: Fetcher, piece: int = PIECE, window: int = WINDOW) -> None:
        self._fetch = fetch
        self.piece = piece
        self.window = window
        self._sources: dict[str, _Source] = {}
        self._closed = threading.Event()
        self.served = 0                      # отдано байт (для прогресса)
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args) -> None:  # тишина в консоли
                log.debug("посредник: " + fmt, *args)

            def do_HEAD(self) -> None:  # noqa: N802
                proxy._handle(self, body=False)

            def do_GET(self) -> None:  # noqa: N802
                proxy._handle(self, body=True)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="range-proxy", daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def add(self, url: str, headers: dict, size: int) -> str:
        key = str(len(self._sources) + 1)
        self._sources[key] = _Source(url, dict(headers), size)
        return f"http://127.0.0.1:{self.port}/{key}"

    def close(self) -> None:
        self._closed.set()
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _handle(self, req: BaseHTTPRequestHandler, body: bool) -> None:
        source = self._sources.get(req.path.strip("/").split("?")[0])
        if source is None:
            req.send_error(404)
            return
        start, end = 0, source.size - 1
        ranged = False
        header = req.headers.get("Range", "")
        if header.startswith("bytes="):
            first, _, last = header[6:].split(",")[0].partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else source.size - 1
                elif last:                       # bytes=-N — последние N байт
                    start = max(0, source.size - int(last))
                ranged = True
            except ValueError:
                pass
        end = min(end, source.size - 1)
        if start >= source.size or start > end:
            req.send_response(416)
            req.send_header("Content-Range", f"bytes */{source.size}")
            req.send_header("Content-Length", "0")
            req.end_headers()
            return
        log.debug("Посредник: %s %s байты %d–%d из %d", req.command, req.path, start, end, source.size)
        req.send_response(206 if ranged else 200)
        req.send_header("Content-Type", "application/octet-stream")
        req.send_header("Accept-Ranges", "bytes")
        req.send_header("Content-Length", str(end - start + 1))
        if ranged:
            req.send_header("Content-Range", f"bytes {start}-{end}/{source.size}")
        req.end_headers()
        if body:
            self._stream(req, source, start, end)

    def _stream(self, req: BaseHTTPRequestHandler, source: _Source, start: int, end: int) -> None:
        offsets = iter(range(start, end + 1, self.piece))
        pending: deque[Future] = deque()
        pool = ThreadPoolExecutor(self.window, thread_name_prefix="range-piece")
        # Окно растёт постепенно: ffmpeg часто читает пару сотен КБ (заголовок,
        # оглавление) и переходит в другое место — не качаем зря десятки МБ.
        window = [2]

        def schedule() -> None:
            while len(pending) < window[0]:
                offset = next(offsets, None)
                if offset is None:
                    return
                pending.append(pool.submit(self._get, source, offset, min(offset + self.piece - 1, end)))

        try:
            schedule()
            while pending and not self._closed.is_set():
                data = pending.popleft().result()
                window[0] = min(self.window, window[0] * 2)
                schedule()
                req.wfile.write(data)
                self.served += len(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                             # ffmpeg перешёл к другому месту файла
        except BaseException as exc:         # и отмена загрузки (Cancelled)
            if isinstance(exc, Exception):
                log.warning("Посредник: часть не скачана: %s", exc)
            req.close_connection = True
        finally:
            for future in pending:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

    def _get(self, source: _Source, start: int, end: int) -> bytes:
        error: Exception | None = None
        for _ in range(RETRIES):
            if self._closed.is_set():
                raise ConnectionAbortedError("посредник закрыт")
            try:
                data = self._fetch(source.url, source.headers, start, end)
                if len(data) == end - start + 1:
                    return data
                error = IOError(f"получено {len(data)} байт вместо {end - start + 1}")
            except Exception as exc:
                error = exc
        raise error or IOError("часть не скачана")
