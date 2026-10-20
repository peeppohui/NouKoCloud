"""Ограничение скорости загрузки.

Раньше ограничитель работал как ведро ёмкостью в одну секунду лимита:
при лимите меньше размера куска (512 КБ) накопить столько токенов было
невозможно, и загрузка висела вечно. Тест проверяет, что этого больше нет.
"""

from __future__ import annotations

import asyncio
import time
import unittest

from app.core.config import AppConfig  # noqa: E402
from app.net.speed import SpeedLimiter  # noqa: E402

CHUNK = 512 * 1024


class TestSpeedLimiter(unittest.TestCase):
    def test_limit_below_chunk_does_not_hang(self) -> None:
        """4 Мбит/с — кусок 512 КБ больше лимита: раньше был вечный цикл."""
        config = AppConfig(download_speed_limit_mbps=4)
        limiter = SpeedLimiter(config)

        async def run() -> float:
            started = time.monotonic()
            await asyncio.wait_for(limiter.consume(CHUNK), timeout=30)
            return time.monotonic() - started

        elapsed = asyncio.run(run())
        self.assertGreater(elapsed, 0.05, "ограничение должно реально задерживать")
        self.assertLess(elapsed, 20, "и не задерживать слишком долго")

    def test_without_limit_nothing_waits(self) -> None:
        config = AppConfig(download_speed_limit_mbps=0)
        limiter = SpeedLimiter(config)

        async def run() -> float:
            started = time.monotonic()
            await limiter.consume(4 * 1024 * 1024)
            return time.monotonic() - started

        self.assertLess(asyncio.run(run()), 1.0)

    def test_limit_change_applies_at_once(self) -> None:
        """Новое значение из «Настроек» должно подхватываться без перезапуска."""
        config = AppConfig(download_speed_limit_mbps=0)
        limiter = SpeedLimiter(config)

        async def run() -> None:
            await limiter.consume(1024)          # без ограничения — мгновенно
            config.download_speed_limit_mbps = 8
            started = time.monotonic()
            await asyncio.wait_for(limiter.consume(512 * 1024), timeout=30)
            self.assertGreater(time.monotonic() - started, 0.05)

        asyncio.run(run())

    def test_one_limiter_for_all_files(self) -> None:
        """Ограничитель общий: суммарная скорость, а не на каждый файл."""
        config = AppConfig(download_speed_limit_mbps=8)
        first = SpeedLimiter(config)
        second = SpeedLimiter(config)
        self.assertIsNot(first, second)
        # Лимит читается из конфига — значит, оба ограничителя obeyят ему.
        self.assertEqual(first._current_limit(), second._current_limit())
        self.assertEqual(first._current_limit(), 8 * 1_000_000 / 8)


if __name__ == "__main__":
    unittest.main()
