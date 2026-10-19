"""Сборка всех компонентов приложения в один объект."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .core.async_runtime import AsyncRuntime
from .core.bus import Bus
from .core.config import AppConfig
from .core.secrets_store import TokenStore
from .downloader.engine import DownloadEngine
from .net.http_client import HttpClient
from .net.metadata_cache import MetadataCache
from .net.ratelimit import RateLimiter
from .oauth.manager import OAuthManager
from .preview.ffmpeg import FfmpegTools
from .preview.server import PreviewServer
from .providers.registry import ProviderRegistry

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    config: AppConfig
    bus: Bus
    runtime: AsyncRuntime
    limiter: RateLimiter
    http: HttpClient
    cache: MetadataCache
    token_store: TokenStore
    oauth: OAuthManager
    registry: ProviderRegistry
    engine: DownloadEngine
    preview: PreviewServer
    ffmpeg: FfmpegTools

    async def startup(self) -> None:
        self.runtime.start()
        await self.preview.start()
        await self.engine.start()
        log.info("Приложение запущено. Сервер превью: %s", self.preview.base_url)

    async def shutdown(self) -> None:
        await self.engine.stop()
        await self.preview.stop()
        await self.http.aclose()
        self.cache.close()
        # Сам event loop останавливаем из GUI-потока (см. AsyncRuntime.stop):
        # из потока loop нельзя join'ить сам себя.


def build_context(config: AppConfig) -> AppContext:
    bus = Bus()
    runtime = AsyncRuntime()
    limiter = RateLimiter(config.requests_per_second)
    http = HttpClient(config, limiter)
    cache = MetadataCache()
    token_store = TokenStore()
    oauth = OAuthManager(config, token_store, bus)
    registry = ProviderRegistry(config, http, oauth, cache)
    engine = DownloadEngine(config, http, registry, cache, bus)
    ffmpeg = FfmpegTools(config)
    preview = PreviewServer(registry, http, config, ffmpeg)
    return AppContext(
        config=config,
        bus=bus,
        runtime=runtime,
        limiter=limiter,
        http=http,
        cache=cache,
        token_store=token_store,
        oauth=oauth,
        registry=registry,
        engine=engine,
        preview=preview,
        ffmpeg=ffmpeg,
    )
