"""Хранение OAuth-токенов.

Токены лежат в %APPDATA%\\svsCloud\\tokens.dat и на Windows
зашифрованы через DPAPI (привязка к пользователю Windows).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from .paths import app_data_dir

log = logging.getLogger(__name__)

TOKEN_FILE = app_data_dir() / "tokens.dat"

try:  # Windows-only
    import win32crypt  # type: ignore

    _HAS_DPAPI = True
except Exception:  # pragma: no cover - на других ОС
    win32crypt = None  # type: ignore
    _HAS_DPAPI = False


def _protect(data: bytes) -> bytes:
    if _HAS_DPAPI:
        return win32crypt.CryptProtectData(  # type: ignore[union-attr]
            data, "svsCloud", None, None, None, 0
        )
    return data


def _unprotect(data: bytes) -> bytes:
    if _HAS_DPAPI:
        blob, _desc = win32crypt.CryptUnprotectData(data, None, None, None, 0)  # type: ignore[union-attr]
        return blob
    return data


class TokenStore:
    """Простое хранилище «провайдер -> токены»."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or TOKEN_FILE
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = self._unprotect(self._path.read_bytes())
            self._data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            log.warning("Не удалось прочитать сохранённые токены: %s", exc)
            self._data = {}

    def _save(self) -> None:
        payload = json.dumps(self._data, ensure_ascii=False, indent=2).encode("utf-8")
        blob = self._protect(payload)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        tmp.replace(self._path)

    def get(self, provider: str) -> dict | None:
        return self._data.get(provider)

    def set(self, provider: str, payload: dict) -> None:
        self._data[provider] = payload
        self._save()

    def clear(self, provider: str) -> None:
        self._data.pop(provider, None)
        self._save()

    def all(self) -> dict[str, dict]:
        return dict(self._data)
