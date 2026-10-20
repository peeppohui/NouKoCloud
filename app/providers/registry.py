"""Реестр провайдеров: создаёт экземпляры и открывает ссылки."""

from __future__ import annotations

import logging

from ..core.config import AppConfig
from ..core.errors import NotFoundError
from ..core.models import CloudFile, EntryKind, ProviderId, SourceKind
from ..net.http_client import HttpClient
from ..net.metadata_cache import MetadataCache
from ..oauth.manager import OAuthManager
from .base import BrowseContext, CloudProvider
from .google import GoogleProvider
from .link_router import ParsedLink, parse_link
from .yandex import YandexProvider

log = logging.getLogger(__name__)


class ProviderRegistry:
    def __init__(
        self,
        config: AppConfig,
        http: HttpClient,
        oauth: OAuthManager,
        cache: MetadataCache,
    ) -> None:
        self._config = config
        self._http = http
        self._oauth = oauth
        self._cache = cache
        self._providers: dict[ProviderId, CloudProvider] = {
            ProviderId.YANDEX: YandexProvider(http, oauth, cache, config),
            ProviderId.GOOGLE: GoogleProvider(http, oauth, cache, config),
        }

    def get(self, pid: ProviderId) -> CloudProvider:
        return self._providers[pid]

    @property
    def yandex(self) -> YandexProvider:
        return self._providers[ProviderId.YANDEX]  # type: ignore[return-value]

    @property
    def google(self) -> GoogleProvider:
        return self._providers[ProviderId.GOOGLE]  # type: ignore[return-value]

    # ------------------------------------------------------------------
    async def open_link(self, url: str) -> tuple[CloudProvider, BrowseContext, list[CloudFile]]:
        """Открывает ссылку и возвращает провайдера, контекст и корневые элементы."""
        parsed: ParsedLink = parse_link(url)
        provider = self._providers[parsed.provider]

        if parsed.provider is ProviderId.YANDEX:
            ctx = BrowseContext(
                source=SourceKind.PUBLIC,
                public_key=parsed.public_key,
                root_path=parsed.path,
                display_name=parsed.label,
            )
            items = await provider.list_children(ctx, None)
            return provider, ctx, items

        # Google: уточняем, папка это или файл, и ищем общий диск.
        file_id = parsed.file_id or ""
        meta = await self._google_metadata(file_id)
        mime = meta.get("mimeType", "")
        is_folder = mime == "application/vnd.google-apps.folder"
        drive_id = meta.get("driveId") or parsed.drive_id

        ctx = BrowseContext(
            source=SourceKind.PUBLIC,
            drive_id=drive_id,
            root_path="/",
            display_name=parsed.label,
        )
        if is_folder:
            parent = CloudFile(
                provider=ProviderId.GOOGLE,
                file_id=file_id,
                path="",
                name=meta.get("name") or "Папка",
                kind=EntryKind.FOLDER,
                source=SourceKind.PUBLIC,
                drive_id=drive_id,
                resource_key=meta.get("resourceKey"),
            )
            items = await provider.list_children(ctx, parent)
            return provider, ctx, items

        single = self._google_item(file_id, meta, ctx)
        return provider, ctx, [single]

    async def _google_metadata(self, file_id: str) -> dict:
        params = {"fields": "id,name,mimeType,driveId,resourceKey,size,modifiedTime,md5Checksum,capabilities(canDownload)", "supportsAllDrives": "true"}
        headers: dict[str, str] = {}
        if self._config.google_api_key:
            params["key"] = self._config.google_api_key
        elif self._oauth.is_logged_in(ProviderId.GOOGLE):
            headers = {"Authorization": f"Bearer {await self._oauth.access_token(ProviderId.GOOGLE)}"}
        else:
            raise NotFoundError(
                "Чтобы открыть ссылку Google, войдите в аккаунт или добавьте API-ключ в настройках."
            )
        response = await self._http.request(
            "GET", f"https://www.googleapis.com/drive/v3/files/{file_id}",
            provider="google", params=params, headers=headers,
        )
        return response.json()

    def _google_item(self, file_id: str, meta: dict, ctx: BrowseContext) -> CloudFile:
        from datetime import datetime, timezone

        modified = meta.get("modifiedTime")
        modified_dt = None
        if modified:
            try:
                modified_dt = datetime.fromisoformat(modified.replace("Z", "+00:00"))
                if modified_dt.tzinfo is None:
                    modified_dt = modified_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                modified_dt = None
        return CloudFile(
            provider=ProviderId.GOOGLE,
            file_id=file_id,
            path=meta.get("name", file_id),
            name=meta.get("name", file_id),
            kind=EntryKind.FILE,
            size=int(meta.get("size") or 0),
            mime=meta.get("mimeType", ""),
            modified=modified_dt,
            md5=meta.get("md5Checksum"),
            source=ctx.source,
            drive_id=meta.get("driveId"),
            resource_key=meta.get("resourceKey"),
            google_mime=meta.get("mimeType"),
            can_download=bool((meta.get("capabilities") or {}).get("canDownload", True)),
        )
