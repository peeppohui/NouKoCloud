"""Кэш потока превью: докачка вперёд, перемотка, moov в конце, повторное использование."""

from __future__ import annotations

import asyncio
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path

from app.preview.stream_cache import (
    CHUNK,
    CacheError,
    StreamCache,
    cache_key,
    evict,
    find_tail_box,
)

SIZE = 40 * CHUNK + 12345               # не кратно куску — проверяем хвост
PAYLOAD = bytes((i * 31 + 7) % 251 for i in range(SIZE))


class FakeResponse:
    def __init__(self, status: int, data: bytes, delay: float) -> None:
        self.status_code = status
        self._data = data
        self._delay = delay
        self.closed = False

    async def aiter_bytes(self, size: int):
        for i in range(0, len(self._data), size):
            if self._delay:
                await asyncio.sleep(self._delay)
            yield self._data[i:i + size]

    async def aclose(self) -> None:
        self.closed = True


class FakeCloud:
    """Облако: отдаёт диапазоны payload, считает запросы; delay — «медленный канал»."""

    def __init__(self, payload: bytes, delay: float = 0.0, status: int = 206) -> None:
        self.payload = payload
        self.delay = delay
        self.status = status
        self.requests: list[tuple[int, int]] = []

    async def open_range(self, start: int, end: int) -> FakeResponse:
        self.requests.append((start, end))
        if self.status != 206:
            return FakeResponse(self.status, b"error", 0)
        return FakeResponse(206, self.payload[start:end + 1], self.delay)


async def read_all(cache: StreamCache, start: int, end: int) -> bytes:
    out = bytearray()
    pos = start
    while pos <= end:
        data = await cache.read(pos, end - pos + 1)
        out += data
        pos += len(data)
    return bytes(out)


