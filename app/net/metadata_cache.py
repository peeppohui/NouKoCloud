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

#: Версия схемы кэша (PRAGMA user_version). Растёт при изменении таблиц.
SCHEMA_VERSION = 2
#: Сколько показывать листинг без повторного запроса к облаку.
ENTRY_TTL = 300.0
#: Пустой ответ кэшируем ненадолго: папку могли только что наполнить.
EMPTY_TTL = 30.0
#: Пределы размера кэша: старые записи удаляются.
MAX_ENTRIES = 10_000
MAX_BYTES = 50 * 1024 * 1024


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
CREATE INDEX IF NOT EXISTS idx_entries_updated ON entries (updated_at);

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
        try:
            self._conn = self._open()
        except sqlite3.DatabaseError as exc:
            # Файл испорчен (обрыв питания, процесс убит во время записи).
            # Это только кэш — откладываем его в сторону и начинаем с чистого,
            # иначе программа не запустится вовсе.
            log.warning("Кэш %s повреждён (%s) — создаём заново", self._path, exc)
            self._set_aside()
            self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            if row is None or row[0] != "ok":
                raise sqlite3.DatabaseError(f"quick_check: {row[0] if row else '?'}")
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            # Миграция схемы: PRAGMA user_version хранится в самом файле и
            # переживает перезапуск. Все шаги идемпотентны (IF NOT EXISTS).
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                conn.executescript(SCHEMA)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            # Старые версии сохраняли сюда заголовок Authorization открытым текстом.
            conn.execute("DELETE FROM links WHERE headers LIKE '%uthorization%'")
            conn.commit()
        except sqlite3.DatabaseError:
            conn.close()
            raise
        return conn

    def _set_aside(self) -> None:
        """Переименовывает испорченный кэш в cache.db.broken (старый такой — заменяется)."""
        for suffix in ("", "-wal", "-shm"):
            src = Path(f"{self._path}{suffix}")
            if not src.exists():
                continue
            dst = Path(f"{self._path}.broken{suffix}")
            try:
                src.replace(dst)
            except OSError:
                src.unlink(missing_ok=True)

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
            self._prune()

    def get_entry(self, provider: str, source: str, path: str) -> dict[str, Any] | None:
        """Сырая запись раздела (для проверок и отладки)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM entries WHERE key = ?",
                (self._entry_key(provider, source, path),),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def put_listing(self, provider: str, source: str, path: str,
                    files: list, modified_ts: float | None = None) -> None:
        """Кладёт листинг папки. Файлы пишутся плоским JSON (CloudFile.to_dict)."""
        payload = {"files": [f.to_dict() for f in files]}
        if modified_ts is None:
            modified_ts = max((f.modified_ts for f in files), default=0.0)
        self.put_entry(provider, source, path, payload, modified_ts)

    def get_listing(self, provider: str, source: str, path: str):
        """(list[CloudFile], updated_at) или None. Битые записи считаются промахом."""
        from ..core.models import CloudFile

        with self._lock:
            row = self._conn.execute(
                "SELECT payload, updated_at FROM entries WHERE key = ?",
                (self._entry_key(provider, source, path),),
            ).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(row["payload"])
            files = [CloudFile.from_dict(item) for item in raw.get("files", [])]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        return files, float(row["updated_at"])

    @staticmethod
    def listing_is_fresh(files: list, updated_at: float, now: float | None = None) -> bool:
        """Свежесть листинга: пустой ответ живёт 30 секунд, обычный — 5 минут."""
        ttl = EMPTY_TTL if not files else ENTRY_TTL
        return ((now if now is not None else time.time()) - updated_at) <= ttl

    def drop_listing(self, provider: str, source: str, path: str) -> None:
        """Инвалидирует запись: ручное «Обновить» должно идти в сеть."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM entries WHERE key = ?", (self._entry_key(provider, source, path),)
            )
            self._conn.commit()

    def clear(self, provider: str | None = None) -> None:
        """Чистит метаданные и ссылки.

        С указанием провайдера трогаем только его записи: выход из Яндекса
        не должен сбрасывать живые ссылки Google. Полная очистка (provider
        is None) по-прежнему удаляет всё.
        """
        with self._lock:
            if provider:
                self._conn.execute("DELETE FROM entries WHERE provider = ?", (provider,))
                # Ключ ссылки: provider|source|path|file_id (см. providers/base.py).
                # LIKE с экранированием не нужен: provider — это 'yandex'/'google'
                # без символов шаблона, а префикс отделён вертикальной чертой.
                self._conn.execute(
                    "DELETE FROM links WHERE key LIKE ?", (f"{provider}|%",)
                )
            else:
                self._conn.execute("DELETE FROM entries")
                self._conn.execute("DELETE FROM links")
            self._conn.commit()

    def clear_metadata(self, provider: str | None = None) -> int:
        """Удаляет только листинги (кэш метаданных), не трогая ссылки."""
        with self._lock:
            if provider:
                cur = self._conn.execute("DELETE FROM entries WHERE provider = ?", (provider,))
            else:
                cur = self._conn.execute("DELETE FROM entries")
            self._conn.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def _prune(self) -> None:
        """Держит кэш в пределах MAX_ENTRIES и MAX_BYTES: старые — первыми."""
        count = self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        if count > MAX_ENTRIES:
            self._conn.execute(
                "DELETE FROM entries WHERE key IN "
                "(SELECT key FROM entries ORDER BY updated_at LIMIT ?)",
                (count - MAX_ENTRIES,),
            )
        try:
            size = self._path.stat().st_size if self._path.exists() else 0
        except OSError:
            size = 0
        if size > MAX_BYTES:
            # Освобождаем пятую часть — не гоняем VACUUM на каждую запись.
            drop = max(1, count // 5)
            self._conn.execute(
                "DELETE FROM entries WHERE key IN "
                "(SELECT key FROM entries ORDER BY updated_at LIMIT ?)",
                (drop,),
            )
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
