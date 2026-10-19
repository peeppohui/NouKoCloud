"""Фоновый asyncio-цикл.

GUI в Qt синхронный, а сеть/загрузки асинхронные. Поэтому поднимаем
отдельный поток с event loop, а общаемся с ним через
``asyncio.run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any, Awaitable, Callable, Coroutine, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class AsyncRuntime:
    """Держит event loop в отдельном потоке."""

    def __init__(self, name: str = "svscloud-io") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._started = False

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)
        log.info("Фоновый цикл запущен")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()

    # ------------------------------------------------------------------
    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("Фоновый цикл не запущен")
        return self._loop

    def submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        """Запускает корутину в фоновом цикле и возвращает concurrent.Future."""
        if not self._started:
            self.start()
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def submit_call(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> concurrent.futures.Future[T]:
        return self.submit(fn(*args, **kwargs))

    def stop(self) -> None:
        if not self._started or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._started = False
        self._loop = None