class TestStreamCache(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.caches: list[StreamCache] = []

    async def asyncTearDown(self) -> None:
        for cache in self.caches:
            await cache.close()
        self._tmp.cleanup()

    def make(self, cloud: FakeCloud, payload: bytes = PAYLOAD, key: str = "k1") -> StreamCache:
        cache = StreamCache(key, len(payload), self.dir, cloud.open_range, name="test.mp4")
        cache.open()
        self.caches.append(cache)
        return cache

    async def test_reads_whole_file_with_one_request(self) -> None:
        cloud = FakeCloud(PAYLOAD)
        cache = self.make(cloud)
        self.assertEqual(await read_all(cache, 0, SIZE - 1), PAYLOAD)
        self.assertEqual(len(cloud.requests), 1, "последовательное чтение — одним запросом")
        self.assertTrue(cache.buffer_state(0).complete)

    async def test_range_in_the_middle(self) -> None:
        cache = self.make(FakeCloud(PAYLOAD))
        start, end = 5 * CHUNK + 100, 9 * CHUNK + 77
        self.assertEqual(await read_all(cache, start, end), PAYLOAD[start:end + 1])

    async def test_far_seek_restarts_download_there(self) -> None:
        """Перемотка далеко вперёд: качальщик перезапускается в нужном месте, а не качает всё подряд."""
        cloud = FakeCloud(PAYLOAD, delay=0.01)   # медленный канал
        cache = self.make(cloud)
        await cache.read(0, 100)
        target = 35 * CHUNK
        t = time.monotonic()
        data = await cache.read(target, 1000)
        self.assertEqual(data, PAYLOAD[target:target + 1000])
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertTrue(any(start == target for start, _end in cloud.requests),
                        f"нужен запрос с позиции перемотки: {cloud.requests}")
        # Начало файла дальше перемотки не скачано целиком — сэкономили канал.
        self.assertLess(cache.contiguous_end(0), target)

    async def test_buffer_state_counts_ahead(self) -> None:
        cache = self.make(FakeCloud(PAYLOAD))
        await read_all(cache, 0, 10 * CHUNK - 1)
        state = cache.buffer_state(2 * CHUNK)
        self.assertGreaterEqual(state.ahead_bytes, 8 * CHUNK)
        self.assertEqual(state.size, SIZE)

    async def test_data_survives_reopen(self) -> None:
        """Повторный просмотр: скачанное берётся с диска, облако не трогаем."""
        cloud = FakeCloud(PAYLOAD)
        cache = self.make(cloud)
        await read_all(cache, 0, SIZE - 1)
        await cache.close()
        self.caches.remove(cache)

        again_cloud = FakeCloud(PAYLOAD)
        again = self.make(again_cloud)
        self.assertEqual(again.contiguous_end(0), SIZE)
        self.assertEqual(await read_all(again, 0, SIZE - 1), PAYLOAD)
        await asyncio.sleep(0.05)
        self.assertEqual(again_cloud.requests, [])

    async def test_stale_index_is_discarded(self) -> None:
        cache = self.make(FakeCloud(PAYLOAD))
        await read_all(cache, 0, 3 * CHUNK)
        await cache.close()
        self.caches.remove(cache)
        other = PAYLOAD[: SIZE - 1000]          # тот же ключ, но другой размер
        fresh = self.make(FakeCloud(other), payload=other)
        self.assertEqual(fresh.contiguous_end(0), 0)
        self.assertEqual(await read_all(fresh, 0, 1000), other[:1001])

    async def test_moov_at_end_is_prefetched(self) -> None:
        """Как у камер Sony: ftyp + mdat (почти весь файл) + moov в конце."""
        mdat_size = 30 * CHUNK
        moov = b"MOOV" * 5000
        payload = (struct.pack(">I4s", 24, b"ftyp") + b"XAVC" + b"\0" * 12
                   + struct.pack(">I4s", mdat_size, b"mdat") + b"\1" * (mdat_size - 8)
                   + struct.pack(">I4s", len(moov) + 8, b"moov") + moov)
        cloud = FakeCloud(payload, delay=0.02)
        cache = self.make(cloud, payload=payload)
        await cache.read(0, 10)
        tail = 24 + mdat_size
        deadline = time.monotonic() + 3
        while cache.contiguous_end(tail) < len(payload) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        self.assertEqual(cache.contiguous_end(tail), len(payload), "хвост с moov подгружен заранее")
        self.assertTrue(any(start <= tail and start > 0 for start, _end in cloud.requests))
        self.assertEqual(await read_all(cache, tail, len(payload) - 1), payload[tail:])

    async def test_refused_by_cloud_fails_fast(self) -> None:
        cache = self.make(FakeCloud(PAYLOAD, status=404))
        t = time.monotonic()
        with self.assertRaises(CacheError):
            await cache.read(0, 10)
        self.assertLess(time.monotonic() - t, 3)
        self.assertTrue(cache.buffer_state(0).error)

    async def test_close_wakes_readers(self) -> None:
        cache = self.make(FakeCloud(PAYLOAD, delay=1.0))
        reader = asyncio.ensure_future(cache.read(20 * CHUNK, 10))
        await asyncio.sleep(0.05)
        await cache.close()
        self.caches.remove(cache)
        with self.assertRaises(CacheError):
            await asyncio.wait_for(reader, 3)


class TestHelpers(unittest.TestCase):
    def test_find_tail_box(self) -> None:
        head = struct.pack(">I4s", 16, b"ftyp") + b"\0" * 8 + struct.pack(">I4s", 1000, b"mdat")
        self.assertEqual(find_tail_box(head + b"\0" * 64, 2000), 1016)
        # moov в начале — ничего подгружать не надо
        head2 = struct.pack(">I4s", 16, b"ftyp") + b"\0" * 8 + struct.pack(">I4s", 500, b"moov")
        self.assertIsNone(find_tail_box(head2 + b"\0" * 64, 2000))
        # mdat до конца файла — после него ничего нет
        self.assertIsNone(find_tail_box(head + b"\0" * 64, 1016))
        # не mp4
        self.assertIsNone(find_tail_box(b"RIFF" + b"\0" * 100, 2000))
        # 64-битный размер mdat
        big = struct.pack(">I4s", 16, b"ftyp") + b"\0" * 8 + struct.pack(">I4sQ", 1, b"mdat", 5000)
        self.assertEqual(find_tail_box(big + b"\0" * 64, 9000), 5016)

    def test_cache_key_is_stable_and_distinct(self) -> None:
        self.assertEqual(cache_key("yandex", "f", 1), cache_key("yandex", "f", 1))
        self.assertNotEqual(cache_key("yandex", "f", 1), cache_key("yandex", "f", 2))

    def test_evict_removes_oldest_but_keeps_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            now = time.time()
            for i, name in enumerate(("old", "mid", "active", "new")):
                (d / f"{name}.dat").write_bytes(b"x" * 1000)
                (d / f"{name}.idx").write_bytes(b"i")
                os.utime(d / f"{name}.dat", (now - 100 + i * 10, now - 100 + i * 10))
            os.utime(d / "active.dat", (now - 1000, now - 1000))   # самый старый, но открыт
            evict(d, keep={"active"}, budget_bytes=2500)
            left = sorted(p.stem for p in d.glob("*.dat"))
            self.assertEqual(left, ["active", "new"])
            self.assertFalse((d / "old.idx").exists())


if __name__ == "__main__":
    unittest.main()
