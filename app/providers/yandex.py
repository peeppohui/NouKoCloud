"""Провайдер Яндекс.Диска.

Особенности API (сверено с https://yandex.ru/dev/disk-api/doc/en/):
* один и тот же ресурс отдаётся через `/resources` (своё) и
  `/public/resources` (публичная ссылка);
* список детей лежит в `_embedded.items` (а не в корне ответа),
  там же `limit`, `offset` и `total`;
* тип папки — `"dir"` (не `"folder"`);
* путь своего диска передаётся со схемой: `disk:/Папка/файл`;
  публичные пути — относительные, без схемы;
* `public_key` принимает и полный ключ, и публичную ссылку целиком —
  короткий идентификатор из ссылки (`/d/XXXX`) API не понимает;
* ссылка на скачивание (`href`) живёт ~30 минут — её надо перевыпускать;
* публичные ссылки ограничены по трафику и часто отвечают 429/503.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..core.errors import NotFoundError
from ..core.models import CloudFile, DownloadLink, EntryKind, MediaInfo, ProviderId, SourceKind
from .base import BrowseContext, CloudProvider

log = logging.getLogger(__name__)

API = "https://cloud-api.yandex.net/v1/disk"
PAGE_LIMIT = 1000
LINK_TTL = 25 * 60      # href живёт ~30 минут, берём с запасом
MAX_PAGES = 50          # предохранитель от бесконечного листинга
DISK_SCHEME = "disk:"


def _parse_dt(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def strip_scheme(path: str) -> str:
    """Убирает префикс «disk:/» — для показа и для структуры папок."""
    if path.startswith(DISK_SCHEME):
        path = path[len(DISK_SCHEME):]
    if not path.startswith("/"):
        path = "/" + path
    return path


def disk_path(path: str) -> str:
    """Готовит путь своего диска для запроса: `disk:/Папка/файл`."""
    if not path:
        return "disk:/"
    if path.startswith(DISK_SCHEME):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return DISK_SCHEME + path


def unpack_listing(payload: dict[str, Any]) -> tuple[list[dict], int | None, int, int]:
    """Достаёт детей, total, limit и offset.

    Актуальная версия API кладёт всё в `_embedded`, но встречается и
    корневой вариант — поддерживаем оба.
    """
    embedded = payload.get("_embedded") or {}
    items = embedded.get("items")
    if items is None:
        items = payload.get("items")
    if not isinstance(items, list):
        items = []
    total = embedded.get("total", payload.get("total"))
    limit = embedded.get("limit", payload.get("limit")) or PAGE_LIMIT
    offset = embedded.get("offset", payload.get("offset")) or 0
    return items, (int(total) if total is not None else None), int(limit), int(offset)


class YandexProvider(CloudProvider):
    id = ProviderId.YANDEX
    display_name = "Яндекс.Диск"

    #: Яндекс ответил «ничего нет» (404) на публичную ссылку, входа нет.
    LINK_NOT_FOUND = (
        "Яндекс отвечает, что по этой ссылке ничего нет. Так бывает, если ссылку "
        "удалили или закрыли, если владелец открыл папку только определённым людям "
        "или если ссылка скопирована не полностью. Откройте её в браузере в режиме "
        "инкогнито: если и там «Ничего не найдено» — попросите у владельца новую "
        "ссылку с доступом «Все, у кого есть ссылка». Если доступ дали лично вам — "
        "войдите в Яндекс («Аккаунты») и откройте ссылку ещё раз."
    )
    #: То же, но запрос уже повторяли от имени аккаунта пользователя.
    LINK_NOT_FOUND_LOGGED_IN = (
        "Яндекс отвечает, что по этой ссылке ничего нет — даже для вашего аккаунта. "
        "Ссылку удалили или закрыли, либо она скопирована не полностью. Проверьте её "
        "в браузере в режиме инкогнито и попросите у владельца новую ссылку "
        "с доступом «Все, у кого есть ссылка»."
    )

    async def _public_get(self, url: str, params: dict) -> httpx.Response:
        """Запрос к публичному ресурсу.

        Сначала без входа (так работает любая обычная ссылка). Если Яндекс
        ответил 404, а пользователь вошёл в Яндекс, — повторяем от его имени:
        так открываются папки, доступ к которым владелец выдал лично ему.
        404 после повтора — NotFoundError с понятным объяснением.
        """
        try:
            return await self.http.request("GET", url, provider="yandex", params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            if not self.oauth.is_logged_in(ProviderId.YANDEX):
                raise NotFoundError(
                    self.LINK_NOT_FOUND,
                    technical=f"404 от Яндекса без входа для {params.get('public_key')}",
                ) from exc
        log.info("Ссылка не открылась без входа — пробуем от имени аккаунта Яндекса")
        try:
            return await self.http.request(
                "GET", url, provider="yandex", params=params, headers=await self._auth_headers()
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise NotFoundError(
                    self.LINK_NOT_FOUND_LOGGED_IN,
                    technical=f"404 от Яндекса и с входом для {params.get('public_key')}",
                ) from exc
            raise

    # ------------------------------------------------------------------
    async def _auth_headers(self) -> dict[str, str]:
        """Заголовок авторизации для «своего» диска."""
        token = await self.oauth.access_token(ProviderId.YANDEX)
        return {"Authorization": f"OAuth {token}"}

    # ------------------------------------------------------------------
    async def list_children(self, ctx: BrowseContext, parent: Optional[CloudFile]) -> list[CloudFile]:
        path = parent.path if parent is not None else ctx.root_path
        if not path.startswith("/"):
            path = "/" + path

        if ctx.source is SourceKind.PUBLIC:
            url = f"{API}/public/resources"
            base_params: dict = {"public_key": ctx.public_key or ""}
            headers: dict[str, str] = {}
            # Корень ссылки — без пути; ссылка на подпапку (…?path=/Папка) — с путём.
            request_path: Optional[str] = None if parent is None and path == "/" else path
        else:
            url = f"{API}/resources"
            base_params = {}
            headers = await self._auth_headers()
            request_path = disk_path(path)

        params: dict = {**base_params, "limit": PAGE_LIMIT, "offset": 0}
        if request_path is not None:
            params["path"] = request_path

        collected: list[CloudFile] = []
        seen_paths: set[str] = set()

        for page_no in range(MAX_PAGES):
            if ctx.source is SourceKind.PUBLIC:
                response = await self._public_get(url, params)
            else:
                response = await self.http.request(
                    "GET", url, provider="yandex", params=params, headers=headers
                )
            payload = response.json()
            if ctx.source is SourceKind.PUBLIC and parent is None and page_no == 0 \
                    and payload.get("name"):
                # Настоящее название папки по ссылке — для раздела «Яндекс.Диск».
                ctx.display_name = str(payload["name"])
            page, total, limit, offset = unpack_listing(payload)
            if not page and not payload.get("_embedded"):
                # Ссылка ведёт сразу на файл, а не на папку: отдаём его как
                # единственный элемент списка.
                single = self._parse_item(payload, ctx)
                if single is not None and single.is_file:
                    return [single]
            for raw in page:
                child = self._parse_item(raw, ctx)
                if child is None or child.path in seen_paths:
                    continue
                seen_paths.add(child.path)
                collected.append(child)

            next_offset = offset + len(page)
            if not page:
                break
            if total is not None and next_offset >= total:
                break
            if total is None and len(page) < limit:
                break
            params["offset"] = next_offset

        return collected

    def _parse_item(self, raw: dict, ctx: BrowseContext) -> Optional[CloudFile]:
        name = raw.get("name")
        if not name:
            return None
        item_type = raw.get("type")
        kind = EntryKind.FOLDER if item_type in ("dir", "folder") else EntryKind.FILE
        raw_path = raw.get("path") or ""
        if raw_path:
            path = strip_scheme(raw_path)
        elif raw.get("type") in ("file", "dir"):
            path = "/"
        else:
            path = f"{ctx.root_path.rstrip('/')}/{name}"
            if not path.startswith("/"):
                path = "/" + path
        return CloudFile(
            provider=ProviderId.YANDEX,
            file_id=str(raw.get("resource_id") or raw.get("id") or raw.get("public_key") or path),
            path=path,
            name=name,
            kind=kind,
            size=int(raw.get("size") or 0),
            mime=str(raw.get("mime_type") or ""),
            modified=_parse_dt(raw.get("modified")),
            md5=raw.get("md5"),
            source=ctx.source,
            public_key=ctx.public_key,
            preview_hint=raw.get("preview"),
        )

    # ------------------------------------------------------------------
    async def resolve_download(self, file: CloudFile) -> DownloadLink:
        cached = await self.cached_link(file)
        if cached is not None and not cached.is_expired():
            return cached

        if file.source is SourceKind.PUBLIC:
            url = f"{API}/public/resources/download"
            params = {"public_key": file.public_key or "", "path": file.path}
            # Без входа, а при 404 — от имени аккаунта (доступ, выданный лично).
            response = await self._public_get(url, params)
        else:
            url = f"{API}/resources/download"
            params = {"path": disk_path(file.path)}
            try:
                response = await self.http.request(
                    "GET", url, provider="yandex", params=params,
                    headers=await self._auth_headers(),
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise NotFoundError() from exc
                raise
        payload = response.json()
        href = payload.get("href")
        if not href:
            raise NotFoundError("Яндекс не дал ссылку на файл. Обновите список и попробуйте снова.")

        link = DownloadLink(
            url=href,
            headers={"Accept-Encoding": "identity"},
            method=payload.get("method", "GET"),
            size=file.size or None,
            expires_at=time.time() + LINK_TTL,
        )
        self.remember_link(file, link)
        return link

    # ------------------------------------------------------------------
    async def probe_media(self, file: CloudFile) -> MediaInfo:
        mime = (file.mime or "").lower()
        if file.name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff")):
            return MediaInfo(kind="image", poster_url=file.preview_hint, transcodable=False)
        if mime.startswith("video/") or file.name.lower().endswith(
            (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv", ".flv", ".mts", ".m2ts")
        ):
            return MediaInfo(kind="video", poster_url=file.preview_hint, transcodable=True)
        if mime.startswith("audio/") or file.name.lower().endswith(
            (".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma")
        ):
            return MediaInfo(kind="audio", transcodable=True)
        return MediaInfo(kind="unknown", transcodable=True)
