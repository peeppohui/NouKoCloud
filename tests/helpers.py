"""Общие приспособления для тестов."""

from __future__ import annotations

import hashlib
import http.server
import os
import socket
import threading


class RangeHandler(http.server.BaseHTTPRequestHandler):
    """Отдаёт файл и умеет Range — как Яндекс и Google."""

    payload = b""
    served_bytes = 0
    hit_count = 0
    fail_after: int | None = None   # оборвать соединение после N байт

    def log_message(self, *args):  # noqa: D102 - глушим вывод
        pass

    def do_GET(self):  # noqa: N802
        RangeHandler.hit_count += 1
        data = self.payload
        total = len(data)
        range_header = self.headers.get("Range")
        start, end = 0, total - 1
        status = 200
        if range_header and range_header.startswith("bytes="):
            spec = range_header.split("=", 1)[1]
            first, _, last = spec.partition("-")
            if first:
                start = int(first)
            if last:
                end = int(last)
            end = min(end, total - 1)
            status = 206
        chunk = data[start : end + 1]
        RangeHandler.served_bytes += len(chunk)

        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()

        limit = self.fail_after
        if limit is not None and limit < len(chunk):
            try:
                self.wfile.write(chunk[:limit])
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True
            return
        self.wfile.write(chunk)


def start_range_server(payload: bytes):
    """Поднимает локальный сервер с поддержкой Range."""
    RangeHandler.payload = payload
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return RangeHandler, thread, server


def stop_server(server) -> None:
    server.shutdown()
    server.server_close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
