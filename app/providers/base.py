"""Общий интерфейс облачных провайдеров."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from ..core.config import AppConfig
from ..core.models import CloudFile, DownloadLink, MediaInfo, ProviderId, SourceKind
from ..net.http_client import HttpClient
from ..net.metadata_cache import MetadataCache
from ..oauth.manager import OAuthManager

log = logging.getLogger(__name__)


@dataclass
class BrowseContext:
    """Что именно мы открываем."""

    source: SourceKind = SourceKind.OAUTH
    public_key: Optional[str] = None      # Яндекс: ключ публичной ссылки
    drive_id: Optional[str] = None       # Google: общий диск
    root_path: str = "/"                 # Яндекс: путь внутри диска
    display_name: str = ""


class CloudProvider(ABC):
    id: ProviderId
    display_name: str

    def __init__(
        self,
        http: HttpClient,
        oauth: OAuthManager,
        cache: MetadataCache,
        config: AppConfig,
    ) -> None:
        self.http = http
        self.oauth = oauth
        self.cache = cache
        self.config = config

    # ------------------------------------------------------------------
    @abstractmethod
    async def list_children(self, ctx: BrowseContext, parent: Optional[CloudFile]) -> list[CloudFile]:
        """Содержимое одной папки (без рекурсии)."""

    @abstractmethod
    async def resolve_download(self, file: CloudFile) -> DownloadLink:
        """Прямая ссылка на файл + заголовки."""

    @abstractmethod
    async def probe_media(self, file: CloudFile) -> MediaInfo:
        """Метаданные для превью."""

    async def list_tree(self, ctx: BrowseContext, parent: Optional[CloudFile] = None) -> AsyncIterator[CloudFile]:
        """Рекурсивный обход папки. parent=None — от корня."""
        folders: list[CloudFile] = []
        if parent is None:
            children = await self.list_children(ctx, None)
            for child in children:
                yield child
            folders = [child for child in children if not child.is_file]
        else:
            folders = [parent]
        while folders:
            folder = folders.pop()
            for child in await self.list_children(ctx, folder):
                yield child
                if not child.is_file:
                    folders.append(child)

    # ------------------------------------------------------------------
    def _cache_key(self, file: CloudFile) -> str:
        return f"{self.id.value}|{file.source.value}|{file.path}|{file.file_id}"

    async def cached_link(self, file: CloudFile) -> Optional[DownloadLink]:
        data = self.cache.get_link(self._cache_key(file), max_age=600)
        if not data:
            return None
        return DownloadLink(
            url=data["url"],
            headers=data.get("headers") or {},
            size=data.get("size"),
            expires_at=data.get("expires_at"),
        )

    def remember_link(self, file: CloudFile, link: DownloadLink) -> None:
        self.cache.put_link(
            self._cache_key(file), link.url, link.headers, link.size, link.expires_at
        )
