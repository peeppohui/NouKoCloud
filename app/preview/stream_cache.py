"""Кэш потока для превью: качаем файл вперёд и отдаём плееру из кэша.

Зачем. Плеер (FFmpeg) сам читает файл по HTTP, но делает это «впритык»:
каждый прыжок (перемотка, `moov` в конце файла у камер Sony) — новое
соединение с облаком, а это 0.5–1.5 с ожидания. А если видео тяжелее
интернета (4K 100 Мбит/с при канале 24 Мбит/с), плеер показывает кадры
по мере поступления — 3–6 кадров в секунду.

Как устроено.
* Файл делится на куски по CHUNK байт. Куски пишутся в журнал-файл
  `<key>.dat` в порядке прихода, а в `<key>.idx` — где какой кусок лежит.
  (Разреженный файл на NTFS не годится: запись в конец 3-ГБ файла
  заставила бы Windows сначала заполнить нулями всё до этого места.)
* Один «качальщик» тянет из облака непрерывный диапазон начиная с места,
  которое сейчас нужно плееру, и дальше — вперёд, на полной скорости канала.
  Если плеер прыгнул далеко (перемотка) — качальщик перезапускается там.
* Для mp4/mov с `moov` в конце служебный блок подтягивается сразу,
  параллельно с началом файла — плееру не приходится ждать лишний прыжок.
* Скачанное остаётся на диске: повторный просмотр и перемотка назад —
  мгновенно. Общий объём кэша ограничен, старое удаляется.

Все методы (кроме чтения состояния для интерфейса) вызываются в потоке
event loop сервера превью.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import struct
import time
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional

import httpx

log = logging.getLogger(__name__)

CHUNK = 256 * 1024
#: Плеер просит место, до которого качальщик дойдёт сам совсем скоро, —
#: не перезапускаем соединение (это дороже, чем подождать).
RETARGET_SLACK = 4 * 1024 * 1024
#: Насколько далеко вперёд от плеера качать. 300 МБ — это ~25 с видео 4K
#: 100 Мбит/с: хватает для ровного показа, но превью не занимает канал
#: надолго, пока в очереди идут настоящие загрузки.
MAX_AHEAD = 300 * 1024 * 1024
#: Хвост mp4/mov с moov подтягиваем, если он не больше этого.
MAX_TAIL_PREFETCH = 64 * 1024 * 1024
# Предзагрузка (файл ещё не открыт, но скорее всего откроют): ровно столько,
# чтобы плеер сразу показал первый кадр, — начало файла и небольшой хвост.
WARM_AHEAD = 2 * 1024 * 1024
WARM_TAIL = 4 * 1024 * 1024
MAX_FAILURES = 6
O_BINARY = getattr(os, "O_BINARY", 0)

#: open_range(start, end) → поток ответа облака (206) на bytes=start-end.
OpenRange = Callable[[int, int], Awaitable[httpx.Response]]


class CacheError(Exception):
    """Облако не отдаёт файл — превью продолжить нельзя."""


class CloudRefused(CacheError):
    """Облако отказало (4xx уже после перевыпуска ссылки) — повторять бессмысленно."""


@dataclass
class BufferState:
    """Сколько данных уже есть впереди позиции плеера (для интерфейса)."""

    ahead_bytes: int        # скачано подряд начиная с позиции
    complete: bool          # до конца файла всё скачано
    rate: float             # скорость скачивания, байт/с (0 — неизвестна)
    size: int
    error: str = ""


def cache_key(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


class StreamCache:
    """Кэш одного файла облака."""

    def __init__(self, key: str, size: int, directory: Path, open_range: OpenRange,
                 name: str = "") -> None:
        if size <= 0:
            raise ValueError("размер файла неизвестен")
        self.key = key
        self.size = size
        self.name = name or key
        self.count = (size + CHUNK - 1) // CHUNK
        self._open_range = open_range
        self._data_path = directory / f"{key}.dat"
        self._index_path = directory / f"{key}.idx"
        # index[i] — смещение куска i в журнале, -1 — куска нет.
        self._index = array("q", [-1]) * self.count
        self._fd = -1
        self._log_end = 0
        self._cond: asyncio.Condition | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._tail_task: asyncio.Task | None = None
        self._target = 0              # с какого места сейчас качать
        self._reader = 0              # где читает плеер (для ограничения «вперёд»)
        self._fetch_next = -1         # какой кусок качальщик получит следующим
        self._reader_seq = 0          # см. new_reader()
        self._active_reader = 0
        self._retarget = False
        self._closed = False
        self.ahead_limit = MAX_AHEAD      # сколько качать впереди плеера
        self.tail_limit = MAX_TAIL_PREFETCH
        self.error = ""
        self.rate = 0.0
        self._rate_mark = (time.monotonic(), 0)
        self._received = 0
        self._last_store = 0.0
        self._dirty = 0

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------
    def open(self) -> None:
        """Открывает журнал; подхватывает ранее скачанное."""
        self._cond = asyncio.Condition()
        self._wake = asyncio.Event()
        self._load_index()
        self._fd = os.open(str(self._data_path), os.O_RDWR | os.O_CREAT | O_BINARY, 0o666)
        self._log_end = os.lseek(self._fd, 0, os.SEEK_END)
        # Кусок не может лежать за концом журнала (журнал обрезали/повредили).
        for i, offset in enumerate(self._index):
            if offset >= 0 and offset + self._chunk_len(i) > self._log_end:
                self._index[i] = -1
        have = sum(1 for o in self._index if o >= 0)
        if have:
            log.info("Превью %s: в кэше уже %d%% файла", self.name, have * 100 // self.count)
        self._task = asyncio.ensure_future(self._fetch_loop())
        self._tail_task = asyncio.ensure_future(self._prefetch_tail())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in (self._task, self._tail_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._task, self._tail_task):
            if task is not None:
                try:
                    await task
                except BaseException:
                    pass
        self._save_index()
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        if self._cond is not None:
            async with self._cond:
                self._cond.notify_all()

    @property
    def closed(self) -> bool:
        return self._closed

    def set_warm(self, warm: bool) -> None:
        """Предзагрузка (немного) или настоящий просмотр (с большим запасом)."""
        self.ahead_limit = WARM_AHEAD if warm else MAX_AHEAD
        if not warm:
            self.tail_limit = MAX_TAIL_PREFETCH
            if self._wake is not None:
                self._wake.set()          # качальщик ждал — теперь можно дальше

    # ------------------------------------------------------------------
    # Чтение (для HTTP-ответа плееру)
    # ------------------------------------------------------------------
    def new_reader(self) -> int:
        """Регистрирует новый запрос плеера; качальщиком управляет самый свежий.

        После перемотки старое соединение плеера ещё какое-то время ждёт
        данные в прежнем месте. Если бы оно тоже управляло качальщиком,
        два места перетягивали бы загрузку друг у друга, и перемотка
        растягивалась бы на десятки секунд.
        """
        self._reader_seq += 1
        self._active_reader = self._reader_seq
        return self._reader_seq

    async def read(self, pos: int, limit: int, reader: int = 0) -> bytes:
        """Отдаёт данные с позиции pos (не больше limit), дожидаясь скачивания.

        reader — номер из new_reader(): загрузкой управляет только самый свежий
        запрос (проверяется на каждом круге ожидания — пока ждём, плеер может
        перемотать). 0 — без учёта читателей: управляет любой вызов.
        """
        assert self._cond is not None
        idx = pos // CHUNK
        if reader == 0 or reader == self._active_reader:
            self._reader = pos
        while self._index[idx] < 0:
            if self._closed:
                raise CacheError("превью закрыто")
            if self.error:
                raise CacheError(self.error)
            if reader == 0 or reader == self._active_reader:
                self._want(pos)
            async with self._cond:
                if self._index[idx] < 0 and not self._closed and not self.error:
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
        chunk_start = idx * CHUNK
        offset = self._index[idx] + (pos - chunk_start)
        length = min(limit, chunk_start + self._chunk_len(idx) - pos)
        os.lseek(self._fd, offset, os.SEEK_SET)
        data = os.read(self._fd, length)
        # Плеер дочитал кусок — пусть качальщик держит запас впереди него.
        if self._fetch_next < 0 or self._retarget:
            self._wake.set()
        return data

    def _want(self, pos: int) -> None:
        """Плееру нужен pos, которого ещё нет: качать оттуда (если далеко — перезапуск)."""
        chunk = pos // CHUNK
        nxt = self._fetch_next
        # Качальщик и так вот-вот дойдёт до нужного места — не рвём соединение,
        # но запоминаем цель: следующий запуск начнётся отсюда.
        near = 0 <= nxt <= chunk and (chunk - nxt) * CHUNK <= RETARGET_SLACK
        if self._target // CHUNK != chunk:
            self._target = chunk * CHUNK
            if not near:
                self._retarget = True
        self._wake.set()

    # ------------------------------------------------------------------
    # Состояние для интерфейса (вызывается из GUI-потока: только чтение)
    # ------------------------------------------------------------------
    def buffer_state(self, pos: int) -> BufferState:
        pos = max(0, min(pos, self.size - 1))
        end = self.contiguous_end(pos)
        # Данные давно не приходят — прежняя скорость уже не правда.
        stalled = time.monotonic() - self._last_store > 3.0
        return BufferState(
            ahead_bytes=end - pos,
            complete=end >= self.size,
            rate=0.0 if stalled else self.rate,
            size=self.size,
            error=self.error,
        )

    def contiguous_end(self, pos: int) -> int:
        """Первый нескачанный байт начиная с pos (size — если всё есть)."""
        index = self._index
        i = pos // CHUNK
        while i < self.count and index[i] >= 0:
            i += 1
        return min(self.size, i * CHUNK) if i < self.count else self.size

    @property
    def downloaded_bytes(self) -> int:
        return sum(self._chunk_len(i) for i, o in enumerate(self._index) if o >= 0)

    # ------------------------------------------------------------------
    # Качальщик
    # ------------------------------------------------------------------
    async def _fetch_loop(self) -> None:
        failures = 0
        while not self._closed:
            self._retarget = False
            start = self._first_missing(self._target // CHUNK)
            if start is None or (start * CHUNK - self._reader) > self.ahead_limit:
                # Впереди всё скачано или запас и так огромный — ждём плеер.
                self._fetch_next = -1
                self._wake.clear()
                await self._wake.wait()
                continue
            end = start
            while end + 1 < self.count and self._index[end + 1] < 0:
                end += 1
            try:
                await self._fetch_run(start, end)
                failures = 0
            except asyncio.CancelledError:
                raise
            except CloudRefused as exc:
                log.warning("Превью %s: %s", self.name, exc)
                self.error = "Облако не отдаёт этот файл (нет доступа или файл удалён)."
                self._fetch_next = -1
                await self._notify()
                return
            except Exception as exc:
                failures += 1
                self._fetch_next = -1
                log.warning("Превью %s: обрыв загрузки (%d/%d): %s",
                            self.name, failures, MAX_FAILURES, exc)
                if failures >= MAX_FAILURES:
                    self.error = "Не удалось получить файл из облака. Проверьте интернет."
                    await self._notify()
                    return
                await asyncio.sleep(min(0.5 * 2 ** (failures - 1), 5.0))

    async def _fetch_run(self, start: int, end: int) -> None:
        """Качает куски start..end одним запросом; прерывается при перемотке."""
        first_byte = start * CHUNK
        last_byte = min(self.size, (end + 1) * CHUNK) - 1
        self._fetch_next = start
        response = await self._open_range(first_byte, last_byte)
        try:
            status = response.status_code
            if status == 200 and first_byte > 0:
                # Облако прислало файл целиком вместо части — сохраняем его по
                # порядку (всё равно пригодится), пока не дойдём до нужного места.
                await self._consume_whole(response)
                return
            if status != 206 and not (status == 200 and first_byte == 0):
                if 400 <= status < 500 and status not in (408, 429):
                    raise CloudRefused(f"облако ответило {status}")
                raise CacheError(f"облако ответило {status} на запрос диапазона")
            idx = start
            buf = bytearray()
            async for data in response.aiter_bytes(64 * 1024):
                buf += data
                while idx <= end and len(buf) >= self._chunk_len(idx):
                    size = self._chunk_len(idx)
                    self._store(idx, bytes(buf[:size]))
                    del buf[:size]
                    idx += 1
                    self._fetch_next = idx if idx <= end else -1
                    await self._notify()
                    if self._retarget or self._closed:
                        return  # плеер прыгнул в другое место — качаем оттуда
                    if (idx * CHUNK - self._reader) > self.ahead_limit:
                        return  # запас огромный — подождём плеер
                    if idx <= end and self._index[idx] >= 0:
                        return  # дальше уже скачано (например, хвост с moov)
                if idx > end:
                    break
            if idx <= end:
                raise CacheError(f"облако оборвало передачу на куске {idx} из {end}")
        finally:
            await response.aclose()

    async def _consume_whole(self, response) -> None:
        """Ответ без Range (файл с начала): сохраняем недостающие куски подряд."""
        idx = 0
        buf = bytearray()
        async for data in response.aiter_bytes(64 * 1024):
            buf += data
            while idx < self.count and len(buf) >= self._chunk_len(idx):
                size = self._chunk_len(idx)
                if self._index[idx] < 0:
                    self._store(idx, bytes(buf[:size]))
                    self._fetch_next = idx + 1
                    await self._notify()
                del buf[:size]
                idx += 1
                if self._closed:
                    return
            if idx >= self.count:
                break
        if idx < self.count:
            raise CacheError(f"облако оборвало передачу на куске {idx} из {self.count}")

    def _store(self, idx: int, data: bytes) -> None:
        if self._index[idx] >= 0 or self._fd < 0:
            return
        os.lseek(self._fd, self._log_end, os.SEEK_SET)
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]
        self._index[idx] = self._log_end
        self._log_end += len(data)
        self._received += len(data)
        self._last_store = time.monotonic()
        self._dirty += 1
        if self._dirty >= 64:          # ~16 МБ — сохраняем карту на случай сбоя
            self._save_index()
        now = time.monotonic()
        mark_time, mark_bytes = self._rate_mark
        if now - mark_time >= 0.5:
            current = (self._received - mark_bytes) / (now - mark_time)
            self.rate = current if not self.rate else self.rate * 0.6 + current * 0.4
            self._rate_mark = (now, self._received)

    async def _notify(self) -> None:
        if self._cond is not None:
            async with self._cond:
                self._cond.notify_all()

    def _first_missing(self, from_chunk: int) -> Optional[int]:
        index = self._index
        for i in range(max(0, from_chunk), self.count):
            if index[i] < 0:
                return i
        return None

    def _chunk_len(self, idx: int) -> int:
        return min(CHUNK, self.size - idx * CHUNK)

    # ------------------------------------------------------------------
    # moov в конце файла (Sony, многие камеры): подтягиваем сразу
    # ------------------------------------------------------------------
    async def _prefetch_tail(self) -> None:
        try:
            head = await self._wait_chunk(0)
            tail_start = find_tail_box(head, self.size)
            if tail_start is None:
                return
            if self.size - tail_start > self.tail_limit:
                return
            start = tail_start // CHUNK
            end = self.count - 1
            if self._first_missing(start) is None:
                return
            log.info("Превью %s: служебные данные в конце файла — подгружаем заранее", self.name)
            response = await self._open_range(start * CHUNK, self.size - 1)
            try:
                if response.status_code != 206:
                    return
                idx = start
                buf = bytearray()
                async for data in response.aiter_bytes(64 * 1024):
                    buf += data
                    while idx <= end and len(buf) >= self._chunk_len(idx):
                        size = self._chunk_len(idx)
                        self._store(idx, bytes(buf[:size]))
                        del buf[:size]
                        idx += 1
                        await self._notify()
            finally:
                await response.aclose()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # не критично: плеер дочитает сам
            log.info("Превью %s: хвост файла заранее не подгрузился: %s", self.name, exc)

    async def _wait_chunk(self, idx: int) -> bytes:
        assert self._cond is not None
        while self._index[idx] < 0:
            if self._closed or self.error:
                raise CacheError("нет данных")
            async with self._cond:
                if self._index[idx] < 0:
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
        os.lseek(self._fd, self._index[idx], os.SEEK_SET)
        return os.read(self._fd, self._chunk_len(idx))

    # ------------------------------------------------------------------
    # Карта кусков на диске
    # ------------------------------------------------------------------
    def _load_index(self) -> None:
        try:
            raw = self._index_path.read_bytes()
            size, count = struct.unpack("<qq", raw[:16])
            if size == self.size and count == self.count and self._data_path.exists():
                loaded = array("q")
                loaded.frombytes(raw[16:16 + count * 8])
                if len(loaded) == self.count:
                    self._index = loaded
                    return
        except (OSError, struct.error, ValueError):
            pass
        # Карты нет или она не от этого файла — начинаем с чистого листа.
        for path in (self._data_path, self._index_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _save_index(self) -> None:
        self._dirty = 0
        try:
            tmp = self._index_path.with_suffix(".idx.tmp")
            tmp.write_bytes(struct.pack("<qq", self.size, self.count) + self._index.tobytes())
            os.replace(tmp, self._index_path)
        except OSError as exc:
            log.debug("Карта кэша превью не сохранилась: %s", exc)


def find_tail_box(head: bytes, size: int) -> Optional[int]:
    """Для mp4/mov: смещение верхнего блока ПОСЛЕ mdat (обычно moov), если он есть.

    Разбираем верхние блоки в первом куске файла; mdat обычно не помещается
    в кусок, но по его длине видно, где начинается следующий блок.
    """
    if len(head) < 16 or head[4:8] not in (b"ftyp", b"wide", b"free", b"mdat", b"moov", b"skip", b"uuid"):
        return None
    pos = 0
    while pos + 8 <= size:
        if pos + 16 > len(head):
            return None  # заголовок блока за пределами первого куска — не знаем
        box_size, box_type = struct.unpack(">I4s", head[pos:pos + 8])
        if box_size == 1:
            box_size = struct.unpack(">Q", head[pos + 8:pos + 16])[0]
        elif box_size == 0:
            box_size = size - pos
        if box_size < 8:
            return None
        if box_type == b"moov":
            return None  # moov в начале — всё уже будет в первых кусках
        if box_type == b"mdat":
            after = pos + box_size
            return after if after < size else None
        pos += box_size
    return None


def cache_usage(directory: Path) -> tuple[int, int]:
    """Сколько места занимает кэш превью: (байт, файлов)."""
    total = count = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0, 0
    for entry in entries:
        if not entry.is_file(follow_symlinks=False):
            continue
        try:
            # os.stat, а не entry.stat(): на Windows scandir берёт размер из
            # записи каталога, а у открытого файла (идёт превью) она отстаёт — 0 байт.
            total += os.stat(entry.path).st_size
            count += 1
        except OSError:
            pass
    return total, count


def clear_directory(directory: Path) -> tuple[int, int]:
    """Удаляет файлы кэша превью: (освобождено байт, не удалось удалить файлов).

    Трогаем только файлы прямо в папке кэша (это всегда отдельная папка
    `.noukocloud-preview`), подпапки и всё остальное не трогаем.
    """
    freed = failed = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0, 0
    for entry in entries:
        if not entry.is_file(follow_symlinks=False):
            continue
        try:
            size = entry.stat().st_size
            os.unlink(entry.path)
            freed += size
        except OSError:
            failed += 1
    return freed, failed


def evict(directory: Path, keep: set[str], budget_bytes: int) -> None:
    """Удаляет самые старые кэши превью, пока общий объём больше бюджета."""
    try:
        entries = []
        total = 0
        for data in directory.glob("*.dat"):
            st = data.stat()
            total += st.st_size
            entries.append((st.st_mtime, data, st.st_size))
    except OSError:
        return
    entries.sort()
    for _mtime, data, size in entries:
        if total <= budget_bytes:
            break
        if data.stem in keep:
            continue
        for path in (data, data.with_suffix(".idx")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        total -= size
        log.info("Кэш превью: удалён старый файл (%d МБ)", size // (1024 * 1024))
