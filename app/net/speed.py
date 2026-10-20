"""Ограничение скорости загрузки.

Ограничитель один на все файлы: настройка «Ограничение скорости» должна
резать суммарный трафик, а не каждый файл по отдельности.
"""

from __future__ import annotations

import asyncio
import time

from ..core.config import AppConfig


class SpeedLimiter:
    """Ведро токенов: наполняется со скоростью limit байт/с.

    За один раз забирается не больше limit: большой кусок (512 КБ) при
    лимите меньше куска раньше приводил к зависанию — накопить столько
    токенов было невозможно, и цикл ждал вечно. Поэтому крупный запрос
    режется на части по limit.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._limit = 0.0
        self._tokens = 0.0
        self._updated = time.monotonic()

    # ------------------------------------------------------------------
    def _current_limit(self) -> float:
        """Лимит в байтах/с. Новое значение из настроек применяется сразу."""
        mbps = int(self._config.download_speed_limit_mbps or 0)
        limit = (mbps * 1_000_000) / 8 if mbps else 0.0
        if limit != self._limit:
            self._limit = limit
            self._tokens = min(self._tokens, limit)
            self._updated = time.monotonic()
        return limit

    async def consume(self, count: int) -> None:
        """Ждёт, пока можно будет забрать count байт."""
        if count <= 0:
            return
        while count > 0:
            limit = self._current_limit()
            if not limit:
                return
            portion = min(count, limit)
            await self._take(portion)
            count -= portion

    async def _take(self, amount: float) -> None:
        limit = self._current_limit()
        if not limit:
            return
        # Лимит мог уменьшиться, пока ждали: режем и его тоже.
        amount = min(amount, limit)
        while True:
            now = time.monotonic()
            self._tokens = min(self._limit, self._tokens + (now - self._updated) * self._limit)
            self._updated = now
            if self._tokens >= amount:
                self._tokens -= amount
                return
            await asyncio.sleep(min((amount - self._tokens) / self._limit, 0.5))
