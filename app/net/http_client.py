"""Асинхронный HTTP-клиент: один на всё приложение.

Отвечает за общие вещи: лимит частоты запросов, повторы с backoff,
User-Agent, прокси. Провайдеры добавляют только свои заголовки и пути.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import httpx

from ..core.config import AppConfig
from ..core.errors import TokenExpiredError
from .backoff import RetryPolicy, compute_delay, parse_retry_after
from .ratelimit import RateLimiter

log = logging.getLogger(__name__)

DEFAULT_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=16)


class HttpClient:
    def __init__(self, config: AppConfig, limiter: RateLimiter) -> None:
        self._config = config
        self._limiter = limiter
        timeout = httpx.Timeout(
            read=config.read_timeout,
            connect=config.connect_timeout,
            write=config.read_timeout,
            pool=config.connect_timeout,
        )
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            limits=DEFAULT_LIMITS,
            headers={"User-Agent": config.user_agent, "Accept-Encoding": "identity"},
            proxy=config.proxy or None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    @property
    def raw(self) -> httpx.AsyncClient:
        return self._client

    async def request(
        self,
        method: str,
        url: str,
        *,
        provider: str = "app",
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        content: Any = None,
        timeout: httpx.Timeout | float | None = None,
        retries: int | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        """Запрос с учётом лимитов и повторов. 401 превращается в TokenExpiredError."""
        attempts = retries if retries is not None else self._config.max_retries
        policy = RetryPolicy(max_attempts=attempts, base_delay=self._config.retry_base_delay,
                             max_delay=self._config.retry_max_delay)
        last_exc: Exception | None = None

        for attempt in range(1, policy.max_attempts + 1):
            await self._limiter.acquire(provider)
            try:
                response = await self._client.request(
                    method, url, headers=headers, params=params, json=json,
                    data=data, content=content, timeout=timeout,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt >= policy.max_attempts:
                    raise
                delay = compute_delay(attempt, None, policy)
                log.warning("Сеть: повтор %d/%d через %.1f с (%s)", attempt, policy.max_attempts, delay, exc)
                await _sleep(delay)
                continue

            if response.status_code == 401:
                raise TokenExpiredError(technical=f"401 от {provider}: {url}")

            if response.status_code in (429, 503):
                retry_after = parse_retry_after(response)
                delay = compute_delay(attempt, retry_after, policy)
                self._limiter.cooldown(provider, delay)
                if attempt >= policy.max_attempts:
                    if raise_for_status:
                        response.raise_for_status()
                    return response
                log.warning("%s: ответ %d, ждём %.1f с", provider, response.status_code, delay)
                await response.aclose()
                await _sleep(delay)
                continue

            if raise_for_status:
                response.raise_for_status()
            return response

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Неизвестный сбой HTTP-клиента")

    # ------------------------------------------------------------------
    async def stream(
        self,
        method: str,
        url: str,
        *,
        provider: str = "app",
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.Response:
        """Открывает поток без повторов — докачкой занимается загрузчик."""
        await self._limiter.acquire(provider)
        request = self._client.build_request(method, url, headers=headers, params=params, timeout=timeout)
        return await self._client.send(request, stream=True)


async def _sleep(delay: float) -> None:
    import asyncio

    await asyncio.sleep(delay)
