"""Кэш метаданных и прямых ссылок в SQLite.

Зачем:
* не перелистывать заново дерево папки при каждом открытии;
* не тратить запрос на получение ссылки, если она ещё жива;
* помнить file_id, чтобы ссылку на файл открыть без листинга.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..core.paths import app_data_dir

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    key         TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    source      TEXT NOT NULL,
    path        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    modified_ts REAL DEFAULT 0,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_lookup ON entries (provider, source, path);

CREATE TABLE IF NOT EXISTS links (
    key         TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    headers     TEXT NOT NULL,
    size        INTEGER,
    expires_at  REAL,
    updated_at  REAL NOT NULL
);
"""


class MetadataCache:
    """Потокобезопасный кэш. Работает и из GUI, и из asyncio-потока."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (app_data_dir() / "cache.db")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            # Старые версии сохраняли сюда заголовок Authorization открытым текстом.
            self._conn.execute("DELETE FROM links WHERE headers LIKE '%uthorization%'")
            self._conn.commit()

    # ------------------------------------------------------------------
    @staticmethod
    def _entry_key(provider: str, source: str, path: str) -> str:
        return f"{provider}|{source}|{path}"

    def put_entry(self, provider: str, source: str, path: str, payload: dict[str, Any], modified_ts: float = 0.0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO entries (key, provider, source, path, payload, modified_ts, updated_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
                "modified_ts=excluded.modified_ts, updated_at=excluded.updated_at",
                (self._entry_key(provider, source, path), provider, source, path,
                 json.dumps(payload, ensure_ascii=False), modified_ts, time.time()),
            )
            self._conn.commit()

    def get_entry(self, provider: str, source: str, path: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM entries WHERE key = ?", (self._entry_key(provider, source, path),)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    def clear(self, provider: str | None = None) -> None:
        with self._lock:
            if provider:
                self._conn.execute("DELETE FROM entries WHERE provider = ?", (provider,))
            else:
                self._conn.execute("DELETE FROM entries")
            self._conn.execute("DELETE FROM links")
            self._conn.commit()

    # ------------------------------------------------------------------
    def put_link(self, key: str, url: str, headers: dict[str, str], size: int | None,
                 expires_at: float | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO links (key, url, headers, size, expires_at, updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET url=excluded.url, headers=excluded.headers, "
                "size=excluded.size, expires_at=excluded.expires_at, updated_at=excluded.updated_at",
                (key, url, json.dumps(headers, ensure_ascii=False), size, expires_at, time.time()),
            )
            self._conn.commit()

    def get_link(self, key: str, max_age: float = 900.0) -> dict[str, Any] | None:
        """Возвращает ссылку, если она ещё не истекла."""
        with self._lock:
            row = self._conn.execute(
                "SELECT url, headers, size, expires_at, updated_at FROM links WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        expires_at = row["expires_at"]
        if expires_at is not None and time.time() + 60 >= expires_at:
            return None
        if time.time() - row["updated_at"] > max_age:
            return None
        try:
            headers = json.loads(row["headers"])
        except json.JSONDecodeError:
            headers = {}
        return {"url": row["url"], "headers": headers, "size": row["size"], "expires_at": expires_at}

    def drop_link(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM links WHERE key = ?", (key,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
