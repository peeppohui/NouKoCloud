"""Провайдер Google Диска.

Особенности API:
* `files.list` — постраничный (до 1000 на страницу), 10 000 запросов
  на 100 секунд на проект; при превышении — 403 с reason
  `rateLimitExceeded` / `userRateLimitExceeded`;
* скачивание бинарников — `files.get?alt=media` (держит Range);
* Google Документы/Таблицы/Презентации скачиваются только через
  `files.export` (и не всё: экспорт в PDF ограничен ~10 МБ);
* публичная папка читается через `api key` (без входа в аккаунт),
  общий диск — через `corpora=drive&driveId=...`.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlencode

from ..core.models import CloudFile, DownloadLink, EntryKind, MediaInfo, ProviderId, SourceKind
from .base import BrowseContext, CloudProvider

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
# В Drive API v3 длительность видео — durationMillis. С несуществующим полем
# (было durationMs) Google отвечает 400 на весь список, и «Мой диск» не открывался.
FIELDS = (
    "nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum,parents,"
    "thumbnailLink,webContentLink,shortcutDetails,driveId,resourceKey,"
    "capabilities(canDownload),videoMediaMetadata(durationMillis,height,width))"
)
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_APP_MIME_PREFIX = "application/vnd.google-apps."

EXPORT_MIME = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),   # не application/png — Google ответит 400
}


class GoogleProvider(CloudProvider):
    id = ProviderId.GOOGLE
    display_name = "Google Диск"

    # ------------------------------------------------------------------
    async def _headers(self, ctx: BrowseContext) -> dict[str, str]:
        """Для своего аккаунта — токен, для публичной ссылки — api key."""
        if ctx.source is SourceKind.PUBLIC:
            key = self.config.google_api_key
            return {}
        token = await self.oauth.access_token(ProviderId.GOOGLE)
        return {"Authorization": f"Bearer {token}"}

    def _extra_params(self, ctx: BrowseContext) -> dict:
        if ctx.source is SourceKind.PUBLIC:
            key = self.config.google_api_key
            return {"key": key} if key else {}
        return {}

    # ------------------------------------------------------------------
    async def list_children(self, ctx: BrowseContext, parent: Optional[CloudFile]) -> list[CloudFile]:
        parent_id = parent.file_id if parent is not None else "root"
        if parent is not None and parent.drive_id:
            ctx = BrowseContext(
                source=ctx.source,
                public_key=ctx.public_key,
                drive_id=parent.drive_id,
                root_path=ctx.root_path,
                display_name=ctx.display_name,
            )

        q = f"'{parent_id}' in parents and trashed = false"
        params: dict = {
            "q": q,
            "fields": FIELDS,
            "pageSize": 1000,
            "orderBy": "folder,name",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        params.update(self._extra_params(ctx))
        if ctx.drive_id:
            params["corpora"] = "drive"
            params["driveId"] = ctx.drive_id

        headers = await self._headers(ctx)
        collected: list[CloudFile] = []
        page_token = ""
        # Путь от корня открытой папки: по нему загрузчик воспроизводит
        # структуру папок, и одноимённые файлы из разных папок не сталкиваются.
        parent_path = (parent.path if parent is not None else "").strip("/")

        while True:
            if page_token:
                params["pageToken"] = page_token
            response = await self.http.request(
                "GET", f"{API}/files", provider="google", params=params, headers=headers
            )
            payload = response.json()
            for raw in payload.get("files", []):
                child = self._parse_item(raw, ctx, parent_path)
                if child is not None:
                    collected.append(child)
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return collected

    def _parse_item(self, raw: dict, ctx: BrowseContext, parent_path: str = "") -> Optional[CloudFile]:
        file_id = raw.get("id")
        name = raw.get("name")
        if not file_id or not name:
            return None
        mime = raw.get("mimeType", "")
        is_folder = mime == FOLDER_MIME

        # Ярлык ведёт к другому файлу — сразу берём цель.
        shortcut = raw.get("shortcutDetails") or {}
        target_id = shortcut.get("targetId")
        if mime == SHORTCUT_MIME and target_id:
            file_id = target_id

        video = raw.get("videoMediaMetadata") or {}
        modified = raw.get("modifiedTime")
        modified_dt = None
        if modified:
            from datetime import datetime, timezone

            try:
                modified_dt = datetime.fromisoformat(modified.replace("Z", "+00:00"))
                if modified_dt.tzinfo is None:
                    modified_dt = modified_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                modified_dt = None

        return CloudFile(
            provider=ProviderId.GOOGLE,
            file_id=file_id,
            path=f"{parent_path}/{name}" if parent_path else name,
            name=name,
            kind=EntryKind.FOLDER if is_folder else EntryKind.FILE,
            size=int(raw.get("size") or 0),
            mime=mime,
            modified=modified_dt,
            md5=raw.get("md5Checksum"),
            source=ctx.source,
            drive_id=raw.get("driveId") or ctx.drive_id,
            resource_key=raw.get("resourceKey"),
            google_mime=mime,
            thumbnail_link=raw.get("thumbnailLink"),
            preview_hint=raw.get("thumbnailLink"),
            media_duration_ms=int(video.get("durationMillis")) if video.get("durationMillis") else None,
            media_width=video.get("width"),
            media_height=video.get("height"),
            can_download=bool((raw.get("capabilities") or {}).get("canDownload", True)),
        )

    # ------------------------------------------------------------------
    async def list_drives(self) -> list[dict]:
        """Общие диски (нужны, чтобы зайти внутрь shared drive)."""
        if not self.config.google_api_key and not self.config.google_client_id:
            return []
        params = {"fields": "drives(id,name)", "pageSize": 100}
        if self.config.google_api_key:
            params["key"] = self.config.google_api_key
        try:
            response = await self.http.request("GET", f"{API}/drives", provider="google", params=params)
            return response.json().get("drives", [])
        except Exception as exc:
            log.info("Список общих дисков недоступен: %s", exc)
            return []

    # ------------------------------------------------------------------
    async def resolve_download(self, file: CloudFile) -> DownloadLink:
        # Ссылка собирается локально, без запроса к API, поэтому кэш не нужен:
        # иначе вместе со ссылкой пришлось бы хранить и (протухающий) токен.
        params: dict = {"supportsAllDrives": "true"}
        if file.source is SourceKind.PUBLIC:
            if self.config.google_api_key:
                params["key"] = self.config.google_api_key
            headers: dict[str, str] = {}
        else:
            headers = {"Authorization": f"Bearer {await self.oauth.access_token(ProviderId.GOOGLE)}"}
        if file.resource_key:
            # Drive v3 принимает ключ ресурса только заголовком.
            headers["X-Goog-Drive-Resource-Keys"] = f"{file.file_id}/{file.resource_key}"

        google_mime = file.google_mime or file.mime or ""
        if google_mime.startswith(GOOGLE_APP_MIME_PREFIX):
            export_mime, ext = EXPORT_MIME.get(google_mime, ("application/pdf", ".pdf"))
            params["mimeType"] = export_mime
            url = f"{API}/files/{file.file_id}/export?{urlencode(params)}"
            link = DownloadLink(
                url=url,
                headers={**headers, "Accept-Encoding": "identity"},
                method="GET",
                size=None,
                expires_at=None,
                content_type=export_mime,
            )
        else:
            params["alt"] = "media"
            url = f"{API}/files/{file.file_id}?{urlencode(params)}"
            link = DownloadLink(
                url=url,
                headers={**headers, "Accept-Encoding": "identity"},
                method="GET",
                size=file.size or None,
                expires_at=None,
                content_type=file.mime or None,
            )
        return link

    # ------------------------------------------------------------------
    async def probe_media(self, file: CloudFile) -> MediaInfo:
        mime = (file.google_mime or file.mime or "").lower()
        name = file.name.lower()
        if mime.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif")):
            return MediaInfo(
                kind="image",
                width=file.media_width,
                height=file.media_height,
                poster_url=file.thumbnail_link,
                transcodable=False,
            )
        if mime.startswith("video/") or name.endswith((".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mts")):
            duration = file.media_duration_ms / 1000.0 if file.media_duration_ms else None
            return MediaInfo(
                kind="video",
                duration=duration,
                width=file.media_width,
                height=file.media_height,
                poster_url=file.thumbnail_link,
                transcodable=True,
            )
        if mime.startswith("audio/") or name.endswith((".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus")):
            duration = file.media_duration_ms / 1000.0 if file.media_duration_ms else None
            return MediaInfo(kind="audio", duration=duration, transcodable=True)
        return MediaInfo(kind="unknown", transcodable=True)
