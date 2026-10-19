"""Разбор ссылки, вставленной пользователем."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

from ..core.errors import BadLinkError
from ..core.models import ProviderId, SourceKind

log = logging.getLogger(__name__)

_YANDEX_HOSTS = ("disk.yandex.ru", "disk.yandex.com", "yadi.sk", "yandex.ru")
_YANDEX_KEY_RE = re.compile(r"/(?:d|i)/([A-Za-z0-9_\-+=]+)")
_GOOGLE_FILE_RE = re.compile(r"/file/d/([A-Za-z0-9_\-]+)")
_GOOGLE_DOC_RE = re.compile(r"/d/([A-Za-z0-9_\-]+)")
_GOOGLE_FOLDER_RE = re.compile(r"/folders/([A-Za-z0-9_\-]+)")


@dataclass
class ParsedLink:
    provider: ProviderId
    source: SourceKind
    label: str = ""
    public_key: str | None = None     # Яндекс: ключ публичной ссылки
    path: str = "/"                   # Яндекс: подпапка внутри ссылки
    file_id: str | None = None        # Google: id файла/папки
    drive_id: str | None = None       # Google: общий диск
    is_folder_hint: bool | None = None


def parse_link(url: str) -> ParsedLink:
    """Определяет облако и идентификатор по ссылке."""
    text = (url or "").strip()
    if not text:
        raise BadLinkError("Вставьте ссылку на папку или файл.")
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text

    parsed = urlparse(text)
    host = parsed.netloc.lower()
    query = parse_qs(parsed.query)

    if "drive.google.com" in host or "docs.google.com" in host:
        return _parse_google(parsed, query)
    if any(h in host for h in _YANDEX_HOSTS):
        return _parse_yandex(parsed, query)

    raise BadLinkError(
        "Поддерживаются ссылки Яндекс.Диска и Google Диска. "
        "Проверьте, что ссылка скопирована целиком."
    )


def _parse_yandex(parsed, query) -> ParsedLink:
    path = parsed.path
    match = _YANDEX_KEY_RE.search(path)
    if not match:
        raise BadLinkError("Не удалось найти ключ в ссылке Яндекс.Диска.")
    key = match.group(1)
    subpath = "/"
    raw_path = (query.get("path") or [""])[0]
    if raw_path:
        subpath = unquote(raw_path)
        if not subpath.startswith("/"):
            subpath = "/" + subpath
    # API принимает и полный ключ, и публичную ссылку целиком.
    # Короткий идентификатор из ссылки (/d/XXXX) API не понимает —
    # отдаём канонический URL.
    public_url = f"https://disk.yandex.ru/d/{key}"
    return ParsedLink(
        provider=ProviderId.YANDEX,
        source=SourceKind.PUBLIC,
        label=f"Яндекс.Диск · {key[:12]}",
        public_key=public_url,
        path=subpath,
    )


def _parse_google(parsed, query) -> ParsedLink:
    path = parsed.path
    file_id = None
    is_folder = None
    for regex, folder in ((_GOOGLE_FOLDER_RE, True), (_GOOGLE_FILE_RE, False), (_GOOGLE_DOC_RE, False)):
        match = regex.search(path)
        if match:
            file_id = match.group(1)
            is_folder = folder
            break
    if not file_id:
        file_id = (query.get("id") or [None])[0]
    if not file_id:
        raise BadLinkError("Не удалось найти файл в ссылке Google Диска.")

    drive_id = (query.get("driveid") or [None])[0]
    return ParsedLink(
        provider=ProviderId.GOOGLE,
        source=SourceKind.PUBLIC,
        label=f"Google Диск · {file_id[:8]}…",
        file_id=file_id,
        drive_id=drive_id,
        is_folder_hint=is_folder,
    )
