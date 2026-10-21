"""Ограничитель частоты запросов (token bucket) + «заморозка» после 429."""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Пропускает не более ``rate`` запросов в секунду с запасом ``burst``."""

    def __init__(self, rate: float, burst: float | None = None) -> None:
        self.rate = max(0.1, rate)
        self.capacity = burst if burst is not None else max(1.0, rate)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0

    def set_rate(self, rate: float) -> None:
        self.rate = max(0.1, rate)
        self.capacity = max(1.0, self.capacity, self.rate)

    async def acquire(self, amount: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                if now < self._cooldown_until:
                    wait = self._cooldown_until - now
                else:
                    self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                    self._updated = now
                    if self._tokens >= amount:
                        self._tokens -= amount
                        return
                    wait = (amount - self._tokens) / self.rate
            await asyncio.sleep(min(max(wait, 0.05), 5.0))

    def cooldown(self, seconds: float) -> None:
        """Замораживает выдачу токенов на ``seconds`` (после 429/503)."""
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)


class RateLimiter:
    """Набор бакетов: по одному на провайдера (+ отдельный для публичных ссылок)."""

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._buckets: dict[str, TokenBucket] = {}

    #: Скачивание частей файла: запросов много (каждая часть — запрос),
    #: но это не вызовы API, а чтение файла из хранилища.
    DOWNLOAD_RATE = 12.0

    def _rate_for(self, key: str) -> float:
        return max(self._rate, self.DOWNLOAD_RATE) if key.endswith("-download") else self._rate

    def bucket(self, key: str) -> TokenBucket:
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(self._rate_for(key))
        return self._buckets[key]

    async def acquire(self, key: str) -> None:
        await self.bucket(key).acquire()

    def cooldown(self, key: str, seconds: float) -> None:
        self.bucket(key).cooldown(seconds)

    def set_rate(self, rate: float) -> None:
        self._rate = rate
        for key, bucket in self._buckets.items():
            bucket.set_rate(self._rate_for(key))
