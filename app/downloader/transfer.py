"""Передача файла: докачка, сегменты, ретраи.

Ключевая идея — файл делится на сегменты, каждый сегмент тянется
отдельным запросом с заголовком Range. Если соединение рвётся, мы
просто продолжаем сегмент с того места, где остановились, а не
качаем файл заново.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from ..core.config import AppConfig
from ..core.models import CloudFile, DownloadJob, DownloadLink, JobState, Segment
from ..net.http_client import HttpClient
from ..providers.registry import ProviderRegistry
from .segments import plan_segments, validate_segments
from .state import clear_state, load_state, save_state

log = logging.getLogger(__name__)

CHUNK = 512 * 1024
# Windows: без O_BINARY файл открывается в текстовом режиме и \n
# превращается в \r\n — файл «разъезжается» и контрольная сумма не сходится.
O_BINARY = getattr(os, "O_BINARY", 0)


class Cancelled(Exception):
    """Задание отменено пользователем."""


class RangeNotSupported(Exception):
    """Сервер проигнорировал Range — переходим на потоковую загрузку."""


class ServerCutStream(Exception):
    """Сервер оборвал передачу (типично для архивов на Яндекс.Диске)."""


def write_at(fd: int, data: bytes, offset: int) -> None:
    """Пишет в файл по заданному смещению.

    os.pwrite есть только в POSIX, поэтому делаем lseek+write. Вызовы
    выполняются без await между ними — в однопоточном event loop
    параллельные сегменты не могут перемешать свои смещения.
    """
    os.lseek(fd, offset, os.SEEK_SET)
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


@dataclass
class TransferContext:
    http: HttpClient
    registry: ProviderRegistry
    config: AppConfig
    streams_semaphore: asyncio.Semaphore
    on_progress: Callable[[DownloadJob], None]


class _SpeedLimiter:
    """Ограничитель скорости (Мбит/с). 0 — без ограничения."""

    def __init__(self, mbps: int) -> None:
        self.limit = (mbps * 1_000_000) / 8 if mbps else 0.0
        self._tokens = self.limit
        self._updated = time.monotonic()

    async def consume(self, count: int) -> None:
        if not self.limit:
            return
        while True:
            now = time.monotonic()
            self._tokens = min(self.limit, self._tokens + (now - self._updated) * self.limit)
            self._updated = now
            if self._tokens >= count:
                self._tokens -= count
                return
            await asyncio.sleep(min((count - self._tokens) / self.limit, 1.0))


class Transfer:
    """Один файл за раз."""

    def __init__(self, ctx: TransferContext) -> None:
        self._ctx = ctx
        self._config = ctx.config
        self._speed = _SpeedLimiter(ctx.config.download_speed_limit_mbps)

    # ------------------------------------------------------------------
    async def run(self, job: DownloadJob) -> bool:
        """Скачивает файл. True — успех."""
        file = job.file
        job.dest_dir.mkdir(parents=True, exist_ok=True)
        final = job.final_path
        part = job.part_path

        if final.exists():
            if not file.size or final.stat().st_size == file.size:
                log.info("Уже скачан: %s", final.name)
                job.state = JobState.SKIPPED
                job.downloaded_bytes = job.total_bytes = file.size
                return True
            try:
                final.unlink()
            except OSError:
                pass

        link = await self._ctx.registry.get(file.provider).resolve_download(file)
        job.link = link
        total = file.size or link.size or await self._probe_size(file, link)
        job.total_bytes = total

        segments = self._restore_segments(job, total)
        if segments is None:
            segments = self._new_segments(job, total)

        job.segments = segments
        job.downloaded_bytes = sum(seg.received for seg in segments)
        save_state(job)
        self._ctx.on_progress(job)

        fd = os.open(str(part), os.O_RDWR | os.O_CREAT | O_BINARY, 0o666)
        try:
            if len(segments) > 1 and self._config.use_segments:
                try:
                    await self._run_parallel(job, link, fd)
                except RangeNotSupported:
                    log.info("%s: сервер не поддержал докачку — качаем одним потоком", file.name)
                    await self._restart_single(job, total)
                    await self._run_single(job, job.link or link, fd)
                except ServerCutStream:
                    # Яндекс.Диск обрывает диапазонные запросы для архивов —
                    # пробуем скачать файл целиком, одним потоком без Range.
                    log.info("%s: сервер обрывает докачку — качаем целиком", file.name)
                    await self._restart_single(job, total)
                    await self._run_single(
                        job, job.link or link, fd, allow_range=False, timeout=30.0
                    )
            else:
                await self._run_single(job, link, fd)
        finally:
            os.close(fd)

        if job.total_bytes and job.downloaded_bytes < job.total_bytes:
            raise IOError(f"Скачано {job.downloaded_bytes} из {job.total_bytes} байт")

        await self._finalize(job)
        return True

    # ------------------------------------------------------------------
    def _new_segments(self, job: DownloadJob, total: int) -> list[Segment]:
        if not self._config.use_segments or total <= 0:
            segments = [Segment(index=0, start=0, end=max(0, total - 1))]
        else:
            segments = plan_segments(
                total,
                segment_size_mb=self._config.segment_min_mb,
                max_segments=max(2, self._config.max_parallel_segments * 2),
            )
        # Создаём/обрезаем файл до нужного размера — так pwrite пишет
        # в правильные смещения, а дырок не остаётся.
        fd = os.open(str(job.part_path), os.O_RDWR | os.O_CREAT | O_BINARY, 0o666)
        try:
            os.ftruncate(fd, total)
        finally:
            os.close(fd)
        return segments

    def _restore_segments(self, job: DownloadJob, total: int) -> Optional[list[Segment]]:
        if not job.part_path.exists():
            return None
        state = load_state(job.state_path)
        if not state:
            return None
        sfile, stotal, segments = state
        if stotal != total or not segments:
            return None
        if not validate_segments(segments, total):
            return None
        # Размер файла на диске должен быть не меньше ожидаемого.
        try:
            if job.part_path.stat().st_size < total:
                return None
        except OSError:
            return None
        log.info("Продолжаем докачку %s: %d сегментов", sfile.name, len(segments))
        return segments

    async def _probe_size(self, file: CloudFile, link: DownloadLink) -> int:
        try:
            response = await self._ctx.http.request(
                "HEAD", link.url, provider=self._provider_key(file), headers=link.headers, raise_for_status=False
            )
            length = response.headers.get("content-length")
            if length and length.isdigit():
                return int(length)
        except Exception as exc:
            log.debug("HEAD не удался: %s", exc)
        return 0

    def _provider_key(self, file: CloudFile) -> str:
        return file.provider.value

    # ------------------------------------------------------------------
    async def _run_parallel(self, job: DownloadJob, link: DownloadLink, fd: int) -> None:
        assert self._ctx.streams_semaphore is not None
        limit = max(1, self._config.max_parallel_segments)
        gate = asyncio.Semaphore(limit)
        tasks = [
            asyncio.create_task(self._segment_worker(job, seg, fd, gate))
            for seg in job.segments
            if not seg.done
        ]
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        errors = [task.exception() for task in done if not task.cancelled() and task.exception()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if errors:
            raise errors[0]

    async def _segment_worker(
        self, job: DownloadJob, seg: Segment, fd: int, gate: asyncio.Semaphore
    ) -> None:
        attempts = 0
        cuts = 0
        while not seg.done:
            if job.cancel_event.is_set():
                raise Cancelled()
            async with gate:
                await self._ctx.streams_semaphore.acquire()
            try:
                await self._pull_segment(job, seg, fd)
                attempts = 0
            except RangeNotSupported:
                raise
            except Cancelled:
                raise
            except ServerCutStream:
                cuts += 1
                if cuts >= 2:
                    # Сервер стабильно обрывает передачу — переходим на один поток.
                    raise
                await asyncio.sleep(1.0)
            except Exception as exc:
                attempts += 1
                if attempts > self._config.max_retries:
                    raise
                delay = min(2 ** attempts, 30)
                log.warning("Сегмент %d: %s — повтор через %d с", seg.index, exc, delay)
                await asyncio.sleep(delay)
            finally:
                try:
                    self._ctx.streams_semaphore.release()
                except ValueError:
                    pass
        job.downloaded_bytes = sum(s.received for s in job.segments)

    async def _pull_segment(self, job: DownloadJob, seg: Segment, fd: int) -> None:
        link = job.link
        assert link is not None
        start = seg.start + seg.received
        headers = {**link.headers, "Range": f"bytes={start}-{seg.end}"}
        provider = self._provider_key(job.file)

        response = await self._ctx.http.stream(
            "GET", link.url, provider=provider, headers=headers
        )
        try:
            status = response.status_code
            if status == 416:
                # Диапазон за пределами файла — считаем сегмент полученным.
                seg.received = seg.total
                return
            if status in (401, 403, 404):
                # Ссылка протухла — перевыпускаем и пробуем снова.
                await self._refresh_link(job)
                raise httpx.HTTPStatusError(
                    f"ссылка протухла ({status})", request=response.request, response=response
                )
            if status == 200:
                raise RangeNotSupported()
            if status >= 400:
                response.raise_for_status()

            expected = seg.total - seg.received
            offset = start
            got = 0
            try:
                async for chunk in response.aiter_bytes(CHUNK):
                    if not chunk:
                        continue
                    await self._checkpoint(job)
                    if got + len(chunk) > expected:
                        chunk = chunk[: expected - got]
                    write_at(fd, chunk, offset)
                    offset += len(chunk)
                    got += len(chunk)
                    seg.received += len(chunk)
                    job.downloaded_bytes = sum(s.received for s in job.segments)
                    self._ctx.on_progress(job)
                    await self._speed.consume(len(chunk))
                    if got >= expected:
                        break
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout) as exc:
                raise ServerCutStream(
                    f"сервер оборвал передачу на {got} из {expected} байт"
                ) from exc
            if got < expected:
                raise ServerCutStream(
                    f"сервер прислал {got} байт вместо {expected}"
                )
        finally:
            await response.aclose()

    async def _run_single(self, job: DownloadJob, link: DownloadLink, fd: int,
                          allow_range: bool = True, timeout: float | None = None) -> None:
        """Потоковая загрузка с докачкой (один сегмент на весь файл)."""
        if not job.segments:
            job.segments = [Segment(index=0, start=0, end=max(0, job.total_bytes - 1))]
        seg = job.segments[0]
        attempts = 0
        max_attempts = min(2, self._config.max_retries) if not allow_range else self._config.max_retries
        while not seg.done:
            if job.cancel_event.is_set():
                raise Cancelled()
            try:
                await self._pull_single(job, seg, fd, allow_range=allow_range, timeout=timeout)
                attempts = 0
            except Cancelled:
                raise
            except Exception as exc:
                attempts += 1
                if attempts > max_attempts:
                    raise
                await asyncio.sleep(min(2 ** attempts, 30))
        job.downloaded_bytes = sum(s.received for s in job.segments)

    async def _pull_single(self, job: DownloadJob, seg: Segment, fd: int,
                           allow_range: bool = True, timeout: float | None = None) -> None:
        link = job.link
        assert link is not None
        start = seg.start + seg.received
        headers = dict(link.headers)
        if start > 0 and allow_range:
            headers["Range"] = f"bytes={start}-"
        provider = self._provider_key(job.file)

        response = await self._ctx.http.stream(
            "GET", link.url, provider=provider, headers=headers, timeout=timeout
        )
        try:
            status = response.status_code
            if status in (401, 403, 404):
                await self._refresh_link(job)
                raise httpx.HTTPStatusError(
                    f"ссылка протухла ({status})", request=response.request, response=response
                )
            if status == 200 and start > 0:
                # Сервер проигнорировал Range — начинаем файл заново.
                log.info("%s: сервер не поддержал докачку, качаем заново", job.file.name)
                seg.start = 0
                seg.received = 0
                start = 0
            if status >= 400:
                response.raise_for_status()

            offset = start
            try:
                async for chunk in response.aiter_bytes(CHUNK):
                    if not chunk:
                        continue
                    await self._checkpoint(job)
                    write_at(fd, chunk, offset)
                    offset += len(chunk)
                    seg.received += len(chunk)
                    job.downloaded_bytes = seg.received
                    self._ctx.on_progress(job)
                    await self._speed.consume(len(chunk))
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout) as exc:
                raise ServerCutStream("сервер оборвал передачу") from exc
        finally:
            await response.aclose()

    async def _restart_single(self, job: DownloadJob, total: int) -> None:
        job.segments = [Segment(index=0, start=0, end=max(0, total - 1))]
        job.downloaded_bytes = 0
        save_state(job)

    async def _refresh_link(self, job: DownloadJob) -> None:
        provider = self._ctx.registry.get(job.file.provider)
        link = await provider.resolve_download(job.file)
        job.link = link
        log.info("Ссылка на %s обновлена", job.file.name)

    # ------------------------------------------------------------------
    async def _checkpoint(self, job: DownloadJob) -> None:
        """Точка остановки: пауза и отмена."""
        if job.cancel_event.is_set():
            raise Cancelled()
        await job.pause_event.wait()
        if job.cancel_event.is_set():
            raise Cancelled()

    async def _finalize(self, job: DownloadJob) -> None:
        part = job.part_path
        final = job.final_path
        try:
            size = part.stat().st_size
        except OSError:
            size = 0
        if job.total_bytes and size != job.total_bytes:
            raise IOError(f"Размер не совпал: {size} вместо {job.total_bytes}")

        if job.file.md5:
            actual = _md5_of(part)
            if actual and actual.lower() != job.file.md5.lower():
                raise IOError("Контрольная сумма не совпала — файл повреждён")

        os.replace(part, final)
        clear_state(job.state_path)
        job.state = JobState.DONE
        job.finished_at = time.time()
        log.info("Готово: %s (%s)", final.name, _human(size))


def _md5_of(path) -> str | None:
    import hashlib

    try:
        digest = hashlib.md5()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _human(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"
